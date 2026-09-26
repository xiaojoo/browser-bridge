"""World 模式**自动落盘**这条路径的量具。

要盯的两件事(都是真出过的毛病):
  A) 一次整理画出两张卡 —— 一张完全空白、一张写着"没有可写入的文件"。
     根因: 服务端连 dry_run 都广播 apply 事件, 而 handleApplyEvent 没有 skipped 分支,
     于是它凭空建一张卡什么都不写, 接口返回值那边又建第二张。
  B) 自动模式下必须"回答一回来就把代码写进工作区", 并且**明确写出写了哪几个文件**;
     没得可写时也要说清为什么, 不能静默。

后端部分在进程内跑(替身工作区, 不联网、不动你真实的工作区); 前端部分用 stub 拦请求。
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import planner, server, workspace  # noqa: E402

URL = "http://127.0.0.1:8765/"
TMP_WS = ROOT / ".tmp" / "world-auto-ws"
ANSWER = "把 EditorArea.qml 改成这样:\n```qml\n// v2 加了滚动条\n```"
NO_CODE = "这个问题一般是驱动没装好, 你先跑一下 nvidia-smi 看看。"

STUB = """(() => {
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
      return json({ ok: true, apply: null, verify: null });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""


async def read_cards(page):
    return await page.evaluate(r"""() =>
      Array.from(document.querySelectorAll("#conv .task-card")).map(c => ({
        title: (c.querySelector("b") || {}).textContent || "",
        foot: Array.from(c.querySelectorAll(".task-foot")).map(e => e.textContent.trim()).join("|"),
        // 行的真实 class 是 .task-item; folded 时 CSS 会把 .task-list 藏掉, 所以两个都要看
        rows: c.querySelectorAll(".task-item").length,
        shown: c.querySelectorAll(".task-list .task-item").length,
        folded: c.classList.contains("folded"),
        btns: Array.from(c.querySelectorAll(".task-btns button")).map(b => b.textContent),
      }))""")


def land_only(cs):
    return [c for c in cs if c["title"].startswith("本地落盘")]


async def main() -> int:
    bad = []

    # ================= 后端: dry_run 不该广播 =================
    shutil.rmtree(TMP_WS, ignore_errors=True)
    TMP_WS.mkdir(parents=True, exist_ok=True)
    (TMP_WS / "EditorArea.qml").write_text("// v1\n", encoding="utf-8")
    events: list = []
    orig_bc, orig_ask, orig_root = server.broadcast, planner.ask, workspace.ROOT

    async def cap(payload):
        events.append(payload)

    async def must_not_ask(cfg, prompt, system=None):
        bad.append("落盘这一步调了本地模型(它不该参与)")
        return "{}"

    server.broadcast, planner.ask = cap, must_not_ask
    try:
        workspace.use_root(TMP_WS)

        events.clear()
        r = await server.api_world_apply(server.WorldApplyRequest(
            task="加滚动条", text=ANSWER, dry_run=True))
        print("1) dry_run 有改动 -> 广播 %d 条(应为 0), 返回 %d 个文件"
              % (len(events), len((r or {}).get("files") or [])))
        if events:
            bad.append("dry_run 还在广播 apply 事件: 会和接口返回值各画一张卡")
        if not (r or {}).get("dry_run"):
            bad.append("dry_run 没带 dry_run 标记")

        events.clear()
        r = await server.api_world_apply(server.WorldApplyRequest(task="加滚动条", text=ANSWER))
        acts = [e.get("action") for e in events]
        wrote = "v2" in (TMP_WS / "EditorArea.qml").read_text(encoding="utf-8")
        print("2) 自动落盘 -> 写入=%s 广播 %s applied=%d"
              % (wrote, acts, len((r or {}).get("applied") or [])))
        if not wrote:
            bad.append("自动模式没有把代码写进工作区文件")
        if acts != ["applied"]:
            bad.append("自动落盘应该正好广播一条 applied 事件, 实际 %s" % acts)

        events.clear()
        r = await server.api_world_apply(server.WorldApplyRequest(task="x", text=NO_CODE,
                                                                  dry_run=True))
        print("3) dry_run 无改动 -> 广播 %d 条(应为 0) no_changes=%s"
              % (len(events), (r or {}).get("no_changes")))
        if events or not (r or {}).get("no_changes"):
            bad.append("dry_run 无改动时行为不对: events=%s r=%s" % (acts, r))

        events.clear()
        r = await server.api_world_apply(server.WorldApplyRequest(task="x", text=NO_CODE))
        acts = [e.get("action") for e in events]
        print("4) 自动无改动 -> 广播 %s text=%r" % (acts, ((r or {}).get("text") or "")[:46]))
        if acts != ["skipped"]:
            bad.append("自动模式没得可写时必须广播一条 skipped(否则界面上什么都没有): %s" % acts)
    finally:
        server.broadcast, planner.ask = orig_bc, orig_ask
        workspace.use_root(orig_root)

    # ================= 前端: 自动模式只该有一张卡 =================
    async def run_page(apply_reply, confirm="0"):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.add_init_script(STUB)
            await page.goto(URL, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(600)
            await page.evaluate(r"""(a) => {
              window.__calls = [];
              showWelcome(false); switchMode("world");
              post = async (url, body) => {
                window.__calls.push([url, body]);
                if (url === "/api/chat") return { ok: true };
                if (url === "/api/world/apply") return a.reply;
                if (url === "/api/world/verify") return { ok: true, gave_up: false, real_error: false,
                  reason: "", rounds: [], changed: [], applied: [], skipped: [] };
                return {};
              };
              const of = window.fetch;
              window.fetch = (u, o) => String(u).indexOf("/api/settings") === 0
                ? Promise.resolve(new Response(JSON.stringify({ planner: {}, engine: {
                    confirm_apply: a.confirm, test_cmd: "" } }),
                    { status: 200, headers: { "Content-Type": "application/json" } }))
                : of(u, o);
            }""", {"reply": apply_reply, "confirm": confirm})
            await page.evaluate("""() => {
              document.getElementById("input").value = "给 EditorArea.qml 加个滚动条";
            }""")
            await page.evaluate("() => send()")
            await page.wait_for_timeout(300)
            await page.evaluate("""() => {
              handle({ type: "message_start" });
              handle({ type: "delta", kind: "text", text: "把 EditorArea.qml 改成这样" });
              handle({ type: "message_end" });
            }""")
            await page.wait_for_timeout(1200)
            cs = await read_cards(page)
            calls = await page.evaluate("() => window.__calls")
            ind = await page.evaluate("""() => {const e = document.getElementById('applyMode');
              const c = getComputedStyle(e);
              return {hidden: e.hidden, text: e.textContent.trim(), cls: e.className,
                      color: c.color, size: c.fontSize};}""")
            await browser.close()
            return cs, calls, ind

    # --- 场景 1: 自动模式有改动 ---
    cs, calls, ind = await run_page({
        "ok": True, "applied": [{"op": "update", "path": "EditorArea.qml", "size": 18}],
        "skipped": [], "diffs": []})
    lc = land_only(cs)
    apply_call = [c for c in calls if c[0] == "/api/world/apply"]
    print("5) 自动模式有改动: 本地落盘卡 %d 张 | apply 调用 %d 次 dry_run=%s"
          % (len(lc), len(apply_call), bool((apply_call[0][1] or {}).get("dry_run"))
             if apply_call else "?"))
    print("   卡片: %s" % json.dumps([{k: v for k, v in c.items() if k != "title"} for c in lc],
                                     ensure_ascii=False)[:300])
    if not apply_call or (apply_call[0][1] or {}).get("dry_run"):
        bad.append("自动模式还在先 dry_run 一次(那就会走勾选卡片那条路)")
    if len(lc) != 1:
        bad.append("自动模式本地落盘卡片应该是 1 张, 实际 %d 张" % len(lc))
    elif not lc[0]["foot"].strip():
        bad.append("那张卡是空白的(没写结果也没写原因)")
    elif "应用 1 项" not in lc[0]["foot"]:
        bad.append("卡片没写明写了几个文件: %r" % lc[0]["foot"][:80])
    elif lc[0]["rows"] < 1:
        # 只靠 WS 事件画行的话, 事件没到就只剩一句"应用 N 项"而列不出改了哪几个文件
        bad.append("卡片脚注说应用了 1 项, 但一行文件都没列出来(清单只挂在一条通道上)")
    if any("识别到的文件改动" in c["title"] for c in cs):
        bad.append("自动模式下还弹了勾选卡片")
    print("   落盘模式指示: %s" % json.dumps(ind, ensure_ascii=False))
    if ind["hidden"] or "自动写入" not in ind["text"]:
        bad.append("外面看不出现在是自动写文件: %s" % json.dumps(ind, ensure_ascii=False))
    if ind["cls"] != "auto":
        bad.append("自动模式没给指示器加 auto 类(只换色那条约定): %r" % ind["cls"])

    # --- 场景 2: 自动模式没得可写(就是截图那一幕) ---
    cs2, _, ind2 = await run_page({"ok": True, "no_changes": True, "reason": "回答里没有代码, 也没有认出文件改动",
                             "text": "这条回答里没有可写入的文件(回答里没有代码, 也没有认出文件改动) —— "
                                     "已跳过: 没有调用本地模型, 也没有动任何文件",
                             "applied": [], "skipped": [], "files": [], "loose": 0})
    lc2 = land_only(cs2)
    print("6) 自动模式没得可写: 本地落盘卡 %d 张" % len(lc2))
    print("   %s" % json.dumps([{k: v for k, v in c.items() if k != "title"} for c in lc2],
                               ensure_ascii=False)[:300])
    if len(lc2) != 1:
        bad.append("没得可写时应该是 1 张卡, 实际 %d 张(截图里是 2 张: 一张空白一张说明)" % len(lc2))
    else:
        c = lc2[0]
        if not c["foot"].strip():
            bad.append("那张卡完全空白 —— 用户看不到任何解释")
        if "没有可写入的文件" not in c["foot"]:
            bad.append("卡片没说明为什么什么都没写: %r" % c["foot"][:80])
        if "重新整理这条回答" not in c["btns"]:
            bad.append("自动模式下没有「重新整理这条回答」的口子: %s" % c["btns"])
    print("   同一处指示(没得写的场景): %s" % json.dumps(ind2, ensure_ascii=False))
    # 第三个场景: 设置里把"先确认"勾回去 —— 外面那行必须跟着变, 而且只许变颜色
    _, _, ind3 = await run_page({"ok": True, "no_changes": True, "text": "x",
                                 "applied": [], "skipped": [], "files": [], "loose": 0},
                                confirm="1")
    print("   勾回确认模式后: %s" % json.dumps(ind3, ensure_ascii=False))
    if "先给你勾选" not in ind3["text"]:
        bad.append("设置改回确认后, 外面那行还写着自动写入: %r" % ind3["text"])
    if ind3["color"] == ind["color"]:
        bad.append("指示器两种状态下颜色一样(等于没跟着状态走)")
    if ind3["size"] != ind["size"]:
        bad.append("指示器换状态时字号变了(约定是只换色): %s -> %s" % (ind["size"], ind3["size"]))

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WORLD_AUTO_OK (dry_run 不再广播 / 自动模式一张卡写明写了哪几个文件 / "
          "没得可写时一张卡说清原因+可重来 / 不再弹勾选卡)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
