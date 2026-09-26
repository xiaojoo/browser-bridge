"""校验: 工作区面板标题显示当前目录名, 且真的看得见、树里列得出文件、无 JS 错误。

这条以前把断言写在 **"workspace"** 这个字面上 —— 那是当年那台机器的目录名。工作区根换成
`H:\\test` 之后标题就是「📁 test」, 于是它天天红, 量的其实不是代码。
现在用 `ws_root_case.workspace_root`: 自己造一个小工作区、把根指过去、断言标题里出现那个目录名,
出了 with 块再断言**根已经还给用户**(以前这个检查写在 with 里面, 那时根当然还是临时目录)。
标题还要断言"看得见"(`offsetWidth>0`) —— 文字在 DOM 里不等于画在屏幕上。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from ws_root_case import get_root, workspace_root  # noqa: E402

BASE = "http://127.0.0.1:8765"


async def main() -> int:
    bad, info = [], {}
    with workspace_root({"a.txt": "x\n"}) as tmp:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(BASE, wait_until="networkidle", timeout=30_000)
            await page.evaluate("() => { showWelcome(false); switchMode('world'); }")
            await page.wait_for_timeout(1600)
            info = await page.evaluate("""() => {
              const t = document.getElementById('wsTitle');
              return {title: t ? t.textContent.trim() : '', w: t ? t.offsetWidth : 0,
                      rows: document.querySelectorAll('#wsTree .ws-file').length,
                      names: [...document.querySelectorAll('#wsTree .ws-file .fn')]
                        .map(e => e.textContent)};
            }""")
            print(json.dumps(info, ensure_ascii=False))
            await browser.close()
        if tmp.name not in info["title"]:
            bad.append("标题没显示目录名: %r (期望含 %r)" % (info["title"], tmp.name))
        if info["w"] < 1:
            bad.append("标题在 DOM 里但没画出来(offsetWidth=%s) —— 等于没做" % info["w"])
        if info["rows"] < 1 or "a.txt" not in " ".join(info["names"]):
            bad.append("树里没列出临时工作区那个文件: %s" % info["names"])
        if errors:
            bad.append("JS 报错: " + " | ".join(errors[:3]))
    back = get_root()
    print("出了临时工作区之后, 根 =", back)
    if tmp.name in back:
        bad.append("没把用户的工作区根还回去: " + back)
    if not bad:
        print("WS_TITLE_OK (标题显示当前工作区目录名且真的可见; 树列得出文件; 退出时把根还给用户)")
        return 0
    print("FAIL:")
    for x in bad:
        print(" -", x)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
