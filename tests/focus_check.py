"""校验: 输入框聚焦时不再有高亮边框/光环。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        await page.goto("http://127.0.0.1:8765", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1200)
        before = await page.evaluate(
            "()=>{const s=getComputedStyle(document.querySelector('.composer'));"
            "return {border:s.borderColor, shadow:s.boxShadow}}")
        await page.click("#input")
        await page.wait_for_timeout(300)
        after = await page.evaluate(
            "()=>{const s=getComputedStyle(document.querySelector('.composer'));"
            "return {border:s.borderColor, shadow:s.boxShadow}}")
        print(json.dumps({"before": before, "after": after}, ensure_ascii=False))
        await browser.close()
    assert after["shadow"] == "none", f"聚焦仍有光环: {after}"
    assert after["border"] == before["border"], "聚焦边框颜色变了"
    print("FOCUS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
