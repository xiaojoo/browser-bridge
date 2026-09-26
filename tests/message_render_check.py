"""消息渲染校验: 助手输出/错误提示都不再带头像图标, 用户气泡仍在右侧。

同时把当前样式截一张图到 .tmp/message_render.png 方便肉眼确认。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

# 测试期间断掉实时状态推送, 免得桥接端当前的站点/会话 id 影响断言
WS_STUB = """(() => {
  const orig = window.fetch.bind(window);
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "deepseek", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: null, conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "deepseek", current: null, items: [] });
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true, apply: null, verify: null });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

URL = "http://127.0.0.1:8765/"
SHOT = ROOT / ".tmp" / "message_render.png"

SEED = r"""() => {
  showWelcome(false);
  addUserMsg("你好, 帮我看看这段代码");
  current = openAssistantMsg();
  current.reason = "先给出排查顺序。";
  current.raw = "**结论**: 先确认 `DMA` 是否真的发生。\n\n```python\nprint('hi')\n```";
  renderCurrent();
  current = null;
  handle({ type: "error", text: "示例错误提示" });
  const c = document.getElementById("conv");
  const msg = c.querySelector(".message");
  const struct = {
    rows: c.querySelectorAll(".message").length,
    avatars: c.querySelectorAll("#conv .msg-avatar").length,
    userBubbles: c.querySelectorAll(".user-row .user-bubble").length,
    assistantBodies: c.querySelectorAll(".message .assistant-body").length,
    firstChildOfMsg: msg && msg.firstElementChild ? msg.firstElementChild.className : null,
    bodyLeft: Math.round(c.querySelector(".assistant-body").getBoundingClientRect().left),
    errLeft: Math.round(c.querySelector(".errbox").getBoundingClientRect().left),
    convLeft: Math.round(c.getBoundingClientRect().left),
    errText: (c.querySelector(".errbox") || {}).textContent,
    markdownHtml: !!c.querySelector(".assistant-body pre.code"),
    fontBody: getComputedStyle(c.querySelector(".assistant-body")).fontSize,
    fontUser: getComputedStyle(c.querySelector(".user-row .user-bubble")).fontSize,
    fontErr: getComputedStyle(c.querySelector(".errbox")).fontSize,
    lhBody: getComputedStyle(c.querySelector(".assistant-body")).lineHeight,
    colorBody: getComputedStyle(c.querySelector(".assistant-body")).color,
    colorUser: getComputedStyle(c.querySelector(".user-row .user-bubble")).color,
    bgUser: getComputedStyle(c.querySelector(".user-row .user-bubble")).backgroundColor,
    codeSize: (e => e ? getComputedStyle(e).fontSize : null)
              (c.querySelector(".assistant-body :not(pre) > code")),
    codeColor: (e => e ? getComputedStyle(e).color : null)
               (c.querySelector(".assistant-body :not(pre) > code")),
    codeBg: (e => e ? getComputedStyle(e).backgroundColor : null)
            (c.querySelector(".assistant-body :not(pre) > code")),
  };
  // 流式中的光标: 必须紧贴最后一段文字右侧, 不能另起一行
  current = openAssistantMsg();
  current.raw = "正在这里输出一段比较长的内容用于观察光标位置";
  current.busy = true;
  renderCurrent();
  const cur = document.querySelector("#conv .cursor");
  const host = cur ? cur.parentElement : null;
  const cr = cur ? cur.getBoundingClientRect() : null;
  const hr = host ? host.getBoundingClientRect() : null;
  // 判"在同一行"用第一行的行盒, 不用"离容器顶 <6px"那种魔法数: 正文从 14px 放大到
  // 16px/26px 之后, 光标(15px 高)天然要往下偏几像素, 那个 6 就把它误判成"另起一行"了。
  const lh = host ? parseFloat(getComputedStyle(host).lineHeight) : 0;
  const cursorInfo = {
    exists: !!cur,
    parentTag: host ? host.tagName.toLowerCase() : null,
    parentIsBody: host ? host.classList.contains("assistant-body") : null,
    lineBox: (cr && hr) ? {top: Math.round(cr.top - hr.top), bottom: Math.round(cr.bottom - hr.top),
                           lh: Math.round(lh)} : null,
    sameLine: !!(cr && hr && lh && cr.top >= hr.top - 1 && cr.bottom <= hr.top + lh + 1),
    snugRight: !!(cr && hr && cr.left > hr.left && cr.left <= hr.right + 4),
  };
  current.busy = false;
  renderCurrent();
  cursorInfo.goneAfterBusy = !document.querySelector("#conv .cursor");
  current = null;

  for (let i = 0; i < 12; i++) addUserMsg("填充消息 " + i + " 让内容超出一屏, 用来检查滚动条位置");
  const wrap = document.getElementById("convWrap");
  convWrap.scrollTop = convWrap.scrollHeight;                 // 滚到底, 检查最后一条有没有被输入框挡住
  const composer = document.querySelector(".composer-wrap");
  const lastBubble = (() => {
    const rows = document.querySelectorAll("#conv .user-row .user-bubble");
    return rows[rows.length - 1];
  })();
  return Object.assign(struct, {
    wrapBottom: Math.round(wrap.getBoundingClientRect().bottom),
    mainBottom: Math.round(document.querySelector(".main").getBoundingClientRect().bottom),
    composerTop: Math.round(composer.getBoundingClientRect().top),
    lastBubbleBottom: Math.round(lastBubble.getBoundingClientRect().bottom),
    reservedPad: getComputedStyle(c.querySelector(".assistant-body").parentElement.parentElement || c).paddingBottom,
    mainRight: Math.round(document.querySelector(".main").getBoundingClientRect().right),
    wrapRight: Math.round(wrap.getBoundingClientRect().right),
    wrapScrolls: wrap.scrollHeight > wrap.clientHeight + 10,
    innerScrolls: c.scrollHeight > c.clientHeight + 10,
    convWidth: Math.round(c.getBoundingClientRect().width),
    sb: (() => {
      const t = document.getElementById("cScroll"), th = document.getElementById("cScrollThumb");
      const tr = t.getBoundingClientRect(), thr = th.getBoundingClientRect();
      return { on: t.classList.contains("on"), top: Math.round(tr.top), bottom: Math.round(tr.bottom),
               right: Math.round(tr.right), thumbTop: Math.round(thr.top), thumbBottom: Math.round(thr.bottom),
               innerH: window.innerHeight, innerW: window.innerWidth,
               scrollTop: Math.round(convWrap.scrollTop),
               maxScroll: Math.round(convWrap.scrollHeight - convWrap.clientHeight) };
    })(),
    cursor: cursorInfo,
  });
}"""


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 760}, device_scale_factor=2)
        await page.add_init_script(WS_STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)
        r = await page.evaluate(SEED)
        await page.wait_for_timeout(300)
        await page.locator(".content").screenshot(path=str(SHOT))
        await browser.close()

    print(json.dumps(r, ensure_ascii=False))
    bad = []
    if r["avatars"]:
        bad.append(f"消息里仍有头像图标: {r['avatars']} 个")
    if r["userBubbles"] != 1 or r["assistantBodies"] != 1:
        bad.append(f"消息结构异常 user={r['userBubbles']} assistant={r['assistantBodies']}")
    if r["firstChildOfMsg"] != "assistant-body":
        bad.append("助手消息的第一个子元素不是正文: " + str(r["firstChildOfMsg"]))
    if abs(r["bodyLeft"] - r["convLeft"]) > 2:
        bad.append(f"助手正文没有靠左对齐 (bodyLeft={r['bodyLeft']}, convLeft={r['convLeft']})")
    if abs(r["errLeft"] - r["convLeft"]) > 2:
        bad.append(f"错误提示没有靠左对齐 (errLeft={r['errLeft']})")
    if "示例错误提示" not in (r["errText"] or ""):
        bad.append("错误提示未渲染: " + str(r["errText"]))
    if not r["markdownHtml"]:
        bad.append("markdown 代码块未渲染")
    # 字号/行高/字色钉的是 2026-09-26 从你 ChatGPT 页面量回来的那套 computed 值:
    #   正文/用户气泡/错误行 都是 16px / 行高 26px; 助手字色 rgb(13,13,13);
    #   用户气泡字色 rgb(12,39,74) 底 rgb(232,243,254); 行内代码 14px 且不再是粉色。
    # 全站字号只许落在 ChatGPT 那几档这件事由 tests/type_scale_check.py 扫 DOM 来管。
    for key, want, name in (("fontBody", "16px", "助手正文"), ("fontUser", "16px", "用户气泡"),
                            ("fontErr", "16px", "错误提示"), ("lhBody", "26px", "助手正文行高"),
                            ("codeSize", "14px", "行内代码字号")):
        if r[key] != want:
            bad.append(f"{name}应为 {want}: {r[key]}")
    for key, want, name in (("colorBody", "rgb(13, 13, 13)", "助手字色"),
                            ("colorUser", "rgb(12, 39, 74)", "用户气泡字色"),
                            ("bgUser", "rgb(232, 243, 254)", "用户气泡底色"),
                            ("codeColor", "rgb(13, 13, 13)", "行内代码字色"),
                            ("codeBg", "rgb(236, 236, 236)", "行内代码底色")):
        if r[key] != want:
            bad.append(f"{name}应为 {want}: {r[key]}")
    print("   字体钉值: %s" % json.dumps({k: r[k] for k in (
        "fontBody", "fontUser", "fontErr", "lhBody", "colorBody", "colorUser", "bgUser",
        "codeSize", "codeColor", "codeBg")}, ensure_ascii=False))
    # 滚动条必须在最外层(主区右边缘), 而不是内容列里
    if not r["wrapScrolls"]:
        bad.append("内容超出一屏时外层容器没有滚动")
    if r["innerScrolls"]:
        bad.append("内容列自己还在滚动(滚动条不在最外侧)")
    if r["mainRight"] - r["wrapRight"] > 2:
        bad.append(f"滚动容器没贴住主区右边缘 (mainRight={r['mainRight']}, wrapRight={r['wrapRight']})")
    if r["convWidth"] > 902:
        bad.append(f"内容列宽度异常 {r['convWidth']}")
    # 输入区是浮层: 滚动容器(含滚动条)要一直到底, 最后一条不能被输入框挡住
    if r["mainBottom"] - r["wrapBottom"] > 2:
        bad.append(f"滚动容器没到主区底部 (wrapBottom={r['wrapBottom']}, mainBottom={r['mainBottom']})")
    if r["lastBubbleBottom"] > r["composerTop"] + 2:
        bad.append(f"滚到底时最后一条消息被输入框挡住 (bubbleBottom={r['lastBubbleBottom']}, composerTop={r['composerTop']})")
    # 自绘滚动条: 轨道从页头下面一直到底, 滚到底时滑块贴住轨道底边
    sb = r["sb"]
    if not sb["on"]:
        bad.append("内容超出一屏时自绘滚动条没有出现")
    if sb["innerH"] - sb["bottom"] > 6:
        bad.append(f"滚动条轨道没到窗口底边 (bottom={sb['bottom']}, innerH={sb['innerH']})")
    if sb["top"] > 70:
        bad.append(f"滚动条轨道起点太低 (top={sb['top']})")
    if sb["maxScroll"] > 0 and sb["scrollTop"] < sb["maxScroll"] - 2:
        bad.append("没有滚到最底, 无法校验滑块位置")
    if sb["maxScroll"] > 0 and abs(sb["thumbBottom"] - sb["bottom"]) > 2:
        bad.append(f"滚到底时滑块没贴住底部 (thumbBottom={sb['thumbBottom']}, trackBottom={sb['bottom']})")
    cur = r["cursor"]
    if not cur["exists"] or not cur["sameLine"] or not cur["snugRight"]:
        bad.append("流式光标没有紧贴文字右侧: " + json.dumps(cur, ensure_ascii=False))
    if cur["parentIsBody"]:
        bad.append("流式光标仍挂在消息体下(会另起一行)")
    if not cur["goneAfterBusy"]:
        bad.append("输出结束后光标没有消失")
    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("MESSAGE_RENDER_OK (助手/错误行无头像, 正文贴左, markdown 正常)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
