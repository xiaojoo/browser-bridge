"""真实发送诊断: 用 ChatGPT 登录态发送一条简短消息, 抓取回答 DOM 结构与捕获结果。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from bridge import browser, capture, config, providers  # noqa: E402

p = providers.get("chatgpt")
profile = config.PROFILE_DIR / "chatgpt"
profile.mkdir(parents=True, exist_ok=True)

DUMP_JS = """() => {
  const markdownCnt = document.querySelectorAll('.markdown').length;
  const ass = document.querySelectorAll('[data-message-author-role="assistant"]');
  const assOut = Array.from(ass).map(a => ({
    firstClass: String(a.className||'').split(' ')[0] || null,
    hasMarkdown: !!a.querySelector('.markdown'),
    textLen: (a.innerText||'').length
  }));
  // 元素里 class 含 markdown / message / prose 的, 以及数据属性带 message 的
  const pat = document.querySelectorAll('[class*="markdown"], [class*="prose"], [data-message-id], [data-testid*="message"]');
  const patSample = Array.from(pat).slice(0, 12).map(e => ({
    tag: e.tagName, cls: String(e.className||'').slice(0, 50),
    role: e.getAttribute && (e.getAttribute('data-message-author-role') || e.getAttribute('data-testid') || ''),
    len: (e.innerText||'').length
  }));
  return { markdownCnt, assistantCount: ass.length, assOut, patSample };
}"""

TEST_MSG = "请只回复两个字: 收到"


async def main() -> int:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            viewport={"width": 1280, "height": 860},
            user_agent=browser._UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            args=["--disable-blink-features=AutomationControlled", "--lang=zh-CN"],
        )
        await ctx.add_init_script(capture.FETCH_WRAPPER_JS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(p.url, wait_until="domcontentloaded", timeout=60_000)
        # 等待登录探测通过
        sels = list(p.composer_selectors)
        for _ in range(40):
            await page.wait_for_timeout(1000)
            try:
                probe = await page.evaluate(browser._LOGIN_PROBE_JS, sels)
            except Exception:
                probe = {}
            if probe.get("hasComposer"):
                break
        print("probe:", json.dumps(probe, ensure_ascii=False))

        # 发送
        composer = None
        for s in sels:
            loc = page.locator(s).first
            try:
                if await loc.count() and await loc.is_visible():
                    composer = loc
                    break
            except Exception:
                continue
        if composer is None:
            print("NO_COMPOSER"); await ctx.close(); return 1
        await composer.click()
        await page.keyboard.type(TEST_MSG, delay=2)
        await page.keyboard.press("Enter")

        # 捕获(dom 模式)
        deltas = []
        async def on_delta(kind, text, snap):
            deltas.append({"kind": kind, "snap": snap, "len": len(text)})
        try:
            truncated, errs = await capture.wait_turn_end(
                page, on_delta, mode="dom", snapshot_selector=p.snapshot_selector)
            print("turn_end:", {"truncated": truncated, "errs": errs})
        except capture.NoDataError as exc:
            print("NoDataError:", exc)
        print("deltas_tail:", json.dumps(deltas[-8:], ensure_ascii=False))
        print("delta_count:", len(deltas))

        info = await page.evaluate(DUMP_JS)
        print("dom:", json.dumps(info, ensure_ascii=False))
        await ctx.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
