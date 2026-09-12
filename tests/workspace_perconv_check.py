"""工作区面板: 默认折叠 / 一键展开折叠 / 每个会话各自的工作区目录
+ **工作区目录只能由用户主动动作改变**(回归: 连接站点时被自动换掉过)。

用真实的 /api/workspace* 接口(测试结束恢复用户原来的根目录), 站点状态用 stub。

规则(用户定的):
    只有"主动点开某个会话 / 主动新建会话 / 在对话框里选目录"才允许换工作区;
    连接站点、刷新页面、站点被限流后自动另开窗口、站点自己换会话 id —— 一律不许动。
所以断言分两类:
    A. 自动路径: 客户端连 POST 都不该发(window.__rootPosts 计数) + 服务端根目录不变
    B. 用户动作: 允许切到那条会话记过的目录
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
  window.__rootPosts = [];                       // 记录所有"改工作区根目录"的请求
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/workspace/root") === 0)
      window.__rootPosts.push(String((o && o.body) || ""));
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
  title: document.getElementById("wsTitle").title,
  rootPosts: (window.__rootPosts || []).slice(),
  map: JSON.parse(localStorage.getItem("wlb.wsroot.v1") || "{}"),
})"""


async def server_root(page) -> str:
    return await page.evaluate("""async () => (await (await fetch("/api/workspace")).json()).root""")


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
            user_root = await server_root(page)
            print("用户原来的工作区:", user_root)
            await page.evaluate("() => switchMode('world')")
            await page.wait_for_timeout(600)

            # 1) 先把工作区切到一个**内容确定**的目录(默认 workspace/ 里可能一个文件夹都没有,
            #    那样下面的"折叠/展开"就无从谈起 —— 测试不该依赖用户当前目录里有什么)
            await page.click("#wsTitle")
            await page.wait_for_timeout(500)
            await page.fill("#wsRootInput", str(WS_A))
            await page.click("#wsApply")
            await page.wait_for_timeout(900)

            # 2) 默认折叠 + 一键展开/折叠
            st = await page.evaluate(READ)
            print("默认:", json.dumps(st["folders"], ensure_ascii=False))
            if not st["folders"]:
                bad.append("临时工作区里没有文件夹, 折叠用例无从测起: " + json.dumps(st["files"]))
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

            # 3) 会话 A 已经记下了这个工作区(下面第 4 步要拿它当对照)
            stA = await page.evaluate(READ)
            print("会话A:", json.dumps({k: stA[k] for k in ("files", "map")}, ensure_ascii=False))
            if "marker_a.txt" not in stA["files"]:
                bad.append("会话 A 的工作区没切过去: " + json.dumps(stA["files"], ensure_ascii=False))
            if not any(k.endswith("convA") for k in stA["map"]):
                bad.append("没有把工作区记到会话 A 名下: " + json.dumps(stA["map"], ensure_ascii=False))

            # 4) 【回归】自动路径一律不许改工作区目录。
            #    先给"另一个会话"在本地记一个不同的目录, 再模拟各种自动变化 ——
            #    以前这里会把根目录悄悄换成那个会话记过的目录(用户实际遇到的 bug)。
            await page.evaluate("""(wsB) => {
              const m = JSON.parse(localStorage.getItem("wlb.wsroot.v1") || "{}");
              m["chatgpt|convX"] = wsB;
              localStorage.setItem("wlb.wsroot.v1", JSON.stringify(m));
              window.__rootPosts.length = 0;          // 从这里开始数
            }""", str(WS_B))
            await page.evaluate("""async () => {
              showFreshStart();                                  // 连接站点/新建会话时的"空白一屏"
              await loadConversations();                         // 连接后拉站点会话列表(会改 conversation_id)
              state.conversation_id = "convX";
              renderHistoryFor(providerKey(), "convX", null);     // 会话变化(可能是站点自己换的)
              adoptConversation("convX");                         // 站点分配了新会话 id
              syncHistoryView(true);                             // 连接后刷新画面
            }""")
            await page.wait_for_timeout(1500)
            auto = await page.evaluate(READ)
            root = await server_root(page)
            print("自动变化后:", json.dumps({"posts": auto["rootPosts"], "root": root,
                                             "files": auto["files"]}, ensure_ascii=False))
            if auto["rootPosts"]:
                bad.append("自动变化时客户端还在改工作区根目录(不该发这些请求): "
                           + json.dumps(auto["rootPosts"], ensure_ascii=False))
            if root != str(WS_A) or "marker_a.txt" not in auto["files"]:
                bad.append("自动变化把工作区换掉了(应保持 " + str(WS_A) + "): "
                           + json.dumps({"root": root, "files": auto["files"]}, ensure_ascii=False))

            # 5) 用户主动点开那条会话 -> 允许切到它自己的工作区
            await page.evaluate("""async () => {
              state.conversation_id = "convX";
              await applyConvWorkspace(false, true);      // openRemote/openGroup 里调的就是这个
            }""")
            await page.wait_for_timeout(1200)
            gest = await page.evaluate(READ)
            root = await server_root(page)
            print("用户主动点开:", json.dumps({"posts": gest["rootPosts"], "root": root,
                                             "files": gest["files"]}, ensure_ascii=False))
            if root != str(WS_B) or "marker_b.txt" not in gest["files"]:
                bad.append("用户主动点开会话没切到它自己的工作区: "
                           + json.dumps({"root": root, "files": gest["files"]}, ensure_ascii=False))

            # 6) 刷新页面也不许自己换工作区(当前服务端在 WS_B, 会话 A 记的是 WS_A)
            await page.evaluate("""async (p) => {
              state.conversation_id = "convA";
              const r = await fetch("/api/workspace/root", { method: "POST",
                headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: p }) });
              return r.ok;
            }""", str(WS_B))                      # 直接摆成"服务端在 B", 模拟用户上次选的是 B
            await page.wait_for_timeout(300)
            await page.reload(wait_until="networkidle")
            await page.wait_for_timeout(1800)
            after = await page.evaluate(READ)
            root = await server_root(page)
            print("刷新后:", json.dumps({"posts": after["rootPosts"], "root": root}, ensure_ascii=False))
            if root != str(WS_B):
                bad.append("刷新页面把工作区自己换掉了(应保持 " + str(WS_B) + "): " + root)

            # 7) 对话框里选目录仍然是用户动作(允许改)
            await page.click("#wsTitle")
            await page.wait_for_timeout(400)
            await page.fill("#wsRootInput", str(WS_A))
            await page.click("#wsApply")
            await page.wait_for_timeout(900)
            if await server_root(page) != str(WS_A):
                bad.append("对话框里选目录没生效(用户动作应该允许改)")
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
          "只有用户主动开会话/选目录才换, 连接·刷新·自动另开窗口一律不动)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
