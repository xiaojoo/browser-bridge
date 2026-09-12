"""本地执行卡片必须持久化: 刷新后还在, 不被历史大小上限丢掉。"""
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
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

# 造 6 张大卡片(每张 24 行 + 长详情), 总量远超旧的 400KB 上限
SEED = r"""() => {
  showWelcome(false);
  transcript = [{ role: "user", text: "任务" }, { role: "assistant", text: "回答" }];
  convEl.innerHTML = "";
  transcript.forEach(renderStoredMessage);
  for (let i = 0; i < 6; i++) {
    const c = worldCard("本地验证(本地模型自己构建/测试) #" + i);
    for (let r = 0; r < 24; r++) {
      worldRow(c, r % 3 === 0 ? "✓" : (r % 3 === 1 ? "✗" : "▸"),
               "第 " + r + " 条执行记录 " + "x".repeat(80),
               r % 3 === 1 ? "err" : "done",
               "输出详情 " + "y".repeat(4000), "查看输出");
    }
    setCardCount(c, "通过");
    setCardFoot(c, "本地模型确认项目跑通了", false);
  }
  return { cards: document.querySelectorAll("#conv .task-card").length };
}"""

READ = r"""() => {
  const all = JSON.parse(localStorage.getItem("wlb.history.v3") || "{}");
  const prov = Object.keys(all)[0] || "";
  const entries = Object.values(all[prov] || {});
  const e = entries.length ? entries[entries.length - 1] : { messages: [] };
  const world = (e.messages || []).filter(m => m.role === "world");
  const others = (e.messages || []).filter(m => m.role !== "world").length;
  return { cards: document.querySelectorAll("#conv .task-card").length,
           stored: world.length, otherMsgs: others,
           bytes: JSON.stringify(e).length,
           titles: world.map(m => m.card.title).slice(0, 3),
           rowsKept: world.map(m => (m.card.rows || []).length).slice(0, 3) };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 820})
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)
        seeded = await page.evaluate(SEED)
        await page.wait_for_timeout(500)
        before = await page.evaluate(READ)
        print("写入前:", json.dumps(before, ensure_ascii=False)[:300])
        if before["stored"] != 6:
            bad.append(f"6 张卡片没有全部存进历史: {before['stored']}")
        if before["otherMsgs"] < 2:
            bad.append("普通消息被卡片挤掉了: " + json.dumps(before, ensure_ascii=False))

        # 刷新后
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(1200)
        after = await page.evaluate(READ)
        shown = await page.evaluate("() => Array.from(document.querySelectorAll('#conv .task-card b')).map(e => e.textContent)")
        print("刷新后:", json.dumps(after, ensure_ascii=False)[:300])
        print("画面上的卡片:", json.dumps(shown, ensure_ascii=False)[:200])
        if after["cards"] != 6:
            bad.append(f"刷新后卡片不见了: 画面 {after['cards']} 张, 历史 {after['stored']} 张")
        if not all("本地验证" in t for t in shown) or len(shown) != 6:
            bad.append("刷新后卡片内容不对: " + json.dumps(shown, ensure_ascii=False)[:200])
        if after["bytes"] > 900000:
            bad.append("单段历史超出预算: " + str(after["bytes"]))
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("CARD_PERSIST_OK (本地执行卡片持久保存, 刷新后 6 张都还在, 普通消息不被挤掉)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
