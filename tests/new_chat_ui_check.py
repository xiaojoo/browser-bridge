"""前端"新对话"按钮行为校验(拦截 post(), 不触碰真实桥接浏览器)。

覆盖:
- 点击后请求 /api/new_chat, 成功则清空本地消息并回到空态欢迎区, 提示"已在 X 打开新对话"
- 失败则提示手动处理, 且本地消息保留
"""
import asyncio
import json
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

SEED = """() => {
  showWelcome(false);
  const row = document.createElement("div");
  row.className = "user-row";
  row.innerHTML = '<div class="user-bubble">旧消息</div>';
  convEl.appendChild(row);
  const a = document.createElement("div");
  a.className = "message";
  a.innerHTML = '<div class="assistant-body">旧回复</div>';
  convEl.appendChild(a);
  return convEl.children.length;
}"""

STUB = """(payload) => {
  window.__posted = [];
  post = async (url) => { window.__posted.push(url); return payload; };
  document.getElementById("btnNew2").disabled = false;
  return true;
}"""

STATE = """() => {
  const t = Array.from(document.querySelectorAll("#toast .toast")).map(e => e.className + "|" + e.textContent);
  return {
    convChildren: convEl.children.length,
    welcome: getComputedStyle(document.getElementById("welcome")).display,
    composerWrap: getComputedStyle(document.querySelector(".composer-wrap")).display,
    composerInSlot: document.getElementById("composerSlot").contains(document.querySelector(".composer")),
    posted: window.__posted || [],
    toasts: t,
  };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(WS_STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(500)

        # ---- 成功路径 ----
        print("预置消息条数:", await page.evaluate(SEED))
        await page.evaluate(STUB, {"ok": True, "provider": "ChatGPT Web", "conversation_id": None})
        await page.click("#btnNew2")
        await page.wait_for_timeout(500)
        st = await page.evaluate(STATE)
        print("成功路径:", json.dumps(st, ensure_ascii=False))
        if st["posted"] != ["/api/new_chat"]:
            bad.append("未请求 /api/new_chat: " + json.dumps(st["posted"]))
        if st["convChildren"] != 0:
            bad.append("本地消息未清空: " + str(st["convChildren"]))
        if st["welcome"] != "flex" or not st["composerInSlot"]:
            bad.append("未回到空态欢迎区")
        if not any(("新对话" in t or "新建会话" in t) and "err" not in t for t in st["toasts"]):
            bad.append("没有成功提示: " + json.dumps(st["toasts"], ensure_ascii=False))

        # ---- 失败路径 ----
        await page.evaluate("() => { document.querySelectorAll('#toast .toast').forEach(e => e.remove()); }")
        await page.evaluate(SEED)
        await page.evaluate(STUB, {"ok": False})
        await page.click("#btnNew2")
        await page.wait_for_timeout(500)
        st2 = await page.evaluate(STATE)
        print("失败路径:", json.dumps(st2, ensure_ascii=False))
        if st2["convChildren"] == 0:
            bad.append("失败时不该清空本地消息")
        if not any("warn" in t for t in st2["toasts"]):
            bad.append("失败没有警示提示: " + json.dumps(st2["toasts"], ensure_ascii=False))

        # ---- 浏览器未就绪时发送: 给出明确提示而不是静默失败 ----
        await page.evaluate("""() => {
          document.querySelectorAll('#toast .toast').forEach(e => e.remove());
          window.__posted = [];
          post = async (url) => { window.__posted.push(url); return { ok: true }; };
          state.state = "idle";
          document.getElementById("input").value = "测试一下";
        }""")
        await page.evaluate("() => send()")
        await page.wait_for_timeout(400)
        st3 = await page.evaluate("""() => ({
          posted: window.__posted,
          toasts: Array.from(document.querySelectorAll("#toast .toast")).map(e => e.className + "|" + e.textContent),
        })""")
        print("未就绪发送:", json.dumps(st3, ensure_ascii=False))
        if any(u.endswith("/api/chat") for u in st3["posted"]):
            bad.append("浏览器未就绪时不该请求 /api/chat")
        if not any("warn" in t and "启动并登录" in t for t in st3["toasts"]):
            bad.append("未就绪发送没有明确提示: " + json.dumps(st3["toasts"], ensure_ascii=False))

        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("NEW_CHAT_UI_OK (成功清空并回到空态, 失败保留消息并提示)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
