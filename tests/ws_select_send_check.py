"""工作区勾选: 文件出现在输入框里, 点发送才发出; 「发送选中」按钮已移除。"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"
WS = ROOT / ".tmp" / "ws-sel"

STUB = r"""(() => {
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
    if (s.indexOf("/api/chat") === 0) {
      window.__sent = JSON.parse((o && o.body) || "{}");
      return json({ ok: true });
    }
    if (s.indexOf("/workspace/tree") === 0)
      return json({ ok: true, root: "H:\\tmp\\ws-sel", name: "ws-sel", default: "", custom: true,
                    items: [{ path: "a.txt", kind: "file", size: 11 },
                            { path: "sub/b.qml", kind: "file", size: 12 }] });
    if (s.indexOf("/workspace/file") === 0) {
      const p = decodeURIComponent(s.split("path=")[1] || "");
      return json({ path: p, binary: false, text: p.indexOf("a.txt") >= 0 ? "MARKER_A=1\n" : "// MARKER_B\n" });
    }
    if (s.indexOf("/api/workspace") === 0)
      return json({ ok: true, root: "H:\\tmp\\ws-sel", name: "ws-sel", default: "", custom: true });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""


async def main() -> int:
    bad = []
    shutil.rmtree(WS, ignore_errors=True)
    WS.mkdir(parents=True, exist_ok=True)
    (WS / "a.txt").write_text("MARKER_A=1\n", encoding="utf-8")
    (WS / "b.qml").write_text("// MARKER_B\n", encoding="utf-8")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.add_init_script(STUB)
            await page.goto(URL, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(700)
            await page.evaluate("() => switchMode('world')")
            await page.wait_for_timeout(1200)

            has_btn = await page.evaluate("() => !!document.getElementById('wsSend')")
            if has_btn:
                bad.append("「发送选中」按钮还在")
            # 勾选 a.txt
            await page.click('input[type="checkbox"][data-path="a.txt"]')
            await page.wait_for_timeout(300)
            st = await page.evaluate(r"""() => ({
              chips: Array.from(document.querySelectorAll("#chips .chiprow .nm")).map(e => e.textContent),
              icons: Array.from(document.querySelectorAll("#chips .chiprow .ic")).map(e => e.textContent),
              chipsShown: getComputedStyle(document.getElementById("chips")).display !== "none",
            })""")
            print("勾选后:", json.dumps(st, ensure_ascii=False))
            if st["chips"] != ["a.txt"]:
                bad.append("勾选后没有出现在输入框: " + json.dumps(st, ensure_ascii=False))
            if st["icons"] != ["📄"]:
                bad.append("输入框里的图标应该是文件图标(📄), 不是文件夹: " + json.dumps(st["icons"], ensure_ascii=False))
            # 再勾一个, 再取消一个
            await page.evaluate("() => setAllWorkspaceFolders(true)")
            await page.wait_for_timeout(200)
            await page.click('input[type="checkbox"][data-path="sub/b.qml"]')
            await page.wait_for_timeout(200)
            await page.click('input[type="checkbox"][data-path="sub/b.qml"]')
            await page.wait_for_timeout(200)
            chips2 = await page.evaluate("() => Array.from(document.querySelectorAll('#chips .chiprow .nm')).map(e => e.textContent)")
            if chips2 != ["a.txt"]:
                bad.append("取消勾选没有从输入框移出: " + json.dumps(chips2, ensure_ascii=False))

            # 打字 + 发送
            await page.evaluate("""() => { document.getElementById("input").value = "看看这个文件"; autoGrowInput(); }""")
            await page.click("#btnSend")
            await page.wait_for_timeout(900)
            sent = await page.evaluate("() => window.__sent || {}")
            print("发出的正文:", (sent.get("text") or "")[:120].replace("\n", " ⏎ "))
            if "【工作区文件 a.txt】" not in (sent.get("text") or ""):
                bad.append("消息里没有带上勾选的工作区文件: " + str(sent)[:200])
            if "MARKER_A=1" not in (sent.get("text") or ""):
                bad.append("消息里没有带上文件内容")
            after = await page.evaluate(r"""() => ({
              checked: Array.from(document.querySelectorAll('#wsTree input[type="checkbox"]')).filter(c => c.checked).length,
              chips: document.querySelectorAll("#chips .chiprow").length,
              bubbles: Array.from(document.querySelectorAll("#conv .user-bubble")).map(e => e.textContent.replace(/\n/g, " | ")),
            })""")
            print("发送后:", json.dumps(after, ensure_ascii=False)[:300])
            if after["chips"] or after["checked"]:
                bad.append("发送后没有清空勾选/输入框附件: " + json.dumps(after, ensure_ascii=False))
            if not any("工作区文件" in b for b in after["bubbles"]):
                bad.append("消息气泡里没显示勾选了哪些工作区文件: " + json.dumps(after["bubbles"], ensure_ascii=False))
            await browser.close()
    finally:
        shutil.rmtree(WS, ignore_errors=True)

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WS_SELECT_SEND_OK (勾选进输入框, 点发送才发出, 无「发送选中」按钮)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
