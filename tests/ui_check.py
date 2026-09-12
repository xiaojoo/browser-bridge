"""UI 冒烟: 加载本地首页, 断言无 JS 运行时错误且关键控件存在。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"


async def main() -> int:
    errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1200)
        check = await page.evaluate("""() => {
          const sel = document.getElementById('selProvider');
          return {
            hasSelect: !!sel,
            options: sel ? Array.from(sel.options).map(o => o.value) : [],
            hasStart: !!document.getElementById('btnStart'),
            startLabel: (document.getElementById('btnStart')||{}).textContent || '',
            chipText: (document.getElementById('chipBrowser')||{}).textContent || '',
          };
        }""")
        print("UI:", check)
        print("pageErrors:", errors if errors else "none")
        await browser.close()
    assert not errors, f"页面 JS 报错: {errors}"
    assert check["hasSelect"] and check["options"] == ["deepseek", "chatgpt"], "Provider 下拉未就绪"
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
