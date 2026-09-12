"""换窗口后: 旧会话 id 与新会话 id 指向同一份本地记录(界面里是一次对话);
连按 Enter / 连点发送 只发一条。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"

STUB = r"""(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.__chats = [];
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "oldConv", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "oldConv", items: [
        { id: "oldConv", key: "/c/oldConv", title: "老窗口", url: "", active: false },
        { id: "newConv", key: "/c/newConv", title: "新窗口", url: "", active: true }] });
    if (s.indexOf("/api/chat") === 0) {
      window.__chats.push(JSON.parse((o && o.body) || "{}"));
      return json({ ok: true, state: "queued" });
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true, apply: null, verify: null });
    }
    if (s.indexOf("/workspace/tree") === 0)
      return json({ ok: true, root: "H:\\tmp", name: "tmp", default: "", custom: false, items: [] });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 820})
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)

        await page.evaluate("() => switchMode('world')")      # 先切到 World(工作区面板可见)
        await page.wait_for_timeout(400)
        # 1) 老会话里有记录; 换窗口后新的 id 也要能看到这份记录
        await page.evaluate(r"""() => {
          state.conversation_id = "oldConv";
          histProvider = providerKey(); histConv = "oldConv"; histSid = "s0";
          transcript = [{ role: "user", text: "第一条: 老窗口发的" },
                        { role: "assistant", text: "老窗口收到的回答" }];
          histFlush();
          // 站点被限流 -> 另开窗口 -> 站点给了新会话 id
          adoptConversation("newConv");
          transcript = [{ role: "user", text: "第一条: 老窗口发的" },
                        { role: "assistant", text: "老窗口收到的回答" },
                        { role: "user", text: "第二条: 新窗口发的" },
                        { role: "assistant", text: "新窗口收到的回答" }];
          histFlush();
        }""")
        await page.wait_for_timeout(300)
        merged = await page.evaluate(r"""() => {
          const out = {};
          for (const cid of ["oldConv", "newConv"]) {
            const sid = "s0";
            out[cid] = histMessages(providerKey(), cid, sid).map(m => m.text);
          }
          out.alias = JSON.parse(localStorage.getItem("wlb.alias.v1") || "{}");
          return out;
        }""")
        # 模式/工作区不该因为"换窗口"而变化
        ui = await page.evaluate(r"""() => ({
          mode: currentMode,
          hasWs: document.documentElement.classList.contains("has-workspace"),
          root: document.getElementById("wsTitle").textContent,
          aliasApplied: wsAppliedConv === wsRootMapKey(),
        })""")
        print("界面状态:", json.dumps(ui, ensure_ascii=False))
        if ui["mode"] != "world" or not ui["hasWs"]:
            bad.append("换窗口后模式/工作区面板被改了: " + json.dumps(ui, ensure_ascii=False))
        if not ui["aliasApplied"]:
            bad.append("换窗口后没有标记成已应用工作区, 可能被切回默认")

        print("两个 id 的记录:", json.dumps(merged, ensure_ascii=False)[:400])
        if len(merged.get("oldConv") or []) != 4 or len(merged.get("newConv") or []) != 4:
            bad.append("换窗口后旧 id / 新 id 没有指向同一份记录: " + json.dumps(merged, ensure_ascii=False)[:300])
        alias = (merged.get("alias") or {}).get("chatgpt") or {}
        if alias.get("oldConv") != "newConv":
            bad.append("没有记下别名(旧 id -> 新 id): " + json.dumps(merged.get("alias"), ensure_ascii=False))

        # 2) 连按 Enter / 连点发送 只发一条
        await page.evaluate(r"""() => { window.__chats = []; document.getElementById("input").value = "只该发一次"; }""")
        await page.evaluate("() => { send(); send(); send(); }")
        await page.wait_for_timeout(900)                 # 等冷却过去
        n1 = await page.evaluate("() => window.__chats.length")
        await page.click("#input")
        await page.keyboard.type("再来一条")
        await page.keyboard.press("Enter")
        await page.keyboard.press("Enter")               # 手快再按一次
        await page.wait_for_timeout(400)
        n2 = await page.evaluate("() => window.__chats.length")
        texts = await page.evaluate("() => window.__chats.map(c => (c.text || '').slice(-4))")
        print("并发 send() 三次 ->", n1, "次请求; 冷却后再连按两次 Enter -> 共", n2, "次", texts)
        if n1 != 1:
            bad.append(f"连续调用 send() 应该只发一条, 实际 {n1}")
        if n2 != 2:
            bad.append(f"冷却后再发一条应该只多一条, 实际总计 {n2}")
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("MERGE_SEND_OK (换窗口后两边记录合并/旧 id 也能看到; 连点连按只发一条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
