"""桥接窗口被关闭时的行为校验(本地 mock, 不碰真实站点/浏览器)。

覆盖:
- page_alive() 能识别窗口已关闭
- ensure_alive() 把状态收敛回 idle 并广播"窗口已关闭"提示
- 此时发消息给出明确错误(而不是含糊的"找不到输入框")
- 页面还活着但没有输入框时, 仍然是"找不到输入框"的提示
- 启动/登录流程中(launching/waiting_login)不会被误判掉线
"""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import providers  # noqa: E402
from bridge.browser import BrowserManager  # noqa: E402

PAGE = """<!doctype html><meta charset="utf-8"><title>mock</title>
<div id="prompt-textarea" contenteditable="true"></div>"""

BARE_PAGE = """<!doctype html><meta charset="utf-8"><title>mock bare</title><p>登录页, 没有输入框</p>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = (BARE_PAGE if self.path.startswith("/sign_in") else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


async def main() -> int:
    srv, root = start_server()
    mock = providers.Provider(
        id="mock", name="Mock Web", url=root + "/", short="MK",
        composer_selectors=("#prompt-textarea",),
        capture_mode="dom", conversation_pattern="/c/",
    )
    bad = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            mgr = BrowserManager()
            mgr.provider = mock
            mgr.page = page
            mgr._ctx = ctx
            mgr.state = "logged_in"
            seen = []

            async def on_change(status, **extra):
                seen.append({"state": status.get("state"),
                             "info": (extra.get("info") or {}).get("text")})

            mgr.on_change = on_change

            # 1) 窗口还在
            await page.goto(root + "/", wait_until="domcontentloaded")
            print("1) 活着:", mgr.page_alive(), await mgr.ensure_alive())
            if not mgr.page_alive() or not await mgr.ensure_alive():
                bad.append("窗口还在时误判掉线")

            # 2) 用户关掉窗口
            await page.close()
            print("2) 关窗后 page_alive:", mgr.page_alive())
            if mgr.page_alive():
                bad.append("关窗后仍认为页面可用")
            alive = await mgr.ensure_alive()
            print("   ensure_alive:", alive, "state:", mgr.state, "events:", json.dumps(seen, ensure_ascii=False))
            if alive or mgr.state != "idle":
                bad.append(f"关窗后状态未收敛到 idle: alive={alive} state={mgr.state}")
            if not any(e["info"] and "窗口已关闭" in e["info"] for e in seen):
                bad.append("没有广播'窗口已关闭'提示")

            # 3) 再发消息: 明确报错
            try:
                await mgr.send_text("你好")
                bad.append("窗口已关闭时 send_text 不该成功")
            except RuntimeError as exc:
                print("3) send_text:", exc)
                if "窗口已关闭" not in str(exc):
                    bad.append("报错文案不是'窗口已关闭': " + str(exc))

            # 4) 页面活着但没有输入框 -> 仍是"找不到输入框"
            page2 = await ctx.new_page() if not ctx.is_closed() else None
            if page2 is None:
                ctx2 = await browser.new_context()
                page2 = await ctx2.new_page()
                mgr._ctx = ctx2
            await page2.goto(root + "/sign_in", wait_until="domcontentloaded")
            mgr.page = page2
            mgr.state = "logged_in"
            try:
                await mgr.send_text("你好")
                bad.append("没有输入框时 send_text 不该成功")
            except RuntimeError as exc:
                print("4) 无输入框:", exc)
                if "找不到输入框" not in str(exc):
                    bad.append("无输入框时文案不对: " + str(exc))

            # 5) 启动/登录流程中不误判
            mgr.state = "waiting_login"
            await page2.close()
            if not await mgr.ensure_alive():
                bad.append("waiting_login 期间不该判定掉线")
            if mgr.state != "waiting_login":
                bad.append("waiting_login 状态被改动了")

            await browser.close()
    finally:
        srv.shutdown()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WINDOW_CLOSED_OK (关窗后状态收敛 + 明确报错; 无输入框/登录中区分正确)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
