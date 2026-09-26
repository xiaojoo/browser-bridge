"""World 模式: 计划/执行过程画在消息列表里的任务卡片(参考图的样式)。

不碰真实站点: 直接喂 engineer 事件给前端。
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

EVENTS = r"""() => {
  showWelcome(false);
  const ev = (o) => handle(Object.assign({ type: "engineer" }, o));
  ev({ stage: "progress", text: "规划中…(本地/规划模型)" });
  ev({ stage: "plan", text: "1. 第一步\n2. 第二步",
       steps: [{ text: "第一步: 改 app.py", files: ["app.py"] },
               { text: "第二步: 加测试", files: [] }] });
  ev({ stage: "step", index: 1, total: 2, files: ["app.py"], chars: 1234,
       prompt: "发给网页模型的请求正文", text: "[1/2] 第一步" });
  ev({ stage: "answer", index: 1, chars: 66, text: '{"message":"ok","files":[{"op":"update","path":"app.py"}]}' });
  ev({ stage: "apply", index: 1, applied: [{ op: "update", path: "app.py", size: 16 }],
       skipped: [], text: "[1] ok" });
  model = "running";
  ev({ stage: "step", index: 2, total: 2, files: [], chars: 800, prompt: "第二步请求", text: "[2/2] 第二步" });
  ev({ stage: "answer", index: 2, chars: 40, text: '{"message":"ok","files":[]}' });
  ev({ stage: "apply", index: 2, applied: [], skipped: ["tests/x.py: 路径越界"], text: "[2] done" });
  ev({ stage: "summary", text: "改动要点: app.py 打印已更新。", text2: "" });
  ev({ stage: "done", text: "执行完成: 应用 1 项, 跳过 1 项, 错误 0 项" });
}"""

READ = """() => {
  const card = document.querySelector("#conv .task-card");
  if (!card) return { card: false };
  return {
    card: true,
    count: card.querySelector(".tcount").textContent,
    foot: card.querySelector(".task-foot").textContent,
    items: Array.from(card.querySelectorAll(".task-list > .task-item")).map(it => ({
      cls: it.className,
      mark: it.querySelector(".ti").textContent,
      text: (it.querySelector(".tt") || it.querySelector(".ttext") || {}).textContent || "",
      meta: (it.querySelector(".tmeta") || {}).textContent || "",
      details: Array.from(it.querySelectorAll("details summary")).map(s => s.textContent),
    })),
    summaryMsg: Array.from(document.querySelectorAll("#conv .assistant-body"))
                      .map(e => e.textContent).filter(t => t.indexOf("工程任务摘要") >= 0).length,
    leftoverPanel: !!document.getElementById("taskPanel"),
    leftoverBtn: !!document.getElementById("btnEngineer"),
  };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)
        await page.evaluate("() => switchMode('world')")
        await page.wait_for_timeout(300)
        # 先撑出滚动条: 列表不滚到底的话, 底部那点 padding 量不出真实间隙(会一直是顶对齐的假数)
        await page.evaluate("""() => { for (let i = 0; i < 14; i++)
          addUserMsg("第 " + i + " 条输入, 用来把消息列表撑出滚动条。"); }""")
        await page.evaluate(EVENTS)
        await page.wait_for_timeout(500)
        r = await page.evaluate(READ)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        if not r["card"]:
            bad.append("消息列表里没有任务卡片")
        else:
            if r["count"] != "2/2 已完成":
                bad.append("完成计数不对: " + r["count"])
            if len(r["items"]) < 2:
                bad.append("步骤条目不全: " + json.dumps(r["items"], ensure_ascii=False))
            else:
                if "done" not in r["items"][0]["cls"] or r["items"][0]["mark"] != "✓":
                    bad.append("步骤1 没有标记完成: " + json.dumps(r["items"][0], ensure_ascii=False))
                if "app.py" not in r["items"][0]["meta"]:
                    bad.append("步骤1 没显示发送给网页模型的文件: " + r["items"][0]["meta"])
                if not any("请求" in s for s in r["items"][0]["details"]) or \
                   not any("回复" in s for s in r["items"][0]["details"]):
                    bad.append("步骤1 没有折叠展示 请求/回复: " + json.dumps(r["items"][0]["details"], ensure_ascii=False))
                if not any("路径越界" in (i["text"] + i["meta"]) for i in r["items"]):
                    bad.append("跳过信息没有显示在卡片里")
            if "执行完成" not in r["foot"]:
                bad.append("卡片底部没有完成信息: " + r["foot"])
            if r["summaryMsg"] < 1:
                bad.append("摘要没有作为消息出现在列表里")
        if r["leftoverPanel"] or r["leftoverBtn"]:
            bad.append("旧的「运行工程任务」面板还在 DOM 里")
        # 折叠
        await page.click("#conv .task-card .task-head")
        await page.wait_for_timeout(200)
        folded = await page.evaluate("() => document.querySelector('#conv .task-card').classList.contains('folded')")
        if not folded:
            bad.append("点标题没有折叠任务卡片")

        # 最后一块和输入框之间的间隙: 只许是"那块自己的 margin + 输入框上沿那 8px 渐变",
        # 不许有额外余量(以前 .content 底部写的是 calc(--composer-h + 26px), 实测空 48px)。
        async def gap_now():
            await page.evaluate("() => { const w = document.getElementById('convWrap');"
                                " w.scrollTop = w.scrollHeight; }")
            await page.wait_for_timeout(250)
            return await page.evaluate("""() => {
              const conv = document.getElementById("conv"), wrap = document.getElementById("convWrap");
              const outer = document.querySelector(".composer-wrap"), inner = document.querySelector(".composer");
              const last = Array.from(conv.children).filter(e => e.getBoundingClientRect().height > 0).pop();
              if (!last) return { err: "列表里没有可见的一块" };
              return { gap: Math.round(inner.getBoundingClientRect().top
                             - last.getBoundingClientRect().bottom),
                       margin: Math.round(parseFloat(getComputedStyle(last).marginBottom)),
                       padTop: Math.round(parseFloat(getComputedStyle(outer).paddingTop)),
                       scrollable: Math.round(wrap.scrollHeight - wrap.clientHeight),
                       who: last.className };
            }""")
        g = await gap_now()
        print("最后一块到输入框:", json.dumps(g, ensure_ascii=False))
        if g.get("err"):
            bad.append("量不了这个间隙: " + g["err"])
        elif g["scrollable"] <= 0:
            bad.append("列表没撑出滚动条, 这条量的不是'滚到底'的那个间隙: scrollable=" + str(g["scrollable"]))
        elif g["gap"] != g["margin"] + g["padTop"]:
            bad.append("最后一块(%s)和输入框之间多了余量: 间隙 %spx, 但它自己的 margin %s + 输入框上沿 %s = %s"
                       % (g["who"], g["gap"], g["margin"], g["padTop"], g["margin"] + g["padTop"]))
        # 反证: 把旧的 +26px 注回去, 上面这条必须报出来(否则那个 0 不可信)
        await page.add_style_tag(content=".content{padding:24px 0 calc(var(--composer-h,0px) + 26px)}")
        old = await gap_now()
        await page.add_style_tag(content=".content{padding:24px 0 var(--composer-h,0px)}")
        back = await gap_now()
        print("注回旧声明:", json.dumps(old, ensure_ascii=False), "| 再改回来:", json.dumps(back, ensure_ascii=False))
        if not g.get("err") and old.get("gap") == g.get("gap"):
            bad.append("这把尺子分辨不出来: 注回旧的 +26px 后间隙还是 %spx(没量到那份额外余量)" % old.get("gap"))
        if not g.get("err") and back.get("gap") != g.get("gap"):
            bad.append("间隙自己不稳定: 同一份内容两次量到 %s / %s px" % (g.get("gap"), back.get("gap")))
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WORLD_CARD_OK (计划/执行/回复都画在消息列表的任务卡片里, 旧面板已移除)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
