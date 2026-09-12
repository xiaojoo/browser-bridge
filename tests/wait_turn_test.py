"""回归: wait_turn_end 的异步回调必须被 await, 否则增量丢失(此前 on_delta 未 await)。"""
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

PORT = 18924
BASE = f"http://127.0.0.1:{PORT}"
CHUNKS = [
    '{"type":"text_delta","content":"\\u4f60"}\n',
    '{"type":"text_delta","content":"\\u597d"}\n',
    '{"reasoning_content":"\\u601d\\u8003"}\n',
    '{"type":"message_end"}\n',
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = b"<html><body>mock</body></html>"
            self.send_response(200); self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        elif self.path == "/mock/chat/completion":
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
            for line in CHUNKS:
                self.wfile.write(line.encode("utf-8")); self.wfile.flush(); time.sleep(0.05)
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


async def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.add_init_script(capture.FETCH_WRAPPER_JS)
        await page.goto(BASE + "/", wait_until="domcontentloaded")

        await capture.enable_capture(page)
        await page.evaluate("fetch('/mock/chat/completion')")

        got: list[str] = []
        reasons: list[str] = []

        # 关键: 用异步回调(与生产 server 相同), 若不 await 则增量丢失
        async def on_delta(kind, text, snapshot):
            (reasons if kind == "reasoning" else got).append(text)
            await asyncio.sleep(0)  # 让出, 模拟真实广播代价

        truncated, errs = await capture.wait_turn_end(
            page, on_delta, mode="stream", snapshot_selector=capture.default_snapshot_selector())
        print("truncated:", truncated, "errs:", errs)
        print("text:", got)
        print("reasoning:", reasons)
        await capture.disable_capture(page)
        await browser.close()

    srv.shutdown()
    assert got == ["你", "好"], f"异步回调增量丢失: {got}"
    assert reasons == ["思考"], f"reasoning 丢失: {reasons}"
    print("WAIT_TURN_AWAIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
