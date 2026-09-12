"""本地模型那条链路: 流式请求(SSE)解析 / 不支持流式时退回整段 / 报错要带上原因 / 上下文拼装。

用一个假的 OpenAI 兼容服务端点跑, 不需要真的本地模型, 也不碰设置文件。
"""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import planner  # noqa: E402

PORT = 18931
BASE = f"http://127.0.0.1:{PORT}/v1"
PIECES = ["好的, ", "这是带缓存的 ", "fib:\n", "```python\n", "cache = {}\n", "```"]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if "/unauthorized" in self.path:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"bad key"}')
            return
        if body.get("stream") and "/nostream" not in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for p in PIECES:
                line = json.dumps({"choices": [{"delta": {"content": p}}]}, ensure_ascii=False)
                self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        # 不支持流式: 直接回整段 JSON
        payload = {"choices": [{"message": {"content": "".join(PIECES)}}]}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def cfg(path=""):
    return {"api_base": BASE + path, "api_model": "local-model", "api_temp": 0.2}


async def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bad = []
    want = "".join(PIECES)

    # 1) 流式: 每段都回调, 拼起来是完整回答
    chunks = []
    async def on_chunk(t):
        chunks.append(t)
        await asyncio.sleep(0)          # 模拟真实广播开销
    text = await planner.ask_stream(cfg(), planner.build_messages("写个带缓存的 fib"), on_chunk=on_chunk)
    print("流式片段:", json.dumps(chunks, ensure_ascii=False))
    if "".join(chunks) != want or text != want:
        bad.append("流式增量/完整回答不对: " + repr(text)[:120])
    if len(chunks) != len(PIECES):
        bad.append(f"没有逐段回调(收到 {len(chunks)} 段, 期望 {len(PIECES)})")

    # 2) 站点不支持流式(回整段 JSON): 也要拿到回答
    text2 = await planner.ask_stream(cfg("/nostream"), planner.build_messages("再写一次"))
    print("退回整段:", repr(text2)[:40])
    if text2 != want:
        bad.append("不支持流式的站点没退回整段: " + repr(text2)[:80])

    # 3) 非流式 ask() 仍然可用(落盘/自检走这条)
    text3 = await planner.ask(cfg("/nostream"), "总结一下")
    print("ask():", repr(text3)[:40])
    if text3 != want:
        bad.append("ask() 坏了: " + repr(text3)[:80])

    # 4) 报错要带上站点原话
    try:
        await planner.ask_stream(cfg("/unauthorized"), planner.build_messages("hi"))
        bad.append("401 没有抛 PlannerError")
    except planner.PlannerError as exc:
        print("401 ->", str(exc)[:60])
        if "401" not in str(exc):
            bad.append("报错里没带状态码: " + str(exc)[:80])

    # 5) 上下文拼装: system + 最近几轮 + 本条
    msgs = planner.build_messages("新问题", [
        {"role": "user", "text": "旧问题"},
        {"role": "assistant", "text": "旧回答"},
        {"role": "world", "text": "卡片不该进上下文"},
        {"role": "user", "text": ""},
    ])
    print("messages:", json.dumps([m["role"] for m in msgs], ensure_ascii=False))
    if [m["role"] for m in msgs] != ["system", "user", "assistant", "user"]:
        bad.append("上下文拼装不对: " + json.dumps(msgs, ensure_ascii=False)[:200])
    if msgs[-1]["content"] != "新问题":
        bad.append("最后一条不是本次输入")

    srv.shutdown()
    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("LOCAL_STREAM_OK (本地模型流式逐段回传, 不支持流式时退回整段, 报错带原因, 上下文拼装正确)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
