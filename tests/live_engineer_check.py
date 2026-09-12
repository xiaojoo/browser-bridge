"""真实工程运行冒烟: /api/engineer/run 让模型在 workspace 新建一个文件并校验落盘。"""
import asyncio
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8765"
TASK = '在工作区新建文件 eng_demo.txt, 内容为 "phase2-engineer-ok"'


def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def get_file():
    with urllib.request.urlopen(BASE + "/workspace/file?path=eng_demo.txt", timeout=10) as r:
        return json.loads(r.read().decode())


def del_file():
    req = urllib.request.Request(BASE + "/workspace/file?path=eng_demo.txt", method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode())


async def main() -> int:
    import websockets
    # 先启动 chatgpt(登录态已存, 秒级)
    print("start:", post("/api/start", {"provider": "chatgpt"}))
    for _ in range(90):
        await asyncio.sleep(1)
        st = get("/api/status")
        if st.get("state") == "logged_in":
            print("status:", st)
            break
    else:
        print("登录失败:", get("/api/status")); return 1
    stages = []
    async with websockets.connect("ws://127.0.0.1:8765/ws") as ws:
        print("run:", post("/api/engineer/run", {"task": TASK, "include": [],
                                                 "plan": False, "summary": False}))
        deadline = asyncio.get_running_loop().time() + 240
        while asyncio.get_running_loop().time() < deadline:
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            except asyncio.TimeoutError:
                continue
            if ev.get("type") != "engineer":
                continue
            stages.append((ev.get("stage"), ev.get("text", "")))
            print("engineer:", ev.get("stage"), "|", (ev.get("text") or "")[:90])
            if ev.get("stage") in ("done", "error"):
                break
    applied_any = any(s == "apply" for s, _ in stages)
    done = any(s == "done" for s, _ in stages)
    err = [t for s, t in stages if s == "error"]
    # 校验文件确实写入
    try:
        f = get_file()
        content = f.get("text", "").replace("\r\n", "\n")
        print("file_exists:", content.strip()[:60])
        del_file()
        print("cleaned_up")
        ok_file = "phase2-engineer-ok" in content
    except Exception as exc:  # noqa: BLE001
        print("file check failed:", exc)
        ok_file = False
    print("RESULT: applied_any=%s done=%s ok_file=%s errors=%s" % (applied_any, done, ok_file, err))
    return 0 if (applied_any and done and ok_file and not err) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
