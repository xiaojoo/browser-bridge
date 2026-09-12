"""Phase3 自测: planner 客户端 + 设置读写(本地 mock OpenAI 兼容 API)。"""
import asyncio
import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import planner  # noqa: E402

BASE = "http://127.0.0.1:8765"
MOCK_PORT = 18925
MOCK = {"api_base": f"http://127.0.0.1:{MOCK_PORT}/v1", "api_model": "mock-plan",
        "api_key": "test-key", "api_temp": 0.1}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            auth = self.headers.get("Authorization", "")
            assert auth == "Bearer test-key", auth
            self._send(200, {"data": [{"id": "mock-plan"}, {"id": "mock-big"}]})
        else:
            self._send(404, {"error": "nf"})

    def do_POST(self):
        if self.path.endswith("/chat/completions"):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            assert body["model"] == "mock-plan"
            prompt = body["messages"][-1]["content"]
            self._send(200, {"choices": [{"message": {"role": "assistant",
                                                      "content": f"[plan:{len(prompt)}]"}}]})
        else:
            self._send(404, {"error": "nf"})

    def log_message(self, *a):
        pass


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8", "replace"))


async def main() -> int:
    # 1) 客户端单元(走 mock)
    out = await planner.ask(MOCK, "把任务拆 3 步")
    assert out.startswith("[plan:"), out
    models = await planner.list_models(MOCK)
    assert models == ["mock-plan", "mock-big"], models

    # 2) 设置 API 读写
    s, g0 = http("GET", "/api/settings")
    assert s == 200 and g0["planner"]["type"] == "web", g0
    s, _ = http("POST", "/api/settings", {"planner": {"type": "api", "api_base": MOCK["api_base"],
                                                      "api_model": MOCK["api_model"],
                                                      "api_key": MOCK["api_key"]}})
    assert s == 200
    s, g1 = http("GET", "/api/settings")
    assert g1["planner"]["has_key"] and g1["planner"]["api_key"] == "", g1

    # 3) 测试连接(不带 key -> 服务端用已存 key)
    s, t = http("POST", "/api/settings/test",
                {"planner": {"type": "api", "api_base": MOCK["api_base"],
                             "api_model": MOCK["api_model"]}})
    assert s == 200 and t.get("ok") and "mock-plan" in t.get("models", []), (s, t)

    # 4) 清理: 回 web + 清 key
    http("POST", "/api/settings", {"planner": {"api_key": "__CLEAR__"}})
    http("POST", "/api/settings", {"planner": {"type": "web"}})
    s, g2 = http("GET", "/api/settings")
    assert g2["planner"]["type"] == "web" and not g2["planner"]["has_key"], g2
    print("PHASE3_OK")
    return 0


if __name__ == "__main__":
    # 确保把本地配置重置(避免影响用户默认)
    cfg = Path(__file__).resolve().parent.parent / ".bridge_settings.json"
    if cfg.exists():
        cfg.unlink()
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    code = asyncio.run(main())
    srv.shutdown()
    if cfg.exists():
        cfg.unlink()
    raise SystemExit(code)
