"""捕获链路端到端测试(本地 mock 流, 不依赖 DeepSeek)。

启动一个本地 NDJSON 流式服务, 在真实 Chromium 里:
1. 注入 FETCH_WRAPPER_JS
2. 打开 mock 页面, 触发 fetch 请求聊天类接口
3. drain 缓冲, 断言文本增量与 reasoning 提取正确、收到 EOF
"""
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from bridge import capture  # noqa: E402

MOCK_PORT = 18923
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"

# 参考 chat.deepseek.com 的常见流格式(逐行 JSON)
CHUNKS = [
    '{"type":"text_delta","content":"\\u4f60"}\n',          # 你
    '{"type":"text_delta","content":"\\u597d"}\n',          # 好
    '{"reasoning_content":"\\u601d\\u8003\\u4e2d"}\n',      # reasoning_content
    '{"choices":[{"delta":{"type":"text_delta","content":"!"}}]}\n',
    '{"type":"message_end"}\n',
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = b"<html><body>mock</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/mock/chat/completion":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            for line in CHUNKS:
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.05)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):  # noqa: D102
        pass


async def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.add_init_script(capture.FETCH_WRAPPER_JS)
        await page.goto(MOCK_BASE + "/", wait_until="domcontentloaded")

        # 非聊天路径不应被捕获
        await capture.enable_capture(page)
        await page.evaluate("fetch('/other')")
        await asyncio.sleep(0.4)
        none_items = await capture.drain_items(page)
        assert none_items == [], f"非聊天请求不应进缓冲: {none_items}"

        # 聊天路径: 触发流式请求
        await page.evaluate("fetch('/mock/chat/completion')")

        got: list[dict] = []
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            items = await capture.drain_items(page)
            got.extend(items)
            if any(it.get("kind") == "__eof__" for it in got):
                break
            await asyncio.sleep(0.1)

        texts = [it.get("text") for it in got if it.get("kind") == "text"]
        reasons = [it.get("text") for it in got if it.get("kind") == "reasoning"]
        has_eof = any(it.get("kind") == "__eof__" for it in got)

        print("captured:", json.dumps(got, ensure_ascii=False))
        assert has_eof, "未收到流 EOF"
        assert texts == ["你", "好", "!"], f"正文提取不符: {texts}"
        assert reasons == ["思考中"], f"reasoning 提取不符: {reasons}"
        await capture.disable_capture(page)
        await browser.close()

    srv.shutdown()
    print("CAPTURE_E2E_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
