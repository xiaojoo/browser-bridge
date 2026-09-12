"""消息不同步: bridge 只记录"从它自己发出去"的消息, 站点那边(你在窗口里直接发的、
站点自己产生的)它从来没有拉回来过。这里覆盖两件事:

1. 读回站点整段对话: 真实无头浏览器 + 合成的站点 DOM(带 data-message-author-role)
2. 前端: 点「⤓ 同步站点对话」把缺的补进本地记录(不重复), 而且**不再静默改会话 id**
   (以前 loadConversations 会悄悄把 state.conversation_id 换成站点当前的会话,
   画面还停在旧会话上 —— 之后发出的消息就记到别人名下)
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

from bridge import capture, server  # noqa: E402

URL = "http://127.0.0.1:8765/"
FAKE = """
<div data-message-author-role="user"><div>第一条问题</div></div>
<div data-message-author-role="assistant">
  <div class="thought">补充 ref 与 reactive 差异 明确依赖清理的执行时机</div>
  <div class="markdown"><p>答案里有 <code>foo()</code></p></div>
</div>
<div data-message-author-role="user"><div>第二条问题</div></div>
<div data-message-author-role="assistant"><div class="markdown"><p>第二段答案</p></div></div>
"""

SITE_MSGS = {"ok": True, "provider": "chatgpt", "count": 4, "messages": [
    {"role": "user", "text": "第一条"},
    {"role": "assistant", "text": "第一条的答案：" + "甲" * 60 + "，后面还有一大段只有站点才有的正文。"},
    {"role": "user", "text": "站点里直接问的(本地没有这条)"},
    {"role": "assistant", "text": "站点里直接答的(本地没有这条)"},
]}

STUB = """(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.__convCalls = [];
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/conversations/messages") === 0) {
      window.__convCalls.push("messages");
      return json(window.__siteMsgs || %s);
    }
    if (s.indexOf("/api/conversations/open") === 0) {
      window.__convCalls.push("open");
      return json({ ok: true, current: "convA" });
    }
    if (s.indexOf("/api/conversations") === 0) {
      window.__convCalls.push("list");
      return json({ ok: true, provider: "chatgpt", current: window.__siteConv || "convA", items: [
        { id: "convA", key: "/c/convA", title: "会话A", url: "https://x/c/convA", active: true },
        { id: "convB", key: "/c/convB", title: "会话B", url: "https://x/c/convB", active: false }] });
    }
    if (s.indexOf("/api/world/state") === 0) { window.__convCalls.push("state"); return json({ ok: true, apply: null, verify: null }); }
    if (s.indexOf("/api/settings") === 0) return json({ engine: {}, planner: {} });
    if (s.indexOf("/api/status") === 0) {
      window.__convCalls.push("status");
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: window.__siteConv || "convA",
                    conversation_url: "https://x/c/" + (window.__siteConv || "convA") });
    }
    if (s.indexOf("/workspace/tree") === 0)
      return json({ ok: true, root: "H:\\\\tmp", name: "tmp", default: "", custom: false, items: [] });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close() {}, send() {} }; };
})();""" % json.dumps(SITE_MSGS, ensure_ascii=False)

READ = """() => ({
  bubbles: Array.from(document.querySelectorAll("#conv .user-bubble")).map(e => e.textContent),
  answers: Array.from(document.querySelectorAll("#conv .assistant-body")).map(e => e.textContent),
  conv: state.conversation_id,
  toasts: Array.from(document.querySelectorAll("#toast .toast")).map(e => e.textContent),
  syncBtn: !!document.getElementById("convSync"),
})"""


async def reader_check() -> list[str]:
    bad: list[str] = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page()
        await page.set_content(f"<html><body>{FAKE}</body></html>")
        got = await page.evaluate(capture._CONV_JS, {"selector": '[data-message-author-role]'})
        print("读回合成对话:", json.dumps(got, ensure_ascii=False)[:300])
        roles = [m["role"] for m in got.get("messages", [])]
        if roles != ["user", "assistant", "user", "assistant"]:
            bad.append("角色顺序不对: " + json.dumps(roles, ensure_ascii=False))
        if "foo()" not in (got["messages"][1]["text"] if len(got.get("messages", [])) > 1 else ""):
            bad.append("代码/正文没有转成文本: " + json.dumps(got, ensure_ascii=False)[:200])
        if any("补充 ref 与 reactive 差异" in (m.get("text") or "") for m in got.get("messages", [])):
            bad.append("把助手的'思考步骤'标签也读进来了(应只读正文 .markdown): "
                       + json.dumps(got, ensure_ascii=False)[:200])
        await b.close()
    return bad


async def answer_fix_check() -> list[str]:
    """后端: 收尾校正该在什么时候用站点那条替换采集结果。

    真实事故: "确认设计，开始编码" 这一轮, 采集把**上一条回答**当成了本轮答案
    (日志: first delta 比 send 晚 4 秒, 1.7 秒后就判定收工), 本地就显示成上一条的内容。
    """
    bad: list[str] = []
    cases = [
        ("同一份且不更长 -> 不动", "同样的回答" * 20, "同样的回答" * 20, ""),
        ("站点更长(截半) -> 用站点的", "开头" + "甲" * 60, "开头" + "甲" * 60 + "后面还有一段", "后面还有一段"),
        ("开头就对不上(抓成上一条) -> 用站点的",
         "我已按你的要求推进到工程设计阶段", "已经开始编码，并完成第一版可运行的 Vue 3 商城", "已经开始编码"),
        ("站点更短但是前缀 -> 不动", "很长很长的回答" * 20, "很长很长的回答" * 5, ""),
        ("采集为空 -> 用站点的", "", "站点这条", "站点这条"),
        ("站点为空 -> 不动", "本站的", "", ""),
    ]
    for name, got, site, want in cases:
        r = server._better_answer(got, site)
        ok = (want in r) if want else (r == "")
        print(f"  [{name}] -> {r[:40]!r} {'OK' if ok else 'FAIL'}")
        if not ok:
            bad.append(f"{name}: 期望 {want!r}, 实际 {r[:80]!r}")
    return bad


async def frontend_checks() -> list[str]:
    bad: list[str] = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)

        # 本地只有"第一条"(而且回答是**被截断**的半截: 是站点那条的前缀)
        await page.evaluate("""() => {
          transcript = [{ role: "user", text: "第一条" },
                        { role: "assistant", text: "第一条的答案：" + "甲".repeat(60) }];
          histConv = liveConvKey(); histProvider = providerKey();
          histFlush();
          renderHistoryFor(providerKey(), liveConvKey(), curSid || null);
        }""")
        await page.wait_for_timeout(200)
        before = await page.evaluate(READ)
        print("同步前:", json.dumps({k: before[k] for k in ("bubbles", "answers", "syncBtn")},
                                   ensure_ascii=False))
        if not before["syncBtn"]:
            bad.append("侧栏没有「同步站点对话」按钮")
        if len(before["bubbles"]) != 1:
            bad.append("前置条件不对(本地应只有 1 条用户消息): " + json.dumps(before["bubbles"], ensure_ascii=False))

        await page.click("#convSync")
        await page.wait_for_timeout(900)
        after = await page.evaluate(READ)
        print("同步后:", json.dumps({k: after[k] for k in ("bubbles", "answers", "toasts")},
                                   ensure_ascii=False)[:400])
        if len(after["bubbles"]) != 2 or "站点里直接问的(本地没有这条)" not in after["bubbles"]:
            bad.append("没有把站点缺的消息补进来: " + json.dumps(after["bubbles"], ensure_ascii=False))
        # 本地这份被截断过(采集提前结束) -> 站点那份更全, 要用站点的补全
        if not any("只有站点才有的正文" in a for a in after["answers"]):
            bad.append("站点更全的那份没有把本地截断的回答补全: "
                       + json.dumps(after["answers"], ensure_ascii=False)[:300])
        if not any("补全" in t for t in after["toasts"]):
            bad.append("补全了截断回答却没有说明: " + json.dumps(after["toasts"], ensure_ascii=False))
        if not any("补回" in t for t in after["toasts"]):
            bad.append("补回来之后没有提示: " + json.dumps(after["toasts"], ensure_ascii=False))

        # 再点一次: 不该重复
        await page.click("#convSync")
        await page.wait_for_timeout(800)
        again = await page.evaluate(READ)
        print("再同步:", json.dumps({k: again[k] for k in ("bubbles", "toasts")}, ensure_ascii=False)[:300])
        if len(again["bubbles"]) != 2:
            bad.append("重复同步造成了重复消息: " + json.dumps(again["bubbles"], ensure_ascii=False))

        # 右上角那个 ⟳ = 刷新并同步(以前它只是个装饰用的 span, 点了没反应)
        await page.evaluate("""() => {
          transcript = [{ role: "user", text: "第一条" }];
          histConv = liveConvKey(); histProvider = providerKey();
          histFlush();
          renderHistoryFor(providerKey(), liveConvKey(), curSid || null);
          window.__convCalls.length = 0;
        }""")
        have_btn = await page.evaluate("() => !!document.getElementById('topRefresh')")
        await page.click("#topRefresh")
        await page.wait_for_timeout(1300)
        ref = await page.evaluate(READ + "")
        ref2 = await page.evaluate("""() => ({
          calls: window.__convCalls.slice(),
          toasts: Array.from(document.querySelectorAll("#toast .toast")).map(e => e.textContent) })""")
        print("右上角刷新:", json.dumps({"btn": have_btn, "calls": ref2["calls"],
                                       "bubbles": ref["bubbles"], "toasts": ref2["toasts"]},
                                      ensure_ascii=False)[:400])
        if not have_btn:
            bad.append("右上角刷新按钮没有 id=topRefresh(还是个装饰 span?)")
        if "messages" not in ref2["calls"] or "list" not in ref2["calls"]:
            bad.append("右上角刷新没有重新拉会话列表/站点消息: " + json.dumps(ref2["calls"]))
        if "站点里直接问的(本地没有这条)" not in ref["bubbles"]:
            bad.append("右上角刷新没有把消息同步回来: " + json.dumps(ref["bubbles"], ensure_ascii=False))
        if not any("已刷新并同步" in t for t in ref2["toasts"]):
            bad.append("右上角刷新没有汇总提示: " + json.dumps(ref2["toasts"], ensure_ascii=False))

        # (c) 本地抓错了(把上一条回答当成本轮答案) -> 同步时用站点的替换, 而不是排在后面留个重复
        await page.evaluate("""() => {
          window.__siteMsgs = { ok: true, provider: "chatgpt", count: 2, messages: [
            { role: "user", text: "确认设计，开始编码" },
            { role: "assistant", text: "已经开始编码，并完成第一版可运行的 Vue 3 工程化商城前台。" + "乙".repeat(300) }] };
          transcript = [{ role: "user", text: "确认设计，开始编码" },
                        { role: "assistant", text: "我已按你的“直接开始”要求推进到工程设计阶段，并完成了：" + "甲".repeat(50) }];
          histConv = liveConvKey(); histProvider = providerKey();
          histFlush();
          renderHistoryFor(providerKey(), liveConvKey(), curSid || null);
        }""")
        await page.evaluate("() => syncFromSite(false)")
        await page.wait_for_timeout(700)
        stale = await page.evaluate(READ)
        print("抓错的那条:", json.dumps({"answers": stale["answers"], "toasts": stale["toasts"]},
                                       ensure_ascii=False)[:300])
        if not any("已经开始编码" in a for a in stale["answers"]):
            bad.append("抓错的回答没有被站点那份替换: " + json.dumps(stale["answers"], ensure_ascii=False)[:200])
        if any("我已按你的" in a for a in stale["answers"]):
            bad.append("抓错的那条还留在列表里(应被替换掉): " + json.dumps(stale["answers"], ensure_ascii=False)[:200])

        # (d) 复刻真实那条例: 本地记录里还带着**换窗口之前**那段(站点那条会话里没有),
        #     末尾那条回答是抓错的 -> 站点那几条要插在对的位置, 末尾那条要换成站点的。
        await page.evaluate("""() => {
          window.__siteMsgs = { ok: true, provider: "chatgpt", count: 4, messages: [
            { role: "user", text: "【上一个窗口的上下文(本地模型汇总…】" + "丙".repeat(200) },
            { role: "assistant", text: "这次属于 Architectural（新项目），所以我会严格按流程走。" },
            { role: "user", text: "确认设计，开始编码" },
            { role: "assistant", text: "已经开始编码，并完成第一版可运行的 Vue 3 工程化商城前台。" + "乙".repeat(200) }] };
          transcript = [
            { role: "user", text: "开始输入啊" },
            { role: "assistant", text: "上一条会话里的旧回答（换窗口之前那段）" },
            { role: "user", text: "确认设计，开始编码" },
            { role: "assistant", text: "我已按你的“直接开始”要求推进到工程设计阶段，并完成了：" + "甲".repeat(40) },
          ];
          histConv = liveConvKey(); histProvider = providerKey();
          histFlush();
          renderHistoryFor(providerKey(), liveConvKey(), curSid || null);
        }""")
        await page.evaluate("() => syncFromSite(false)")
        await page.wait_for_timeout(700)
        mixed = await page.evaluate(READ)
        print("换窗口前后混在一起:", json.dumps({"bubbles": mixed["bubbles"],
                                                "answers": mixed["answers"],
                                                "toasts": mixed["toasts"]}, ensure_ascii=False)[:400])
        if not any("已经开始编码" in a for a in mixed["answers"]):
            bad.append("站点那条新回答没有被用上: " + json.dumps(mixed["answers"], ensure_ascii=False)[:200])
        if any("我已按你的" in a for a in mixed["answers"]):
            bad.append("抓错的那条还留着: " + json.dumps(mixed["answers"], ensure_ascii=False)[:200])
        if not any("上一条会话里的旧回答" in a for a in mixed["answers"]):
            bad.append("本地独有的那段历史被弄丢了: " + json.dumps(mixed["answers"], ensure_ascii=False)[:200])
        if not any("上一个窗口的上下文" in b for b in mixed["bubbles"]):
            bad.append("站点那边本地缺的消息没有补进来: " + json.dumps(mixed["bubbles"], ensure_ascii=False)[:200])

        # (e) 复刻"已和站点一致"却还是不对那种: 站点 4 条本地**都已经有了**,
        #     只是末尾多了一条"上一轮回答的副本"(采集抓错留下的残影)
        await page.evaluate("""() => {
          const u1 = "直接开始，不用确认了";
          const a1 = "我已按你的“直接开始”要求推进到工程设计阶段，并完成了：- docs/superpowers/specs/…";
          const u2 = "确认设计，开始编码";
          const a2 = "已经开始编码，并完成第一版可运行的 Vue 3 工程化商城前台。" + "乙".repeat(200);
          window.__siteMsgs = { ok: true, provider: "chatgpt", count: 4, messages: [
            { role: "user", text: u1 }, { role: "assistant", text: a1 },
            { role: "user", text: u2 }, { role: "assistant", text: a2 }] };
          transcript = [
            { role: "user", text: u1 }, { role: "assistant", text: a1 },
            { role: "user", text: u2 }, { role: "assistant", text: a2 },
            { role: "assistant", text: a1 },        // ← 抓错留下的残影(站点里也有这条, 只是位置在上一轮)
          ];
          histConv = liveConvKey(); histProvider = providerKey();
          histFlush();
          renderHistoryFor(providerKey(), liveConvKey(), curSid || null);
        }""")
        await page.evaluate("() => syncFromSite(false)")
        await page.wait_for_timeout(700)
        tail = await page.evaluate(READ)
        print("尾巴残影:", json.dumps({"answers": [a[:26] for a in tail["answers"]],
                                       "bubbles": [b[:14] for b in tail["bubbles"]],
                                       "toasts": tail["toasts"]}, ensure_ascii=False)[:400])
        dropped = len(tail["answers"]) == 2 and len(tail["bubbles"]) == 2
        if not dropped:
            bad.append("抓错的尾巴没有被丢掉(应剩 2 问 2 答): "
                       + json.dumps([len(tail["answers"]), len(tail["bubbles"])], ensure_ascii=False))
        if tail["answers"] and not tail["answers"][-1].startswith("已经开始编码"):
            bad.append("最后一条不是站点那条回答: " + tail["answers"][-1][:40])
        if not any("抓错" in t for t in tail["toasts"]):
            bad.append("丢尾巴没有提示: " + json.dumps(tail["toasts"], ensure_ascii=False))

        # 站点那边自己换了会话 -> **不许切画面**(切了会让人以为"刚发的消息没了"), 只提示;
        # 真正发消息之前, alignSiteConversation() 会把站点切回你正在看的那条。
        await page.evaluate("""() => {
          setFreshFlag(false);
          state.conversation_id = "convA";
          window.__siteConv = "convB";
        }""")
        await page.evaluate("() => loadConversations()")
        await page.wait_for_timeout(700)
        switched = await page.evaluate(READ)
        print("站点换会话后:", json.dumps({k: switched[k] for k in ("conv", "toasts")}, ensure_ascii=False)[:300])
        if switched["conv"] != "convA":
            bad.append("站点换了会话就把画面切走了(应保持不动): " + str(switched["conv"]))
        if not any("站点窗口现在停在另一条会话" in t for t in switched["toasts"]):
            bad.append("站点换了会话却没有提示: " + json.dumps(switched["toasts"], ensure_ascii=False))
        # 发送前对齐: 站点在 convB、界面在看 convA -> 应该把站点切回 convA
        await page.evaluate("""() => { window.__convCalls.length = 0; }""")
        aligned = await page.evaluate("""async () => {
          const ok = await alignSiteConversation();
          return { ok: ok, calls: window.__convCalls.slice(), conv: state.conversation_id };
        }""")
        print("发送前对齐:", json.dumps(aligned, ensure_ascii=False))
        if "open" not in aligned["calls"]:
            bad.append("发送前没有把站点切回正在看的会话: " + json.dumps(aligned, ensure_ascii=False))
        await b.close()
    return bad


async def main() -> int:
    print("[1] 读回站点整段对话(合成 DOM)")
    bad = await reader_check()
    print("[2] 收尾校正: 什么时候该用站点那条")
    bad += await answer_fix_check()
    print("[3] 前端: 同步补回 + 不再静默换会话")
    bad += await frontend_checks()
    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("CONV_SYNC_OK (能读回站点那段对话、按 ⤓ 补进本地记录且不重复; "
          "站点自己换会话会明确提示并跟着切, 不再静默改)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
