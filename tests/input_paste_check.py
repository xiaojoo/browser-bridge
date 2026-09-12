"""输入框: 必须是原生 textarea, Ctrl+V 能直接粘贴, 长文本换行且只纵向滚动。

(用户反馈: 新建对话后 Ctrl+V 贴不进去; 输入框改用原生 input/textarea。)
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"
PASTE = ("当前项目中, EditorArea.qml 这内容区域滚动条隐藏了, 让它显示出来。和左边一样。\n"
         "项目远程地址 https://github.com/xiaojoo/SmartClip.git\n" + "长文本 " * 40)

STUB = """(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/api/new_chat") === 0) return json({ ok: true, conversation_id: "convNEW" });
    if (s.indexOf("/api/chat") === 0) { window.__sent = (o && o.body) || ""; return json({ ok: true }); }
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

READ = """() => {
  const el = document.getElementById("input");
  return { tag: el.tagName, len: el.value.length, head: el.value.slice(0, 22),
           h: Math.round(el.getBoundingClientRect().height),
           scrollH: el.scrollHeight, clientH: el.clientHeight,
           scrollW: el.scrollWidth, clientW: el.clientWidth,
           focused: document.activeElement === el };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1200, "height": 820})
        await ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="http://127.0.0.1:8765")
        page = await ctx.new_page()
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)

        # 新建对话之后粘贴
        await page.click("#btnNew2")
        await page.wait_for_timeout(600)
        await page.evaluate("(t) => navigator.clipboard.writeText(t)", PASTE)
        await page.click("#input")
        await page.keyboard.press("Control+V")
        await page.wait_for_timeout(500)
        st = await page.evaluate(READ)
        print("粘贴后:", json.dumps(st, ensure_ascii=False))
        if st["tag"] != "TEXTAREA":
            bad.append("输入框不是原生 textarea: " + st["tag"])
        if st["len"] < len(PASTE) - 5:
            bad.append(f"Ctrl+V 没有贴进输入框(期望 {len(PASTE)} 字, 实际 {st['len']})")
        if st["head"] != PASTE[:22]:
            bad.append("粘贴内容不对: " + st["head"])
        if st["h"] > 181:
            bad.append("输入框高度没限制住: " + str(st["h"]))
        if st["scrollW"] > st["clientW"] + 1:
            bad.append("输入框出现横向滚动: " + json.dumps(st, ensure_ascii=False))
        if st["scrollH"] > st["clientH"] + 1 and st["h"] < 179:
            bad.append("长文本没有撑高输入框(也没到上限): " + json.dumps(st, ensure_ascii=False))

        # Enter 发送: 内容清空 + 真的发出去
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(600)
        after = await page.evaluate("""() => ({
          value: document.getElementById("input").value,
          h: Math.round(document.getElementById("input").getBoundingClientRect().height),
          sent: (window.__sent || "").length,
          bubbles: document.querySelectorAll("#conv .user-bubble").length,
        })""")
        print("发送后:", json.dumps(after, ensure_ascii=False))
        if after["value"] != "":
            bad.append("发送后输入框没有清空: " + str(after["value"])[:40])
        if after["h"] > 70:
            bad.append("发送后输入框没有缩回去: " + str(after["h"]))
        if after["bubbles"] < 1:
            bad.append("消息列表里没有出现自己的消息气泡")
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("INPUT_PASTE_OK (原生 textarea: Ctrl+V 可粘贴, 长文本换行并只纵向滚动, Enter 发送后清空)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
