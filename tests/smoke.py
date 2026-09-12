"""烟雾测试: 校验本地服务端点与 WS 事件(不启动浏览器)。"""
import asyncio
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8765"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read().decode("utf-8", "replace")


async def ws_check() -> str:
    import websockets
    async with websockets.connect("ws://127.0.0.1:8765/ws") as ws:
        return await asyncio.wait_for(ws.recv(), timeout=10)


def main() -> int:
    s1, b1 = get("/")
    print(f"GET /             -> {s1}  html={'<html' in b1.lower()} len={len(b1)}")
    s2, b2 = get("/api/status")
    print(f"GET /api/status   -> {s2}  {b2}")
    try:
        body = json.loads(b2)
    except json.JSONDecodeError:
        print("status 非 JSON, 失败"); return 1
    msg = asyncio.run(ws_check())
    print(f"WS /ws 首条事件   -> {msg[:180]}")
    try:
        ev = json.loads(msg)
        assert ev.get("type") == "status", "期望 status 事件"
    except Exception as exc:  # noqa: BLE001
        print(f"WS 事件解析失败: {exc}"); return 1
    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
