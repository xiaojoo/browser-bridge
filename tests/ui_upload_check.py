"""上传 UI 校验: 文本/文件判定、chips 渲染与模式切换。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"

CHECK = """async () => {
  const out = {};
  const mk = (name, type, content) => new File([content || ('x'.repeat(50))], name, {type});
  out.isText_py = isTextFile(mk('a.py'));
  out.isText_md = isTextFile(mk('README.md'));
  out.isText_json = isTextFile(mk('data.json'));
  out.isText_txt = isTextFile(mk('note.txt','text/plain'));
  out.isImage_png = isTextFile(mk('shot.png','image/png'));
  out.isText_zip = isTextFile(mk('x.zip','application/zip'));
  // 渲染两个文件: 一个 py(可切换), 一个 png(无切换)
  staged = [];
  addFiles([mk('main.py'), mk('pic.png','image/png')]);
  const chips = Array.from(document.querySelectorAll('.chiprow'));
  out.chipCount = chips.length;
  const toggles = Array.from(document.querySelectorAll('.chiprow .tmode'));
  out.toggleCount = toggles.length;
  out.toggleLabel = toggles.length ? toggles[0].textContent : null;
  // 点击切换 -> 文本模式
  if (toggles.length) { toggles[0].click(); }
  out.afterToggle = staged[0].sendAsText === true;
  out.toggleLabelAfter = (document.querySelector('.chiprow .tmode')||{}).textContent;
  const togglesAfter = document.querySelectorAll('.chiprow .tmode');
  out.toggleClassAfter = togglesAfter.length ? togglesAfter[0].className : null;
  return out;
}"""


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)
        r = await page.evaluate(CHECK)
        import json
        print(json.dumps(r, ensure_ascii=False, indent=1))
        await browser.close()
    assert r["isText_py"] and r["isText_md"] and r["isText_json"] and r["isText_txt"], "文本类型判定失败"
    assert not r["isImage_png"] and not r["isText_zip"], "非文本类型误判"
    assert r["chipCount"] == 2 and r["toggleCount"] == 1, "chips/切换数量异常"
    assert r["toggleLabel"] == "文件", "默认应为文件模式"
    assert r["afterToggle"] and r["toggleLabelAfter"] == "文本", "切换无效"
    assert "txt" in r["toggleClassAfter"], "切换后样式未变"
    print("UPLOAD_UI_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
