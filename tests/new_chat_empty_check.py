"""点了「新对话」之后, 消息区必须是空的 —— 不能被服务端残留的上一轮状态凭空画出旧卡片。

真实故障(用户 22:41 点新对话, 22:41:39 存下这一段):
  新的一段 ~new|smtvmyv2jkxo 里只有两条 world 卡片(落盘 + 验证), 一条普通消息都没有;
  服务端 /api/world/state 还留着上一次运行的 applied/finished,
  页面一加载(或刷新)就把这两张卡片重画进这个空会话里 —— 看起来就是"新对话没清干净"。
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
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: sessionStorage.getItem("__newChatted") ? null : "convA",
                    conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt",
                    current: sessionStorage.getItem("__newChatted") ? null : "convA", items: [] });
    // 服务端还留着上一轮的落盘/验证状态
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true,
        apply: { action: "applied", text: "将 EditorArea.qml 的滚动条改为 AlwaysOn",
                 applied: [{ op: "update", path: "EditorArea.qml", size: 1771 }], skipped: [] },
        verify: { action: "finished", ok: false, text: "没能完成验证(命令在这台机器上跑不了)" } });
    if (s.indexOf("/api/new_chat") === 0) {
      sessionStorage.setItem("__newChatted", "1");   // 站点新建会话后状态里就没有会话 id(和真实一致)
      return json({ ok: true, provider: "ChatGPT Web", conversation_id: null });
    }
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

SEED = r"""(() => {
  if (localStorage.getItem("wlb.seeded")) return;            // 只种一次(重载不许再覆盖)
  localStorage.setItem("wlb.seeded", "1");
  const land = { role: "world", card: {
    title: "本地落盘(本地模型写入工作区)", wid: "w-old", count: "1 个文件",
    foot: "正在把 ChatGPT 的回答落实成文件改动…", busy: false,
    rows: [{ mark: "✓", text: "update EditorArea.qml (1771B)", cls: "done" }] } };
  localStorage.setItem("wlb.history.v3", JSON.stringify({ chatgpt: {
    "convA|s1": { conv: "convA", sid: "s1", ts: 1789051310565, title: "滚动条", url: "", messages: [
      { role: "user", text: "这个代码滚动条实现了，但是还有个bug" },
      { role: "assistant", text: "对，这个现象说明滚动条本身已经实现了…" },
      land ] } } }));
  localStorage.setItem("wlb.cur.v1", JSON.stringify({ chatgpt: { conv: "convA", sid: "s1" } }));
})();"""

READ = r"""() => {
  const all = JSON.parse(localStorage.getItem("wlb.history.v3") || "{}");
  const st = all.chatgpt || {};
  const segs = {};
  for (const k of Object.keys(st)) {
    const msgs = st[k].messages || [];
    segs[k] = { total: msgs.length,
                plain: msgs.filter(m => m.role !== "world").length,
                cards: msgs.filter(m => m.role === "world").length };
  }
  return {
    cards: Array.from(document.querySelectorAll("#conv .task-card")).map(c => c.querySelector("b").textContent),
    messages: document.querySelectorAll("#conv .message").length,
    userRows: document.querySelectorAll("#conv .user-row").length,
    welcome: getComputedStyle(document.getElementById("welcome")).display,
    segs: segs,
    cursor: localStorage.getItem("wlb.cur.v1"),
  };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: bad.append("页面报错: " + str(e)[:160]))
        await page.add_init_script(STUB)
        await page.add_init_script(SEED)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1000)

        first = await page.evaluate(READ)
        print("加载后:", json.dumps({k: v for k, v in first.items() if k != "segs"}, ensure_ascii=False))
        print("  段落:", json.dumps(first["segs"], ensure_ascii=False))
        if first["userRows"] != 1 or len(first["cards"]) != 1:
            bad.append("旧记录/状态补正不对(前面的用例管): " + json.dumps(first["cards"], ensure_ascii=False))
        if "正在把 ChatGPT 的回答落实成文件改动" in await page.evaluate(
                "() => (document.querySelector('#conv .task-foot') || {}).textContent || ''"):
            bad.append("刷新后卡片没被状态同步补正")

        # ---- 点「新对话」: 消息区应该清空 ----
        await page.click("#btnNew2")
        await page.wait_for_timeout(900)
        after = await page.evaluate(READ)
        print("点新对话后:", json.dumps({k: v for k, v in after.items() if k != "segs"}, ensure_ascii=False))
        if after["cards"] or after["messages"] or after["userRows"]:
            bad.append("新对话后消息区没清空: " + json.dumps(
                {"cards": after["cards"], "messages": after["messages"]}, ensure_ascii=False))

        # ---- 再刷新一次(真实场景): 旧卡片也不许冒出来 ----
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(1200)
        again = await page.evaluate(READ)
        print("再刷新后:", json.dumps({k: v for k, v in again.items() if k != "segs"}, ensure_ascii=False))
        print("  段落:", json.dumps(again["segs"], ensure_ascii=False))
        if again["cards"] or again["messages"] or again["userRows"]:
            bad.append("刷新后上一轮的卡片又冒出来了: " + json.dumps(again["cards"], ensure_ascii=False))
        ghost = [k for k, v in again["segs"].items() if v["cards"] and not v["plain"]]
        if ghost:
            bad.append("空会话里留下了「只有卡片」的坏段落: " + json.dumps(ghost, ensure_ascii=False))
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("NEW_CHAT_EMPTY_OK (新对话后消息区是空的, 刷新也不会冒出上一轮的卡片/坏段落)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
