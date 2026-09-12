"""校验静态分离: /css/app.css 与 /js/app.js 可达, 页面渲染出样式且脚本函数已执行。"""
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8765" + path, timeout=10) as r:
        return r.status, len(r.read())


async def main() -> int:
    errors = []
    for p in ["/", "/css/app.css", "/js/app.js"]:
        code, n = get(p)
        assert code == 200 and n > 0, f"{p} -> {code}"
    print("assets_OK: /, /css/app.css, /js/app.js all 200")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto("http://127.0.0.1:8765", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1500)
        info = await page.evaluate("""() => ({
          bodyBg: getComputedStyle(document.body).backgroundColor,
          hasLoadWorkspace: typeof loadWorkspace === 'function',
          hasRefreshUi: typeof refreshUi === 'function',
          options: document.querySelectorAll('#selProvider option').length,
          wsTitle: (document.getElementById('wsTitle')||{}).textContent || '',
          cssLoaded: !!document.styleSheets.length
        })""")
        print(json.dumps(info, ensure_ascii=False))
        await browser.close()
    assert not errors, f"JS 报错: {errors}"
    assert info["cssLoaded"] and info["bodyBg"] == "rgb(255, 255, 255)", "CSS 未生效"
    assert info["hasLoadWorkspace"] and info["hasRefreshUi"], "app.js 未执行"
    assert info["options"] == 2, "Provider 下拉未就绪"
    print("STATIC_SEPARATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
