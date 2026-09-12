"""侧栏「会话」列表校验(拦截站点接口, 不碰真实站点)。

覆盖:
- 站点会话 + 同 id 下的多段本地对话(侧栏"开启新对话"产生的分段)分级显示
- 站点有但本地没记录的标"未记录"; 只有本地记录的标"本地"
- 点击站点会话 -> 调 /api/conversations/open 并切过去
- 点击同一会话下的另一段本地对话 -> 只切画面, 不请求远端
- 只有本地记录的会话 -> 按 id 还原地址回跳; 地址还原不出来时才只做本地回看
- 记录时带上当前会话地址, 供以后回跳
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
CUR_URL = "https://chatgpt.com/c/convA"
OLD_ID = "oldsession123"
PATTERN = "/c/"

FAKE = {
    "ok": True, "provider": "chatgpt", "current": "convA",
    "items": [
        {"id": "convA", "key": "/c/convA", "title": "站点上的当前会话", "url": "https://x/c/convA", "active": True},
        {"id": "convB", "key": "/c/convB", "title": "站点上的另一个会话", "url": "https://x/c/convB", "active": False},
    ],
}

INSTALL = """(args) => {
  window.__posted = [];
  window.__fake = args.fake;
  const orig = window.fetch;
  window.fetch = (u, o) => String(u).indexOf("/api/conversations") === 0
    ? Promise.resolve(new Response(JSON.stringify(window.__fake), { status: 200, headers: { "Content-Type": "application/json" } }))
    : orig(u, o);
  post = async (url, body) => {
    window.__posted.push([url, body]);
    const p = String((body && (body.url || body.key)) || "");
    const id = p.indexOf(args.pattern) >= 0 ? p.split(args.pattern).pop() : p.split("/").pop();
    window.__fake = Object.assign({}, window.__fake, { current: id });
    return { ok: true, current: id };
  };
  try { if (ws) ws.close(); } catch (e) {}     // 断开真实 WS, 免得伺服端状态事件把站点/会话改掉
  connect = () => {};
  showWelcome(false);
  state.provider = "chatgpt";
  state.state = "logged_in";
  state.conversation_id = "convA";
  state.conversation_url = args.curUrl;
  providersById["chatgpt"] = Object.assign({}, providersById["chatgpt"] || {},
    { conversation_pattern: args.pattern, name: "ChatGPT Web", short: "GPT" });   // 新版 /api/providers 才带 pattern
  localStorage.setItem("wlb.history.v3", JSON.stringify({ chatgpt: {
    "convA|s1": { conv: "convA", sid: "s1", ts: 1, title: "第一段提问", url: args.curUrl, messages: [
      { role: "user", text: "第一段提问" }, { role: "assistant", text: "第一段回答" }] },
    "convA|s2": { conv: "convA", sid: "s2", ts: 9, title: "本地开的第二段", url: args.curUrl, messages: [
      { role: "user", text: "本地开的第二段提问" }] },
    [args.oldId + "|s1"]: { conv: args.oldId, sid: "s1", ts: 2, title: "老会话(只有本地记录)", messages: [
      { role: "user", text: "很久以前的提问" }] },
    "noUrlAtAll|s1": { conv: "noUrlAtAll", sid: "s1", ts: 3, title: "连地址都没有的会话", messages: [
      { role: "user", text: "更早的提问" }] },
  } }));
  return true;
}"""

READ = """() => {
  const box = document.getElementById("convList");
  return {
    rows: Array.from(box.querySelectorAll(".item")).map(el => ({
      title: (el.querySelector(".nm") || el).textContent.trim(),
      tag: (el.querySelector(".tag") || {}).textContent || "",
      active: el.classList.contains("active"),
      sub: el.classList.contains("sub"),
    })),
    transcript: Array.from(document.querySelectorAll("#conv .user-row .user-bubble")).map(e => e.textContent),
    hint: (document.querySelector("#conv .hint-box") || {}).textContent || "",
    posted: window.__posted || [],
    toasts: Array.from(document.querySelectorAll("#toast .toast")).map(e => e.className + "|" + e.textContent),
    convKey: liveConvKey(),
    sid: histSid,
    curSid: curSid,
  };
}"""

CLICK = """(title) => {
  const el = Array.from(document.querySelectorAll("#convList .item"))
    .find(x => (x.querySelector(".nm") || x).textContent.trim() === title);
  if (!el) return false;
  el.click();
  return true;
}"""


async def click_row(page, title):
    ok = await page.evaluate(CLICK, title)
    if not ok:
        raise AssertionError("找不到会话行: " + title)
    await page.wait_for_timeout(450)


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(WS_STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(600)
        await page.evaluate(INSTALL, {"fake": FAKE, "curUrl": CUR_URL, "oldId": OLD_ID, "pattern": PATTERN})
        await page.evaluate("() => loadConversations()")
        await page.evaluate("() => syncHistoryView(true)")
        await page.wait_for_timeout(400)
        st = await page.evaluate(READ)
        print("初始列表:", json.dumps(st["rows"], ensure_ascii=False))
        titles = [r["title"] for r in st["rows"]]
        by_title = {r["title"]: r for r in st["rows"]}

        # 1) 分级显示: 站点会话 + 同一 id 下的两段本地对话
        if "站点上的当前会话" not in titles or "站点上的另一个会话" not in titles:
            bad.append("站点会话没列出来: " + json.dumps(titles, ensure_ascii=False))
        if by_title.get("站点上的当前会话", {}).get("tag") != "2 段":
            bad.append("同一站点会话下有多段本地对话时, 应显示段数: " + json.dumps(by_title.get("站点上的当前会话"), ensure_ascii=False))
        subs = [r["title"] for r in st["rows"] if r["sub"]]
        if sorted(subs) != sorted(["第一段提问", "本地开的第二段"]):
            bad.append("同一 id 下的本地分段没作为子项列出: " + json.dumps(subs, ensure_ascii=False))
        if by_title.get("站点上的另一个会话", {}).get("tag") != "未记录":
            bad.append("站点有但本地没记录的会话应标『未记录』")
        if by_title.get("老会话(只有本地记录)", {}).get("tag") != "本地":
            bad.append("只有本地记录的会话应标『本地』")
        if not by_title.get("站点上的当前会话", {}).get("active"):
            bad.append("当前站点会话没有高亮")
        if st["transcript"] != ["本地开的第二段提问"]:
            bad.append("默认应显示该会话最新一段的记录: " + json.dumps(st["transcript"], ensure_ascii=False))

        # 2) 点同一会话下的另一段本地对话: 只切画面, 不该请求远端
        await click_row(page, "第一段提问")
        st2 = await page.evaluate(READ)
        print("切本地分段:", json.dumps({"transcript": st2["transcript"], "posted": st2["posted"]}, ensure_ascii=False))
        if st2["transcript"] != ["第一段提问"]:
            bad.append("切到另一段本地记录失败: " + json.dumps(st2["transcript"], ensure_ascii=False))
        if st2["posted"]:
            bad.append("同一站点会话内切分段不该请求远端切换: " + json.dumps(st2["posted"], ensure_ascii=False))
        if not next(r for r in st2["rows"] if r["title"] == "第一段提问")["active"]:
            bad.append("当前分段没有高亮")

        # 3) 切到站点上的另一个会话
        await click_row(page, "站点上的另一个会话")
        st3 = await page.evaluate(READ)
        if not any(u[0] == "/api/conversations/open" and u[1]["key"] == "/c/convB" for u in st3["posted"]):
            bad.append("没有请求切换站点会话: " + json.dumps(st3["posted"], ensure_ascii=False))
        if st3["convKey"] != "convB":
            bad.append("切换后当前会话键不对: " + str(st3["convKey"]))
        if "没有本地记录" not in (st3["hint"] or ""):
            bad.append("切到无本地记录的会话时应提示: " + str(st3["hint"]))

        # 4) 只有本地记录的会话: 按 id 还原地址回跳
        await click_row(page, "老会话(只有本地记录)")
        st4 = await page.evaluate(READ)
        opened = [u for u in st4["posted"] if u[0] == "/api/conversations/open"][-1][1]
        print("回跳老会话:", json.dumps(opened, ensure_ascii=False))
        if opened["url"] != "https://chatgpt.com/c/" + OLD_ID:
            bad.append("没有按 id 还原出会话地址: " + json.dumps(opened, ensure_ascii=False))
        if st4["transcript"] != ["很久以前的提问"]:
            bad.append("回跳后没显示该会话的本地记录: " + json.dumps(st4["transcript"], ensure_ascii=False))

        # 5) 地址完全还原不出来: 只本地回看 + 提示
        await page.evaluate("() => { state.conversation_url = ''; renderConversations(); }")
        await click_row(page, "连地址都没有的会话")
        st5 = await page.evaluate(READ)
        if st5["transcript"] != ["更早的提问"]:
            bad.append("无法回跳时应显示本地记录: " + json.dumps(st5["transcript"], ensure_ascii=False))
        if not any("warn" in t and "还原不出远端地址" in t for t in st5["toasts"]):
            bad.append("无法回跳时应有提示: " + json.dumps(st5["toasts"], ensure_ascii=False))

        # 6) 会话行之间的间隙 + hover 才出现的"更多"按钮
        gap = await page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll("#convList .item"));
          return Math.round(rows[1].getBoundingClientRect().top - rows[0].getBoundingClientRect().bottom);
        }""")
        if gap < 2:
            bad.append(f"会话行之间没有间隙: {gap}px")
        more = await page.evaluate("""() => {
          const el = document.querySelector("#convList .item");
          const m = el.querySelector(".more");
          const hidden = getComputedStyle(m).opacity;
          el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
          el.classList.add("__hover");
          return { exists: !!m, opacityIdle: hidden, svg: !!m.querySelector("svg") };
        }""")
        print("更多按钮:", json.dumps(more, ensure_ascii=False))
        if not more["exists"] or not more["svg"] or more["opacityIdle"] != "0":
            bad.append("会话行缺少默认隐藏的『更多』按钮: " + json.dumps(more, ensure_ascii=False))
        hover_op = await page.evaluate("""() => {
          const el = document.querySelector("#convList .item");
          el.querySelector(".more").click();
          const m = document.getElementById("rowMenu");
          return { open: m.classList.contains("open"),
                   labels: Array.from(m.querySelectorAll(".picker-item")).map(b => b.textContent.trim()) };
        }""")
        print("菜单:", json.dumps(hover_op, ensure_ascii=False))
        if not hover_op["open"] or hover_op["labels"] != ["置顶会话", "删除会话"]:
            bad.append("『更多』菜单内容不对: " + json.dumps(hover_op, ensure_ascii=False))

        # 7) 置顶 -> 排到最前 + 标记; 删除 -> 本地隐藏, 不请求站点
        before_top = await page.evaluate("() => document.querySelector('#convList .item .nm').textContent.trim()")
        await page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll("#convList .item"));
          const row = rows.find(r => (r.querySelector(".nm") || {}).textContent.trim() === "站点上的另一个会话");
          row.querySelector(".more").click();
        }""")
        await page.evaluate("""() => {
          const m = document.getElementById("rowMenu");
          Array.from(m.querySelectorAll(".picker-item")).find(b => b.textContent.indexOf("置顶") >= 0).click();
        }""")
        await page.wait_for_timeout(300)
        st7 = await page.evaluate(READ)
        print("置顶后:", json.dumps(st7["rows"][:2], ensure_ascii=False))
        if st7["rows"][0]["title"] != "站点上的另一个会话":
            bad.append("置顶后没有排到最前: " + json.dumps(st7["rows"][:2], ensure_ascii=False))
        prefs = await page.evaluate("() => JSON.parse(localStorage.getItem('wlb.convprefs.v1') || '{}')")
        if not any(v.get("pinned", {}).get("conv:convB") for v in prefs.values()):
            bad.append("置顶没有落到本地存储: " + json.dumps(prefs, ensure_ascii=False))
        if st7["posted"] and len(st7["posted"]) != len(st5["posted"]):
            bad.append("置顶不该请求站点: " + json.dumps(st7["posted"], ensure_ascii=False))

        await page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll("#convList .item"));
          const row = rows.find(r => (r.querySelector(".nm") || {}).textContent.trim() === "站点上的另一个会话");
          row.querySelector(".more").click();
        }""")
        await page.evaluate("""() => {
          const m = document.getElementById("rowMenu");
          Array.from(m.querySelectorAll(".picker-item")).find(b => b.textContent.indexOf("删除") >= 0).click();
        }""")
        await page.wait_for_timeout(300)
        st8 = await page.evaluate(READ)
        print("删除后:", json.dumps([r["title"] for r in st8["rows"]], ensure_ascii=False))
        if any(r["title"] == "站点上的另一个会话" for r in st8["rows"]):
            bad.append("删除后该会话仍在列表里")
        prefs2 = await page.evaluate("() => JSON.parse(localStorage.getItem('wlb.convprefs.v1') || '{}')")
        if not any(v.get("hidden", {}).get("conv:convB") for v in prefs2.values()):
            bad.append("删除没有落到本地隐藏列表: " + json.dumps(prefs2, ensure_ascii=False))
        if len(st8["posted"]) != len(st5["posted"]):
            bad.append("删除不该请求站点: " + json.dumps(st8["posted"], ensure_ascii=False))

        # 8) 会话列表: 不画滚动条 + 滚动懒加载(换成一份很长的站点列表再验)
        await page.evaluate("""() => {
          const items = window.__fake.items.slice();
          for (let i = 1; i <= 30; i++) items.push({
            id: "convN" + i, key: "/c/convN" + i, title: "站点上的第 " + i + " 个会话",
            url: "https://x/c/convN" + i, active: false });
          window.__fake = Object.assign({}, window.__fake, { items: items });
        }""")
        await page.evaluate("() => loadConversations()")
        await page.wait_for_timeout(300)
        lazy = await page.evaluate("""() => {
          const box = document.getElementById("convList");
          const cs = getComputedStyle(box);
          return { scrollbarWidth: cs.scrollbarWidth, overflowY: cs.overflowY,
                   gutter: box.offsetWidth - box.clientWidth,
                   rows: box.querySelectorAll(".item:not(.sub)").length,
                   hint: (box.querySelector(".load-more") || {}).textContent || "" };
        }""")
        print("懒加载初始:", json.dumps(lazy, ensure_ascii=False))
        if lazy["scrollbarWidth"] != "none" or lazy["gutter"] != 0:
            bad.append("会话列表没有隐藏滚动条: " + json.dumps(lazy, ensure_ascii=False))
        if lazy["rows"] < 12:
            bad.append(f"首屏至少渲染 12 条: {lazy['rows']}")
        # 反复滚到底, 直到把站点那 34 条都渲染出来
        rows2 = lazy["rows"]
        for _ in range(8):
            await page.evaluate("() => { const b = document.getElementById('convList'); b.scrollTop = b.scrollHeight; }")
            await page.wait_for_timeout(350)
            n = await page.evaluate("() => document.querySelectorAll('#convList .item:not(.sub)').length")
            print("  滚到底 ->", n)
            if n <= rows2 and n >= 34:
                break
            rows2 = n
        total = await page.evaluate("() => convRows.length")
        if rows2 <= lazy["rows"]:
            bad.append(f"滚到底没有加载更多: {rows2}")
        if rows2 < total:
            bad.append(f"没把所有会话都加载出来: {rows2} < {total}")

        # 9) 记录时带上当前会话地址, 供以后回跳
        await page.evaluate("""() => {
          state.conversation_id = "convB";
          state.conversation_url = "https://chatgpt.com/c/convB";
          histConv = "convB"; histSid = "s9"; curSid = "s9";
          transcript = [];
          histPush({ role: "user", text: "新问题" });
        }""")
        saved = await page.evaluate("""() => (JSON.parse(localStorage.getItem("wlb.history.v3") || "{}").chatgpt || {})["convB|s9"] || null""")
        print("落库记录:", json.dumps(saved, ensure_ascii=False))
        if not saved or saved.get("url") != "https://chatgpt.com/c/convB":
            bad.append("本地记录没保存会话地址, 以后无法回跳: " + json.dumps(saved, ensure_ascii=False))

        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("CONVERSATIONS_UI_OK (站点会话 + 同 id 本地分段分级显示, 切换/回跳都正确)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
