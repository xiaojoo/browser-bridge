"""刷新后卡片状态要和最近一轮一致(不能停在"正在…"); 事件不会新开重复卡片。"""
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
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true,
        apply: { action: "applied", text: "改好了",
                 applied: [{ op: "update", path: "qml/models/ClipboardModel.qml", size: 420 }], skipped: [] },
        verify: { action: "finished", ok: true, text: "本地模型确认项目跑通了" } });
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
        await page.wait_for_timeout(900)
        await page.evaluate("() => { showWelcome(false); }")
        # 造"刷新前留下的旧卡片"(只有初始文案), 然后让 syncWorldState 补状态。
        # 注意: 刷新后的页面上两张卡片都在(运行时都写进记录了), 状态同步只负责补正它们。
        await page.evaluate(r"""() => {
          transcript = [
            { role: "world", card: { title: "本地落盘(本地模型写入工作区)", wid: "w-old",
                                     count: "", foot: "正在把 ChatGPT 的回答落实成文件改动…",
                                     busy: false, rows: [] } },
            { role: "world", card: { title: "本地验证(本地模型自己构建/测试)", wid: "v-old",
                                     count: "", foot: "本地模型正在跑: cmake --build build",
                                     busy: false, rows: [] } },
          ];
          convEl.innerHTML = "";
          transcript.forEach(renderStoredMessage);
          return syncWorldState();
        }""")
        await page.wait_for_timeout(600)
        st = await page.evaluate(r"""() => ({
          cards: Array.from(document.querySelectorAll("#conv .task-card")).map(c => ({
            wid: c.dataset.wid || "",
            title: c.querySelector("b").textContent,
            foot: c.querySelector(".task-foot").textContent,
            rows: Array.from(c.querySelectorAll(".task-item .tt")).map(e => e.textContent),
          })),
        })""")
        print("同步后:", json.dumps(st, ensure_ascii=False)[:500])
        cards = st["cards"]
        feet = " | ".join(c["foot"] for c in cards)
        if "正在把 ChatGPT 的回答落实成文件改动" in feet:
            bad.append("同步后仍然停在过期文案: " + feet)
        if "应用 1 项" not in feet:
            bad.append("没有补上落盘结果: " + feet)
        if len([c for c in cards if c["title"].startswith("本地落盘")]) != 1:
            bad.append("落盘卡片重复了: " + json.dumps([c["title"] for c in cards], ensure_ascii=False))
        if len([c for c in cards if c["title"].startswith("本地验证")]) != 1:
            bad.append("验证卡片重复了: " + json.dumps([c["title"] for c in cards], ensure_ascii=False))
        if not any("ClipboardModel.qml" in r for c in cards for r in c["rows"]):
            bad.append("没有补上写入的文件: " + json.dumps(cards, ensure_ascii=False)[:300])
        if "本地模型确认项目跑通了" not in feet:
            bad.append("没有补上验证结果: " + feet)

        # 反复同步(等价于反复刷新页面): 同一批结果不能越积越多(以前每刷一次多一条重复记录)
        land_rows = len([c for c in cards if c["title"].startswith("本地落盘")][0]["rows"])
        for _ in range(3):
            await page.evaluate("() => syncWorldState()")
            await page.wait_for_timeout(350)
        again = await page.evaluate(r"""() => Array.from(document.querySelectorAll("#conv .task-card"))
          .filter(c => c.querySelector("b").textContent.indexOf("本地落盘") === 0)
          .map(c => Array.from(c.querySelectorAll(".task-item .tt")).map(e => e.textContent))""")
        print("反复同步后:", json.dumps(again, ensure_ascii=False)[:300])
        if len(again) != 1 or len(again[0]) != land_rows:
            bad.append("反复同步把行数涨上去了: 原来 " + str(land_rows) + " 行, 现在 "
                       + json.dumps(again, ensure_ascii=False))
        hist_rows = await page.evaluate(r"""() => {
          const e = transcript.filter(m => m && m.role === "world"
            && String((m.card || {}).title || "").indexOf("本地落盘") === 0).pop();
          return e ? (e.card.rows || []).map(r => r.text) : null;
        }""")
        if not hist_rows or len(hist_rows) != land_rows:
            bad.append("历史记录里的行也跟着涨了(下次刷新还会冒出来): " + json.dumps(hist_rows, ensure_ascii=False))

        # 已经被积出来的重复行: 重画 + 存历史时都要清掉(否则下次刷新还会冒出来)
        healed = await page.evaluate(r"""() => {
          transcript = [{ role: "world", card: { title: "本地落盘(本地模型写入工作区)", wid: "w-dup",
            count: "1 个文件", foot: "应用 1 项", busy: false,
            rows: [{ mark: "✓", text: "update EditorArea.qml (4154B)", cls: "done" },
                   { mark: "✓", text: "update EditorArea.qml (4154B)", cls: "done" },
                   { mark: "✓", text: "update EditorArea.qml (4154B)", cls: "done" }] } }];
          convEl.innerHTML = "";
          transcript.forEach(renderStoredMessage);
          histFlush();
          const all = JSON.parse(localStorage.getItem(HIST_KEY) || "{}");
          let stored = null;
          for (const k of Object.keys(all)) {
            for (const m of ((all[k] || {}).messages || [])) {
              if (m.role === "world" && String((m.card || {}).title || "").indexOf("本地落盘") === 0)
                stored = (m.card.rows || []).length;
            }
          }
          return { dom: Array.from(document.querySelectorAll("#conv .task-card .task-item")).length,
                   storeRows: cardForStore(transcript[0].card, true).rows.length,   // 写历史用的那份
                   stored: stored, keys: Object.keys(localStorage).length,
                   prov: providerKey() };
        }""")
        print("去重后:", json.dumps(healed, ensure_ascii=False))
        if healed.get("dom") != 1 or healed.get("storeRows") != 1:
            bad.append("重复行没有被清掉(画出来 / 写历史那份): " + json.dumps(healed, ensure_ascii=False))
        if healed.get("stored") not in (None, 1):
            bad.append("localStorage 里存的还是重复行: " + json.dumps(healed, ensure_ascii=False))
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("STATE_SYNC_OK (刷新后卡片状态按最近一轮补正, 不重复、不停在过期文案)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
