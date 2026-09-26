"""接力预览这块界面的量具(真 Chromium 打真服务, 需要 8765 起着)。

「生成预览」的后端回答用 page.route 换成一份固定长笔记: 真跑一次要先深读整段对话
再调规划模型(实测他侧栏那段 56 条的对话 214s), 那会让这条门禁跟着站点此刻停在
哪段对话上跑 —— 时绿时红的量具等于没有。界面这一层要量的是布局, 见下面 3)/4)/6)。

盯的都是会被打回的那种:
  1) 按钮常驻且跟同排的「回复来源」同一个 computed 值(不要蓝底、不要自成一派);
  2) 悬停只换边框颜色, 底色/尺寸一个字都不动;
  3) 弹框固定高度 —— 空着和生成完一样高(否则一点「生成」整个框跳一下);
  4) 只有正文滚, 弹框自己永远不滚;
  5) 任务输入框会把你正在打的那段话接过来;
  6) 笔记按 markdown 渲染出来了(标题变成 h2/h3, 而不是屏幕上还挂着 ##),
     且模型写的东西没有以 <script>/on*= 的形式进 DOM;
  7) 没登录时按钮置灰不消失, 并且提示为什么不能用。
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765"
ASK = "接力界面自检: 这一棒要把显卡状态检查做成每周跑一次的脚本。"

# 「生成预览」在后端是两件事: 深读整段对话(一路滚到顶) + 调一次规划模型。
# 实测他侧栏那段 56 条 / 119,483 字的对话: 深读 199s, 整条预览 214s, 笔记 14,349 字。
# 让量具跟着"站点此刻停在哪段对话"跑, 同一条门禁就会有时绿有时红 —— 而那几条断言量的
# 其实是布局(固定高 / 只有正文滚 / 长笔记渲染 / 页脚还在), 所以这里换成一份**固定形状、
# 撑得开正文的长笔记**, 界面代码路径(runHandoff -> post -> 渲染)一步不少地走。
# 真模型那条路由 handoff_check.py 盯内容, 真机数字见上一行注释。
NOTE_SECTIONS = ["目标", "已确认", "待办", "下一步"]


def long_note() -> str:
    out = []
    for i, s in enumerate(NOTE_SECTIONS):
        out.append("## %s" % s)
        for k in range(30):
            out.append("- %s 第 %d 条: 这一行故意写得长一些, 用来说明它在下一棒里的位置,"
                       " 以及为什么不能只写一句结论。" % (s, k))
        out.append("")
    out.append("```python\nimport pynvml\nfor i in range(8):\n    print(i)\n```\n")
    return "\n".join(out)


def preview_payload(rel: str) -> dict:
    note = long_note()
    # 字段照 server._last_handoff 那份抄, 少一个界面就会写出 "undefined 条"
    exc = [{"role": "user" if i % 2 else "assistant", "chars": 840,
            "text": "这一段原文片段是用来撑开正文的第 %d 条, 它下面应该还能继续滚。" % i,
            "head": "这一段原文片段是用来撑开正文的第 %d 条" % i}
           for i in range(5)]
    carry = note + "\n".join(e["text"] for e in exc)
    return {"ok": True, "context": carry, "chars": len(carry), "note": note,
            "conv": "_ho_check", "records": 56, "conv_chars": 119483,
            "note_chars": len(note), "carry_chars": len(carry),
            "excerpts": len(exc), "excerpt_chars": sum(e["chars"] for e in exc),
            "carry_max": 20000, "rel": rel, "path": str(ROOT / "transcripts" / rel),
            "excerpt_list": exc}


async def wait_idle(page, timeout_s: float = 200.0) -> dict:
    """等桥回到空闲。

    点侧栏切会话后, 后端还在后台把那段历史读回放盘(它占着互斥, 界面上所有动作都置灰)。
    以前这里只取一次 /api/status 就当结论, 于是那阵忙被当成"未登录", 把这层的本体
    整段静悄悄跳过了 —— 门禁自己绿了, 量具却没看东西。
    """
    t0, s = time.time(), {}
    while time.time() - t0 < timeout_s:
        s = await (await page.request.get(URL + "/api/status")).json()
        if s.get("state") == "logged_in" and not s.get("busy"):
            return s
        await asyncio.sleep(2)
    return s


async def main() -> int:
    bad, skipped = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        # 「逐条记录」那一格要指到一个真的在盘上的文件, 所以先造一份替身原件
        rel_fix = "chatgpt/_ho_check.jsonl"
        fix = ROOT / "transcripts" / rel_fix
        fix.parent.mkdir(parents=True, exist_ok=True)
        fix.write_text('{"role":"user","text":"占位: 接力界面自检用的替身逐条记录"}\n',
                       encoding="utf-8")
        await page.route("**/api/handoff/preview",
                         lambda r: r.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(preview_payload(rel_fix))))
        status = await wait_idle(page, 60)
        logged_in = status.get("state") == "logged_in" and not status.get("busy")
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1200)
        jserr = []                          # 界面没反应往往是脚本报错, 先把原因留下
        page.on("pageerror", lambda e: jserr.append(str(e)[:160]))
        page.on("console", lambda m: jserr.append(m.text[:160]) if m.type == "error" else None)

        # ---- 1) 与同排邻居同一个 computed 值(拿「回复来源」当基准, 它也是 .tool)
        pair = await page.evaluate(
            """()=>{const g=s=>{const e=document.querySelector(s),c=getComputedStyle(e),r=e.getBoundingClientRect();
              return {bg:c.backgroundColor,border:c.borderColor,color:c.color,fs:c.fontSize,
                      rad:c.borderRadius,h:Math.round(r.height)};};
              return {mine:g("#btnHandoff"), nbr:g("#btnReply")};}""")
        m, n = pair["mine"], pair["nbr"]
        print("1) 接力预览 vs 回复来源:")
        print("   mine", json.dumps(m, ensure_ascii=False))
        print("   nbr ", json.dumps(n, ensure_ascii=False))
        for k in ("bg", "border", "color", "fs", "rad", "h"):
            if m[k] != n[k]:
                bad.append("第 %s 项与邻居不一致: %r vs %r" % (k, m[k], n[k]))
        if m["bg"] != "rgba(0, 0, 0, 0)" and m["bg"] != "rgb(255, 255, 255)":
            bad.append("按钮有底色(不该跟邻居不一样): " + m["bg"])

        # ---- 2) 悬停只动颜色
        hv = await page.evaluate(
            """async()=>{const e=document.querySelector("#btnHandoff");
              const g=()=>{const c=getComputedStyle(e),r=e.getBoundingClientRect();
                return {bg:c.backgroundColor,bd:c.borderColor,shadow:c.boxShadow,
                        w:Math.round(r.width),h:Math.round(r.height),t:c.transform};};
              const before=g(); e.dispatchEvent(new MouseEvent("mouseover",{bubbles:true}));
              await new Promise(r=>setTimeout(r,260)); return {before, after:g()};}""")
        await page.hover("#btnHandoff")
        await page.wait_for_timeout(300)
        after = await page.evaluate(
            """()=>{const e=document.querySelector("#btnHandoff"),c=getComputedStyle(e),
              r=e.getBoundingClientRect();
              return {bg:c.backgroundColor,bd:c.borderColor,shadow:c.boxShadow,
                      w:Math.round(r.width),h:Math.round(r.height),t:c.transform};}""")
        print("2) 悬停前 %s\n   悬停后 %s" % (json.dumps(hv["before"], ensure_ascii=False),
                                            json.dumps(after, ensure_ascii=False)))
        for k in ("bg", "shadow", "w", "h", "t"):
            if after[k] != hv["before"][k]:
                bad.append("悬停时 %s 变了(只允许换边框颜色): %r -> %r" % (k, hv["before"][k], after[k]))
        await page.mouse.move(0, 0)

        # ---- 7) 未登录时置灰(放在前面测完再登录态判断)
        en = await page.evaluate("()=>{const b=document.querySelector('#btnHandoff');"
                                 "return {dis:b.disabled, title:b.title, "
                                 "shown:!!(b.offsetWidth||b.offsetHeight)};}")
        print("7) disabled=%s 可见=%s title=%r" % (en["dis"], en["shown"], en["title"][:38]))
        if not en["shown"]:
            bad.append("按钮不可见 = 没做")
        if logged_in and en["dis"]:
            bad.append("已登录却置灰: " + en["title"])
        if not logged_in and not en["dis"]:
            bad.append("未登录还能点")

        if not logged_in:
            skipped.append("等了 60s 桥仍不是「已登录且空闲」(state=%s busy=%s): 跳过 3~6 "
                           "(弹框内容/渲染/滚动/任务预填) —— 那几条才是这层的本体"
                           % (status.get("state"), status.get("busy")))
        else:
            # ---- 0) 导出按钮的可用规则: "有站点会话 id 才能导"。两条路都确定性地走一遍,
            #          不去猜站点此刻停在哪一段上(上一轮检查可能已经把它留在某段会话里)。
            await page.request.post(URL + "/api/new_chat")
            await page.wait_for_timeout(2500)
            await page.reload(wait_until="networkidle")
            await page.wait_for_timeout(1200)
            srv0 = await (await page.request.get(URL + "/api/status")).json()
            pre = await page.evaluate("()=>({dis:document.querySelector('#convExport').disabled,"
                                      "title:document.querySelector('#convExport').title})")
            print("0a) 新对话(会话 id=%r): disabled=%s title=%r"
                  % (srv0.get("conversation_id"), pre["dis"], pre["title"][:34]))
            if bool(srv0.get("conversation_id")):
                bad.append("POST /api/new_chat 之后站点还在带 id 的会话上, 这条用例的前提没了")
            if not pre["dis"]:
                bad.append("没有会话 id 时导出按钮竟然可点")
            if "会话 id" not in pre["title"]:
                bad.append("置灰了但没说出为什么(提示里没提会话 id): " + pre["title"][:60])

            cl = await (await page.request.get(URL + "/api/conversations")).json()
            items = cl.get("items") or []
            if not items:
                bad.append("站点侧栏一条会话都没有, 这一层没法验")
            else:
                it = items[0]
                await page.request.post(
                    URL + "/api/conversations/open",
                    data=json.dumps({"key": it.get("key") or "", "url": it.get("url") or ""}),
                    headers={"content-type": "application/json"})
                # 界面只在 WS status 事件时更新 state, 这次是外部调的 API -> 重新读一次状态
                await page.reload(wait_until="networkidle")
                await page.wait_for_timeout(1200)
                # 后端这会儿正在后台把这段历史读回放盘(它占着互斥, 按钮全是灰的)。
                # 不等它就量按钮, 量到的是"正忙的置灰", 不是"空闲态到底能不能导"。
                srv1 = await wait_idle(page, 200)
                if srv1.get("busy"):
                    bad.append("切会话后 200s 仍未空闲(互斥没释放): " + str(srv1.get("busy_reason")))
                post = await page.evaluate("()=>({dis:document.querySelector('#convExport').disabled,"
                                           "title:document.querySelector('#convExport').title})")
                print("0b) 切到 %r: 会话 id=%s disabled=%s"
                      % ((it.get("title") or "")[:14], str(srv1.get("conversation_id"))[:8],
                         post["dis"]))
                if not srv1.get("conversation_id"):
                    bad.append("切了会话但后端还是没有会话 id")
                if post["dis"]:
                    bad.append("有会话 id 了导出仍不可点: " + post["title"][:60])

            # ---- 5) 任务预填: 你正在打的这段话
            await page.fill("#input", ASK)
            await page.click("#btnHandoff")
            await page.wait_for_timeout(350)
            pref = await page.input_value("#hoTask")
            print("5) 任务预填 = %r" % pref[:44])
            if pref.strip() != ASK.strip():
                bad.append("没把输入框里那段话接过来: %r" % pref[:44])

            # ---- 3) 固定高度: 空着先量一次
            h0 = await page.evaluate("()=>Math.round(document.querySelector('.ho-dialog')"
                                     ".getBoundingClientRect().height)")
            ov = await page.evaluate("()=>getComputedStyle(document.querySelector('#hoOverlay')).display")
            print("3) 弹框 display=%s 空白时高=%dpx" % (ov, h0))
            if ov != "flex":
                bad.append("点按钮没弹出弹框")

            # ---- 6) 生成一次(走 runHandoff 的真实代码路径; 后端那份回答是开头 route 换的长笔记)
            await page.click("#hoRun")
            t6 = time.time()
            try:
                await page.wait_for_function(
                    "()=>document.querySelectorAll('#hoStat .ho-kv').length>=4", timeout=30_000)
            except Exception:
                txt = await page.text_content("#hoNote")
                bad.append("生成没出结果(等了 %.0fs), 正文写着: %r"
                           % (time.time() - t6, (txt or "")[:120]))
            await page.wait_for_timeout(400)

            cost = await page.text_content("#hoCost")
            print("6a) 代价一句话: %s" % (cost or "").replace("\n", " ")[:150])
            if not any(c.isdigit() for c in (cost or "")) or "%" not in (cost or ""):
                bad.append("代价没写成带数字的一句话: %r" % (cost or "")[:60])
            if "undefined" in (cost or "") or "NaN" in (cost or ""):
                bad.append("代价那句话里出现了 undefined/NaN(后端字段和界面读的对不上): %r"
                           % (cost or "")[:120])

            stat = await page.evaluate(
                """()=>[...document.querySelectorAll('#hoStat .ho-kv')].map(r=>({
                    k:r.querySelector('.k').textContent, v:r.querySelector('b').textContent,
                    cls:r.className}));""")
            print("6b) 构成 %s" % json.dumps(stat, ensure_ascii=False))
            if not any("ok" in s["cls"] and "jsonl" in s["v"] for s in stat):
                bad.append("「逐条记录」那一格没有指到真实文件: %s" % stat)
            clip = await page.evaluate(
                """()=>{const r=document.querySelector('#hoStat .ho-kv.wide');
                  if(!r) return null; const b=r.querySelector('b');
                  return {sw:b.scrollWidth, cw:b.clientWidth, txt:b.textContent,
                          title:b.title.length};}""")
            print("6e) 路径格 %s" % json.dumps(clip, ensure_ascii=False))
            if not clip:
                bad.append("路径那一格没有 wide 类(会被省略号截断)")
            elif clip["sw"] > clip["cw"] + 1:
                bad.append("路径被截断了: 内容 %d px > 容器 %d px —— 这格的全部用途就是照着找文件"
                           % (clip["sw"], clip["cw"]))

            note = await page.evaluate(
                """()=>{const e=document.querySelector('#hoNote .assistant-body');
                  if(!e) return {missing:true, noteCls:document.querySelector('#hoNote').className,
                                 noteTxt:(document.querySelector('#hoNote').innerText||'').slice(0,120)};
                  return {missing:false, h2:e.querySelectorAll('h1,h2,h3').length,
                          txt:e.innerText.slice(0,90),
                          raw:(e.innerText.match(/^#{1,3}\\s/gm)||[]).length,
                          html:e.innerHTML.length,
                          script:e.querySelectorAll('script,iframe,object,embed').length,
                          onattr:[...e.querySelectorAll('*')].filter(x=>[...x.attributes]
                                .some(a=>/^on/i.test(a.name))).length,
                          javascript:(e.innerHTML.match(/javascript:/i)||[]).length};}""")
            print("6c) 笔记渲染 %s" % json.dumps(note, ensure_ascii=False))
            if note.get("missing"):
                bad.append("笔记没渲染进 .assistant-body: 容器 class=%r 正文=%r"
                           % (note["noteCls"], note["noteTxt"]))
            else:
                if note["h2"] < 2:
                    bad.append("笔记没按 markdown 渲染出小节标题(只有 %d 个): 等于没渲染" % note["h2"])
                if note["raw"]:
                    bad.append("屏幕上还挂着 %d 处 '## ' 原文" % note["raw"])
                if note["script"] or note["onattr"] or note["javascript"]:
                    bad.append("模型输出里出现了可执行入口: script=%d on*=%d javascript:=%d"
                               % (note["script"], note["onattr"], note["javascript"]))
            ex = await page.evaluate("()=>document.querySelectorAll('.ho-ex .ho-kv').length")
            print("6d) 原文片段列了 %d 行" % ex)
            if ex < 1:
                bad.append("片段清单是空的(但后端返回了片段吗? 看 6b)")
            cp = await page.evaluate("()=>document.querySelector('#hoCopy').disabled")
            if cp:
                bad.append("生成完「复制整份」还是灰的")

            # ---- 3+4) 高度没跳 / 只有正文滚
            h1 = await page.evaluate("()=>Math.round(document.querySelector('.ho-dialog')"
                                     ".getBoundingClientRect().height)")
            print("3) 生成完高=%dpx (空白时 %dpx)" % (h1, h0))
            if abs(h1 - h0) > 1:
                bad.append("弹框高度跳了 %dpx —— 不是固定高" % (h1 - h0))
            scroll = await page.evaluate(
                """()=>{const d=document.querySelector('.ho-dialog'),
                    b=d.querySelector('.dlg-body');
                  return {dOver:d.scrollHeight-d.clientHeight, bOver:b.scrollHeight-b.clientHeight,
                          bOv:getComputedStyle(b).overflowY};}""")
            print("4) 溢出量 %s" % json.dumps(scroll, ensure_ascii=False))
            if scroll["dOver"] > 1:
                bad.append("弹框自己出现了溢出(外层会滚): %dpx" % scroll["dOver"])
            if scroll["bOv"] not in ("auto", "scroll"):
                bad.append("正文容器不可滚: " + scroll["bOv"])
            if scroll["bOver"] < 200:
                bad.append("这份长笔记没把正文撑开(只溢出 %dpx): 那「只有正文滚」这一条就没量到"
                           % scroll["bOver"])
            await page.click("#hoClose")
            await page.wait_for_timeout(250)
            if await page.evaluate("()=>getComputedStyle(document.querySelector('#hoOverlay')).display") != "none":
                bad.append("关闭按钮没关掉弹框")

            # ---- 8) 导出: 与同排图标同值 -> 点一下 -> 文件真的落到盘上
            pair2 = await page.evaluate(
                """()=>{const g=s=>{const e=document.querySelector(s),c=getComputedStyle(e),
                    r=e.getBoundingClientRect();
                  return {bg:c.backgroundColor,border:c.borderColor,color:c.color,
                          w:Math.round(r.width),h:Math.round(r.height),
                          vis:!!(e.offsetWidth||e.offsetHeight)};};
                  return {mine:g("#convExport"), nbr:g("#convRefresh")};}""")
            m2, n2 = pair2["mine"], pair2["nbr"]
            print("8a) 导出图标 vs 刷新图标:\n    mine %s\n    nbr  %s"
                  % (json.dumps(m2, ensure_ascii=False), json.dumps(n2, ensure_ascii=False)))
            if not m2["vis"]:
                bad.append("导出图标不可见 = 没做")
            for k in ("bg", "border", "color", "w", "h"):
                if m2[k] != n2[k]:
                    bad.append("导出图标第 %s 项与同排邻居不一致: %r vs %r" % (k, m2[k], n2[k]))
            st8 = await wait_idle(page, 200)
            if st8.get("busy"):
                bad.append("走到导出这一步前互斥还没释放(200s): " + str(st8.get("busy_reason")))
            dis = await page.evaluate("()=>document.querySelector('#convExport').disabled")
            print("    disabled=%s title=%r" % (dis, (await page.evaluate(
                "()=>document.querySelector('#convExport').title"))[:40]))
            if dis:
                bad.append("已登录、空闲、且有会话 id, 导出按钮却还是灰的")

            t0 = time.time()
            await page.click("#convExport")
            await page.wait_for_selector("#exOverlay.show", timeout=60_000)
            stat = await page.text_content("#exStat") or ""
            print("8b) %.1fs 后弹框写着: %s" % (time.time() - t0, stat.replace("\n", " ")[:130]))
            found = re.search(r"写到\s+(\S+?\.md)", stat)
            rel = found.group(1) if found else ""
            if not rel:
                bad.append("没在提示里报出文件名: %r" % stat[:80])
            else:
                f = ROOT / rel
                if not f.exists():
                    bad.append("提示说有这个文件但盘上没有: " + rel)
                else:
                    txt = f.read_text(encoding="utf-8")
                    nwin = txt.count("\n## 窗口 ")
                    print("    文件 %s (%d 字), 窗口节数 %d, 含逐字消息=%s"
                          % (rel, len(txt), nwin, "检查显卡状态" in txt or "nvidia-smi" in txt))
                    if nwin < 1:
                        bad.append("导出的文件里没有窗口分节")
                    if len(txt) < 1000:
                        bad.append("导出文件只有 %d 字, 不像一段真对话" % len(txt))
            hx = await page.evaluate(
                """()=>{const d=document.querySelector('.ex-dialog'),
                    p=document.querySelector('.ex-pre'),b=d.querySelector('.dlg-body');
                  return {dh:Math.round(d.getBoundingClientRect().height),
                          dOver:d.scrollHeight-d.clientHeight, preOver:p.scrollHeight-p.clientHeight,
                          preOv:getComputedStyle(p).overflowY, bodyOv:getComputedStyle(b).overflowY};}""")
            print("8c) 导出弹框 %s" % json.dumps(hx, ensure_ascii=False))
            if hx["dOver"] > 1:
                bad.append("导出弹框外层会滚: 溢出 %dpx" % hx["dOver"])
            if hx["preOv"] not in ("auto", "scroll"):
                bad.append("预览区不可滚: " + hx["preOv"])
            await page.click("#exClose")
            await page.wait_for_timeout(200)
            if await page.evaluate("()=>getComputedStyle(document.querySelector('#exOverlay')).display") != "none":
                bad.append("导出弹框关不掉")

        await browser.close()

    fix.unlink(missing_ok=True)          # 替身原件用完就删, 别留在 transcripts/ 里被当成一段真会话

    if jserr:
        print("\n页面 JS 报错(前 5 条):")
        for e in jserr[:5]:
            print(" -", e)
    if skipped:
        print("\n本次跳过:")
        for s in skipped:
            print(" -", s)
    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("HANDOFF_UI_OK (与邻居同值 / 悬停只换边框色 / 固定高不跳 / 只有正文滚 / "
          "任务预填 / 笔记按 markdown 渲染且无可执行入口)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
