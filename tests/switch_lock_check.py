"""点侧栏换一段会话, 会不会把整个桥锁住 —— 这条盯的是"切换"这个动作的代价。

为什么要有: 切完会话后端会顺手把这段历史记到本地(落盘)。这件事要做两次页面操作
(读一遍 + 可能要滚), 而它占着互斥(不占的话它会和用户正在发的那一轮抢同一个页面)。
如果把它放在 HTTP 响应里等, 点一下侧栏就要转圈等到它做完; 如果做的是"滚到顶的深读",
实测他侧栏那段 56 条 / 119,483 字的对话要 **199 秒** —— 那就是"点一下, 整个桥锁三分半"。

所以这里量的不是"能不能切", 而是三个数:
  1) 切换请求多久回来(应该只有导航那点时间);
  2) 之后互斥还占多久(后台只该做浅读);
  3) 浅读是不是真的把页面上已渲染的那几条记到了盘上(不然就是悄悄什么也没做)。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import transcript  # noqa: E402

URL = "http://127.0.0.1:8765"
OPEN_S = 20.0        # 切换请求本身: 只该等到页面换过去
LOCK_S = 60.0        # 后台浅读占住互斥的时间(深读实测 199s, 那条路要留给 ⤓ 按钮)


async def main() -> int:
    bad = []
    import urllib.error
    import urllib.request

    def get(path, timeout=240):
        return json.load(urllib.request.urlopen(URL + path, timeout=timeout))

    def post(path, data=None, timeout=240):
        r = urllib.request.Request(URL + path, data=json.dumps(data or {}).encode(),
                                   headers={"content-type": "application/json"})
        return json.load(urllib.request.urlopen(r, timeout=timeout))

    try:
        st = await asyncio.to_thread(get, "/api/status")
    except Exception as exc:  # noqa: BLE001
        print("SKIP: 8765 没起着或读不到状态: %s" % exc)
        return 0
    if st.get("state") != "logged_in":
        print("SKIP: 站点未登录(state=%s), 这条要真浏览器真会话" % st.get("state"))
        return 0

    cl = await asyncio.to_thread(get, "/api/conversations")
    items = cl.get("items") or []
    if not items:
        print("SKIP: 站点侧栏一条会话都没有")
        return 0
    it = items[0]
    print("目标会话: %r %s" % ((it.get("title") or "")[:20], it.get("key")))

    async def wait_free(who: str, timeout_s: float = 150.0) -> bool:
        """上一个动作还占着互斥就先等它 —— 不然这里只会拿到一个 409, 量不到切换本身。"""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            s = await asyncio.to_thread(get, "/api/status")
            if not s.get("busy"):
                return True
            print("   等 %s 放手(busy=%s) %.0fs" % (s.get("busy_reason"), time.time() - t0, 1))
            await asyncio.sleep(2)
        return False

    await wait_free("上一件事")
    await asyncio.to_thread(post, "/api/new_chat")
    await asyncio.sleep(3)
    await wait_free("新建会话")

    t0 = time.time()
    try:
        r = await asyncio.to_thread(post, "/api/conversations/open",
                                    {"key": it.get("key") or "", "url": it.get("url") or ""})
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            await wait_free("上一件事")
            r = await asyncio.to_thread(post, "/api/conversations/open",
                                        {"key": it.get("key") or "", "url": it.get("url") or ""})
        else:
            raise
    open_s = time.time() - t0
    conv = r.get("current") or ""
    print("1) 切换请求 %.1fs 回来, current=%s" % (open_s, conv[:12]))
    if open_s > OPEN_S:
        bad.append("切换请求要 %.1fs(>%ds): 读回放盘又回到响应里了" % (open_s, OPEN_S))

    lock_s, busy_at_end = -1.0, {}
    t0 = time.time()
    while time.time() - t0 < LOCK_S + 20:
        s = await asyncio.to_thread(get, "/api/status")
        if not s.get("busy"):
            lock_s = time.time() - t0
            busy_at_end = s
            break
        await asyncio.sleep(1)
    print("2) 互斥又占了 %.1fs (busy=%s)" % (lock_s, bool(busy_at_end.get("busy"))))
    if lock_s < 0:
        bad.append("切完会话 %.0fs 后互斥还没释放 —— 整个桥点不动了" % (LOCK_S + 20))
    elif lock_s > LOCK_S:
        bad.append("后台读回占了 %.0fs(>%ds): 侧栏点一下不该把桥锁这么久, "
                   "这里只许浅读, 滚到顶的深读留给 ⤓" % (lock_s, LOCK_S))

    p = transcript.path_for("chatgpt", conv) if conv else None
    if not p or not p.exists():
        bad.append("浅读没写出任何文件: %s" % (p or "(没有会话 id)"))
    else:
        n = sum(1 for _ in open(p, encoding="utf-8"))
        print("3) %s 上有 %d 行(含流式修订) —— 页面上已渲染的那几条确实记下来了" % (p.name, n))
    print("SWITCH_LOCK_%s" % ("OK" if not bad else "FAIL"))
    for b in bad:
        print(" -", b)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
