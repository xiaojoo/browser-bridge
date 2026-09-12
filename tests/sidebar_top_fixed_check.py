"""侧栏顶部(品牌 + 开启新对话)必须固定高度, 不被下面的列表挤扁。

构造极端情况: 视口很矮 + 会话列表很长 + World 模式文件树很高,
断言 品牌行 58px / 开启新对话 32px / 会话标题 26px 全都没变, 由会话列表承担收缩。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"

STUB = """(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0) {
      const items = [];
      for (let i = 0; i < 60; i++)
        items.push({ id: "c" + i, key: "/c/" + i, title: "很长的会话标题 " + i + " 用来把列表顶满", url: "https://x/c/" + i, active: false });
      return json({ ok: true, provider: "chatgpt", current: "convA", items: items });
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true, apply: null, verify: null });
    }
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

MEASURE = """() => {
  const h = (sel) => {
    const el = document.querySelector(sel);
    return el ? Math.round(el.getBoundingClientRect().height) : null;
  };
  const sb = document.querySelector(".sidebar");
  return {
    viewportH: window.innerHeight,
    sidebarH: Math.round(sb.getBoundingClientRect().height),
    brand: h(".brand"), btnNew: h("#btnNew"), section: h(".sec-head"),
    history: h("#convList"), profile: h(".profile"), btnStart: h("#btnStart"),
    wsTree: h("#wsTree"),
    historyShrunk: (() => {
      const el = document.getElementById("convList");
      return el.scrollHeight > el.clientHeight + 2;
    })(),
    clipped: sb.scrollHeight > sb.clientHeight + 2,
  };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1180, "height": 560})   # 故意矮
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(900)
        await page.evaluate("() => switchMode('world')")     # 再加上文件树一起挤
        await page.wait_for_timeout(600)
        r = await page.evaluate(MEASURE)
        print("矮窗口+长列表+World:", json.dumps(r, ensure_ascii=False))
        if r["brand"] != 58:
            bad.append(f"品牌行高度被改了: {r['brand']} (应 58)")
        if r["btnNew"] != 32:
            bad.append(f"「开启新对话」高度被改了: {r['btnNew']} (应 32)")
        if r["section"] != 26:
            bad.append(f"「会话」标题行高度被改了: {r['section']} (应 26)")
        if r["profile"] != 52:
            bad.append(f"左下用户栏高度被改了: {r['profile']} (应 52)")
        if r["btnStart"] != 32:
            bad.append(f"启动按钮高度被改了: {r['btnStart']} (应 32)")
        if r["clipped"]:
            bad.append("整条侧栏出现滚动(顶部被顶出去了): scrollHeight > clientHeight")
        if not r["historyShrunk"]:
            bad.append("长列表下会话列表没有自己出现内部滚动")

        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("SIDEBAR_TOP_FIXED_OK (顶部区域固定高度, 收缩只发生在会话列表里)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
