"""侧栏排版校验: 侧栏内文本一律单行, 超出用省略号截断(不换行, 不撑宽/撑高侧栏)。

例外: **工作区文件树**里的目录/文件名不再被省略号切掉, 而是靠文件树自己的横向滚动条看全
(见 tests/ws_tree_hscroll_check.py) —— 所以这两个元素只要求"单行 + 不撑高", 不要求截断,
但要求溢出被文件树自己吸收(侧栏整体仍然不许出现横向溢出)。

判定: 把每个文本元素换成超长字符串后, 元素盒高与侧栏 scrollHeight 必须不变(换行必然撑高),
同时要求 white-space:nowrap + text-overflow:ellipsis + overflow:hidden 且 scrollWidth>clientWidth
(即确实是用省略号截断, 而不是靠换行消化超长文本)。
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

# 测试期间断掉实时状态推送, 免得桥接端当前的站点/会话 id 影响断言
WS_STUB = """(() => {
  const orig = window.fetch.bind(window);
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "deepseek", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: null, conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "deepseek", current: null, items: [] });
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true, apply: null, verify: null });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

URL = "http://127.0.0.1:8765/"
ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / "workspace" / "_nowrap_tmp"
TMP_FILE = TMP_DIR / "长文件名测试-0123456789abcdef.txt"

SELECTORS = {
    "brand": ".sidebar .brand .name",
    "btnNew": ".sidebar .new span",
    "btnStart": ".sidebar .btnStart",
    "section": ".sidebar .section",
    "historyItem": ".sidebar .item",
    "wsTitle": ".sidebar .ws-title",
    "wsFolderName": ".sidebar .ws-folder-name",
    "wsFileName": ".sidebar .ws-file .fn",
    "wsSel": ".sidebar .ws-sel",
    "wsBtn": ".sidebar .ws-btn",
    "pickerName": ".sidebar .p-pick-name",
}

CHECK = """(args) => {
  const pick = (sel) => document.querySelector(sel);
  const h = (el) => Math.round(el.getBoundingClientRect().height * 100) / 100;
  const sb = pick(".sidebar");

  // 记录原始文本; 空元素先塞一个短文本做基准(空元素盒高为 0, 不能作为"是否被撑高"的基准)
  const orig = {};
  for (const [k, sel] of Object.entries(args.SELECTORS)) {
    const el = pick(sel);
    if (!el) continue;
    orig[k] = el.textContent;
    if (!el.textContent.trim()) el.textContent = "基准";
  }

  const base = { sidebarH: h(sb), sidebarScrollH: sb.scrollHeight, items: {} };
  for (const [k, sel] of Object.entries(args.SELECTORS)) {
    const el = pick(sel);
    if (el) base.items[k] = h(el);
  }

  // 注入超长文本
  for (const [k, sel] of Object.entries(args.SELECTORS)) {
    const el = pick(sel);
    if (el) el.textContent = args.LONG;
  }

  const after = { sidebarH: h(sb), sidebarScrollH: sb.scrollHeight, items: {} };
  const style = {};
  for (const [k, sel] of Object.entries(args.SELECTORS)) {
    const el = pick(sel);
    if (!el) continue;
    const cs = getComputedStyle(el);
    after.items[k] = h(el);
    style[k] = {
      nowrap: cs.whiteSpace === "nowrap",
      ellipsis: cs.textOverflow === "ellipsis",
      clipped: cs.overflowX === "hidden" || cs.overflowX === "clip",
      truncated: el.scrollWidth > el.clientWidth + 1,
      clippedPx: el.scrollWidth - el.clientWidth,
    };
  }

  for (const k of Object.keys(orig)) {
    const el = pick(args.SELECTORS[k]);
    if (el) el.textContent = orig[k];
  }
  return {
    base, after, style,
    sidebarWidth: Math.round(sb.getBoundingClientRect().width),
    sidebarOverflowX: sb.scrollWidth - sb.clientWidth,
    treeOverflowX: (() => { const t = pick("#wsTree"); return t ? t.scrollWidth - t.clientWidth : -1; })(),
  };
}"""

# 文件树里的名字不靠省略号, 靠文件树自己的横条(超出时); 其余元素照旧必须截断
TREE_NAMES = ("wsFolderName", "wsFileName")


def make_fixture() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    TMP_FILE.write_text("nowrap check\n", encoding="utf-8")


def clean_fixture() -> None:
    shutil.rmtree(TMP_DIR, ignore_errors=True)


async def main() -> int:
    make_fixture()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.add_init_script(WS_STUB)
            await page.goto(URL, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(400)
            await page.click("#modeWorld")          # 工作区只在 World 模式显示
            await page.wait_for_timeout(300)
            await page.click("#wsRefresh")          # 渲染出刚建的超长目录/文件
            await page.wait_for_timeout(1200)
            await page.click("#wsExpand")           # 文件夹默认折叠, 展开后再量文件名
            await page.wait_for_timeout(400)
            res = await page.evaluate(CHECK, {"LONG": "超长内容LongText" * 8, "SELECTORS": SELECTORS})
            await browser.close()
    finally:
        clean_fixture()

    bad = []
    rows = []
    for key, st in res["style"].items():
        bh, ah = res["base"]["items"].get(key), res["after"]["items"].get(key)
        grew = abs((ah or 0) - (bh or 0)) > 1
        rows.append((key, bh, ah, st["clippedPx"], "Y" if st["truncated"] else "N"))
        if not st["nowrap"]:
            bad.append(f"{key} 未应用单行样式")
        if grew:
            bad.append(f"{key} 超长文本把元素撑高了 {bh} -> {ah}(发生了换行)")
        if key in TREE_NAMES:
            continue                      # 文件树: 不截断, 由文件树横条兜住(下面单独校验)
        if not (st["ellipsis"] and st["clipped"]):
            bad.append(f"{key} 未应用 省略号/裁剪 样式")
        if not st["truncated"]:
            bad.append(f"{key} 超长文本未被省略号截断")
    if res.get("treeOverflowX", 0) <= 1:
        bad.append("文件树的超长名字没有被横向滚动条兜住(treeOverflowX="
                   + str(res.get("treeOverflowX")) + ")")
    for key in ("wsFolderName", "wsFileName"):
        if key not in res["style"]:
            bad.append(f"{key} 未渲染, 无法校验")
    if res["after"]["sidebarScrollH"] - res["base"]["sidebarScrollH"] > 1:
        bad.append(f"侧栏被撑高 {res['base']['sidebarScrollH']} -> {res['after']['sidebarScrollH']}")
    if res["sidebarOverflowX"] > 1:
        bad.append(f"侧栏出现横向溢出 {res['sidebarOverflowX']}px")
    if res["sidebarWidth"] != 240:
        bad.append(f"侧栏宽度异常 {res['sidebarWidth']}px")

    print("元素".ljust(14), "原高", "超长后", "截断px")
    for key, bh, ah, cut, tr in rows:
        print(key.ljust(14), str(bh).ljust(6), str(ah).ljust(6), f"{cut} ({tr})")
    print("侧栏: width", res["sidebarWidth"], "overflowX", res["sidebarOverflowX"],
          "scrollHeight", res["base"]["sidebarScrollH"], "->", res["after"]["sidebarScrollH"])
    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print(f"SIDEBAR_NOWRAP_OK ({len(rows)} 个元素: 单行 + 省略号 + 盒高不变)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
