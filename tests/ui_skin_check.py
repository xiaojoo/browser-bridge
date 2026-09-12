"""皮肤结构化校验: 断言浅色主题、布局元素与 markdown 渲染产物。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

CHECK = """() => {
  const cs = (el) => el ? getComputedStyle(el) : null;
  const bodyBg = cs(document.body).backgroundColor;
  const sidebar = document.querySelector('.sidebar');
  const sw = sidebar ? sidebar.getBoundingClientRect().width : 0;
  const markdown = renderMarkdown('# 标题\\n\\n```python\\ndef f():\\n    return 1\\n```\\n\\n> 引用');
  const conv = document.getElementById('conv');
  conv.innerHTML = markdown;
  const pre = conv.querySelector('pre.code');
  const h1 = conv.querySelector('h1');
  const bq = conv.querySelector('blockquote');
  return {
    bodyBg,
    sidebarWidth: sw,
    hasComposer: !!document.querySelector('.composer textarea.input'),   // 原生 textarea
    hasSend: !!document.getElementById('btnSend'),
    hasSelect: !!document.getElementById('selProvider'),
    preBg: pre ? cs(pre).backgroundColor : null,
    h1Text: h1 ? h1.textContent : null,
    hasBlockquote: !!bq,
    inputPlaceholder: (document.querySelector('.input')||{}).placeholder || null,
    bodyFont: cs(document.body).fontSize,
    inputFont: cs(document.querySelector('.input')).fontSize
  };
}"""


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(WS_STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)
        r = await page.evaluate(CHECK)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        await browser.close()
    assert r["bodyBg"] in ("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)"), "非浅色主题: " + str(r)
    assert r["sidebarWidth"] == 240, "侧栏宽度异常"
    assert r["hasComposer"] and r["hasSend"] and r["hasSelect"]
    assert r["h1Text"] == "标题", "markdown h1 渲染失败"
    assert r["hasBlockquote"] and r["preBg"], "代码块/引用渲染失败"
    assert r["bodyFont"] == "14px", f"界面主字体应为 14px: {r['bodyFont']}"
    assert r["inputFont"] == "14px", f"输入框字号应为 14px: {r['inputFont']}"
    print("SKIN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
