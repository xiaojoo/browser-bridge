"""「只用本地模型」的服务端那一轮: 事件顺序要和网页模型完全一致
(message_start -> delta* -> message_end), 没配置时要有可照做的提示。

打桩 planner / settings / broadcast, 不联网、不碰真实设置文件, 也不启动浏览器。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import planner, server  # noqa: E402

SEEN: list[str] = []


def as_body(resp):
    """端点成功时直接返回 dict, 出错时返回 JSONResponse。"""
    if isinstance(resp, dict):
        return resp
    return json.loads(resp.body.decode("utf-8"))


def fake_ask_stream_factory(pieces):
    async def fake_ask_stream(cfg, messages, *, on_chunk=None, timeout=None):
        for p in pieces:
            if on_chunk:
                await on_chunk(p)
        return "".join(pieces)
    return fake_ask_stream


async def main() -> int:
    bad = []
    real_broadcast, real_load, real_ask = server.broadcast, server.settings.load, planner.ask_stream

    async def collect(ev):
        SEEN.append(ev.get("type"))

    try:
        server.broadcast = collect
        server.settings.load = lambda: {"planner": {"type": "web", "api_key": ""}, "engine": {}}
        resp = await server.api_local_chat(server.LocalChatRequest(text="你好"))
        body = as_body(resp)
        print("未配置时:", resp.status_code, body.get("error", "")[:60])
        if resp.status_code != 400 or "本地模型" not in body.get("error", ""):
            bad.append("没配置本地模型时应明确报错并指路: " + json.dumps(body, ensure_ascii=False)[:120])

        server.settings.load = lambda: {"planner": {"type": "local", "api_base": "http://127.0.0.1:1234/v1",
                                                   "api_model": "qwen2.5-coder-7b"}, "engine": {}}
        planner.ask_stream = fake_ask_stream_factory(["第一段 ", "第二段"])
        SEEN.clear()
        resp2 = await server.api_local_chat(server.LocalChatRequest(text="你好", history=[]))
        body2 = as_body(resp2)
        print("已配置时:", getattr(resp2, "status_code", 200), json.dumps(body2, ensure_ascii=False))
        await asyncio.sleep(0.3)
        print("事件序列:", SEEN)
        if SEEN[:1] != ["message_start"]:
            bad.append("不是先 message_start: " + json.dumps(SEEN))
        if SEEN[-1:] != ["message_end"]:
            bad.append("结尾不是 message_end(前端要靠它收尾+入历史): " + json.dumps(SEEN))
        if SEEN.count("delta") != 2:
            bad.append("增量没有逐段推: " + json.dumps(SEEN))
        if "error" in SEEN:
            bad.append("正常一轮里出现了 error: " + json.dumps(SEEN))
        if body2.get("model") != "qwen2.5-coder-7b":
            bad.append("响应里没带上用的是哪个模型: " + json.dumps(body2, ensure_ascii=False))

        # 本地模型报错 -> 要把原因带给界面
        async def boom(cfg, messages, *, on_chunk=None, timeout=None):
            raise planner.PlannerError("HTTP 401: bad key")
        planner.ask_stream = boom
        SEEN.clear()
        await server.api_local_chat(server.LocalChatRequest(text="你好", history=[]))
        await asyncio.sleep(0.2)
        print("报错时事件序列:", SEEN)
        if "error" not in SEEN or "message_end" in SEEN:
            bad.append("本地模型报错时应该只发 error, 不发 message_end: " + json.dumps(SEEN))
    finally:
        server.broadcast, server.settings.load, planner.ask_stream = real_broadcast, real_load, real_ask

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("LOCAL_TURN_EVENTS_OK (只用本地模型那一轮的事件顺序与网页模型一致, 报错/未配置都有明确提示)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

