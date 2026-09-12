"""消息列表右侧的「输入导航」: 收起和展开**是同一个框、同一套行距** ——
收起时框只有一列横线那么宽(文字藏起来), 鼠标落在框上展开 -> 框往左变宽、文字显示出来;
行数、行距、框高都不变。active 跟着正文滚动, 对应那一格 + 正文里那条输入一起高亮。

覆盖:
  1) 有 ≥2 条输入时出现导航; 每条 = 一处输入, 文案是那句话说的一行预览;
  2) 默认收起: 右侧一个圆角框(白底 + 边框), 里面一列横线; 行高 30 / 行距 32 跟展开态一致,
     框在屏幕纵向居中, 全部落在右轨里, 不溢出也没有滚动条;
  3) 鼠标落在框上才展开: 在右轨空白处划过不展开, 离开框就收回;
  4) 两态对比: 右边线不动、框高不变、行距不变、显示的条目一样, 只有宽度变大;
  5) 悬停某一行: 那一行有底色, 并在卡片左边浮出提示气泡 —— 里面是这条输入的**完整**文字,
     气泡内部不滚(没有滚动条), 太长就按 12 行截断、末尾显示「…」; 鼠标离开就收掉;
  6) active = 主题色文字 + 更长的主题色横线, 正文里对应那条输入的泡泡也亮一圈(两边同步跟滚动走);
  7) 点某一行 -> 滚到那条消息(落点对齐"当前那条"的判定线, 点完高亮就是它自己, 不会跑到上一条);
  8) 一屏放不下时: 不滚动、不出滚动条、不留半行, 改成显示一行「……」, 两态一致, 且当前那条仍可见;
  9) 导航里没有「−」删除功能; 只有一条输入/空会话时不显示。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"
ACCENT = "rgb(77, 107, 254)"          # --accent: #4D6BFE

STUB = r"""(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.__posted = [];
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/providers") === 0)
      return json({ default: "chatgpt", providers: [{ id: "chatgpt", name: "ChatGPT Web", short: "GPT",
        capture_mode: "dom", conversation_pattern: "/c/" }] });
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/api/chat") === 0) { window.__posted.push(JSON.parse((o && o.body) || "{}")); return json({ ok: true }); }
    if (s.indexOf("/workspace/tree") === 0) return json({ ok: true, root: "H:\\tmp", name: "ws", items: [] });
    if (s.indexOf("/api/workspace") === 0) return json({ ok: true, root: "H:\\tmp", name: "ws", custom: true });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

# 造 4 轮对话(用户输入 + 回答)
SEED = r"""() => {
  showWelcome(false);
  transcript = [];
  convEl.innerHTML = "";
  for (let i = 1; i <= 4; i++) {
    const q = i === 2
      ? "tree /F 这个显示目录，排除 .venv 这种大目录，只看前两层就行"
      : "第 " + i + " 个问题";
    transcript.push({ role: "user", text: q });
    addUserMsg(q, transcript.length - 1);
    transcript.push({ role: "assistant", text: "第 " + i + " 个回答。" + "内容".repeat(600) });
    renderStoredMessage(transcript[transcript.length - 1], transcript.length - 1);
  }
  scrollBottom();
  syncScrollbar();
  return transcript.length;
}"""

# 追加到 n 条输入(短回答, 便于把导航撑爆)
MORE = r"""(n) => {
  const have = convEl.querySelectorAll(".user-row[data-mi]").length;
  for (let i = have + 1; i <= n; i++) {
    transcript.push({ role: "user", text: "第 " + i + " 个问题(很长的文字用来验证省略号与右对齐效果)" });
    addUserMsg(transcript[transcript.length - 1].text, transcript.length - 1);
    transcript.push({ role: "assistant", text: "第 " + i + " 个回答。" });
    renderStoredMessage(transcript[transcript.length - 1], transcript.length - 1);
  }
  scrollBottom();
  syncScrollbar();
  return convEl.querySelectorAll(".user-row[data-mi]").length;
}"""

READ = r"""() => {
  const box = document.getElementById("convNav");
  const card = document.getElementById("convNavCard");
  const list = document.getElementById("convNavList");
  const items = Array.from(document.querySelectorAll("#convNavList .cnav-item"));
  const lbl = (it) => it.querySelector(".t");
  const dsh = (it) => it.querySelector(".cnav-d");
  const rect = (el) => { const b = el.getBoundingClientRect();
    return { l: Math.round(b.left), r: Math.round(b.right), t: Math.round(b.top), b: Math.round(b.bottom),
             h: Math.round(b.height), w: Math.round(b.width) }; };
  const shown = items.filter(i => i.style.display !== "none");
  const kids = Array.from(list.children).filter(el => el.style.display !== "none");
  const act = shown.find(i => i.classList.contains("active")) || null;
  const more = document.getElementById("convNavMore");
  const moreShown = !!more && more.style.display !== "none";
  const marked = Array.from(document.querySelectorAll(".user-row.nav-active"));
  return {
    hidden: box.hidden,
    open: box.classList.contains("open"),
    box: box.hidden ? null : rect(box),
    card: rect(card),
    cardBg: getComputedStyle(card).backgroundColor,
    cardShadow: getComputedStyle(card).boxShadow,
    cardBorder: getComputedStyle(card).borderTopColor + " / " + getComputedStyle(card).borderTopWidth,
    labelsVisible: items.length ? getComputedStyle(lbl(items[0])).display !== "none" : null,
    labels: items.map(i => lbl(i).textContent),
    mis: items.map(i => i.dataset.mi),
    shownMIs: shown.map(i => i.dataset.mi),
    labelRights: shown.map(i => Math.round(lbl(i).getBoundingClientRect().right)),
    // 横线本体画在 .cnav-d 上, .cnav-tail 是固定宽度的格子(保证文字右边缘/横线位置不被顶跑)
    dashWidths: shown.map(i => Math.round(dsh(i).getBoundingClientRect().width)),
    dashColors: shown.map(i => getComputedStyle(dsh(i)).backgroundColor),
    dashRects: shown.map(i => { const r = dsh(i).getBoundingClientRect();
      return [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)]; }),
    tailRects: shown.map(i => { const r = i.querySelector(".cnav-tail").getBoundingClientRect();
      return [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)]; }),
    textColors: shown.map(i => getComputedStyle(lbl(i)).color),
    tops: shown.map(i => i.offsetTop),
    heights: shown.map(i => i.offsetHeight),
    dashTs: shown.map(i => Math.round(dsh(i).getBoundingClientRect().top)),
    dashHs: shown.map(i => Math.round(dsh(i).getBoundingClientRect().height)),
    dashPitches: shown.slice(1).map((it, i) => it.offsetTop - shown[i].offsetTop),
    center: (box.hidden || !kids.length) ? null : {
      rail: Math.round((rect(box).t + rect(box).b) / 2),
      col: Math.round((kids[0].getBoundingClientRect().top +
                       kids[kids.length - 1].getBoundingClientRect().bottom) / 2),
      card: Math.round((rect(card).t + rect(card).b) / 2),
    },
    active: act ? act.dataset.mi : null,
    activeShown: !!act,
    activeMarked: marked.map(el => el.dataset.mi),
    markRing: marked.length ? getComputedStyle(marked[0].querySelector(".user-bubble")).boxShadow : null,
    moreShown: moreShown,
    moreText: more ? more.textContent : null,
    moreRect: moreShown ? rect(more) : null,
    lastShownBottom: shown.length ? Math.round(shown[shown.length - 1].getBoundingClientRect().bottom) : null,
    hasTipEl: !!document.getElementById("convNavTip"),
    tipShown: (() => { const t = document.getElementById("convNavTip"); return !!t && !t.hidden; })(),
    tipText: (() => { const t = document.getElementById("convNavTip"); return t ? t.textContent : ""; })(),
    tipRect: (() => { const t = document.getElementById("convNavTip");
      if (!t || t.hidden) return null; const r = t.getBoundingClientRect();
      return { l: Math.round(r.left), r: Math.round(r.right), t: Math.round(r.top), b: Math.round(r.bottom),
               w: Math.round(r.width), h: Math.round(r.height) }; })(),
    tipScrollbarW: (() => { const t = document.getElementById("convNavTip");
      return t ? t.offsetWidth - t.clientWidth : null; })(),
    tipScrollbarWidth: (() => { const t = document.getElementById("convNavTip");
      return t ? getComputedStyle(t).scrollbarWidth : null; })(),
    tipClamp: (() => { const t = document.getElementById("convNavTip");
      return t ? getComputedStyle(t).webkitLineClamp : null; })(),
    activeVisible: (() => {
      if (!act) return null;
      const t = act.offsetTop - list.scrollTop, b = t + act.offsetHeight;
      return t >= -1 && b <= list.clientHeight + 1;
    })(),
    list: { scrollTop: Math.round(list.scrollTop), h: Math.round(list.clientHeight), sh: Math.round(list.scrollHeight),
            offsetW: list.offsetWidth, clientW: list.clientWidth,
            scrollbarWidth: getComputedStyle(list).scrollbarWidth },
    rows: items.map(i => ({ mi: i.dataset.mi, shown: i.style.display !== "none",
      hovered: i.matches(":hover"), bg: getComputedStyle(i).backgroundColor })),
  };
}"""


async def away(page):
    """鼠标挪开(收起来)并等过渡结束"""
    await page.mouse.move(700, 400)
    await page.wait_for_timeout(450)


def compare_states(col, exp, where, bad):
    """两态应该一模一样地待在原地: 条目、行距、框高、行尾那格/横线的位置都不变, 只是变宽 + 显示文字"""
    if exp["card"]["h"] != col["card"]["h"]:
        bad.append(where + ": 展开后框高变了(应该只是变宽): " + str(col["card"]["h"]) +
                   " -> " + str(exp["card"]["h"]))
    if exp["card"]["r"] != col["card"]["r"]:
        bad.append(where + ": 展开后右边线动了: " + str(col["card"]["r"]) + " -> " + str(exp["card"]["r"]))
    if exp["dashPitches"] != col["dashPitches"]:
        bad.append(where + ": 两态行距不一样: " + json.dumps([col["dashPitches"], exp["dashPitches"]]))
    if exp["shownMIs"] != col["shownMIs"]:
        bad.append(where + ": 两态显示的条目不一样: " + json.dumps([col["shownMIs"][:8], exp["shownMIs"][:8]]))
    if exp["moreShown"] != col["moreShown"]:
        bad.append(where + ": 两态「……」不一致")
    # 位置不能动: 行尾那格 + 每根横线(当前那条会变长, 单独放行)
    if exp["tailRects"] != col["tailRects"]:
        bad.append(where + ": 展开后行尾那格挪位了: " + json.dumps([col["tailRects"][:3], exp["tailRects"][:3]]))
    inact = [i for i, mi in enumerate(col["shownMIs"]) if mi != col["active"]]
    if [col["dashRects"][i] for i in inact] != [exp["dashRects"][i] for i in inact]:
        bad.append(where + ": 展开后横线挪位了: " +
                   json.dumps([[col["dashRects"][i] for i in inact[:3]],
                               [exp["dashRects"][i] for i in inact[:3]]]))
    if not exp["card"]["w"] > col["card"]["w"]:
        bad.append(where + ": 展开没有变宽: " + str(col["card"]["w"]) + " -> " + str(exp["card"]["w"]))
    if exp["card"]["h"] != exp["list"]["h"] + 2:
        bad.append(where + ": 框高跟内容对不上(有空白): card=" + str(exp["card"]["h"]) +
                   " list=" + str(exp["list"]["h"]))


def check_fits(st, where, bad):
    """一屏放不下时: 不滚、没滚动条、不留半行; 放得下时也不该有滚动条"""
    if st["list"]["sh"] > st["list"]["h"]:
        bad.append(where + ": 导航列表溢出了(会冒出滚动条/半行): " + json.dumps(st["list"], ensure_ascii=False))
    if st["list"]["offsetW"] != st["list"]["clientW"]:
        bad.append(where + ": 导航占了滚动条宽度: " + json.dumps(st["list"], ensure_ascii=False))
    if st["list"]["scrollbarWidth"] != "none":
        bad.append(where + ": 导航列表没关掉滚动条: " + str(st["list"]["scrollbarWidth"]))
    if st["list"]["scrollTop"] != 0:
        bad.append(where + ": 导航列表被滚动了: " + str(st["list"]["scrollTop"]))


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("pageerror", lambda e: bad.append("页面报错: " + str(e)[:200]))
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)
        n = await page.evaluate(SEED)
        await page.wait_for_timeout(400)

        st = await page.evaluate(READ)
        print("种子消息:", n)
        print("收起态:", json.dumps({k: st[k] for k in ("hidden", "open", "box", "cardBg", "labelsVisible")},
                                   ensure_ascii=False))
        if st["hidden"]:
            bad.append("有 4 条输入时导航没有出现")
        if len(st["labels"]) != 4:
            bad.append("导航条目数不对: " + json.dumps(st["labels"], ensure_ascii=False))
        if not any("tree /F" in x for x in st["labels"]):
            bad.append("导航里没有那条输入: " + json.dumps(st["labels"], ensure_ascii=False))
        if st["open"]:
            bad.append("默认就展开了, 应该先收起")
        if st["labelsVisible"]:
            bad.append("收起时还显示文字预览")
        if st["box"]["w"] > 60:
            bad.append("收起态太宽了: " + json.dumps(st["box"]))
        if st["box"]["r"] > 1392:
            bad.append("导航压到最右边(要给自绘滚动条留位置): " + json.dumps(st["box"]))
        # 收起态 = 什么都没有的框(透明): 没有白底也没有边框, 就是那几根横线
        if st["cardBg"] not in ("rgba(0, 0, 0, 0)", "transparent"):
            bad.append("收起时不该有背景: " + st["cardBg"])
        if "rgba(0, 0, 0, 0)" not in st["cardBorder"]:
            bad.append("收起时不该有边框: " + st["cardBorder"])
        if set(st["heights"]) != {30}:
            bad.append("收起态的行高不是 30(应该跟展开时一样): " + json.dumps(st["heights"]))
        print("收起态短线:", json.dumps({"ts": st["dashTs"], "pitches": st["dashPitches"],
                                       "center": st["center"]}, ensure_ascii=False))
        if len(set(st["dashPitches"])) > 1:
            bad.append("收起态各格间距不一样(应该等间距): " + json.dumps(st["dashPitches"]))
        if abs(st["center"]["col"] - st["center"]["rail"]) > 3:
            bad.append("收起态没有纵向居中: 列中心 " + str(st["center"]["col"]) +
                       " / 轨道中心 " + str(st["center"]["rail"]))
        if st["moreShown"]:
            bad.append("4 条输入时不该出现「……」")
        check_fits(st, "收起态(4 条)", bad)
        short_pitch = st["dashPitches"][0] if st["dashPitches"] else None

        # 鼠标落在右轨空白处(卡片之外)不该展开
        await page.mouse.move(st["box"]["l"] + st["box"]["w"] // 2, st["box"]["t"] + 20)
        await page.wait_for_timeout(400)
        empty_hover = await page.evaluate(READ)
        print("鼠标在右轨空白处:", json.dumps({"open": empty_hover["open"],
                                             "cardH": empty_hover["card"]["h"]}, ensure_ascii=False))
        if empty_hover["open"]:
            bad.append("鼠标只是划过右轨空白处就展开了(应该只有落在卡片上才展开)")

        # 鼠标落在卡片上才展开
        await page.hover("#convNavCard")
        await page.wait_for_timeout(500)
        opened = await page.evaluate(READ)
        print("悬停卡片:", json.dumps({"open": opened["open"], "w": opened["box"]["w"],
                                     "bg": opened["cardBg"], "cardH": opened["card"]["h"],
                                     "center": opened["center"], "labelsVisible": opened["labelsVisible"]},
                                    ensure_ascii=False))
        if not opened["open"] or opened["box"]["w"] < 200 or not opened["labelsVisible"]:
            bad.append("鼠标落在卡片上没展开成卡片: " + json.dumps(opened["box"], ensure_ascii=False))
        if opened["cardBg"] != "rgb(255, 255, 255)":
            bad.append("展开后不是白卡片: " + opened["cardBg"])
        if opened["cardShadow"] in ("none", ""):
            bad.append("卡片没有浮起来的投影")
        if "rgba(0, 0, 0, 0)" in opened["cardBorder"]:
            bad.append("展开后应该有边框: " + opened["cardBorder"])
        if abs(opened["center"]["card"] - opened["center"]["rail"]) > 3:
            bad.append("展开的卡片没有纵向居中: 卡片中心 " + str(opened["center"]["card"]) +
                       " / 轨道中心 " + str(opened["center"]["rail"]))
        if len(set(opened["heights"])) != 1:
            bad.append("各行高度不一致: " + json.dumps(opened["heights"]))
        deltas = [opened["tops"][i + 1] - opened["tops"][i] for i in range(len(opened["tops"]) - 1)]
        if len(set(deltas)) != 1:
            bad.append("展开态各行间距不一致: " + json.dumps(opened["tops"]))
        if deltas and deltas[0] < opened["heights"][0]:
            bad.append("展开态行距比行高还小(行压在一起了): 行距 " + str(deltas[0]) +
                       " 行高 " + str(opened["heights"][0]))
        if max(opened["labelRights"]) - min(opened["labelRights"]) > 2:
            bad.append("文字不是右对齐的: " + json.dumps(opened["labelRights"]))
        check_fits(opened, "展开态(4 条)", bad)
        compare_states(st, opened, "4 条", bad)

        # 悬停某一行: 那一行有底色; 并且在卡片左边浮出提示气泡(完整输入, 内部不滚)
        await page.hover("#convNavList .cnav-item[data-mi='2']")
        await page.wait_for_timeout(400)
        tip = await page.evaluate(READ)
        print("悬停气泡:", json.dumps({"shown": tip["tipShown"], "text": tip["tipText"][:36],
                                     "rect": tip["tipRect"], "cardL": tip["card"]["l"],
                                     "sbW": tip["tipScrollbarW"], "clamp": tip["tipClamp"]},
                                    ensure_ascii=False))
        if not tip["tipShown"]:
            bad.append("悬停没有浮出提示气泡")
        else:
            if "tree /F" not in tip["tipText"] or ".venv" not in tip["tipText"]:
                bad.append("气泡里不是完整输入: " + tip["tipText"][:60])
            if tip["tipRect"]["r"] > tip["card"]["l"] + 2:
                bad.append("气泡压在卡片上了(应该贴在卡片左边): tip=" + json.dumps(tip["tipRect"]) +
                           " card=" + json.dumps(tip["card"]))
            if tip["tipScrollbarW"] != 0 or tip["tipScrollbarWidth"] != "none":
                bad.append("气泡内部出现了滚动条: " + json.dumps([tip["tipScrollbarW"], tip["tipScrollbarWidth"]]))
        hovered = [r for r in tip["rows"] if r["hovered"]]
        others = [r for r in tip["rows"] if not r["hovered"]]
        if len(hovered) != 1 or hovered[0]["mi"] != "2":
            bad.append("指着的行没被识别: " + json.dumps(tip["rows"], ensure_ascii=False)[:200])
        elif hovered[0]["bg"] in ("rgba(0, 0, 0, 0)", "transparent"):
            bad.append("鼠标指着的那一行没有高亮: " + json.dumps(hovered[0], ensure_ascii=False))
        elif any(o["bg"] not in ("rgba(0, 0, 0, 0)", "transparent") for o in others):
            bad.append("高亮不在鼠标指着的那一行: " + json.dumps(tip["rows"], ensure_ascii=False)[:200])

        # active = 主题色文字 + 更长更粗的主题色横线
        act_i = tip["shownMIs"].index(tip["active"]) if tip["active"] in tip["shownMIs"] else -1
        inact = [i for i in range(len(tip["shownMIs"])) if i != act_i]
        if act_i < 0:
            bad.append("没有任何一行是 active")
        else:
            if tip["textColors"][act_i] != ACCENT:
                bad.append("active 的文字不是主题色: " + tip["textColors"][act_i])
            if tip["dashColors"][act_i] != ACCENT:
                bad.append("active 的横线不是主题色: " + tip["dashColors"][act_i])
            if not all(tip["dashWidths"][act_i] > tip["dashWidths"][i] for i in inact):
                bad.append("active 的横线没有比别的长: " + json.dumps(tip["dashWidths"]))
        print("正文高亮:", tip["activeMarked"], "| 圈:", tip["markRing"])
        if tip["activeMarked"] != [tip["active"]]:
            bad.append("正文里没有跟着高亮同一条输入: 导航 active=" + str(tip["active"]) +
                       " 正文高亮=" + json.dumps(tip["activeMarked"]))
        elif not tip["markRing"] or tip["markRing"] == "none":
            bad.append("正文那条没有高亮外观: " + str(tip["markRing"]))

        # 点第 1 条 -> 滚上去
        await page.evaluate("() => { document.getElementById('convWrap').scrollTop = 99999; }")
        await page.wait_for_timeout(300)
        await page.click("#convNavList .cnav-item[data-mi='0']")
        await page.wait_for_timeout(900)
        jumped = await page.evaluate(r"""() => {
          const el = document.querySelector('.user-row[data-mi="0"]');
          const wrap = document.getElementById("convWrap").getBoundingClientRect();
          const r = el.getBoundingClientRect();
          return { inView: r.top >= wrap.top - 4 && r.top <= wrap.bottom, top: Math.round(r.top - wrap.top) };
        }""")
        print("跳转:", json.dumps(jumped, ensure_ascii=False))
        if not jumped["inView"]:
            bad.append("点导航没有跳到那条消息: " + json.dumps(jumped, ensure_ascii=False))

        # 点中间一条: 落点要对齐"判定线"(不能落在视口中间), 而且点完"当前那条"必须就是它
        await page.evaluate("() => { document.getElementById('convWrap').scrollTop = 0; }")
        await page.wait_for_timeout(300)
        await page.click("#convNavList .cnav-item[data-mi='4']")
        await page.wait_for_timeout(1000)
        landed = await page.evaluate(r"""() => {
          const wrap = document.getElementById("convWrap");
          const wr = wrap.getBoundingClientRect();
          const el = document.querySelector('.user-row[data-mi="4"]');
          const r = el.getBoundingClientRect();
          const act = document.querySelector("#convNavList .cnav-item.active");
          const mark = document.querySelector(".user-row.nav-active");
          return { off: Math.round(r.top - wr.top),
                   line: Math.round(Math.min(140, Math.max(48, wrap.clientHeight * 0.16))),
                   active: act ? act.dataset.mi : null, marked: mark ? mark.dataset.mi : null,
                   inView: r.top >= wr.top - 2 && r.top <= wr.bottom };
        }""")
        print("点第 3 条:", json.dumps(landed, ensure_ascii=False))
        if not landed["inView"]:
            bad.append("点了中间那条但没滚到它: " + json.dumps(landed, ensure_ascii=False))
        elif landed["off"] > landed["line"] + 10:
            bad.append("落点太靠下(没对齐判定线, 于是显示成上一条的内容): " + json.dumps(landed, ensure_ascii=False))
        if landed["active"] != "4" or landed["marked"] != "4":
            bad.append("点第 3 条之后高亮偏移了(应该就是它自己): " + json.dumps(landed, ensure_ascii=False))

        # 滚动时高亮跟着走
        await away(page)
        await page.evaluate("() => { document.getElementById('convWrap').scrollTop = 0; }")
        await page.wait_for_timeout(300)
        await page.hover("#convNavCard")
        await page.wait_for_timeout(400)
        topst = await page.evaluate(READ)
        await away(page)
        await page.evaluate("() => { document.getElementById('convWrap').scrollTop = 99999; }")
        await page.wait_for_timeout(400)
        await page.hover("#convNavCard")
        await page.wait_for_timeout(400)
        bottom = await page.evaluate(READ)
        print("滚到顶部: 高亮", topst["active"], topst["activeMarked"],
              "| 滚到底部: 高亮", bottom["active"], bottom["activeMarked"])
        if topst["active"] != "0" or topst["activeMarked"] != ["0"]:
            bad.append("在顶部时高亮不是第一条: " + json.dumps([topst["active"], topst["activeMarked"]]))
        if bottom["active"] == "0":
            bad.append("滚到底部了高亮还停在第一条")
        if bottom["activeMarked"] != [bottom["active"]]:
            bad.append("滚动后正文高亮没跟着走: " + json.dumps([bottom["active"], bottom["activeMarked"]]))

        # 30 条: 一屏放不下 -> 两态显示同样的若干条 + 「……」; 不滚动、不出滚动条、不留半行
        total = await page.evaluate(MORE, 30)
        await page.wait_for_timeout(400)
        await page.evaluate("() => { document.getElementById('convWrap').scrollTop = 99999; }")
        await page.wait_for_timeout(400)
        await away(page)
        col30 = await page.evaluate(READ)
        await page.hover("#convNavCard")
        await page.wait_for_timeout(500)
        many = await page.evaluate(READ)
        print("30 条 收起:", json.dumps({"shown": len(col30["shownMIs"]), "more": col30["moreShown"],
                                       "pitch": sorted(set(col30["dashPitches"])), "card": col30["card"]},
                                      ensure_ascii=False))
        print("30 条 展开:", json.dumps({"shown": len(many["shownMIs"]), "all": len(many["mis"]),
                                       "more": many["moreShown"], "active": many["active"],
                                       "activeShown": many["activeShown"], "list": many["list"],
                                       "card": many["card"], "lastRowBottom": many["lastShownBottom"]},
                                      ensure_ascii=False))
        if len(many["mis"]) < 25:
            bad.append("条目没跟上(应该 30 条左右): " + str(len(many["mis"])))
        if not many["moreShown"] or (many["moreText"] or "").strip() != "……":
            bad.append("卡片放不下时没有显示「……」: " + json.dumps([many["moreShown"], many["moreText"]],
                                                                 ensure_ascii=False))
        if len(many["shownMIs"]) >= len(many["mis"]):
            bad.append("放不下却没藏起任何一行: shown=" + str(len(many["shownMIs"])))
        if not many["activeShown"]:
            bad.append("放不下时当前那条被藏起来了: active=" + str(many["active"]) +
                       " shown=" + json.dumps(many["shownMIs"]))
        if many["card"]["b"] > many["box"]["b"] + 1:
            bad.append("卡片超出右轨: card=" + json.dumps(many["card"]) + " rail=" + json.dumps(many["box"]))
        if many["lastShownBottom"] is not None and many["lastShownBottom"] > many["card"]["b"] + 1:
            bad.append("末行被卡片切了一半: 末行底=" + str(many["lastShownBottom"]) +
                       " 卡片底=" + str(many["card"]["b"]))
        check_fits(many, "展开态(30 条)", bad)
        check_fits(col30, "收起态(30 条)", bad)
        compare_states(col30, many, "30 条", bad)
        if abs(col30["center"]["col"] - col30["center"]["rail"]) > 3:
            bad.append("收起态(30 条)没有纵向居中: 列中心 " + str(col30["center"]["col"]) +
                       " / 轨道中心 " + str(col30["center"]["rail"]))
        if short_pitch is not None and col30["dashPitches"] and \
           abs(col30["dashPitches"][0] - short_pitch) > 1:
            bad.append("行距跟着条数变了(应该恒定): 4 条时 " + str(short_pitch) +
                       " 30 条时 " + str(col30["dashPitches"][0]))
        if col30["active"] is None:
            bad.append("收起态没有高亮任何一条")

        # 收起态滚动 -> 蓝色那根要换
        await page.evaluate("() => { document.getElementById('convWrap').scrollTop = 0; }")
        await page.wait_for_timeout(400)
        top30 = await page.evaluate(READ)
        if top30["active"] == col30["active"]:
            bad.append("收起态滚动后蓝色那根没换: " + str(col30["active"]))

        # 60 条: 两态依然一模一样(同样的窗口 + 「……」), 不滚动
        total = await page.evaluate(MORE, 60)
        await page.wait_for_timeout(500)
        await page.evaluate("() => { document.getElementById('convWrap').scrollTop = 99999; }")
        await page.wait_for_timeout(400)
        await away(page)
        dense = await page.evaluate(READ)
        print("60 条 收起:", json.dumps({"shown": len(dense["shownMIs"]), "all": len(dense["mis"]),
                                       "more": dense["moreShown"], "active": dense["active"],
                                       "activeShown": dense["activeShown"], "list": dense["list"]},
                                      ensure_ascii=False))
        if len(dense["mis"]) < 55:
            bad.append("条目没跟上(应该 60 条左右): " + str(len(dense["mis"])))
        if not dense["moreShown"]:
            bad.append("收起态放不下时没有显示「……」")
        if not dense["activeShown"]:
            bad.append("收起态放不下时当前那条被藏起来了")
        if abs(dense["center"]["col"] - dense["center"]["rail"]) > 3:
            bad.append("收起态(60 条)没有纵向居中: " + str(dense["center"]))
        check_fits(dense, "收起态(60 条)", bad)

        # 收起态那一列短线也能点: 鼠标移上去先展开, 再点其中一格(真实操作就是这样)
        await page.hover("#convNavCard")
        await page.wait_for_timeout(600)
        dense2 = await page.evaluate(READ)
        target = dense2["shownMIs"][0]
        await page.click("#convNavList .cnav-item[data-mi='" + target + "']")
        await page.wait_for_timeout(900)
        mapJump = await page.evaluate(r"""(mi) => {
          const el = document.querySelector('.user-row[data-mi="' + mi + '"]');
          const wrap = document.getElementById("convWrap").getBoundingClientRect();
          const r = el.getBoundingClientRect();
          return { inView: r.top >= wrap.top - 4 && r.top <= wrap.bottom, top: Math.round(r.top - wrap.top) };
        }""", target)
        print("收起态点短线(第", target, "条):", json.dumps(mapJump, ensure_ascii=False))
        if not mapJump["inView"]:
            bad.append("收起态点短线没跳到那条消息: " + json.dumps(mapJump, ensure_ascii=False))

        # 导航里不该再有「−」这种删除功能(行尾只剩那条横线)
        gone = await page.evaluate("""() => ({
          x: document.querySelectorAll("#convNavList .cnav-x").length,
          dashes: document.querySelectorAll("#convNavList .cnav-d").length,
        })""")
        print("删除功能残留:", json.dumps(gone))
        if gone["x"]:
            bad.append("导航里还留着「−」删除按钮: " + json.dumps(gone))

        # 超长输入: 气泡有高度上限、内部不滚, 末尾截断成「…」(line-clamp)
        await page.evaluate(r"""() => {
          transcript = []; convEl.innerHTML = ""; clearConvNav();
          const long = "很长的输入内容, 用来验证气泡的截断".repeat(200);
          transcript.push({ role: "user", text: long });
          addUserMsg(long, 0);
          transcript.push({ role: "assistant", text: "回答" });
          renderStoredMessage(transcript[1], 1);
          transcript.push({ role: "user", text: "第二条输入" });
          addUserMsg("第二条输入", 2);
          transcript.push({ role: "assistant", text: "第二条回答" });
          renderStoredMessage(transcript[3], 3);
          scrollBottom(); syncScrollbar();
        }""")
        await page.wait_for_timeout(400)
        await page.hover("#convNavCard")
        await page.wait_for_timeout(500)
        await page.hover("#convNavList .cnav-item[data-mi='0']")
        await page.wait_for_timeout(400)
        big = await page.evaluate(READ)
        print("超长输入的气泡:", json.dumps({"shown": big["tipShown"], "h": big["tipRect"]["h"] if big["tipRect"] else None,
                                          "w": big["tipRect"]["w"] if big["tipRect"] else None,
                                          "sbW": big["tipScrollbarW"], "clamp": big["tipClamp"],
                                          "textLen": len(big["tipText"])}, ensure_ascii=False))
        if not big["tipShown"]:
            bad.append("超长输入时气泡没出来")
        else:
            if big["tipRect"]["h"] > 242:
                bad.append("气泡没有高度上限: " + str(big["tipRect"]["h"]))
            if big["tipScrollbarW"] != 0 or big["tipScrollbarWidth"] != "none":
                bad.append("超长输入时气泡内部出了滚动条: " +
                           json.dumps([big["tipScrollbarW"], big["tipScrollbarWidth"]]))
            if big["tipClamp"] in (None, "none"):
                bad.append("气泡没有做截断(line-clamp): " + str(big["tipClamp"]))
        await page.mouse.move(700, 400)
        await page.wait_for_timeout(300)
        gone = await page.evaluate(READ)
        if gone["tipShown"]:
            bad.append("鼠标离开那一行后气泡没收掉")

        # 空会话 / 只有一条输入 -> 不显示, 正文也不留高亮
        await page.evaluate(r"""() => {
          transcript = []; convEl.innerHTML = ""; clearConvNav();
          transcript.push({ role: "user", text: "只有一条" });
          addUserMsg("只有一条", 0);
        }""")
        await page.wait_for_timeout(250)
        one = await page.evaluate(READ)
        print("只有一条输入:", json.dumps({"hidden": one["hidden"], "marked": one["activeMarked"]},
                                        ensure_ascii=False))
        if not one["hidden"]:
            bad.append("只有一条输入时导航还占着地方")
        if one["activeMarked"]:
            bad.append("导航藏起来后正文还留着高亮: " + json.dumps(one["activeMarked"]))
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("CONV_NAV_OK (右侧输入导航: 两态行距/条目/框高/行尾位置完全一致, 什么都不动 —— 收起=透明无边框的一列横线, "
          "鼠标落在框上才展开成白卡片(只是变宽+露出文字), 悬停某行在卡片左边浮出完整输入的气泡(内部不滚, 超长截断成「…」), "
          "放不下时用「……」截断, active 与正文高亮同步跟随滚动, 点选项精确对齐判定线, 没有「−」删除功能)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
