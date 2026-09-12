"""欢迎区截图(供用户查看居中效果)。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

OUT = Path(r"H:\browser-bridge\.tmp\welcome_preview.png")


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900},
                                      device_scale_factor=1.5)
        await page.goto("http://127.0.0.1:8765", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1200)
        await page.screenshot(path=str(OUT), full_page=False)
        await browser.close()
    print("saved:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
