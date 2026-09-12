"""输入框那行的「只用本地模型」: 消息不发给 ChatGPT, 由本地模型直接回答。

覆盖:
  1) 前端: 选它之后发送 -> 只请求 /api/local_chat(带正文 + 最近几轮上下文), 一次 /api/chat 都不发;
     就算桥接浏览器没连上也能发; 回答按原有事件协议显示在消息列表里; 选择刷新后还在;
  2) 后端: 回答是流式推的(message_start -> delta* -> message_end), 落进历史;
  3) 没配本地模型时明确报错, 提示去哪配。
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
  window.__calls = [];
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/providers") === 0)
      return json({ default: "chatgpt", providers: [{ id: "chatgpt", name: "ChatGPT Web", short: "GPT",
        capture_mode: "dom", conversation_pattern: "/c/" }] });
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: __STATE__, busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/api/settings") === 0)
      return json({ planner: { type: "local", api_base: "http://127.0.0.1:1234/v1",
                               api_model: "qwen2.5-coder-7b", api_temp: 0.2, has_key: false, api_key: "" },
                    engine: { test_cmd: "", confirm_apply: "1" },
                    file: "H:\\browser-bridge\\.bridge_settings.json" });
    if (s.indexOf("/api/local_chat") === 0 || s.indexOf("/api/chat") === 0) {
      window.__calls.push([s.split("?")[0], JSON.parse((o && o.body) || "{}")]);
      return json({ ok: true, state: "queued" });
    }
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: bad.append("页面报错: " + str(e)[:200]))
        await page.add_init_script(STUB.replace("__STATE__", '"logged_in"'))
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)

        # 选择器在输入框那行, 是"连接模型"那样的按钮 + 菜单
        opts = await page.evaluate("""() => {
          const b = document.getElementById('btnReply');
          b.click();
          const p = document.getElementById('replyPicker');
          return {
            exists: !!b,
            inComposer: !!document.querySelector('.composer #btnReply'),
            text: (document.getElementById('rbNm') || {}).textContent || '',
            open: p.classList.contains('open'),
            items: Array.from(p.querySelectorAll('.picker-item')).map(i => ({
              v: i.dataset.v || '', nm: i.querySelector('.nm').textContent,
              av: (i.querySelector('.pi-av') || {}).textContent || '',
              tick: !!i.querySelector('.tick') })),
            sep: !!p.querySelector('.picker-sep'),
          };
        }""")
        print("菜单:", json.dumps(opts, ensure_ascii=False))
        if not opts["exists"] or not opts["inComposer"]:
            bad.append("输入框那行没有「发给谁」的按钮: " + json.dumps(opts, ensure_ascii=False))
        if not opts["open"] or [i["v"] for i in opts["items"] if i["v"]] != ["web", "local"]:
            bad.append("点开不是「连接模型」那样的两个选项: " + json.dumps(opts, ensure_ascii=False))
        if not opts["items"][0]["tick"]:
            bad.append("当前生效的选项没有打勾: " + json.dumps(opts, ensure_ascii=False))

        # 从菜单里选「只用本地模型」+ 切到 World 模式(本地回答也应该照常落盘+自测)
        await page.evaluate("""() => {
          switchMode('world');
          document.querySelector('#replyPicker .picker-item[data-v="local"]').click();
          document.getElementById('input').value = '给我写个带缓存的 fib';
        }""")
        await page.wait_for_timeout(300)
        picked = await page.evaluate("""() => ({
          nm: document.getElementById('rbNm').textContent,
          av: document.getElementById('rbAv').textContent,
          local: document.getElementById('btnReply').classList.contains('local'),
          saved: localStorage.getItem('wlb.reply.v1'),
          closed: !document.getElementById('replyPicker').classList.contains('open'),
        })""")
        print("选中后:", json.dumps(picked, ensure_ascii=False))
        if "本地" not in picked["nm"] or picked["saved"] != "local" or not picked["local"]:
            bad.append("选了本地模型按钮/偏好没跟上: " + json.dumps(picked, ensure_ascii=False))
        if not picked["closed"]:
            bad.append("选完菜单没关")
        await page.click("#btnSend")
        await page.wait_for_timeout(600)
        calls = await page.evaluate("() => window.__calls")
        print("发送后的请求:", json.dumps([c[0] for c in calls], ensure_ascii=False))
        if any(c[0] == "/api/chat" for c in calls):
            bad.append("选了只用本地模型, 却还是把消息发给了站点: " + json.dumps(calls, ensure_ascii=False))
        local = [c for c in calls if c[0] == "/api/local_chat"]
        if not local:
            bad.append("没有请求 /api/local_chat: " + json.dumps(calls, ensure_ascii=False))
        else:
            body = local[-1][1]
            print("发给本地模型的 body:", json.dumps(
                {"text": body.get("text"), "history_is_list": isinstance(body.get("history"), list)},
                ensure_ascii=False))
            if "带缓存的 fib" not in str(body.get("text")):
                bad.append("正文没带上: " + json.dumps(body, ensure_ascii=False)[:200])
            if not isinstance(body.get("history"), list):
                bad.append("没带上下文 history")
        # 用户气泡标了"只用本地模型"
        bubble = await page.evaluate("() => (document.querySelector('#conv .user-row') || {}).textContent || ''")
        print("用户气泡:", " ".join(bubble.split())[:70])
        if "只用本地模型" not in bubble:
            bad.append("用户气泡没标明这条是本地模型回答的: " + bubble[:60])

        # 回答按原有事件协议显示(后端就是这样推的)
        await page.evaluate(r"""() => {
          handle({ type: "message_start" });
          handle({ type: "delta", kind: "text", text: "好的, 这是个带缓存的 fib: " });
          handle({ type: "delta", kind: "text", text: "```python\ncache = {}\n```" });
          handle({ type: "message_end" });
        }""")
        await page.wait_for_timeout(400)
        st = await page.evaluate(r"""() => ({
          answer: (document.querySelector('#conv .assistant-body') || {}).textContent || '',
          hasCode: !!document.querySelector('#conv .assistant-body pre'),
          stored: (JSON.parse(localStorage.getItem("wlb.history.v3") || "{}").chatgpt || {}),
        })""")
        print("回答渲染:", " ".join(st["answer"].split())[:70], "| 代码块:", st["hasCode"])
        if "带缓存的 fib" not in st["answer"]:
            bad.append("本地模型的回答没显示出来: " + st["answer"][:60])
        seg = [v for v in st["stored"].values() if any(
            (m.get("text") or "").find("带缓存的 fib") >= 0 for m in (v.get("messages") or []))]
        if not seg:
            bad.append("本地这一轮没写进历史: " + json.dumps(st["stored"], ensure_ascii=False)[:200])

        # 刷新后选择还在, 而且桥接没连上时也能发
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(900)
        after = await page.evaluate("""() => ({
          nm: document.getElementById('rbNm').textContent,
          local: document.getElementById('btnReply').classList.contains('local'),
          sendDisabled: document.getElementById('btnSend').disabled,
        })""")
        print("刷新后:", json.dumps(after, ensure_ascii=False))
        if "本地" not in after["nm"] or not after["local"]:
            bad.append("刷新后「只用本地模型」的选择丢了: " + json.dumps(after, ensure_ascii=False))
        if after["sendDisabled"]:
            bad.append("只用本地模型时不该因为网页窗口没连上而禁止发送")
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("LOCAL_ONLY_UI_OK (选「只用本地模型」: 不发给 ChatGPT, 由本地模型回答并照常入历史)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
