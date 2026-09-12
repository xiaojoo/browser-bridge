"""规划模型设置的「测试连接」校验:
1) 后端: 请求里带了 key 就用它(不用先保存); 没带才回落到已保存的 key
2) 前端: 点「测试连接」会把输入框里的 key 一起发出去, 成功显示绿色 ✓, 失败显示红色 ✗
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import planner, server, settings  # noqa: E402

URL = "http://127.0.0.1:8765/"


async def main() -> int:
    bad = []
    # ---------- 1) 后端: key 优先级 ----------
    captured = {}
    orig_list = planner.list_models

    async def fake_list(p):
        captured.clear()
        captured.update(p)
        return ["deepseek-flash", "deepseek-v4-pro"]

    planner.list_models = fake_list
    saved = settings.load()["planner"]
    had_key = saved.get("api_key") or ""
    try:
        # 先存一个"旧 key"
        settings.save_planner({"api_key": "sk-OLD-SAVED"})
        req = server.SettingsRequest(planner=server.SettingsPlanner(
            type="api", api_base="https://api.deepseek.com/v1", api_model="m",
            api_key="sk-TYPED-NEW", api_temp=0.3))
        r = await server.api_settings_test(req)
        print("带 key 请求:", json.dumps(r, ensure_ascii=False), "-> 实际用的 key:", captured.get("api_key"))
        if captured.get("api_key") != "sk-TYPED-NEW" or r.get("key_source") != "request":
            bad.append("请求里带 key 时没有优先用它: " + json.dumps(r, ensure_ascii=False))

        # 不带 key -> 回落到已保存的
        req2 = server.SettingsRequest(planner=server.SettingsPlanner(
            type="api", api_base="https://api.deepseek.com/v1", api_model="m"))
        r2 = await server.api_settings_test(req2)
        print("不带 key 请求:", json.dumps(r2, ensure_ascii=False), "-> 实际用的 key:", captured.get("api_key"))
        if captured.get("api_key") != "sk-OLD-SAVED" or r2.get("key_source") != "saved":
            bad.append("没带 key 时应回落到已保存的 key: " + json.dumps(r2, ensure_ascii=False))
    finally:
        planner.list_models = orig_list
        settings.save_planner({"api_key": had_key or "__CLEAR__"})

    # ---------- 1b) 本地模型发现(替身探测, 不依赖真的开着服务) ----------
    orig_find_list = planner.list_models

    async def fake_list(p, timeout: int = 30):
        if p.get("api_base") == "http://127.0.0.1:1234/v1":
            return ["qwen3-coder-30b-a3b-instruct"]
        raise planner.PlannerError("connection refused")

    planner.list_models = fake_list
    try:
        found = await planner.find_local()
        print("find_local:", json.dumps(found, ensure_ascii=False))
        if len(found) != 1 or found[0]["base"] != "http://127.0.0.1:1234/v1":
            bad.append("本地模型发现逻辑不对: " + json.dumps(found, ensure_ascii=False))
    finally:
        planner.list_models = orig_find_list

    # ---------- 2) 前端 ----------
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)
        await page.evaluate(r"""() => {
          window.__posted = [];
          window.__fail = false;
          post = async (url, body) => {
            window.__posted.push([url, body]);
            if (window.__fail) throw new Error("401 Unauthorized");
            return { ok: true, models: ["deepseek-flash", "deepseek-v4-pro"], key_source: "request" };
          };
          openSettings();
        }""")
        await page.wait_for_timeout(300)
        await page.evaluate(r"""() => {
          document.getElementById("setType").value = "api";
          document.getElementById("setType").dispatchEvent(new Event("change"));
          document.getElementById("setBase").value = "https://api.deepseek.com/v1";
          document.getElementById("setModel").value = "deepseek-v4-flash-vision-exp";
          document.getElementById("setKey").value = "sk-TYPED-NEW";
        }""")
        await page.click("#setTest")
        await page.wait_for_timeout(400)
        st = await page.evaluate("""() => {
          const b = document.getElementById("setResult");
          return { cls: b.className, color: getComputedStyle(b).color, text: b.textContent,
                   posted: window.__posted, hint: document.getElementById("setKeyHint").textContent };
        }""")
        print("测试成功:", json.dumps({k: st[k] for k in ("cls", "color", "text", "hint")}, ensure_ascii=False))
        body = (st["posted"] or [["", {}]])[-1][1] or {}
        if (body.get("planner") or {}).get("api_key") != "sk-TYPED-NEW":
            bad.append("测试连接没有把输入框里的 key 发出去: " + json.dumps(body, ensure_ascii=False))
        if "ok" not in st["cls"] or st["color"] != "rgb(43, 182, 115)":
            bad.append(f"成功提示不是绿色: {st['cls']} / {st['color']}")
        if not st["text"].startswith("✓"):
            bad.append("成功提示缺少 ✓: " + st["text"])
        if "尚未保存" not in st["hint"]:
            bad.append("测试通过后没有提示 key 尚未保存: " + st["hint"])

        # 「检测本地模型」: 一键填好类型/地址/模型
        await page.evaluate(r"""() => {
          const orig = window.fetch;
          window.fetch = (u, o) => String(u).indexOf("/api/local-models") === 0
            ? Promise.resolve(new Response(JSON.stringify({ ok: true, found: [
                { base: "http://127.0.0.1:1234/v1", models: ["some-embed", "qwen3-coder-30b-a3b-instruct"] }] }),
                { status: 200, headers: { "Content-Type": "application/json" } }))
            : orig(u, o);
        }""")
        await page.click("#setDetect")
        await page.wait_for_timeout(400)
        det = await page.evaluate("""() => ({
          type: document.getElementById("setType").value,
          base: document.getElementById("setBase").value,
          model: document.getElementById("setModel").value,
          cls: document.getElementById("setResult").className,
          text: document.getElementById("setResult").textContent,
        })""")
        print("检测本地模型:", json.dumps(det, ensure_ascii=False))
        if det["type"] != "local" or det["base"] != "http://127.0.0.1:1234/v1":
            bad.append("检测后没有切到本地模型: " + json.dumps(det, ensure_ascii=False))
        if det["model"] != "qwen3-coder-30b-a3b-instruct":
            bad.append("检测后没有优先挑编码模型: " + det["model"])
        if "ok" not in det["cls"]:
            bad.append("检测结果不是绿色成功提示: " + det["cls"])

        # 失败 -> 红色
        await page.evaluate("() => { window.__fail = true; }")
        await page.click("#setTest")
        await page.wait_for_timeout(400)
        st2 = await page.evaluate("""() => {
          const b = document.getElementById("setResult");
          return { cls: b.className, color: getComputedStyle(b).color, text: b.textContent };
        }""")
        print("测试失败:", json.dumps(st2, ensure_ascii=False))
        if "bad" not in st2["cls"] or st2["color"] != "rgb(229, 72, 77)":
            bad.append(f"失败提示不是红色: {st2['cls']} / {st2['color']}")

        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("SETTINGS_KEY_OK (测试连接用未保存的 key, 成功显示绿色 ✓, 失败红色 ✗)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
