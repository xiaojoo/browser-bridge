"""新建对话链路校验(本地 mock 站点 + 直接调用 BrowserManager.new_chat, 不碰真实站点/浏览器)。

覆盖三种情况:
 1. 站内有可点的"新对话"按钮  -> 点它完成 SPA 内切换, URL 回到新会话页
 2. 站内按钮缺失/失效          -> 自动回退到站点首页(= 新会话页)
 3. 本身已在新会话页            -> 直接成功, 不做多余跳转
"""
import asyncio
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import providers  # noqa: E402
from bridge.browser import BrowserManager  # noqa: E402

ROOT_PAGE = """<!doctype html><meta charset="utf-8"><title>mock</title>
<div id="prompt-textarea" contenteditable="true"></div>
<div id="msgs"></div>"""

CONV_PAGE = """<!doctype html><meta charset="utf-8"><title>mock conv</title>
<button data-testid="create-new-chat-button" aria-label="New chat"
        onclick="history.pushState({}, '', '/')">New chat</button>
<div id="prompt-textarea" contenteditable="true"></div>
<div id="msgs">旧对话内容</div>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/c/broken"):
            body = ROOT_PAGE.replace("mock", "broken conv").encode()
        elif self.path.startswith("/c/"):
            body = CONV_PAGE.encode()
        else:
            body = ROOT_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静音
        pass


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


async def main() -> int:
    srv, root = start_server()
    mock = providers.Provider(
        id="mock", name="Mock Web", url=root + "/", short="MK",
        composer_selectors=("#prompt-textarea",),
        capture_mode="dom",
        new_chat_selectors=('[data-testid="create-new-chat-button"]', 'a[aria-label="New chat"]'),
        new_chat_names=("New chat",),
        conversation_pattern="/c/",
    )
    bad = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            mgr = BrowserManager()
            mgr.provider = mock
            mgr.page = page
            mgr.state = "logged_in"

            # 1) 站内按钮可用
            await page.goto(root + "/c/abc123", wait_until="domcontentloaded")
            assert mgr.conversation_id() == "abc123", "mock 会话 id 解析失败"
            ok = await mgr.new_chat()
            print("1) 站内按钮:", ok, page.url)
            if not ok or page.url.rstrip("/") != root.rstrip("/"):
                bad.append(f"情况1 失败: ok={ok} url={page.url}")

            # 2) 站内按钮缺失 -> 回退首页
            await page.goto(root + "/c/broken999", wait_until="domcontentloaded")
            ok = await mgr.new_chat()
            print("2) 回退首页:", ok, page.url, "conv_id=", mgr.conversation_id())
            if not ok or mgr.conversation_id() is not None or page.url.rstrip("/") != root.rstrip("/"):
                bad.append(f"情况2 失败: ok={ok} url={page.url}")

            # 3) 已在新会话页
            await page.goto(root + "/", wait_until="domcontentloaded")
            ok = await mgr.new_chat()
            print("3) 已在新页:", ok, page.url)
            if not ok or mgr.conversation_id() is not None:
                bad.append(f"情况3 失败: ok={ok} url={page.url}")

            # 4) 未启动浏览器时不得抛异常
            mgr.page = None
            print("4) 未就绪:", await mgr.new_chat())
            if await mgr.new_chat():
                bad.append("情况4 失败: 无页面时不该返回成功")

            await browser.close()
    finally:
        srv.shutdown()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("NEW_CHAT_OK (站内按钮 / 首页回退 / 已在新页 / 未就绪 四种情况均符合预期)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
