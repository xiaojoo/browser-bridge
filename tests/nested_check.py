"""校验嵌套文件树: 默认折叠、点一下展开、展开后子层真的比根层缩进。

这条以前常年红，报的是"文件夹未默认展开"，但根因有三个，全在量具这一侧：
  1) 它查的选择器 `.wsdir` 在现在的树里根本不存在(树早改成 `div.ws-node.ws-folder` +
     `aria-expanded`)，`dirCount` 恒为 0；
  2) 它没切到 World 模式 —— 工作区那块面板在非 World 模式下是 display:none，
     整棵树 `offsetWidth=0`(实测 left=0 w=0)，"存在但看不见"被当成"没展开"；
  3) 产品决定就是**默认折叠**(`setOpen(false)  // 默认折叠`)，断言却要求默认全开。
另外它还跑在**用户真实工作区根**上(往 H:\\test 里建 help/nested.md 再删)，并把整棵树的
文件名打印出来(node_modules 一灌 46KB)。

现在：临时工作区 + World 模式 + 按"折叠→点击→展开→缩进 20px"这条真链路断言。
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

from ws_root_case import req, workspace_root  # noqa: E402

BASE = "http://127.0.0.1:8765"

# 每行: 文字 / 是否看得见 / 离树左边多远 / 文件夹的 aria-expanded / 子项是否藏在 hidden 容器里
GEO = """() => [...document.querySelectorAll('#wsTree .ws-file, #wsTree .ws-folder-row')]
  .map(e => { const r = e.getBoundingClientRect();
    return {txt: e.textContent.trim().slice(0, 14),
            vis: !!(e.offsetWidth || e.offsetHeight),
            left: Math.round(r.left), w: Math.round(r.width),
            exp: e.getAttribute('aria-expanded'),
            inHidden: !!e.closest('.ws-folder-children[hidden]')}; })"""


def at(rows, name):
    return next((r for r in rows if name in r["txt"]), None)


async def main() -> int:
    bad = []
    with workspace_root({"README.txt": "根层文件, 当缩进基准\n"}) as tmp:
        req("POST", "/workspace/file", {"path": "help/nested.md", "content": "# nested\n"})
        req("POST", "/workspace/file", {"path": "help/deep/older.md", "content": "# deep\n"})
        await asyncio.sleep(0.4)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            await page.goto(BASE, wait_until="networkidle", timeout=30_000)
            await page.evaluate("() => { showWelcome(false); switchMode('world'); }")
            await page.wait_for_timeout(1800)
            closed = await page.evaluate(GEO)
            print("折叠时:", json.dumps(closed, ensure_ascii=False))
            await page.click("#wsTree .ws-folder-row")
            await page.wait_for_timeout(400)
            opened = await page.evaluate(GEO)
            print("展开后:", json.dumps(opened, ensure_ascii=False))
            await browser.close()

        folder_c = at(closed, "help")
        nested_c, nested_o = at(closed, "nested.md"), at(opened, "nested.md")
        root_o = at(opened, "README.txt")
        if not (folder_c and folder_c["vis"]):
            bad.append("文件夹行在 World 模式下不可见(整块树没画出来): %s" % json.dumps(closed))
        elif folder_c["exp"] != "false":
            bad.append("产品决定是默认折叠, 现在 aria-expanded=%s" % folder_c["exp"])
        if not nested_c or nested_c["vis"]:
            bad.append("折叠时子层文件本该不可见: %s" % json.dumps(closed))
        if not (nested_o and nested_o["vis"]):
            bad.append("点文件夹没展开(子层文件还是看不见): %s" % json.dumps(opened))
        elif folder_c and at(opened, "help")["exp"] != "true":
            bad.append("展开了但 aria-expanded 还是 false(读屏器/样式都认不出)")
        elif not (nested_o["left"] > (root_o or {}).get("left", 0)):
            bad.append("子层没有比根层更缩进: nested=%s README=%s"
                       % (nested_o["left"], (root_o or {}).get("left")))
        elif not at(opened, "older.md"):
            bad.append("两层深的 help/deep/older.md 没出现在树里: %s" % json.dumps(opened))
    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("NESTED_OK (默认折叠; 点一下展开且 aria-expanded 跟着翻; 子层比根层缩进 20px; "
          "全程只在自己造的临时工作区里跑)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
