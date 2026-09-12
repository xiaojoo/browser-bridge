"""工作区目录可切换校验(接口 + 界面, 结束后恢复默认根目录)。

覆盖:
- GET /api/workspace 返回当前根目录
- 点侧栏「📁 工作区」标题打开选目录弹层, 能列子目录
- 切到别的目录后: 文件树来自新目录, 「新建文件」提示里的目录名跟着变
- 越界路径(../)仍然被拒绝
- 恢复默认
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
FIXTURE = ROOT / ".tmp" / "ws-root-fixture"
MARKER = FIXTURE / "marker_from_fixture.txt"

WS_STUB_OFF = None   # 这个用例要看真实的 /api/workspace, 所以不 stub


async def api(page, path, method="GET", body=None):
    return await page.evaluate(
        """async ([p, m, b]) => {
            const r = await fetch(p, m === "GET" ? undefined : {
              method: m, headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
            let j = null; try { j = await r.json(); } catch (e) {}
            return { status: r.status, json: j };
        }""", [path, method, body])


async def main() -> int:
    bad = []
    FIXTURE.mkdir(parents=True, exist_ok=True)
    MARKER.write_text("fixture\n", encoding="utf-8")
    (FIXTURE / "sub_dir").mkdir(exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)
        await page.evaluate("() => switchMode('world')")   # 工作区只在 World 模式显示
        await page.wait_for_timeout(300)
        user_root = ""                                     # 用户真正配的目录(下面读到后覆盖)
        try:
            info = await api(page, "/api/workspace")
            print("初始:", json.dumps(info["json"], ensure_ascii=False))
            if info["status"] != 200 or "root" not in (info["json"] or {}):
                bad.append("GET /api/workspace 不可用: " + json.dumps(info, ensure_ascii=False))
            default_root = info["json"]["default"]
            user_root = info["json"]["root"] or ""      # 用户真正配的目录: 跑完必须原样还回去
            await api(page, "/api/workspace/root", "POST", {"path": ""})   # 先确保从默认开始
            await page.wait_for_timeout(400)
            info = await api(page, "/api/workspace")
            if info["json"]["root"] != default_root:
                bad.append("恢复默认没生效: " + json.dumps(info["json"], ensure_ascii=False))
            await page.evaluate("() => loadWorkspace()")
            await page.wait_for_timeout(300)

            # 1) 界面上打开选目录弹层
            await page.click("#wsTitle")
            await page.wait_for_timeout(400)
            dlg = await page.evaluate("""() => ({
              open: document.getElementById("wsOverlay").classList.contains("show"),
              input: document.getElementById("wsRootInput").value,
              browsePath: document.getElementById("wsBrowsePath").textContent,
              entries: Array.from(document.querySelectorAll("#wsBrowseList .ws-dir")).map(e => e.textContent.trim()),
            })""")
            print("弹层:", json.dumps(dlg, ensure_ascii=False))
            if not dlg["open"] or dlg["input"] != default_root:
                bad.append("点标题没打开选目录弹层/没带出当前目录: " + json.dumps(dlg, ensure_ascii=False))
            if dlg["browsePath"] != "这台电脑":
                bad.append("弹层默认应停在『这台电脑』: " + str(dlg["browsePath"]))
            if any(not e.startswith("💽") for e in dlg["entries"]):
                bad.append("『这台电脑』视图里不该出现文件夹, 只该有盘符: " + json.dumps(dlg["entries"], ensure_ascii=False))
            if not dlg["entries"]:
                bad.append("没列出磁盘")

            # 点一个磁盘 -> 进到该盘, 这时才列文件夹
            await page.evaluate("""() => {
              const el = Array.from(document.querySelectorAll("#wsBrowseList .ws-dir"))
                .find(e => e.textContent.indexOf("💽") === 0);
              el.click();
            }""")
            await page.wait_for_timeout(400)
            indrive = await page.evaluate("""() => ({
              browsePath: document.getElementById("wsBrowsePath").textContent,
              input: document.getElementById("wsRootInput").value,
              hasDir: Array.from(document.querySelectorAll("#wsBrowseList .ws-dir")).some(e => e.textContent.indexOf("📁") === 0),
            })""")
            print("进磁盘:", json.dumps(indrive, ensure_ascii=False))
            if not indrive["hasDir"] or not indrive["input"]:
                bad.append("进入磁盘后没有列出文件夹/没更新输入框: " + json.dumps(indrive, ensure_ascii=False))
            # 回到"这台电脑"
            await page.click("#wsUp")
            await page.wait_for_timeout(300)
            await page.click("#wsUp")
            await page.wait_for_timeout(300)

            # 2) 通过弹层切到 fixture 目录
            await page.fill("#wsRootInput", str(FIXTURE))
            await page.click("#wsApply")
            await page.wait_for_timeout(700)
            st = await page.evaluate("""() => ({
              title: document.getElementById("wsTitle").textContent.trim(),
              tooltip: document.getElementById("wsTitle").title,
              tree: Array.from(document.querySelectorAll("#wsTree .ws-file .fn")).map(e => e.textContent.trim()),
              empty: !!document.querySelector("#wsTree .ws-empty"),
            })""")
            print("切换后:", json.dumps(st, ensure_ascii=False))
            if st["title"] != "📁 ws-root-fixture":
                bad.append("标题没跟着换目录名: " + st["title"])
            if "marker_from_fixture.txt" not in st["tree"]:
                bad.append("文件树没来自新目录: " + json.dumps(st["tree"], ensure_ascii=False))
            if str(FIXTURE) not in st["tooltip"]:
                bad.append("标题 tooltip 没显示新路径: " + st["tooltip"])

            # 3) 新建文件提示里用的是新目录名
            await page.click("#wsNew")
            await page.wait_for_timeout(300)
            prompt = await page.evaluate("() => document.getElementById('dlgTitle').textContent")
            print("新建提示:", prompt)
            if "ws-root-fixture" not in prompt:
                bad.append("新建文件提示没跟上目录名: " + prompt)
            await page.click("#dlgCancel")
            await page.wait_for_timeout(200)

            # 4) 越界路径仍被拒
            esc = await api(page, "/workspace/file?path=../bridge/config.py")
            print("越界读取:", esc["status"])
            if esc["status"] == 200:
                bad.append("越界读取没有被拒绝")

            # 5) 浏览接口能列子目录
            br = await api(page, "/api/workspace/browse?path=" + str(FIXTURE))
            names = [d["name"] for d in (br["json"] or {}).get("dirs", [])]
            print("浏览:", json.dumps(names, ensure_ascii=False))
            if "sub_dir" not in names:
                bad.append("浏览接口没列出子目录: " + json.dumps(names, ensure_ascii=False))

            # 6) 恢复默认
            back = await api(page, "/api/workspace/root", "POST", {"path": ""})
            print("恢复默认:", json.dumps(back["json"], ensure_ascii=False))
            if back["status"] != 200 or back["json"]["root"] != default_root:
                bad.append("恢复默认失败: " + json.dumps(back, ensure_ascii=False))
        finally:
            # 兜底恢复: 还回**用户原来配的那个目录**(以前一律恢复成内置默认, 会把用户的配置冲掉)
            if user_root:
                await api(page, "/api/workspace/root", "POST", {"path": user_root})
            await browser.close()
            shutil.rmtree(FIXTURE, ignore_errors=True)

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WORKSPACE_ROOT_OK (可切换工作区目录, 越界仍被拒, 可恢复默认)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
