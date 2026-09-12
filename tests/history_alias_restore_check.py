"""刷新后本地执行卡片必须还是"完整那次"(不能退回最开始那一行)。

真实故障: 站点被限流换窗口时, 同一段记录会存成两条(旧 id 一条 = 最早的快照,
新 id 一条 = 后来真正跑完的完整卡片), 会话 id 又回到旧的那个之后,
刷新按别名读到的是"旧的那半", 于是消息列表只剩卡片标题 + 一句"正在把 ChatGPT 的回答…",
随后状态同步再按残缺的骨架重画一遍, 把完整的那份覆盖掉。

断言: 别名指向的旧记录更新较旧时, 刷新要显示新记录里那份完整卡片; 之后继续写入也不许覆盖它。
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
                    started: true, conversation_id: "NEW", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "NEW", items: [] });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

# 换窗口前(旧 id)只剩最开始那一行; 换窗口后(新 id)是真正跑完的完整卡片
SEED = r"""(() => {
  const rich = {
    role: "world", card: {
      title: "本地落盘(本地模型写入工作区)", wid: "w-rich", count: "1 个文件",
      foot: "重做 EditorArea 的文本编辑区 · 应用 1 项", busy: false,
      rows: [
        { mark: "•", text: "update qml/components/EditorArea.qml +133/-105 行 · 11528B", cls: "" },
        { mark: "✓", text: "update qml/components/EditorArea.qml (11528B)", cls: "done" },
      ] } };
  const verify = {
    role: "world", card: {
      title: "本地验证(本地模型自己构建/测试)", wid: "w-verify", count: "没搞定",
      foot: "本地模型没能自己完成验证(不是项目报错: 它的命令在这台机器上跑不了), 先停在这里",
      busy: false,
      rows: [
        { mark: "▸", text: "执行: powershell -NoProfile -Command \"Get-Content -Raw 'qml/components/EditorArea.qml'\"", cls: "run" },
        { mark: "✓", text: "powershell … → 退出码 0", cls: "done",
          detailsLabel: "查看输出", detailsText: "输出的内容" },
        { mark: "!l", text: "本地模型没给出可解析的验证决策", cls: "err" },
      ] } };
  const stale = {
    role: "world", card: {
      title: "本地落盘(本地模型写入工作区)", wid: "w-stale", count: "",
      foot: "正在把 ChatGPT 的回答落实成文件改动…", busy: false, rows: [] } };
  const msgs = [
    { role: "user", text: "这个界面更加不对了，右侧内容变形了" },
    { role: "assistant", text: "看了你贴出的 EditorArea.qml，这次右侧内容变形的原因基本可以确定…" },
  ];
  if (localStorage.getItem("wlb.history.v3")) return;      // 只种一次(后面重载不许再覆盖)
  localStorage.setItem("wlb.history.v3", JSON.stringify({ chatgpt: {
    // 旧 id: 换窗口前留下的那半(只有最开始的一行)
    "OLD|s1": { conv: "OLD", sid: "s1", ts: 1000, title: "旧的那半", url: "",
                messages: msgs.concat([stale]) },
    // 新 id: 真正跑完的那份
    "NEW|s1": { conv: "NEW", sid: "s1", ts: 2000, title: "新的那半", url: "",
                messages: msgs.concat([rich, verify]) },
  } }));
  localStorage.setItem("wlb.alias.v1", JSON.stringify({ chatgpt: { NEW: "OLD" } }));
  localStorage.setItem("wlb.cur.v1", JSON.stringify({ chatgpt: { conv: "NEW", sid: "s1" } }));
})();"""

READ = r"""() => {
  const cards = Array.from(document.querySelectorAll("#conv .task-card")).map(c => ({
    title: c.querySelector("b").textContent,
    count: c.querySelector(".tcount").textContent,
    foot: c.querySelector(".task-foot").textContent,
    rows: Array.from(c.querySelectorAll(".task-item")).map(r => ({
      mark: r.querySelector(".ti").textContent,
      text: r.querySelector(".tt").textContent,
      details: (r.querySelector("details pre") || {}).textContent || "",
    })),
  }));
  const all = JSON.parse(localStorage.getItem("wlb.history.v3") || "{}");
  const st = all.chatgpt || {};
  const stored = {};
  for (const k of Object.keys(st)) {
    stored[k] = (st[k].messages || []).filter(m => m.role === "world")
      .map(m => ({ title: m.card.title, rows: (m.card.rows || []).length, count: m.card.count }));
  }
  return { cards, stored,
           msgs: Array.from(document.querySelectorAll("#conv .message")).length };
}"""


def check(bad, tag, st):
    cards = st["cards"]
    print(f"  {tag}: {len(cards)} 张卡片", json.dumps(
        [{"title": c["title"][:8], "count": c["count"], "rows": len(c["rows"]),
          "foot": c["foot"][:24]} for c in cards], ensure_ascii=False))
    land = [c for c in cards if c["title"].startswith("本地落盘")]
    ver = [c for c in cards if c["title"].startswith("本地验证")]
    if len(land) != 1 or len(ver) != 1:
        bad.append(f"{tag}: 卡片数量不对 " + json.dumps([c["title"] for c in cards], ensure_ascii=False))
        return
    if land[0]["count"] != "1 个文件" or len(land[0]["rows"]) != 2:
        bad.append(f"{tag}: 落盘卡片退回了残缺版本 count={land[0]['count']!r} rows={len(land[0]['rows'])}")
    if "正在把 ChatGPT 的回答落实成文件改动" in land[0]["foot"]:
        bad.append(f"{tag}: 落盘卡片还停在过期文案: {land[0]['foot']!r}")
    if len(ver[0]["rows"]) != 3:
        bad.append(f"{tag}: 验证卡片的执行记录没了: {len(ver[0]['rows'])} 行")
    if not any(r["details"] for r in ver[0]["rows"]):
        bad.append(f"{tag}: 验证卡片的「查看输出」详情没了")


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1240, "height": 900})
        page.on("pageerror", lambda e: bad.append("页面报错: " + str(e)[:160]))
        await page.add_init_script(STUB)
        await page.add_init_script(SEED)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1000)
        before = await page.evaluate(READ)
        check(bad, "刷新后", before)
        print("  存储:", json.dumps(before["stored"], ensure_ascii=False)[:300])

        # 再补一条消息(会整段回写): 完整的那份记录不能被残缺的覆盖
        await page.evaluate(r"""() => { histPush({ role: "user", text: "再补一句" }); }""")
        await page.wait_for_timeout(300)
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(1000)
        after = await page.evaluate(READ)
        check(bad, "再写入+刷新", after)
        print("  存储:", json.dumps(after["stored"], ensure_ascii=False)[:300])
        # 新写的一条要落在"完整的那条记录"里(不能写去残缺的那条, 也不能把完整的覆盖掉)
        live = await page.evaluate(r"""() => {
          const msgs = histMessages(providerKey(), liveConvKey(), "s1");
          const world = msgs.filter(m => m.role === "world");
          return { n: msgs.length, texts: msgs.map(m => (m.text || "").slice(0, 6)),
                   cards: world.map(m => ({ rows: (m.card.rows || []).length, count: m.card.count })) };
        }""")
        print("  当前那段:", json.dumps(live, ensure_ascii=False)[:300])
        if live["n"] != 5 or live["texts"][-1] != "再补一句":
            bad.append("新消息没写进完整的那条记录: " + json.dumps(live, ensure_ascii=False)[:200])
        if not live["cards"] or live["cards"][0]["rows"] != 2:
            bad.append("完整记录被残缺版本覆盖了: " + json.dumps(live, ensure_ascii=False)[:200])
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("ALIAS_RESTORE_OK (别名指向旧记录时, 刷新仍显示完整卡片, 且不会被残缺版本覆盖)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
