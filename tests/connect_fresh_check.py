"""每次连接都是"新窗口": 连上之后消息列表必须是空的, 不能把上次会话的首条信息捡回来。

用户诉求(原话):「每次这个 ✓ ChatGPT Web 已连接 点击之后都会将上次的首条信息发送到消息列表,
把这个优化掉。每次连接都是新窗口, 不用回到上次的界面, 也不要有消息。」

这里用替身验证前端那一半(不联网、不碰真实站点):
  1) 预置"上次那轮对话"的本地记录, 模拟连接完成 —— 而且让站点那边**仍报着上次那个会话 id**(最坏情况);
  2) 连上之后: 消息区必须是空的(欢迎页), 侧栏里那条会话还在(记录没被破坏);
  3) 刷新页面: 仍然干净(这次连接是干净的, 刷新也不该把它捡回来);
  4) 主动点侧栏那条会话: 记录照样能看回去;
  5) 而且全程**不许动用户配的工作区根目录**(这条会话没单独设过工作区时, 一根汗毛都不能动)。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"
OLD_MSG = "上次的首条信息"

STUB = """(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  window.__connected = false;
  const orig = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: window.__connected ? "chatgpt" : null,
                    state: window.__connected ? "logged_in" : "idle",
                    busy: false, error: null, started: !!window.__connected,
                    conversation_id: window.__connected ? "convA" : null,
                    conversation_url: window.__connected ? "https://chatgpt.com/c/convA" : null });
    if (s.indexOf("/api/conversations/open") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [
        { id: "convA", key: "/c/convA", title: "上次那个会话",
          url: "https://chatgpt.com/c/convA", active: true }] });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/api/start") === 0) return json({ ok: true, state: "launching" });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
  // 模拟"启动并登录"完成: 站点那边仍报着上次那个会话 id
  window.__connect = () => {
    window.__connected = true;
    const sel = document.getElementById("selProvider");
    if (sel) sel.value = "chatgpt";
    state.state = "logged_in"; state.started = true; state.provider = "chatgpt";
    state.conversation_id = "convA"; state.conversation_url = "https://chatgpt.com/c/convA";
    refreshUi();
  };
  // 预置"上次那轮对话"的本地记录
  localStorage.setItem("wlb.history.v3", JSON.stringify({ chatgpt: {
    "convA|s0": { conv: "convA", sid: "s0", ts: Date.now() - 60000, title: "上次那个会话",
                  url: "https://chatgpt.com/c/convA",
                  messages: [{ role: "user", text: "上次的首条信息" },
                             { role: "assistant", text: "上次的回答" }] } } }));
  localStorage.setItem("wlb.cur.v1", JSON.stringify({ chatgpt: { conv: "convA", sid: "s0" } }));
  localStorage.setItem("wlb.provider.v1", "chatgpt");
})();"""

READ = """() => ({
  msgs: (document.getElementById("conv") || {}).innerText || "",
  welcome: (document.getElementById("welcome") || { style: {} }).style.display || "",
  titles: Array.from(document.querySelectorAll("#convList .item .nm")).map(e => e.textContent.trim()),
  fresh: sessionStorage.getItem("wlb.fresh.v1"),
  rows: (typeof convRows !== "undefined" && convRows) ? convRows.length : -1,
  saved: (() => { try {
    const all = JSON.parse(localStorage.getItem("wlb.history.v3") || "{}");
    const e = ((all.chatgpt || {})["convA|s0"] || {});
    return (e.messages || []).map(m => m.text).join(" | ");
  } catch (err) { return "ERR"; } })(),
})"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(STUB)
        user_root = ""
        try:
            await page.goto(URL, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(800)
            user_root = await page.evaluate("""async () => {
              try { const r = await fetch("/api/workspace"); return (await r.json()).root || ""; }
              catch (e) { return ""; } }""")
            print("用户原来的工作区:", user_root)

            seed = await page.evaluate(READ)
            print("预置记录:", json.dumps(seed["saved"], ensure_ascii=False))
            if OLD_MSG not in seed["saved"]:
                bad.append("测试自己没把上次的记录塞进去: " + json.dumps(seed["saved"], ensure_ascii=False))

            # 点「启动并登录」-> 连上(站点仍报着上次那个会话 id)
            await page.evaluate("() => document.getElementById('btnStart').click()")
            await page.wait_for_timeout(300)
            await page.evaluate("() => window.__connect()")
            await page.wait_for_timeout(1400)          # 等 autoRefreshOnConnect 的定时刷新
            st1 = await page.evaluate(READ)
            print("刚连上:", json.dumps({k: st1[k] for k in ("welcome", "fresh", "rows", "titles")},
                                        ensure_ascii=False))
            print("  消息区:", json.dumps(st1["msgs"][:80], ensure_ascii=False))
            if OLD_MSG in st1["msgs"]:
                bad.append("连上之后又出现了上次会话的首条信息: " + st1["msgs"][:200])
            if st1["welcome"] != "flex":
                bad.append("连上之后不是空白(欢迎页)状态: welcome=" + repr(st1["welcome"]))
            if st1["fresh"] != "1":
                bad.append("没有标记成「这次连接是干净的」: " + repr(st1["fresh"]))
            if "上次那个会话" not in st1["titles"]:
                bad.append("连上后侧栏没有列出站点会话: " + json.dumps(st1["titles"], ensure_ascii=False))

            # 刷新页面: 仍然干净(不该因为刷新又把上次的记录画回来)
            await page.reload(wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(900)
            st2 = await page.evaluate(READ)
            print("刷新后:", json.dumps({k: st2[k] for k in ("welcome", "fresh")}, ensure_ascii=False),
                  "| 消息区:", json.dumps(st2["msgs"][:60], ensure_ascii=False))
            if OLD_MSG in st2["msgs"]:
                bad.append("刷新之后上次会话的首条信息又回来了: " + st2["msgs"][:200])
            if st2["welcome"] != "flex":
                bad.append("刷新之后不是空白状态: welcome=" + repr(st2["welcome"]))

            # 主动点侧栏那条会话: 记录还在, 应该能看回去
            await page.evaluate("() => { const r = document.querySelector('#convList .item'); if (r) r.click(); }")
            await page.wait_for_timeout(900)
            st3 = await page.evaluate(READ)
            print("点开旧会话:", json.dumps({"welcome": st3["welcome"], "fresh": st3["fresh"]},
                                           ensure_ascii=False), "| 消息区:", json.dumps(st3["msgs"][:80],
                                                                                      ensure_ascii=False))
            if OLD_MSG not in st3["msgs"]:
                bad.append("主动点开旧会话也看不到记录了(不该把记录弄丢): " + st3["msgs"][:200])
            if st3["fresh"] is not None:
                bad.append("主动点开之后还挂着「刚连上」的标记: " + repr(st3["fresh"]))

            # 连上/换会话/点旧会话, 都不该把用户配的工作区根目录换掉
            now_root = await page.evaluate("""async () => {
              const r = await fetch("/api/workspace"); return (await r.json()).root; }""")
            print("当前工作区:", now_root, "| 原来:", user_root)
            if user_root and now_root != user_root:
                bad.append("连接/切换过程中把用户的工作区目录换掉了: " + user_root + " -> " + now_root)
        finally:
            if user_root:
                await page.evaluate("""async (p) => {
                  try { await fetch("/api/workspace/root", { method: "POST",
                    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: p }) }); } catch (e) {}
                }""", user_root)
            await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("CONNECT_FRESH_OK (连上后消息列表是空的; 刷新也干净; 旧记录还在, 点一下能看回去; "
          "全程不动用户的工作区目录)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
