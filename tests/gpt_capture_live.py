"""有头复现: 在真实会话里发一条, 记录 baseline 快照与每次 on_delta 输出, 定位 dom 捕获。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from bridge import browser, capture, config, providers  # noqa: E402

p = providers.get("chatgpt")
profile = config.PROFILE_DIR / "chatgpt"

PRE = """() => {
  const msgs = Array.from(document.querySelectorAll('[data-message-author-role]'));
  return msgs.map(m => ({
    role: m.getAttribute('data-message-author-role'),
    len: (m.innerText||'').length,
    head: (m.innerText||'').slice(0,16)
  }));
}"""
TEST = "请说：已收到"


async def main() -> int:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False,
            viewport={"width": 1280, "height": 860},
            user_agent=browser._UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            args=["--disable-blink-features=AutomationControlled", "--lang=zh-CN"],
        )
        await ctx.add_init_script(capture.FETCH_WRAPPER_JS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(p.url, wait_until="domcontentloaded", timeout=60_000)
        for _ in range(40):
            await page.wait_for_timeout(1000)
            try:
                probe = await page.evaluate(browser._LOGIN_PROBE_JS, list(p.composer_selectors))
            except Exception:
                probe = {}
            if probe.get("hasComposer"):
                break
        print("pre_messages:", json.dumps(await page.evaluate(PRE), ensure_ascii=False))

        sel = p.snapshot_selector
        baseline = (await capture.read_snapshot(page, sel)).get("text") or ""
        print("baseline_len:", len(baseline), "head:", baseline[:20])

        composer = None
        for s in p.composer_selectors:
            loc = page.locator(s).first
            try:
                if await loc.count() and await loc.is_visible():
                    composer = loc; break
            except Exception:
                continue
        if composer is None:
            print("NO_COMPOSER"); await ctx.close(); return 1
        await composer.click()
        await page.keyboard.type(TEST, delay=2)
        await page.keyboard.press("Enter")

        emits = []
        def on_delta(kind, text, snapshot):
            emits.append({"kind": kind, "snap": snapshot, "len": len(text), "head": text[:20]})
        try:
            truncated, errs = await capture.wait_turn_end(
                page, on_delta, mode="dom", snapshot_selector=sel)
            print("turn_end:", {"truncated": truncated, "errs": errs})
        except capture.NoDataError as exc:
            print("NoDataError:", exc)
        final = (await capture.read_snapshot(page, sel)).get("text") or ""
        print("emits:", json.dumps(emits, ensure_ascii=False))
        print("final_len:", len(final), "final_head:", final[:40])
        print("post_messages:", json.dumps(await page.evaluate(PRE), ensure_ascii=False))

        # 第二轮: 此时会话已有 assistant 消息, 复现"非空 baseline"场景
        baseline2 = (await capture.read_snapshot(page, sel)).get("text") or ""
        print("baseline2_len:", len(baseline2), "head:", baseline2[:16])
        await composer.click()
        await page.keyboard.type("请说：再次收到", delay=2)
        await page.keyboard.press("Enter")
        emits2 = []
        def on_delta2(kind, text, snapshot):
            emits2.append({"kind": kind, "snap": snapshot, "len": len(text), "head": text[:16]})
        try:
            t2, e2 = await capture.wait_turn_end(
                page, on_delta2, mode="dom", snapshot_selector=sel)
            print("turn2_end:", {"truncated": t2, "errs": e2})
        except capture.NoDataError as exc:
            print("turn2 NoDataError:", exc)
        final2 = (await capture.read_snapshot(page, sel)).get("text") or ""
        print("emits2:", json.dumps(emits2, ensure_ascii=False))
        print("final2_len:", len(final2), "final2_head:", final2[:40])

        await ctx.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
