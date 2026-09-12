"""全链路实测: 直接对运行中的服务完成 启动chatgpt -> ws订阅 -> 发消息 -> 看事件是否到达。"""
import asyncio
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"


def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode())


async def main() -> int:
    import websockets
    events = []
    async with websockets.connect("ws://127.0.0.1:8765/ws") as ws:
        # 启动(登录态已存, 秒级)
        print("start:", post("/api/start", {"provider": "chatgpt"}))
        # 等待 logged_in
        for _ in range(60):
            await asyncio.sleep(1)
            st = get("/api/status")
            if st.get("state") == "logged_in":
                print("status:", st)
                break
        else:
            print("未登录成功, status:", get("/api/status")); return 1
        # 发送
        print("chat:", post("/api/chat", {"text": "请只回复:OK"}))
        # 收集事件直到 message_end 或超时
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
            except asyncio.TimeoutError:
                print("  (recv timeout)")
                continue
            print("  recv:", ev.get("type"), ev.get("kind", ""), str(ev.get("text", ""))[:24])
            events.append(ev)
            if ev.get("type") in ("message_end", "error"):
                break
        print("busy_at_end:", get("/api/status").get("busy"))
    kinds = {}
    for e in events:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    print("event_kinds:", kinds)
    deltas = [e for e in events if e["type"] == "delta"]
    print("delta_count:", len(deltas))
    if deltas:
        full = "".join(d["text"] for d in deltas)
        print("joined_text:", full[:80])
    errs = [e for e in events if e["type"] == "error"]
    if errs:
        print("errors:", [e["text"] for e in errs])
    return 0 if deltas or any(e["type"] == "message_end" for e in events) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
