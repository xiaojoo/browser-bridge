"""校验: 工作区面板标题显示目录名, 且文件树正常、无 JS 错误。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402


async def main() -> int:
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto("http://127.0.0.1:8765", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1500)
        info = await page.evaluate("""() => ({
          title: (document.getElementById('wsTitle') || {}).textContent || '',
          rows: document.querySelectorAll('#wsTree .ws-file').length,
          dirs: document.querySelectorAll('#wsTree .wsdir').length,
          firstFile: (document.querySelector('#wsTree .ws-file .fn') || {}).textContent || ''
        })""")
        print(json.dumps(info, ensure_ascii=False))
        await browser.close()
    assert not errors, f"JS 报错: {errors}"
    assert "workspace" in info["title"], f"标题未显示目录名: {info['title']}"
    assert info["rows"] >= 1, "未列出文件"
    print("WS_TITLE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
