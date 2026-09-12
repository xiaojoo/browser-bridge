"""刷新不丢上下文校验:
1. 本地历史: 刷新后消息/思考过程被恢复, 并且回到消息区(固定输入框)而不是空态欢迎页
2. 站点选择: 刷新后仍是你上次选的那个站点(不乱跳到默认站点)
3. 刷新不会调用 /api/start, 即不会重新拉一个浏览器窗口
4. 点"新对话"会清掉该站点本地历史, 刷新后是干净的空态
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

SEED_HISTORY = """() => {
  histPush({ role: "user", text: "历史问题" });
  histPush({ role: "assistant", text: "**历史回答**", reason: "思考一下" });
  return JSON.parse(localStorage.getItem("wlb.history.v3") || "{}");
}"""

PAGE_STATE = """() => {
  const c = document.getElementById("conv");
  const w = document.getElementById("welcome");
  return {
    userRows: c.querySelectorAll(".user-row").length,
    assistantBodies: c.querySelectorAll(".assistant-body").length,
    assistantText: (c.querySelector(".assistant-body") || {}).textContent || "",
    hasReasoning: !!c.querySelector(".reasoning-details"),
    welcome: getComputedStyle(w).display,
    composerWrap: getComputedStyle(document.querySelector(".composer-wrap")).display,
    composerInSlot: document.getElementById("composerSlot").contains(document.querySelector(".composer")),
    providerValue: document.getElementById("selProvider").value,
    btnStart: document.getElementById("btnStart").textContent,
    stored: JSON.parse(localStorage.getItem("wlb.history.v3") || "{}"),
    convKey: (typeof liveConvKey === "function") ? liveConvKey() : null,
    sid: (typeof histSid !== "undefined") ? histSid : null,
  };
}"""


async def main() -> int:
    bad = []
    starts = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(WS_STUB)
        page.on("request", lambda r: starts.append(r.url) if r.url.endswith("/api/start") else None)

        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(600)
        stored = await page.evaluate(SEED_HISTORY)
        print("写入历史:", json.dumps(stored, ensure_ascii=False)[:160])

        # 选 ChatGPT, 让"上次选的站点"与默认站点(deepseek)不同
        await page.click("#btnProvider")
        await page.wait_for_timeout(250)
        await page.evaluate("""() => {
          const it = Array.from(document.querySelectorAll(".picker-item"))
            .find(i => i.dataset.id !== document.getElementById("selProvider").value);
          it.click();
        }""")
        await page.wait_for_timeout(250)
        picked = await page.evaluate("() => document.getElementById('selProvider').value")
        print("切换站点选择 ->", picked)

        # ---- 刷新 ----
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(900)
        st = await page.evaluate(PAGE_STATE)
        print("刷新后:", json.dumps({k: v for k, v in st.items() if k != "stored"}, ensure_ascii=False))
        if st["userRows"] != 1 or st["assistantBodies"] != 1:
            bad.append(f"历史消息未恢复: user={st['userRows']} assistant={st['assistantBodies']}")
        if "历史回答" not in st["assistantText"]:
            bad.append("助手内容未恢复: " + st["assistantText"][:60])
        if not st["hasReasoning"]:
            bad.append("思考过程未恢复")
        if st["welcome"] != "none" or st["composerInSlot"]:
            bad.append(f"刷新后仍停在空态欢迎页 (welcome={st['welcome']}, composerInSlot={st['composerInSlot']})")
        if st["providerValue"] != picked:
            bad.append(f"站点选择被重置: {st['providerValue']} != {picked}")
        if "切换到" not in st["btnStart"] and "已连接" not in st["btnStart"]:
            bad.append("启动按钮文案异常: " + st["btnStart"])
        if starts:
            bad.append("刷新触发了 /api/start(会换窗口): " + json.dumps(starts))
        prov = await page.evaluate("() => providerKey()")        # 当前桥接窗口对应的站点
        msgs = (st["stored"].get(prov, {}) or {}).get(st["convKey"] + "|" + st["sid"], {}).get("messages", [])
        if len(msgs) != 2:
            bad.append(f"本地历史 {prov}/{st['convKey']} 丢失/条数不对: "
                       + json.dumps(st["stored"], ensure_ascii=False)[:200])

        # ---- 新对话应清掉该站点历史 ----
        await page.evaluate("""() => { post = async () => ({ ok: true, provider: "ChatGPT Web" }); }""")
        await page.evaluate("() => { document.getElementById('btnNew2').disabled = false; }")
        await page.click("#btnNew2")
        await page.wait_for_timeout(400)
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(900)
        st2 = await page.evaluate(PAGE_STATE)
        print("新对话+刷新:", json.dumps({k: v for k, v in st2.items() if k != "stored"}, ensure_ascii=False))
        if st2["userRows"] or st2["assistantBodies"]:
            bad.append("新对话后本地历史未清空")
        if st2["welcome"] != "flex":
            bad.append("新对话后应回到空态欢迎页")

        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("HISTORY_OK (刷新保留历史与站点选择, 不重启窗口; 新对话清空历史)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
