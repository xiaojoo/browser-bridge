"""工作区文件树: 深目录/长文件名横向超出时要能横向滚动(底部出现横条), 名字不再被压成 "Cl…"。

原来 .ws-tree 虽然写了 overflow:auto, 但每一行都被压到面板宽度以内(靠省略号截断),
所以永远不会横向超出 -> 横条永远不出现, 深层文件名也看不到。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"
DEEP = "build_verify/CMakeFiles/4.1.0-rc1/CompilerIdCXX/Debug/CompilerIdCXX"
LONG_NAME = "CMakeCXXCompilerId_with_a_really_long_name.qml"

TREE = {
    "ok": True, "root": "H:\\tmp\\ws-hscroll", "name": "steward", "default": "", "custom": True,
    "items": [
        {"path": "src/main.py", "kind": "file", "size": 120},
        {"path": DEEP + "/" + LONG_NAME, "kind": "file", "size": 30700},
        {"path": DEEP + "/a_short_one.o", "kind": "file", "size": 708},
    ],
}

STUB = r"""(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  const TREE = __TREE__;
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/providers") === 0)
      return json({ default: "chatgpt", providers: [{ id: "chatgpt", name: "ChatGPT Web", short: "GPT",
        capture_mode: "dom", conversation_pattern: "/c/" }] });
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/workspace/tree") === 0) return json(TREE);
    if (s.indexOf("/api/workspace") === 0)
      return json({ ok: true, root: TREE.root, name: TREE.name, default: "", custom: true });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();""".replace("__TREE__", json.dumps(TREE, ensure_ascii=False))

MEASURE = r"""() => {
  const t = document.getElementById("wsTree");
  const styles = getComputedStyle(t);
  const files = Array.from(t.querySelectorAll(".ws-file .fn"));
  const deep = files.find(e => e.textContent.indexOf("CMakeCXXCompilerId") >= 0);
  const short = files.find(e => e.textContent === "main.py");
  return {
    clientW: t.clientWidth, scrollW: t.scrollWidth,
    overflowX: styles.overflowX, scrollbarWidth: styles.scrollbarWidth,
    canScrollX: t.scrollWidth > t.clientWidth + 2,
    deepName: deep ? deep.textContent : "",
    deepClipped: deep ? (deep.scrollWidth > deep.clientWidth + 1) : null,
    shortClipped: short ? (short.scrollWidth > short.clientWidth + 1) : null,
    shortRowW: short ? Math.round(short.parentElement.getBoundingClientRect().width) : 0,
    topRowW: Math.round((document.querySelector("#wsTree > .ws-node > .ws-folder-row") || {getBoundingClientRect: () => ({width: 0})}).getBoundingClientRect().width),
    panelW: Math.round(t.getBoundingClientRect().width),
    left: Math.round(t.scrollLeft),
  };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1180, "height": 820})
        page.on("pageerror", lambda e: bad.append("页面报错: " + str(e)[:160]))
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.evaluate("() => switchMode('world')")
        await page.wait_for_timeout(900)

        # 全部展开(用工具栏那个按钮), 让最深的文件真正参与布局
        await page.click("#wsExpand")
        await page.wait_for_timeout(500)
        opened = await page.evaluate(
            "() => document.querySelectorAll('#wsTree .ws-folder-row[aria-expanded=\"true\"]').length")
        st = await page.evaluate(MEASURE)
        print("展开的目录:", opened)
        print("文件树:", json.dumps(st, ensure_ascii=False))

        if not st["canScrollX"]:
            bad.append("深树没有横向超出, 横条也就不会出现: " + json.dumps(st, ensure_ascii=False))
        if st["overflowX"] not in ("auto", "scroll"):
            bad.append("文件树不是可横向滚动: overflow-x=" + st["overflowX"])
        if st["deepName"] != LONG_NAME:
            bad.append("深层文件名没显示全: " + st["deepName"])
        if st["deepClipped"]:
            bad.append("深层文件名被截断成省略号了(看不到全名)")
        if st["shortClipped"]:
            bad.append("普通行也被截断了: main.py")
        # 短行/顶层行要撑满面板宽度, 否则 hover 高亮只照亮半截
        if st["topRowW"] + 14 < st["panelW"]:
            bad.append(f"顶层行没有撑满面板: 行 {st['topRowW']} / 面板 {st['panelW']}")

        # 横条真的能滚: 滚到最右, 最深那个文件应该完整露出
        scrolled = await page.evaluate(r"""() => {
          const t = document.getElementById("wsTree");
          t.scrollLeft = t.scrollWidth;
          return t.scrollLeft;
        }""")
        await page.wait_for_timeout(250)
        after = await page.evaluate(MEASURE)
        print("滚到最右:", scrolled, "->", after["left"])
        if scrolled <= 0:
            bad.append("横向滚不动")
        vis = await page.evaluate(r"""() => {
          const t = document.getElementById("wsTree").getBoundingClientRect();
          const fn = Array.from(document.querySelectorAll("#wsTree .ws-file .fn"))
            .find(e => e.textContent.indexOf("CMakeCXXCompilerId") >= 0);
          const r = fn.getBoundingClientRect();
          return { right: Math.round(r.right), panelRight: Math.round(t.right) };
        }""")
        print("滚动后最长名字右边界:", json.dumps(vis, ensure_ascii=False))
        if vis["right"] > vis["panelRight"] + 2:
            bad.append("滚到最右仍然看不到文件名结尾: " + json.dumps(vis, ensure_ascii=False))
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WS_TREE_HSCROLL_OK (深目录/长文件名横向超出时出现横条, 能滚到最右看全名字)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
