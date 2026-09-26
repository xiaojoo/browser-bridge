"""设置里「规划模型类型」必须是**自绘**的下拉, 不是操作系统画的那一片。

起因: 原生 select 展开后字号/行距/高亮/勾选全由系统决定, 和全站(已对齐 ChatGPT 档位)不是一套。
做法沿用站点的选择器: 原生 select 留着当状态容器(代码里 6 处读它的 .value), 可见控件是按钮 +
菜单(菜单复用侧栏行菜单 .rowmenu, fixed 定位, 所以不会被设置面板 .dlg-body 的 overflow 裁掉)。

盯五条(每条都量真实渲染, 不看声明):
  1) 那个 select 不可见, 而鼠标落在字段位置上命中的是**按钮**(命中区=看得见的那个控件);
  2) 按钮外框和同面板里始终可见的输入框逐项相等(高/字号/行距/边框/圆角/上下内边距/底色);
  3) 点开的是 DOM 里的菜单: 项数 = option 数、宽度 = 按钮宽、当前项有「✓」、字号 14/20、
     整块在视口内、项没被菜单自己的框裁掉 —— 原生 select 的下拉在 DOM 里根本不存在, 所以这条
     只有自绘才可能绿(改回原生就是红的);
  3b) 选项上下的间隙**照站点选择器**(.picker 的 gap / padding), 且只加在这个菜单的修饰类上
     (摘掉修饰类必须回到侧栏行菜单原来的留白 —— 否则就是顺手改了别的界面);
  4) 选一项之后: 隐藏 select 的 value 变了、依赖它的 .api-only 行真的出现/消失、按钮文字跟着换;
  5) 键盘 Enter 也能开、Esc 能关; 全程无 JS 报错。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"

BOX = """() => {
  const pick = document.getElementById("setTypeBtn");
  const inp = document.getElementById("setRepo");
  const sel = document.getElementById("setType");
  const f = (el) => { const c = getComputedStyle(el), r = el.getBoundingClientRect();
    return { h: Math.round(r.height), fs: c.fontSize, lh: c.lineHeight, bw: c.borderTopWidth,
             bc: c.borderTopColor, br: c.borderTopLeftRadius, pt: c.paddingTop,
             pb: c.paddingBottom, bg: c.backgroundColor }; };
  const a = f(pick), b = f(inp);
  const r = pick.getBoundingClientRect();
  const hit = document.elementFromPoint(Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2));
  return { pick: a, input: b,
           diff: Object.keys(a).filter(k => String(a[k]) !== String(b[k])),
           selH: Math.round(sel.getBoundingClientRect().height), selHidden: sel.hidden,
           hitTag: hit ? hit.closest("button,select").tagName.toLowerCase() : "none",
           hitId: hit ? (hit.closest("button,select") || {}).id : "" };
}"""

MENU = """() => {
  const m = document.getElementById("rowMenu"), btn = document.getElementById("setTypeBtn");
  const sel = document.getElementById("setType");
  if (!m.classList.contains("open")) return { open: false };
  const mr = m.getBoundingClientRect(), br = btn.getBoundingClientRect();
  const items = Array.from(m.querySelectorAll(".picker-item"));
  const tops = items.map(i => Math.round(i.getBoundingClientRect().top - mr.top));
  const hs = items.map(i => Math.round(i.getBoundingClientRect().height));
  const between = tops.slice(1).map((t, i) => t - (tops[i] + hs[i]));
  const house = getComputedStyle(document.getElementById("providerPicker"));
  const base = getComputedStyle(m);
  m.classList.remove("pick");
  const barePad = getComputedStyle(m).paddingTop;      // 修饰类摘掉之后 = 侧栏行菜单那套
  if (items.length) m.classList.add("pick");
  return { open: true, n: items.length, options: sel.options.length,
           btnW: Math.round(br.width), menuW: Math.round(mr.width),
           ticks: items.map(i => (i.querySelector(".tick") ? "1" : "0")),
           fs: items.length ? getComputedStyle(items[0]).fontSize + "/" + getComputedStyle(items[0]).lineHeight : "",
           inViewport: mr.left >= 0 && mr.top >= 0 && mr.right <= innerWidth + 1 && mr.bottom <= innerHeight + 1,
           clipped: items.some(i => { const r = i.getBoundingClientRect();
             return r.bottom > mr.bottom + 1 || r.right > mr.right + 1; }),
           between: between, itemH: hs[0] || 0,
           padTop: base.paddingTop, padBottom: base.paddingBottom,
           houseGap: house.rowGap, housePad: house.paddingTop, barePad: barePad };
}"""

STATE = """() => ({
  sel: document.getElementById("setType").value,
  btnText: document.querySelector("#setTypeBtn .pick-t").textContent,
  apiRows: Array.from(document.querySelectorAll("#setOverlay .api-only"))
    .filter(r => r.style.display !== "none").length,
  visibleSelects: Array.from(document.querySelectorAll("select"))
    .filter(s => !s.hidden && s.getBoundingClientRect().height > 0).map(s => s.id),
})"""


async def main() -> int:
    bad, errs = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("pageerror", lambda e: errs.append(str(e)))
        try:
            await page.goto(BASE, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(1500)
            await page.evaluate("() => openSettings()")
            await page.wait_for_timeout(900)

            box = await page.evaluate(BOX)
            print("外框对比:", json.dumps(box, ensure_ascii=False))
            if box["selH"] > 0 or not box["selHidden"]:
                bad.append("原生 select 还看得见(高 %spx)—— 那弹出来就是系统画的那一片" % box["selH"])
            if box["hitTag"] != "button" or box["hitId"] != "setTypeBtn":
                bad.append("字段位置上命中的不是自绘按钮: %s#%s" % (box["hitTag"], box["hitId"]))
            if box["diff"]:
                bad.append("按钮外框和旁边的输入框不一致, 差在这些项: " + json.dumps(
                    {k: [box["pick"][k], box["input"][k]] for k in box["diff"]}, ensure_ascii=False))

            st0 = await page.evaluate(STATE)
            await page.click("#setTypeBtn")
            await page.wait_for_timeout(350)
            menu = await page.evaluate(MENU)
            print("菜单:", json.dumps(menu, ensure_ascii=False))
            if not menu.get("open"):
                bad.append("点按钮没有浮出菜单(自绘菜单没做出来)")
            else:
                if menu["n"] != menu["options"]:
                    bad.append("菜单项数和 option 数不一致: %s vs %s" % (menu["n"], menu["options"]))
                if menu["menuW"] < menu["btnW"]:
                    bad.append("菜单比字段窄: 菜单 %s 按钮 %s" % (menu["menuW"], menu["btnW"]))
                if menu["ticks"].count("1") != 1:
                    bad.append("当前选中项没有且只有一个「✓」: " + "".join(menu["ticks"]))
                if menu["fs"] != "14px/20px":
                    bad.append("菜单项字号不是全站那一档(14/20): " + menu["fs"])
                if not menu["inViewport"]:
                    bad.append("菜单跑出视口了")
                if menu["clipped"]:
                    bad.append("菜单项被菜单自己的框裁掉了")
                # 选项上下的间隙: 照站点选择器(.picker)那一套, 不另发明数
                hgap = float(str(menu["houseGap"]).replace("px", "") or 0)
                if any(abs(b - hgap) > 0.5 for b in menu["between"]) or not menu["between"]:
                    bad.append("选项之间的间隙和站点选择器不一致: 选项间 %s, .picker 的 gap %s"
                               % (json.dumps(menu["between"]), menu["houseGap"]))
                if menu["padTop"] != menu["housePad"] or menu["padBottom"] != menu["housePad"]:
                    bad.append("菜单上下留白和站点选择器不一致: %s/%s, .picker 是 %s"
                               % (menu["padTop"], menu["padBottom"], menu["housePad"]))
                if menu["barePad"] == menu["padTop"]:
                    bad.append("这份间距是加在 .rowmenu 本体上的(会连侧栏那个行菜单一起改): "
                               "摘掉修饰类还是 %s" % menu["padTop"])

            items = await page.query_selector_all("#rowMenu .picker-item")
            await items[2].click()
            await page.wait_for_timeout(400)
            st1 = await page.evaluate(STATE)
            print("选「本地模型」后:", json.dumps(st1, ensure_ascii=False)[:160])
            if st1["sel"] != "local":
                bad.append("选完没写回 select: " + st1["sel"])
            if st1["apiRows"] <= st0["apiRows"]:
                bad.append("选完依赖项没跟着变(.api-only 行还是 %s 个)" % st1["apiRows"])
            if "本地模型" not in st1["btnText"]:
                bad.append("按钮文字没换成所选那项: " + st1["btnText"][:24])
            if st1["visibleSelects"]:
                bad.append("全站还有看得见的原生 select: " + json.dumps(st1["visibleSelects"], ensure_ascii=False))
            if await page.evaluate("() => document.getElementById('rowMenu').classList.contains('open')"):
                bad.append("选完菜单没关")

            await page.focus("#setTypeBtn")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(300)
            km = await page.evaluate(MENU)
            print("键盘打开:", json.dumps(km, ensure_ascii=False))
            if not km.get("open"):
                bad.append("键盘 Enter 打不开菜单")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(250)
            if await page.evaluate("() => document.getElementById('rowMenu').classList.contains('open')"):
                bad.append("Esc 关不掉菜单")
        finally:
            if errs:
                bad.append("JS 报错: " + " | ".join(errs[:3]))
            await browser.close()
    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("SETTINGS_PICK_OK (规划模型类型是自绘下拉: 外框和输入框逐项一致, 菜单在 DOM 里且当前项带勾, "
          "选完写回 select 并带动依赖行, 键盘可开可关; 全站没有看得见的原生 select)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
