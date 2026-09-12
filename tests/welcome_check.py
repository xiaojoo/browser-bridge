"""校验空状态: 欢迎区居中显示、composer 移入其中、Chat/World 模式切换。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


async def main() -> int:
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        await page.add_init_script(WS_STUB)
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto("http://127.0.0.1:8765", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1500)
        r = await page.evaluate("""() => {
          const wEl = document.getElementById('welcome');
          const b = wEl.getBoundingClientRect();
          const main = document.querySelector('.main').getBoundingClientRect();
          return {
            welcomeDisplay: wEl.style.display || getComputedStyle(wEl).display,
            composerInSlot: !!document.getElementById('composerSlot').querySelector('.composer'),
            composerWrapDisplay: getComputedStyle(document.querySelector('.composer-wrap')).display,
            convDisplay: getComputedStyle(document.getElementById('convWrap')).display,
            chatActive: document.getElementById('modeChat').classList.contains('active'),
            worldActive: document.getElementById('modeWorld').classList.contains('active'),
            label: document.getElementById('welcomeModeLabel').textContent,
            centerDx: Math.abs((b.left + b.width/2) - (main.left + main.width/2)),
            centerDy: Math.abs((b.top + b.height/2) - (main.top + main.height/2)),
            inputAlign: getComputedStyle(document.getElementById('input')).textAlign,
            settingsGone: !document.getElementById('btnSettings'),
            topTools: document.querySelectorAll('.top-right .icon').length,
            noTaskToggle: !document.getElementById('btnTaskToggle'),
            noModeTag: !document.getElementById('modeTag'),
            heroW: document.querySelector('.w-hero').getBoundingClientRect().width,
            heroH: document.querySelector('.w-hero').getBoundingClientRect().height,
            wsBlockHidden: document.getElementById('wsBlock').hidden,
            wsTreeVisible: !!document.getElementById('wsTree').offsetParent,
            histMaxH: getComputedStyle(document.getElementById('convList')).maxHeight,
            histHeight: getComputedStyle(document.getElementById('convList')).height,
            histRatio: Math.round(document.getElementById('convList').getBoundingClientRect().height
                                  / document.querySelector('.sidebar').getBoundingClientRect().height * 100) / 100
          };
        }""")
        print("before:", json.dumps(r, ensure_ascii=False))
        await page.click("#modeWorld")
        await page.wait_for_timeout(300)
        r2 = await page.evaluate("""() => ({
          label: document.getElementById('welcomeModeLabel').textContent,
          worldActive: document.getElementById('modeWorld').classList.contains('active'),
          chatActive: document.getElementById('modeChat').classList.contains('active'),
          placeholder: document.getElementById('input').dataset.placeholder,
          sub: document.getElementById('welcomeSub').textContent,
          wsBlockHidden: document.getElementById('wsBlock').hidden,
          wsTreeVisible: !!document.getElementById('wsTree').offsetParent,
          histMaxH: getComputedStyle(document.getElementById('convList')).maxHeight,
          histHeight: getComputedStyle(document.getElementById('convList')).height,
          histRatio: Math.round(document.getElementById('convList').getBoundingClientRect().height
                                / document.querySelector('.sidebar').getBoundingClientRect().height * 100) / 100
        })""")
        await page.click("#modeChat")
        await page.wait_for_timeout(250)
        rChat = await page.evaluate("""() => ({
          wsBlockHidden: document.getElementById('wsBlock').hidden,
          wsTreeVisible: !!document.getElementById('wsTree').offsetParent,
          histMaxH: getComputedStyle(document.getElementById('convList')).maxHeight,
          histHeight: getComputedStyle(document.getElementById('convList')).height,
          histRatio: Math.round(document.getElementById('convList').getBoundingClientRect().height
                                / document.querySelector('.sidebar').getBoundingClientRect().height * 100) / 100
        })""")
        print("back  :", json.dumps(rChat, ensure_ascii=False))
        print("world :", json.dumps(r2, ensure_ascii=False))
        # 侧栏: 窄视口自动隐藏 + 按钮展开
        await page.set_viewport_size({"width": 700, "height": 800})
        await page.wait_for_timeout(300)
        rw = await page.evaluate("()=>document.querySelector('.sidebar').getBoundingClientRect().width")
        rwHero = await page.evaluate("()=>document.querySelector('.w-hero').getBoundingClientRect().width")
        await page.click("#btnSidebar")
        await page.wait_for_timeout(300)
        rs = await page.evaluate("()=>document.querySelector('.sidebar').getBoundingClientRect().width")
        await page.click("#btnSidebar")
        await page.wait_for_timeout(200)
        rh = await page.evaluate("()=>document.querySelector('.sidebar').getBoundingClientRect().width")
        await page.set_viewport_size({"width": 320, "height": 800})
        await page.wait_for_timeout(300)
        rwHeroMin = await page.evaluate("()=>document.querySelector('.w-hero').getBoundingClientRect().width")
        await page.set_viewport_size({"width": 1400, "height": 900})
        await page.wait_for_timeout(300)
        await browser.close()
    assert not errors, f"JS 报错: {errors}"
    assert r["welcomeDisplay"] == "flex", "欢迎区未显示"
    assert r["composerInSlot"], "composer 未移入欢迎区"
    assert r["convDisplay"] == "none", "空态应隐藏消息区"
    assert r["chatActive"] and not r["worldActive"], "默认应为 Chat 模式"
    assert r["centerDx"] < 30 and r["centerDy"] < 40, f"未居中 dx={r['centerDx']} dy={r['centerDy']}"
    assert r["inputAlign"] == "left", f"输入应为左对齐: {r['inputAlign']}"
    assert r["settingsGone"] and r["topTools"] == 1, "设置入口不应再出现在顶栏/用户栏(已移入站点弹框)"
    assert r["noTaskToggle"] and r["noModeTag"], "应已移除 工程任务/逐字流式 按钮"
    assert abs(r["heroW"] - 840) < 2, f"宽视口 w-hero 应为 840: {r['heroW']}"
    assert 321 < rwHero < 839, f"中窄视口应流式收缩(先缩空白): {rwHero}"
    assert abs(rwHeroMin - 320) < 2, f"极窄视口应 clamp 到 320: {rwHeroMin}"
    assert rw < 4, f"窄视口应自动隐藏侧栏: {rw}"
    assert rs > 200, f"按钮应能展开侧栏: {rs}"
    assert rh < 4, f"再点应收起: {rh}"
    assert r2["worldActive"] and not r2["chatActive"], "World 切换失败"
    assert "World" in r2["label"] and "工程" in r2["sub"], "World 文案错误"
    assert r["wsBlockHidden"] and not r["wsTreeVisible"], "Chat 模式不该显示侧栏工作区"
    assert not r2["wsBlockHidden"] and r2["wsTreeVisible"], "World 模式应显示侧栏工作区"
    assert rChat["wsBlockHidden"] and not rChat["wsTreeVisible"], "切回 Chat 后工作区应重新隐藏"
    # 会话列表高度: 没有工作区时撑满, 有工作区时最多占一半(给文件树留位置)
    assert r["histMaxH"] == "none" and r["histRatio"] > 0.5, \
        f"Chat 模式下会话列表没有撑满: {r['histMaxH']} / {r['histRatio']}"
    assert r2["histMaxH"] != "none" and r2["histRatio"] < 0.5, \
        f"World 模式下会话列表没限制在一半以内: {r2['histMaxH']} / {r2['histRatio']}"
    assert rChat["histMaxH"] == "none" and rChat["histRatio"] > 0.5, \
        f"切回 Chat 后会话列表应重新撑满: {rChat['histMaxH']} / {rChat['histRatio']}"
    print("WELCOME_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
