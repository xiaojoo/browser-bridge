"""校验"发送"这条路: 附件还在上传时站点把发送键按住, 回车是空的 —— 不许再把没发出去
当成发出去了。

用一个**本地复刻**的 ChatGPT 输入框(带对话正文、隐藏 file input、附件 chip、发送键)跑:
  1) 附件判据只在"输入框那张卡片"里找文件名 —— 正文里出现过同一个名字不算落地
  2) 发送键禁用时: 直接回车 = 没发出去(复刻用户遇到的那个 bug)
  3) _send_and_confirm: 先等发送键可点, 可点了才回车, 并且确认输入框被清空
  4) 认不出发送键的站点(比如 DeepSeek 换了 DOM) -> 不拦, 立刻放行
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import browser, providers  # noqa: E402

P = providers.get("chatgpt")
NAME = "browser-bridge(2).7z"
TEXT = "分析这个项目"

# 复刻 ChatGPT: 正文里出现过同一个文件名; 附件 chip 在输入框那张卡片里;
# 上传没完成时发送键 disabled, 此时回车不提交(站点行为)
HTML = """<html><head><style>
  #prompt-textarea { min-height: 40px; width: 320px; border: 1px solid #ccc; }
  #composer-submit-button { width: 80px; height: 30px; }
</style></head><body>
  <div id="transcript">
    <div data-message-author-role="assistant">如果你是说刚才上传的 browser-bridge(2).7z 后消息没有正常发出去</div>
  </div>
  <form id="composer">
    <div id="chips"></div>
    <div id="prompt-textarea" contenteditable="true"></div>
    <input type="file" style="display:none">
    <button id="composer-submit-button" data-testid="send-button" aria-label="Send prompt" disabled>send</button>
  </form>
  <script>
    const ed = document.getElementById('prompt-textarea');
    const btn = document.getElementById('composer-submit-button');
    ed.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || e.shiftKey) return;
      e.preventDefault();
      if (btn.disabled) return;                 // 站点把发送键按住 -> 这一下回车是空的
      const t = ed.innerText;
      ed.innerText = '';
      const d = document.createElement('div');
      d.setAttribute('data-message-author-role', 'user');
      d.textContent = t;
      document.getElementById('transcript').appendChild(d);
    });
  </script>
</body></html>"""


def norm(s: str) -> str:
    return "".join((s or "").split())


async def main() -> int:
    bad: list[str] = []
    sels = list(P.composer_selectors)
    send_sels = list(P.send_selectors)

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page(viewport={"width": 900, "height": 600})
        await page.set_content(HTML)

        mgr = browser.BrowserManager()
        mgr.provider = P
        mgr.page = page

        # 1) 正文里有文件名 ≠ 附件已落地
        diag = await page.evaluate(browser._ATTACH_DIAG_JS, {"names": [NAME], "sels": sels})
        ok = diag["landed"][NAME] is False and diag["landedHtml"][NAME] is False
        print(f"  [正文里的文件名不算附件] landed={diag['landed'][NAME]} {'OK' if ok else 'FAIL'}")
        if not ok:
            bad.append("附件判据又被正文里的文件名骗了")

        # chip 真的出现在输入框那张卡片里 -> 才算落地
        await page.evaluate("""(n) => {
          const c = document.createElement('div');
          c.id = 'chip'; c.textContent = n;
          document.getElementById('chips').appendChild(c);
        }""", NAME)
        diag = await page.evaluate(browser._ATTACH_DIAG_JS, {"names": [NAME], "sels": sels})
        ok = bool(diag["landed"][NAME] or diag["landedHtml"][NAME])
        print(f"  [chip 进卡片算落地] landed={diag['landed'][NAME]} {'OK' if ok else 'FAIL'}")
        if not ok:
            bad.append("附件 chip 真进来了却没认出来")

        # 2) 发送键禁用 = 不可发
        st = await page.evaluate(browser._SENDABLE_JS, send_sels)
        ok = st == {"found": 1, "enabled": 0}
        print(f"  [上传中发送键不可点] {json.dumps(st)} {'OK' if ok else 'FAIL'}")
        if not ok:
            bad.append(f"发送键状态探测不对: {st}")

        composer = await mgr._composer()
        await mgr._fill_composer(composer, TEXT)

        # 3) 复刻用户的 bug: 不做任何等待, 直接回车 -> 一个字都没发出去
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)
        left = norm(await mgr._composer_text())
        sent_users = await page.evaluate(
            "() => document.querySelectorAll('[data-message-author-role=\"user\"]').length")
        ok = left == norm(TEXT) and sent_users == 0
        print(f"  [禁用时直接回车=发不出去] 输入框剩={left!r} 已发出={sent_users} {'OK' if ok else 'FAIL'}")
        if not ok:
            bad.append("复刻页没有复现'回车发不出去'")

        # 4) 等不到可点 -> 如实返回 False, 不硬发
        ok = await mgr._wait_sendable(timeout=1.2) is False
        print(f"  [一直不可点 -> 不硬发] {'OK' if ok else 'FAIL'}")
        if not ok:
            bad.append("_wait_sendable 在发送键一直禁用时竟然放行")

        # 5) 上传完成(发送键变可点) -> 自动等到可点才回车, 并且确认输入框被清空
        task = asyncio.create_task(mgr._send_and_confirm(TEXT))
        await asyncio.sleep(1.5)
        await page.evaluate("() => { document.getElementById('composer-submit-button').disabled = false; }")
        confirmed = await task
        left = norm(await mgr._composer_text())
        sent_users = await page.evaluate(
            "() => document.querySelectorAll('[data-message-author-role=\"user\"]').length")
        got = await page.evaluate(
            "() => (document.querySelector('[data-message-author-role=\"user\"]') || {}).innerText || ''")
        ok = confirmed is True and left == "" and sent_users == 1 and norm(got) == norm(TEXT)
        print(f"  [等到可点才发] confirmed={confirmed} 输入框剩={left!r} 已发出={sent_users} "
              f"正文={norm(got)!r} {'OK' if ok else 'FAIL'}")
        if not ok:
            bad.append("上传结束后没能把消息真的发出去")

        # 6) 认不出发送键的站点 -> 不拦(立刻放行), 免得卡住整轮
        bare = await b.new_page()
        await bare.set_content("<html><body><div id='prompt-textarea' contenteditable='true' "
                               "style='min-height:40px;width:300px'>hi</div></body></html>")
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        mgr.page = bare
        ok = await mgr._wait_sendable(timeout=30.0) is True and (loop.time() - t0) < 1.0
        print(f"  [认不出按钮就不拦] {'OK' if ok else 'FAIL'}")
        if not ok:
            bad.append("认不出发送键时没有立刻放行")
        await bare.close()
        await b.close()

    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("SEND_GATE_OK (附件判据只在输入框卡片里找; 发送键不可点时不硬回车, "
          "可点了才发并确认输入框被清空)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
