"""回归: 卡片要把"接着上一条"的文件一起列全(37 个文件不是一个 30 个)。

真实情况: ChatGPT 第 1~7 个文件在上一条回答里, 这一条从 `## 8. src/api/index.ts` 接着写到
`## 37. src/types.ts`(共 30 个)。只整理当前这条, 卡片就少了前面那批 —— 用户看到的就是
"chatgpt 有 37 个文件啊, 你只列了 30 个"。

断言:
  1) 上一条回答原文放进 `extra_texts`(不和本条回答混成一段, 服务端才分得清来源);
  2) 卡片里来自前面回答的文件行标上「上一条 ·」;
  3) 内容和工作区一样的(unchanged)行默认不勾, 其它默认勾;
  4) 脚注写清"本条 N 个 + 前面回答 M 个"。
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
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.__posted = null;
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/api/settings") === 0)
      return json({ planner: {}, engine: { confirm_apply: "1" } });
    if (s.indexOf("/api/world/apply") === 0) {
      window.__posted = JSON.parse((o && o.body) || "{}");
      const hasPrev = (window.__posted.extra_texts || []).length > 0;
      return json({ ok: true, dry_run: true, empty: false, loose: 1, files: [
        { op: "update", path: "src/api/index.ts", add: 3, del: 1, size: 100,
          content: "// 本条回答里的文件", unchanged: false, fromPrev: false },
        { op: "update", path: "src/data/mock.ts", add: 1, del: 0, size: 20,
          content: "// 上一条回答里的文件", unchanged: true, fromPrev: hasPrev }
      ] });
    }
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

PREV = "### 7. `src/data/mock.ts`\n\n```ts\nexport const products = []\n```\n"
CUR = "### 8. `src/api/index.ts`\n\n```ts\nexport const getProducts = () => []\n```\n"

RUN = """async () => {
  window.__pre = %s;
  window.__cur = %s;
  showWelcome(false);
  transcript.push({ role: "user", text: "继续" });
  transcript.push({ role: "assistant", text: window.__pre });
  transcript.push({ role: "assistant", text: window.__cur });
  worldPipeline("接着写剩下的文件", window.__cur);      // 不 await: 卡片在等用户勾选
  return true;
}""" % (json.dumps(PREV), json.dumps(CUR))

READ = """() => {
  const card = document.querySelector("#conv .task-card");
  const rows = card ? Array.from(card.querySelectorAll(".task-list > .task-item")) : [];
  return {
    posted: window.__posted || {},
    rows: rows.map(r => ({
      text: (r.querySelector(".tt") || {}).textContent || "",
      checked: !!(r.querySelector('input[type=checkbox]') || {}).checked,
    })),
    foot: card ? card.querySelector(".task-foot").textContent : "",
    loose: card ? Array.from(card.querySelectorAll(".hint2")).map(h => h.textContent) : [],
  };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)
        await page.evaluate("() => switchMode('world')")
        await page.evaluate(RUN)
        await page.wait_for_timeout(600)
        r = await page.evaluate(READ)
        await browser.close()

    text = (r["posted"] or {}).get("text") or ""
    extra = (r["posted"] or {}).get("extra_texts") or []
    print("本条回答字数:", len(text), "| extra_texts:", [len(x) for x in extra])
    print("行:", json.dumps(r["rows"], ensure_ascii=False))
    print("脚注:", r["foot"])

    if len(extra) != 1 or "src/data/mock.ts" not in extra[0]:
        bad.append("没有把上一条回答原文放进 extra_texts: " + json.dumps(extra)[:200])
    if "src/api/index.ts" not in text:
        bad.append("本条回答原文没传: " + text[:200])
    if "src/data/mock.ts" in text:
        bad.append("本条回答里不该混进上一条回答的原文(要靠 extra_texts, 服务端才分得清来源)")
    if not (r["posted"] or {}).get("dry_run"):
        bad.append("整理阶段必须 dry_run(先只列清单, 等用户勾选)")
    if len(r["rows"]) != 2:
        bad.append("卡片行数不对: " + json.dumps(r["rows"], ensure_ascii=False))
    else:
        api_row = [x for x in r["rows"] if "src/api/index.ts" in x["text"]]
        mock_row = [x for x in r["rows"] if "src/data/mock.ts" in x["text"]]
        if not api_row or api_row[0]["text"].startswith("上一条"):
            bad.append("本条回答里的文件不该标「上一条」: " + json.dumps(api_row, ensure_ascii=False))
        if not mock_row or not mock_row[0]["text"].startswith("上一条 ·"):
            bad.append("前面回答里的文件应标「上一条 ·」: " + json.dumps(mock_row, ensure_ascii=False))
        if api_row and not api_row[0]["checked"]:
            bad.append("本条回答的文件应默认勾上")
        if mock_row and mock_row[0]["checked"]:
            bad.append("内容没变化(unchanged)的文件不该默认勾上")
    if "前面回答 1 个" not in r["foot"]:
        bad.append("脚注没有写清「本条 N 个 + 前面回答 M 个」: " + r["foot"])

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WORLD_CARRY_OK (接着上一条写的文件也一起列出来: 顺序正确、标了来源、没变化的默认不勾)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
