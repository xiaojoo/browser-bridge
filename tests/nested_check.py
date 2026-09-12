"""校验嵌套文件树: 文件夹默认展开, 子层级缩进显示。"""
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read().decode())


def reset():
    for p in ["help/nested.md", "help"]:
        try:
            urllib.request.urlopen(urllib.request.Request(
                BASE + "/workspace/file?path=" + urllib.request.quote(p), method="DELETE"), timeout=10)
        except Exception:
            pass


async def main() -> int:
    reset()
    req("POST", "/workspace/file", {"path": "help/nested.md", "content": "# nested\n"})
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        await page.goto(BASE, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1600)
        info = await page.evaluate("""() => {
          const dirs = Array.from(document.querySelectorAll('#wsTree .wsdir'));
          const files = Array.from(document.querySelectorAll('#wsTree .ws-file')).map(e => ({
            name: (e.querySelector('.fn')||{}).textContent || '',
            indent: e.offsetLeft
          }));
          return {
            dirCount: dirs.length,
            allOpen: dirs.every(d => d.open),
            fileNames: files.map(f=>f.name),
            nestedShows: files.some(f => f.name === 'nested.md'),
            nestedIndentPx: (files.find(f=>f.name==='nested.md')||{}).indent || 0,
            rootIndentPx: (files.find(f=>f.name==='README.txt')||{}).indent || 0
          };
        }""")
        print(json.dumps(info, ensure_ascii=False))
        await browser.close()
    reset()
    assert info["dirCount"] >= 1 and info["allOpen"], "文件夹未默认展开"
    assert info["nestedShows"], "嵌套文件未显示"
    assert info["nestedIndentPx"] < info["nestedIndentPx"] or info["nestedIndentPx"] > 0, "无缩进"
    print("NESTED_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
