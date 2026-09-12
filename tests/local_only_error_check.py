"""「只用本地模型」失败时必须在消息列表里说清楚(不能只弹个 6 秒的 toast, 更不能什么都不显示)。

真实情况: 后端还没重启 -> POST /api/local_chat 405 Method Not Allowed,
前端只 toast 了一下就没了, 用户看到的是"我发了一条, 后面什么都没有"。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"
TEXT = "给我写个带缓存的 fib"

# __MODE__: missing = 405(接口不存在, 纯文本错误体); badcfg = 400 + JSON 错误(服务端原话)
STUB = r"""(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  const MODE = "__MODE__";
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/providers") === 0)
      return json({ default: "chatgpt", providers: [{ id: "chatgpt", name: "ChatGPT Web", short: "GPT" }] });
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/api/settings") === 0)
      return json({ planner: { type: "local", api_base: "http://127.0.0.1:1234/v1",
                               api_model: "qwen3-coder", api_temp: 0.2, has_key: false, api_key: "" },
                    engine: {}, file: "H:\\browser-bridge\\.bridge_settings.json" });
    if (s.indexOf("/api/local_chat") === 0) {
      window.__called = (window.__called || 0) + 1;
      if (MODE === "missing")                       // 旧后端: 路由不存在(FastAPI 的 StaticFiles 给 405)
        return Promise.resolve(new Response("Method Not Allowed", { status: 405 }));
      return Promise.resolve(new Response(JSON.stringify({ error: "需要先在设置里配一个「能对话的模型」" }),
        { status: 400, headers: { "Content-Type": "application/json" } }));
    }
    if (s.indexOf("/api/chat") === 0) { window.__postedChat = true; return json({ ok: true }); }
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

READ = r"""() => ({
  errRows: Array.from(document.querySelectorAll('#conv .errbox')).map(e => e.textContent),
  bubbles: Array.from(document.querySelectorAll('#conv .user-bubble')).map(e => e.textContent),
  input: document.getElementById('input').value,
  sendDisabled: document.getElementById('btnSend').disabled,
  toasts: Array.from(document.querySelectorAll('#toast > *')).map(e => e.textContent),
  localCalls: window.__called || 0,
  chatCalls: !!window.__postedChat,
})"""


async def run(page, url, mode, expect_restart_hint):
    bad = []
    await page.add_init_script(STUB.replace("__MODE__", mode))
    await page.goto(url, wait_until="networkidle", timeout=30_000)
    await page.wait_for_timeout(800)
    await page.evaluate("""(t) => {
      document.querySelector('#replyPicker') && document.getElementById('btnReply').click();
      const item = document.querySelector('#replyPicker .picker-item[data-v="local"]');
      item.click();
      document.getElementById('input').value = t;
    }""", TEXT)
    await page.wait_for_timeout(200)
    await page.click("#btnSend")
    await page.wait_for_timeout(900)
    st = await page.evaluate(READ)
    print(f"[{mode}]", json.dumps(st, ensure_ascii=False)[:420])
    if st["localCalls"] != 1:
        bad.append(f"[{mode}] 没有请求 /api/local_chat")
    if st["chatCalls"]:
        bad.append(f"[{mode}] 消息还是发去了站点")
    if not st["errRows"]:
        bad.append(f"[{mode}] 消息列表里没有任何错误提示(用户看到的是一片空白)")
    else:
        row = st["errRows"][-1]
        if expect_restart_hint:
            if "405" not in row:
                bad.append(f"[{mode}] 错误里没带上状态码: {row[:80]}")
            if "/api/local_chat" not in row or "重启" not in row:
                bad.append(f"[{mode}] 没告诉用户下一步(重启 bridge): {row[:140]}")
        elif "需要先在设置里配" not in row:
            bad.append(f"[{mode}] 没把服务端原话显示出来: {row[:120]}")
    if not st["toasts"]:
        bad.append(f"[{mode}] 连 toast 都没有")
    if st["input"] != TEXT:
        bad.append(f"[{mode}] 没发出去的消息没还给用户: {st['input']!r}")
    if st["sendDisabled"]:
        bad.append(f"[{mode}] 失败后发送按钮卡住了")
    return bad


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1240, "height": 880})
        page.on("pageerror", lambda e: bad.append("页面报错: " + str(e)[:160]))
        bad += await run(page, URL, "missing", True)
        bad += await run(page, URL, "badcfg", False)
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("LOCAL_ONLY_ERROR_OK (本地模型失败时消息列表里有明确提示: 405 指路重启, 400 显示原话; 输入不丢)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
