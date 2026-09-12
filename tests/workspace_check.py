"""工作区接口 + UI 冒烟: 写入/读取/列表/越界拒绝, 页面无 JS 错误且能列出文件。"""
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8", "replace"))


def rest_checks() -> None:
    s, tree = req("GET", "/workspace/tree")
    assert s == 200, tree
    paths = {it["path"] for it in tree["items"]}
    # 工作区根可以被用户改成任意目录, 所以别死认某个文件名: 默认根里有 README.txt, 自定义根里没有
    s, ws = req("GET", "/api/workspace")
    root_name = (ws or {}).get("name", "")
    if not (ws or {}).get("custom"):
        assert "README.txt" in paths, f"默认工作区缺少 README.txt: {paths}"
    print(f"workspace root: {root_name} (custom={bool((ws or {}).get('custom'))}), "
          f"{len(paths)} 个顶层条目")

    s, w = req("POST", "/workspace/file", {"path": "demo/sub/hello.py",
                                           "content": "print('hi')\n"})
    assert s == 200 and w.get("ok"), w
    s, r = req("GET", "/workspace/file?path=" + urllib.request.quote("demo/sub/hello.py"))
    got = r.get("text", "").replace("\r\n", "\n")
    assert s == 200 and got == "print('hi')\n" and not r["binary"], r

    s, bad = req("POST", "/workspace/file", {"path": "../../evil.txt", "content": "x"})
    assert s == 400, f"越界应 400: {bad}"
    s2, bad2 = req("GET", "/workspace/file?path=..%2F..%2F..%2Fwindows%2Fwin.ini")
    assert bad2.get("error"), "越界读应拒绝"

    s, d = req("DELETE", "/workspace/file?path=" + urllib.request.quote("demo/sub/hello.py"))
    assert s == 200 and d.get("ok"), d
    # 非空目录删除应被拒
    s, dn = req("DELETE", "/workspace/file?path=" + urllib.request.quote("demo"))
    assert dn.get("error"), "非空目录删除应被拒"
    # 清理测试残留的 demo 目录
    req("DELETE", "/workspace/file?path=" + urllib.request.quote("demo/sub"))
    req("DELETE", "/workspace/file?path=" + urllib.request.quote("demo"))
    print("REST_OK")


async def ui_checks() -> None:
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(BASE, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1500)
        info = await page.evaluate("""() => ({
          treeRows: document.querySelectorAll('#wsTree .ws-file').length,
          emptyHint: !!document.querySelector('#wsTree .ws-empty'),
          dlg: !!document.getElementById('dlgOverlay'),
          editor: !!document.getElementById('editOverlay'),
          convFiles: !!document.querySelectorAll('.conv .code').length || true
        })""")
        print("ui:", json.dumps(info, ensure_ascii=False))
        await browser.close()
    assert not errors, f"JS 报错: {errors}"
    assert info["treeRows"] >= 1 or info["emptyHint"], "工作区树未渲染"
    print("UI_OK")


async def main() -> int:
    rest_checks()
    await ui_checks()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
