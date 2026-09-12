"""连接成功后自动刷新会话列表校验。

模拟: 页面以"未启动"状态打开 -> 点 btnStart(走 /api/start 的假返回) -> 状态变成"已连接"
断言: 这一刻会自动再拉一次 /api/conversations, 并且列表渲染出来。
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
  window.__calls = { status: 0, conv: 0, start: 0 };
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  window.__connected = false;
  const orig = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0) {
      window.__calls.status++;
      return json({ provider: window.__connected ? "chatgpt" : null,
                    state: window.__connected ? "logged_in" : "idle",
                    busy: false, error: null, started: !!window.__connected,
                    conversation_id: window.__connected ? "convA" : null,
                    conversation_url: window.__connected ? "https://chatgpt.com/c/convA" : null });
    }
    if (s.indexOf("/api/conversations") === 0) {
      window.__calls.conv++;
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [
        { id: "convA", key: "/c/convA", title: "连上后才该出现的会话", url: "https://x/c/convA", active: true }] });
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true, apply: null, verify: null });
    }
    if (s.indexOf("/api/start") === 0) { window.__calls.start++; return json({ ok: true, state: "launching" }); }
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
  window.__connect = () => {           // 模拟"启动并登录"完成: 状态变已连接
    window.__connected = true;
    document.getElementById("selProvider").value = "chatgpt";   // 目标站点跟着切过去
    state.state = "logged_in"; state.started = true; state.provider = "chatgpt";
    state.conversation_id = "convA"; state.conversation_url = "https://chatgpt.com/c/convA";
    refreshUi();
  };
})();"""

READ = """() => ({
  calls: window.__calls,
  titles: Array.from(document.querySelectorAll("#convList .item .nm")).map(e => e.textContent.trim()),
  btn: document.getElementById("btnStart").textContent.trim(),
})"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)
        st0 = await page.evaluate(READ)
        print("未连接:", json.dumps(st0, ensure_ascii=False))
        if st0["titles"]:
            bad.append("未连接时不该有站点会话: " + json.dumps(st0["titles"], ensure_ascii=False))
        before = st0["calls"]["conv"]

        await page.evaluate("() => window.__connect()")     # 相当于点完 btnStart 连上了
        await page.wait_for_timeout(900)
        st1 = await page.evaluate(READ)
        print("连接后:", json.dumps(st1, ensure_ascii=False))
        if st1["calls"]["conv"] <= before:
            bad.append(f"连接后没有自动刷新会话列表: {before} -> {st1['calls']['conv']}")
        if "连上后才该出现的会话" not in st1["titles"]:
            bad.append("连接后列表里没有出现站点会话: " + json.dumps(st1["titles"], ensure_ascii=False))
        if "已连接" not in st1["btn"]:
            bad.append("启动按钮状态没更新: " + st1["btn"])

        # 底部名称 = 选中的站点(即使已连接的是另一个)
        await page.evaluate("""() => {
          document.getElementById("selProvider").value = "deepseek";
          refreshUi();
        }""")
        await page.wait_for_timeout(200)
        st3 = await page.evaluate("""() => ({
          name: document.getElementById("chipName").textContent.trim(),
          avatar: document.getElementById("pAvatar").textContent.trim(),
          btn: document.getElementById("btnStart").textContent.trim(),
          title: document.getElementById("chatTitle").textContent.trim(),
        })""")
        print("选中=deepseek, 已连接=chatgpt:", json.dumps(st3, ensure_ascii=False))
        if st3["name"] != "DeepSeek Web" or st3["avatar"] != "DS":
            bad.append("底部名称/头像没跟着选中的站点: " + json.dumps(st3, ensure_ascii=False))
        if "切换到 DeepSeek Web" not in st3["btn"]:
            bad.append("启动按钮文案不对: " + st3["btn"])
        if "ChatGPT Web" not in st3["title"]:
            bad.append("主区标题仍应显示已连接的站点: " + st3["title"])

        # 再触发几次状态刷新: 不应该反复拉取
        n = st1["calls"]["conv"]
        await page.evaluate("() => { refreshUi(); refreshUi(); }")
        await page.wait_for_timeout(700)
        st2 = await page.evaluate(READ)
        if st2["calls"]["conv"] != n:
            bad.append(f"同一个连接状态被重复刷新了: {n} -> {st2['calls']['conv']}")

        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("CONNECT_REFRESH_OK (连上后自动刷新一次会话列表, 不会重复刷)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
