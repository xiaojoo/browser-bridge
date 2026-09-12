"""侧栏底部站点选择校验: 平时显示为文字, 点击后在用户栏上方弹出选择框。

覆盖:
- 原生下拉已隐藏, 底部是一行文字(无边框/无底色) + 小箭头
- 用户栏无上边框、上方无分隔线, 原 chipSub 文本块已移除
- 点击文字: 弹框出现在文字上方, 条目数=站点数, 当前站点带 ✓
- 选择其它站点: 弹框关闭且目标站点值改变; Esc / 点击外部 也能关闭
- 站名很长时文字与弹框条目都是单行省略, 不换行、不撑高
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

STRUCT = """() => {
  const g = (id) => document.getElementById(id);
  const sel = g("selProvider"), btn = g("btnProvider"), picker = g("providerPicker");
  const prof = document.querySelector(".profile"), gear = document.querySelector(".profile .ws-icon");
  const sb = document.querySelector(".sidebar");
  const cs = (el) => el ? getComputedStyle(el) : null;
  const r = (el) => el ? el.getBoundingClientRect() : null;
  return {
    selectHidden: !sel || sel.offsetParent === null,
    btnInProfile: !!(prof && btn && prof.contains(btn)),
    btnText: btn ? btn.textContent.trim() : null,
    btnBg: cs(btn) ? cs(btn).backgroundColor : null,
    btnBorder: cs(btn) ? cs(btn).borderTopWidth : null,
    btnStyle: cs(btn) ? cs(btn).whiteSpace + " / " + cs(btn).textOverflow : null,
    hasDot: !!document.querySelector(".profile .dot"),
    hasCaret: !!document.querySelector(".p-pick-caret"),
    ariaExpanded: btn ? btn.getAttribute("aria-expanded") : null,
    pickerClosed: picker ? !picker.classList.contains("open") : null,
    pickerBox: picker ? r(picker).height : null,
    profileBorderTop: cs(prof) ? cs(prof).borderTopWidth : null,
    prevIsDivider: !!(prof && prof.previousElementSibling
                      && prof.previousElementSibling.classList.contains("divider")),
    oldSubGone: !document.getElementById("chipSub") && !document.querySelector(".p-name"),
    optionCount: sel ? sel.options.length : 0,
    selectValue: sel ? sel.value : null,
    sidebarOverflowX: sb.scrollWidth - sb.clientWidth,
    profileBottom: prof ? Math.round(r(prof).bottom) : 0,
    sidebarBottom: Math.round(r(sb).bottom),
    btnStartTop: Math.round(r(document.getElementById("btnStart")).top),
    btnStartBottom: Math.round(r(document.getElementById("btnStart")).bottom),
    profileRight: prof ? Math.round(r(prof).right) : 0,
    gearInProfile: !!gear,
    sidebarLeft: Math.round(r(sb).left),
    avatarLeft: Math.round(r(document.querySelector(".profile .avatar")).left),
    btnStartLeft: Math.round(r(document.getElementById("btnStart")).left),
    pickPad: cs(btn) ? (cs(btn).paddingTop + " " + cs(btn).paddingRight + " " + cs(btn).paddingBottom + " " + cs(btn).paddingLeft) : null,
    startPad: (() => {
      const b = document.getElementById("btnStart"), c = getComputedStyle(b);
      return c.paddingTop + " " + c.paddingRight + " " + c.paddingBottom + " " + c.paddingLeft;
    })(),
    pickRadius: cs(btn) ? cs(btn).borderTopLeftRadius : null,
    startRadius: (() => getComputedStyle(document.getElementById("btnStart")).borderTopLeftRadius)(),
    btnRight: btn ? Math.round(r(btn).right) : 0,
  };
}"""

OPEN = """() => {
  const g = (id) => document.getElementById(id);
  const btn = g("btnProvider"), picker = g("providerPicker");
  const items = Array.from(picker.querySelectorAll(".picker-item[data-id]"));
  const rb = btn.getBoundingClientRect(), rp = picker.getBoundingClientRect();
  return {
    open: picker.classList.contains("open"),
    ariaExpanded: btn.getAttribute("aria-expanded"),

    items: items.length,
    labels: items.map(i => (i.querySelector(".nm") || i).textContent.trim()),
    activeIdx: items.findIndex(i => i.classList.contains("active")),
    hasTick: items.some(i => !!i.querySelector(".tick")),
    avatars: items.map(i => { const a = i.querySelector(".pi-av"); return a ? a.textContent.trim() : null; }),
    cardStyle: (() => {
      const cs = getComputedStyle(picker);
      return [cs.borderTopWidth, cs.borderTopLeftRadius, cs.boxShadow === "none" ? "no-shadow" : "shadow"].join(" / ");
    })(),
    rowH: items.length ? Math.round(items[0].getBoundingClientRect().height) : 0,
    sepCount: picker.querySelectorAll(".picker-sep").length,
    itemGap: items.length > 1
      ? Math.round(items[1].getBoundingClientRect().top - items[0].getBoundingClientRect().bottom) : 0,
    settingsLabel: (() => {
      const s = picker.querySelector('.picker-item[data-act="settings"]');
      return s ? s.textContent.trim() : null;
    })(),
    settingsIcon: !!picker.querySelector('.picker-item[data-act="settings"] .pi-ic svg'),
    pickerAboveBtn: rp.bottom <= rb.top + 1,
    pickerBottom: Math.round(rp.bottom),
    btnTop: Math.round(rb.top),
    pickerInSidebar: rp.top >= 0 && rp.left >= 0,
    pageX: Math.round(rp.left),
  };
}"""

LONGNAME = """(LONG) => {
  const g = (id) => document.getElementById(id);
  const picker = g("providerPicker");
  const nm = picker.querySelector(".picker-item .nm");
  const name = g("chipName");
  const h = (el) => Math.round(el.getBoundingClientRect().height * 100) / 100;
  const res = { before: { item: h(nm), name: h(name) } };
  const o1 = nm.textContent, o2 = name.textContent;
  nm.textContent = LONG; name.textContent = LONG;
  res.after = { item: h(nm), name: h(name) };
  res.style = {
    item: getComputedStyle(nm).whiteSpace + " / " + getComputedStyle(nm).textOverflow,
    name: getComputedStyle(name).whiteSpace + " / " + getComputedStyle(name).textOverflow,
  };
  res.truncated = { item: nm.scrollWidth > nm.clientWidth + 1, name: name.scrollWidth > name.clientWidth + 1 };
  nm.textContent = o1; name.textContent = o2;
  return res;
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(WS_STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)

        st = await page.evaluate(STRUCT)
        print("结构:", json.dumps(st, ensure_ascii=False))

        if not st["selectHidden"] or not st["btnInProfile"]:
            bad.append("底部文字按钮未替换原生下拉")
        if not st["btnText"]:
            bad.append("底部文字为空")
        if st["btnBg"] not in ("rgba(0, 0, 0, 0)", "transparent"):
            bad.append("默认不是文字样式, 有底色: " + str(st["btnBg"]))
        if st["btnBorder"] != "0px":
            bad.append("默认不是文字样式, 有边框: " + str(st["btnBorder"]))
        if st["hasDot"]:
            bad.append("左下用户栏的绿点没去掉")
        if st["hasCaret"]:
            bad.append("站点文字后面的下拉箭头没去掉")
        if not st["pickerClosed"] or st["pickerBox"]:
            bad.append("默认就显示了弹框")
        if st["profileBorderTop"] != "0px":
            bad.append("用户栏仍有上边框")
        if st["prevIsDivider"]:
            bad.append("用户栏上方仍是分隔线")
        if not st["oldSubGone"]:
            bad.append("原 chipSub/.p-name 文本块未移除")
        if st["ariaExpanded"] != "false":
            bad.append("aria-expanded 初值不是 false")
        if st["gearInProfile"]:
            bad.append("用户栏里还有齿轮(设置入口应已移入弹框)")
        if st["btnRight"] > st["profileRight"] - 6:
            bad.append("文字按钮超出用户栏右侧内边距")
        if abs((st["avatarLeft"] - st["sidebarLeft"]) - (st["btnStartLeft"] - st["sidebarLeft"])) > 1:
            bad.append(f"用户栏左侧内边距与启动按钮不一致 "
                       f"(avatarLeft={st['avatarLeft']}, btnStartLeft={st['btnStartLeft']})")
        if st["pickPad"] != st["startPad"]:
            bad.append(f"站点文字按钮 padding 与启动按钮不一致: {st['pickPad']} vs {st['startPad']}")
        if st["pickRadius"] != st["startRadius"]:
            bad.append(f"圆角与启动按钮不一致: {st['pickRadius']} vs {st['startRadius']}")
        if st["sidebarOverflowX"] > 1:
            bad.append("侧栏横向溢出")
        if st["btnStartTop"] < st["profileBottom"] + 3:
            bad.append(f"启动按钮没在用户栏下方留出间距 (btnStartTop={st['btnStartTop']}, profileBottom={st['profileBottom']})")
        if st["sidebarBottom"] - st["btnStartBottom"] > 12:
            bad.append(f"启动按钮未贴住侧栏底部 (btnStartBottom={st['btnStartBottom']}, sidebarBottom={st['sidebarBottom']})")

        # 点击文字 -> 上方弹出选择框
        await page.click("#btnProvider")
        await page.wait_for_timeout(250)
        op = await page.evaluate(OPEN)
        print("打开:", json.dumps(op, ensure_ascii=False))
        if not op["open"] or op["ariaExpanded"] != "true":
            bad.append("点击后弹框未打开")
        if op["items"] != st["optionCount"] or op["items"] < 2:
            bad.append(f"弹框条目数 {op['items']} != 站点数 {st['optionCount']}")
        if not op["pickerAboveBtn"]:
            bad.append(f"弹框没有出现在文字上方 (pickerBottom={op['pickerBottom']}, btnTop={op['btnTop']})")
        if not op["pickerInSidebar"] or op["pageX"] < 0:
            bad.append("弹框跑出侧栏范围")
        if op["activeIdx"] < 0 or not op["hasTick"]:
            bad.append("当前站点未高亮/无 ✓")
        if not all(op["avatars"]):
            bad.append("弹框条目缺少站点头像: " + json.dumps(op["avatars"], ensure_ascii=False))
        if op["cardStyle"] != "0px / 14px / shadow":
            bad.append("弹框卡片样式不符(应为无边框/圆角14px/投影): " + op["cardStyle"])
        if op["rowH"] < 36:
            bad.append(f"弹框条目过矮: {op['rowH']}px")
        if op["itemGap"] < 2:
            bad.append(f"弹框选项之间没有上下间隙: {op['itemGap']}px")


        # 弹框里的"系统设置"入口: 分隔线 + 图标 + 点击打开设置弹层
        if op["sepCount"] != 1 or op["settingsLabel"] != "系统设置" or not op["settingsIcon"]:
            bad.append("弹框里缺少分隔线/系统设置入口: " + json.dumps(
                {k: op[k] for k in ("sepCount", "settingsLabel", "settingsIcon")}, ensure_ascii=False))
        await page.click('.picker-item[data-act="settings"]')
        await page.wait_for_timeout(350)
        sres = await page.evaluate("""() => ({
          overlay: document.getElementById("setOverlay").classList.contains("show"),
          pickerOpen: document.getElementById("providerPicker").classList.contains("open"),
        })""")
        print("设置入口:", sres)
        if not sres["overlay"] or sres["pickerOpen"]:
            bad.append("点系统设置没有打开设置弹层(或弹框没关闭)")
        await page.click("#setClose")               # 关掉设置弹层(走真实关闭按钮)
        await page.wait_for_timeout(300)
        await page.click("#btnProvider")            # 重新打开, 继续后面的用例
        await page.wait_for_timeout(250)

        # 长站名单行省略
        ln = await page.evaluate(LONGNAME, "超长站点名称VeryLongProviderName" * 4)
        print("长名:", json.dumps(ln, ensure_ascii=False))
        for key in ("item", "name"):
            if abs(ln["after"][key] - ln["before"][key]) > 1:
                bad.append(f"长站名把 {key} 撑高了 {ln['before'][key]} -> {ln['after'][key]}")
            if not ln["truncated"][key]:
                bad.append(f"长站名在 {key} 上未用省略号截断")
        if "nowrap" not in ln["style"]["item"] or "nowrap" not in ln["style"]["name"]:
            bad.append("长站名样式不是单行: " + json.dumps(ln["style"], ensure_ascii=False))

        # 选择另一项
        items = await page.query_selector_all(".picker-item[data-id]")   # 只取站点项(排除"系统设置")
        target_id = await items[-1].get_attribute("data-id")
        await items[-1].click()
        await page.wait_for_timeout(250)
        after = await page.evaluate("""() => ({
          value: document.getElementById("selProvider").value,
          open: document.getElementById("providerPicker").classList.contains("open"),
          aria: document.getElementById("btnProvider").getAttribute("aria-expanded"),
          name: document.getElementById("chipName").textContent.trim(),
        })""")
        print("选中后:", json.dumps(after, ensure_ascii=False))
        if after["value"] != target_id:
            bad.append(f"选择未生效: {after['value']} != {target_id}")
        if after["open"] or after["aria"] != "false":
            bad.append("选择后弹框未关闭")

        # Esc 关闭
        await page.click("#btnProvider")
        await page.wait_for_timeout(200)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        if await page.evaluate("() => document.getElementById('providerPicker').classList.contains('open')"):
            bad.append("Esc 未关闭弹框")

        # 点击外部关闭
        await page.click("#btnProvider")
        await page.wait_for_timeout(200)
        await page.mouse.click(900, 300)
        await page.wait_for_timeout(200)
        if await page.evaluate("() => document.getElementById('providerPicker').classList.contains('open')"):
            bad.append("点击外部未关闭弹框")

        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("SIDEBAR_PICKER_OK (默认文字显示, 点击在其上方弹出选择框)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
