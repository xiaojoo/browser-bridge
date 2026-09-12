"""设置(planner)是全局的, 而且选「本地模型」时必须把地址/模型一起存下去。

真实故障: 只填类型不带地址 —— 界面上选了「本地模型」(或点了「检测本地模型」自动填好)再保存,
发出去的却是 {planner: {type: "local"}}, 地址/模型名被丢掉; 于是落盘时用的是默认的云端地址,
报"本地模型调用失败/需要先配置规划模型", 看起来就像"设置换了窗口就失效"。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"

SAVED = {"planner": {"type": "local", "api_base": "http://127.0.0.1:1234/v1",
                     "api_model": "qwen2.5-coder-7b-instruct", "api_temp": 0.2, "has_key": False,
                     "api_key": ""},
         "engine": {"repo": "", "test_cmd": "", "confirm_apply": "1"},
         "file": "H:\\browser-bridge\\.bridge_settings.json"}

STUB = r"""(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.__saved = window.__saved || __SAVED__;
  window.__posted = [];
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/providers") === 0)
      return json({ default: "chatgpt", providers: [{ id: "chatgpt", name: "ChatGPT Web" }] });
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/api/local-models") === 0)
      return json({ ok: true, found: [
        { base: "http://127.0.0.1:1234/v1", models: ["qwen2.5-coder-7b-instruct", "llama-3.1-8b"] }] });
    if (s.indexOf("/api/settings/test") === 0)
      return json({ ok: true, models: ["qwen2.5-coder-7b-instruct"], key_source: "saved" });
    if (s.indexOf("/api/settings") === 0) {
      if (o && o.method === "POST") {
        const body = JSON.parse((o && o.body) || "{}");
        window.__posted.push(body);
        const pl = window.__saved.planner;
        // 服务端只更新 patch 里给的字段
        Object.keys(body.planner || {}).forEach(k => { if (body.planner[k] !== "") pl[k] = body.planner[k]; });
        if (body.planner && body.planner.api_key) pl.has_key = true;
        if (body.engine) window.__saved.engine = Object.assign(window.__saved.engine, body.engine);
      }
      return json(window.__saved);
    }
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();""".replace("__SAVED__", json.dumps(SAVED, ensure_ascii=False))


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: bad.append("页面报错: " + str(e)[:160]))
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)

        # 打开设置(左下角齿轮菜单里)
        await page.evaluate("() => openSettings()")
        await page.wait_for_timeout(400)
        note = await page.evaluate("() => document.getElementById('setNote').textContent")
        shown = await page.evaluate("() => document.getElementById('setResult').textContent")
        print("说明:", " ".join(note.split())[:90])
        print("回显:", shown[:110])
        if "全局" not in note:
            bad.append("设置面板没说明这是全局设置")
        if "全局" not in shown:
            bad.append("回显里没说清是不是全局生效: " + shown[:80])

        # 1) 点「检测本地模型」-> 保存: 地址/模型名必须一起发出去
        await page.evaluate("() => { document.getElementById('setKey').value = ''; }")
        await page.click("#setDetect")
        await page.wait_for_timeout(400)
        filled = await page.evaluate("""() => ({
          type: document.getElementById('setType').value,
          base: document.getElementById('setBase').value,
          model: document.getElementById('setModel').value,
        })""")
        print("检测后表单:", json.dumps(filled, ensure_ascii=False))
        if filled["type"] != "local" or "1234" not in filled["base"] or not filled["model"]:
            bad.append("「检测本地模型」没把表单填好: " + json.dumps(filled, ensure_ascii=False))

        await page.click("#setSave")
        await page.wait_for_timeout(500)
        posted = await page.evaluate("() => window.__posted")
        print("保存时发出的 planner:", json.dumps(posted[-1].get("planner") if posted else None,
                                              ensure_ascii=False))
        if not posted:
            bad.append("保存没有发出请求")
        else:
            pl = posted[-1].get("planner") or {}
            if pl.get("type") != "local":
                bad.append("没保存类型: " + json.dumps(pl, ensure_ascii=False))
            if "1234" not in str(pl.get("api_base", "")):
                bad.append("选本地模型却没把地址发出去(会被默认云端地址顶掉): " + json.dumps(pl, ensure_ascii=False))
            if not pl.get("api_model"):
                bad.append("选本地模型却没把模型名发出去: " + json.dumps(pl, ensure_ascii=False))
        toast = await page.evaluate("() => document.getElementById('toast').textContent")
        print("保存提示:", " ".join(toast.split())[:100])
        if "全局" not in toast:
            bad.append("保存后没有回显「全局生效/用的哪个模型」: " + toast[:60])

        # 2) 切成 API 模式: 地址/模型/Key 都要带上
        await page.evaluate("""() => {
          document.getElementById('setType').value = 'api';
          document.getElementById('setType').dispatchEvent(new Event('change'));
          document.getElementById('setBase').value = 'https://api.deepseek.com/v1';
          document.getElementById('setModel').value = 'deepseek-chat';
          document.getElementById('setKey').value = 'sk-test-123';
        }""")
        await page.click("#setSave")
        await page.wait_for_timeout(400)
        posted = await page.evaluate("() => window.__posted")
        pl = posted[-1].get("planner") or {}
        print("API 模式发出的 planner:", json.dumps({k: (v[:12] + "…" if k == "api_key" else v)
                                                 for k, v in pl.items()}, ensure_ascii=False))
        for k in ("api_base", "api_model", "api_key"):
            if not pl.get(k):
                bad.append(f"API 模式漏发 {k}: " + json.dumps(pl, ensure_ascii=False))

        # 3) 回显要跟着变成 API
        shown2 = await page.evaluate("() => document.getElementById('setResult').textContent")
        print("保存后回显:", " ".join(shown2.split())[:110])
        if "deepseek-chat" not in shown2:
            bad.append("保存后回显没跟上: " + shown2[:90])
        if "已填 Key" not in shown2:
            bad.append("回显没看出 Key 已经保存: " + shown2[:90])
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("SETTINGS_GLOBAL_OK (planner 设置全局保存: 本地模型/API 的地址·模型·Key 都随保存写下去, 面板回显当前生效项)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
