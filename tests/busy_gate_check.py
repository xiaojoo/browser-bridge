"""互斥闸门的自检: "空闲就占住" 必须是原子的, 且并发下只放一个进去。

盯的是这个 bug 本身: 以前各处写 `if manager.busy: 409` 然后 `_spawn(后台协程)`,
而 busy=True 要等那个协程真被调度起来才设 —— 中间隔着 await, 第二个请求挤得进来,
于是两路同时驱动同一个 page(串话 / 捕获切页 / 两条回答叠在一起)。

第 1 部分离线跑闸门语义; 第 2 部分打真服务并发 5 发 —— 那才是竞态的正证。
8765 没起着时第 2 部分会**明确报跳过**(跳过项就是这条门禁的盲区)。
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import server  # noqa: E402   (只 import, 不会启动浏览器)

BASE = "http://127.0.0.1:8765"
SLOW = '%s -c "import time;time.sleep(3)"' % (Path(sys.executable).as_posix(),)


def post(path, body, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return f.status, json.loads(f.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return "ERR", {"error": str(e)[:120]}


async def main() -> int:
    bad = []
    m = server.manager

    # ---- 1) 闸门语义
    m.busy = False
    m.busy_reason = ""
    ok1 = server.claim("聊天")
    ok2 = server.claim("本地验证")
    why = server._busy_why()
    print("1) 第一次 claim=%s 第二次=%s 占着的是=%r 409 话术=%r" % (ok1, ok2, m.busy_reason, why))
    if not (ok1 and not ok2):
        bad.append("第二次 claim 竟然也成功了 —— 闸门没关")
    if m.busy_reason != "聊天":
        bad.append("busy_reason 没记住是谁占着: " + m.busy_reason)
    if "聊天" not in why:
        bad.append("409 没说出在忙什么(只会说「正在生成中」会让人以为聊天卡死): " + why)
    await server.release()
    if m.busy or m.busy_reason:
        bad.append("release 之后仍然占着")
    if not server.claim("本地验证"):
        bad.append("release 之后 reclaim 不回来")
    await server.release()

    # ---- 2) 并发正证: 5 发同时打进去, 只能有 1 发真的开始跑
    try:
        urllib.request.urlopen(BASE + "/api/status", timeout=3).read()
        up = True
    except Exception:
        up = False
    if not up:
        print("2) 跳过: 8765 没起着 —— 并发这一条只有真服务能证, 别把绿当成验过了")
    else:
        st = await _get("/api/status")
        if st.get("busy"):
            print("2) 跳过: 服务正忙(%s), 这次测不出并发" % st.get("busy_reason"))
        else:
            async def one(i):
                return await asyncio.to_thread(post, "/api/world/test",
                                               {"command": SLOW, "timeout": 30})
            res = await asyncio.gather(*(one(i) for i in range(5)))
            codes = [c for c, _ in res]
            # 真跑过的那一发返回值里带 "command"; 被闸门挡掉的只有 output, 没有 command
            ran = sum(1 for c, b in res if c == 200 and "command" in b)
            refused = sum(1 for c, b in res if "正在" in str(b.get("output") or b.get("error")))
            print("2) 5 发并发跑 3 秒的命令 -> 状态码 %s | 真跑了 %d 发 | 被挡 %d 发"
                  % (codes, ran, refused))
            if ran != 1:
                bad.append("并发下真跑起来的不是 1 发而是 %d 发 —— 闸门是假的" % ran)
            if refused != 4:
                bad.append("被挡住的应该正好 4 发, 实际 %d 发" % refused)
            after = await _get("/api/status")
            if after.get("busy"):
                bad.append("测完之后锁还握着(泄漏): " + str(after.get("busy_reason")))

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("BUSY_GATE_OK (claim 原子 / 只放一发进去 / 409 说得出在忙什么 / release 不泄漏)")
    return 0


async def _get(path):
    return await asyncio.to_thread(
        lambda: json.loads(urllib.request.urlopen(BASE + path, timeout=30).read().decode("utf-8")))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
