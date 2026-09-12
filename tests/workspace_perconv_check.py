"""工作区面板: 默认折叠 / 一键展开折叠 / 每个会话各自的工作区目录。

用真实的 /api/workspace* 接口(测试结束恢复默认根目录), 站点状态用 stub。
"""
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
WS_A = ROOT / ".tmp" / "ws-conv-a"
WS_B = ROOT / ".tmp" / "ws-conv-b"

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
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [
        { id: "convA", key: "/c/convA", title: "会话 A", url: "https://x/c/convA", active: true },
        { id: "convB", key: "/c/convB", title: "会话 B", url: "https://x/c/convB", active: false }] });
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true, apply: null, verify: null });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

READ = """() => ({
  files: Array.from(document.querySelectorAll("#wsTree .ws-file .fn")).map(e => e.textContent.trim()),
  folders: Array.from(document.querySelectorAll("#wsTree .ws-folder-row"))
             .map(e => e.getAttribute("aria-expanded")),
  root: document.getElementById("wsTitle").title,
  map: JSON.parse(localStorage.getItem("wlb.wsroot.v1") || "{}"),
})"""


async def main() -> int:
    bad = []
    WS_A.mkdir(parents=True, exist_ok=True)
    (WS_A / "marker_a.txt").write_text("a\n", encoding="utf-8")
    (WS_A / "sub").mkdir(exist_ok=True)
    WS_B.mkdir(parents=True, exist_ok=True)
    (WS_B / "marker_b.txt").write_text("b\n", encoding="utf-8")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(STUB)
        user_root = ""                      # 用户真正配的工作区目录, 跑完必须原样还回去
        try:
            await page.goto(URL, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(900)
            user_root = await page.evaluate("""async () => {
              try { const r = await fetch("/api/workspace"); return (await r.json()).root || ""; }
              catch (e) { return ""; } }""")
            print("用户原来的工作区:", user_root)
            await page.evaluate("() => switchMode('world')")
            await page.wait_for_timeout(600)

            # 1) 默认折叠 + 一键展开/折叠
            st = await page.evaluate(READ)
            print("默认:", json.dumps(st["folders"], ensure_ascii=False))
            if any(f == "true" for f in st["folders"]):
                bad.append("工作区文件夹默认不是折叠的: " + json.dumps(st["folders"]))
            await page.click("#wsExpand")
            await page.wait_for_timeout(300)
            exp = await page.evaluate("() => Array.from(document.querySelectorAll('#wsTree .ws-folder-row')).map(e => e.getAttribute('aria-expanded'))")
            if not exp or any(f != "true" for f in exp):
                bad.append("一键展开没生效: " + json.dumps(exp, ensure_ascii=False))
            await page.click("#wsCollapse")
            await page.wait_for_timeout(300)
            col = await page.evaluate("() => Array.from(document.querySelectorAll('#wsTree .ws-folder-row')).map(e => e.getAttribute('aria-expanded'))")
            if any(f != "false" for f in col):
                bad.append("一键折叠没生效: " + json.dumps(col, ensure_ascii=False))

            # 2) 给会话 A 选一个工作区
            await page.click("#wsTitle")
            await page.wait_for_timeout(500)
            await page.fill("#wsRootInput", str(WS_A))
            await page.click("#wsApply")
            await page.wait_for_timeout(900)
            stA = await page.evaluate(READ)
            print("会话A:", json.dumps({k: stA[k] for k in ("files", "map")}, ensure_ascii=False))
            if "marker_a.txt" not in stA["files"]:
                bad.append("会话 A 的工作区没切过去: " + json.dumps(stA["files"], ensure_ascii=False))
            if not any(k.endswith("convA") for k in stA["map"]):
                bad.append("没有把工作区记到会话 A 名下: " + json.dumps(stA["map"], ensure_ascii=False))

            # 3) 切到"没单独设置过工作区"的会话 B -> **不许动服务端目录**
            #    (以前这里会 POST 空路径把根目录恢复成内置默认, 于是用户配的项目目录被默默换掉)
            await page.evaluate("""() => {
              state.conversation_id = "convB";
              renderHistoryFor(providerKey(), "convB", null);
            }""")
            await page.wait_for_timeout(1200)
            stB = await page.evaluate(READ)
            rootB = await page.evaluate("""async () => (await (await fetch("/api/workspace")).json()).root""")
            print("会话B:", json.dumps({"files": stB["files"], "root": rootB}, ensure_ascii=False))
            if "marker_a.txt" not in stB["files"] or rootB != str(WS_A):
                bad.append("切到没设置过的会话 B 时把工作区目录动了(应沿用当前目录): "
                           + json.dumps({"files": stB["files"], "root": rootB}, ensure_ascii=False))

            # 4) 给会话 B 另选一个
            await page.click("#wsTitle")
            await page.wait_for_timeout(400)
            await page.fill("#wsRootInput", str(WS_B))
            await page.click("#wsApply")
            await page.wait_for_timeout(900)
            stB2 = await page.evaluate(READ)
            if "marker_b.txt" not in stB2["files"]:
                bad.append("会话 B 的工作区没切过去: " + json.dumps(stB2["files"], ensure_ascii=False))

            # 5) 切回会话 A -> 工作区跟着回来
            await page.evaluate("""() => {
              state.conversation_id = "convA";
              renderHistoryFor(providerKey(), "convA", null);
            }""")
            await page.wait_for_timeout(1200)
            stA2 = await page.evaluate(READ)
            print("切回A:", json.dumps(stA2["files"], ensure_ascii=False))
            if "marker_a.txt" not in stA2["files"]:
                bad.append("切回会话 A 没有恢复它自己的工作区: " + json.dumps(stA2["files"], ensure_ascii=False))

            # 6) 刷新页面: 仍然是会话 A 的工作区
            await page.reload(wait_until="networkidle")
            await page.wait_for_timeout(1500)
            stA3 = await page.evaluate(READ)
            print("刷新后:", json.dumps(stA3["files"], ensure_ascii=False))
            if "marker_a.txt" not in stA3["files"]:
                bad.append("刷新后没有恢复会话 A 的工作区: " + json.dumps(stA3["files"], ensure_ascii=False))
        finally:
            # 兜底恢复: 还回**用户原来配的那个目录**(以前一律恢复成内置默认, 会把用户的配置冲掉)
            if user_root:
                await page.evaluate("""async (p) => {
                  try { await fetch("/api/workspace/root", { method: "POST",
                    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: p }) }); } catch (e) {}
                }""", user_root)
            await browser.close()
            shutil.rmtree(WS_A, ignore_errors=True)
            shutil.rmtree(WS_B, ignore_errors=True)

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WORKSPACE_PERCONV_OK (默认折叠 + 一键展开折叠 + 每个会话各自工作区; "
          "没设置过的会话沿用当前目录, 绝不动用户的根目录)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
