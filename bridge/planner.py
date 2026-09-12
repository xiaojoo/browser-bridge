"""OpenAI 兼容客户端: 供 planner(官方 API)调用, 不需要 openai 依赖。"""
import asyncio
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("planner")

SYSTEM = ("你是工程任务助手: 负责把用户任务拆成可执行步骤清单, 或在收到改动清单后写简要总结。"
          "输出精炼、结构清晰。")

DEFAULT_TIMEOUT = 180


class PlannerError(Exception):
    pass


def _headers(planner: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if planner.get("api_key"):
        h["Authorization"] = "Bearer " + planner["api_key"]
    return h


def _chat(planner: dict, prompt: str, system: str | None = None, timeout: int = DEFAULT_TIMEOUT):
    base = (planner.get("api_base") or "").rstrip("/")
    model = planner.get("api_model") or ""
    if not base or not model:
        raise PlannerError("未配置 api_base / api_model")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system or SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "temperature": float(planner.get("api_temp", 0.2)),
    }
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 headers=_headers(planner), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise PlannerError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PlannerError(str(exc)) from exc
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError):
        raise PlannerError("API 返回异常: " + json.dumps(data, ensure_ascii=False)[:300]) from None


async def ask(planner: dict, prompt: str, system: str | None = None) -> str:
    return await asyncio.to_thread(_chat, planner, prompt, system)


CHAT_SYSTEM = ("你是本地代码助手, 直接在对话里回答用户。默认用简体中文, 回答精炼, "
               "需要贴代码就用 ``` 代码块, 不要输出多余的开场白。")


def build_messages(prompt: str, history: list | None = None, system: str | None = None) -> list[dict]:
    """把最近几轮对话 + 本条输入拼成 OpenAI 兼容的 messages(历史只留最近 20 条, 每条最多 8000 字)。"""
    out = [{"role": "system", "content": system or CHAT_SYSTEM}]
    for m in (history or [])[-20:]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        if role not in ("user", "assistant"):        # 本地执行卡片之类的记录不进上下文
            continue
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        out.append({"role": role, "content": text[-8000:]})
    out.append({"role": "user", "content": str(prompt or "")})
    return out


def _chunks_of(obj: dict) -> list[str]:
    """从一行 SSE JSON 里抠出文本增量(兼容 delta.content / reasoning_content 等常见字段)。"""
    out: list[str] = []
    for choice in (obj.get("choices") or []):
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            if isinstance(delta.get("content"), str):
                out.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                out.append(delta["reasoning_content"])
        msg = choice.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            out.append(msg["content"])
    return out


def _stream_chat(planner: dict, messages: list[dict], emit, timeout: int = DEFAULT_TIMEOUT) -> None:
    """流式请求(stream=true), 每段文本调用 emit(text); 站点不支持流式时退回整段。"""
    base = (planner.get("api_base") or "").rstrip("/")
    model = planner.get("api_model") or ""
    if not base or not model:
        raise PlannerError("未配置 api_base / api_model")
    body = {"model": model, "messages": messages, "stream": True,
            "temperature": float(planner.get("api_temp", 0.2))}
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 headers=_headers(planner), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "event-stream" not in ctype:                 # 不支持流式: 当整段 JSON 处理
                data = json.loads(resp.read().decode("utf-8", "replace"))
                try:
                    text = data["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, TypeError):
                    raise PlannerError("API 返回异常: " + json.dumps(data, ensure_ascii=False)[:300]) from None
                if text:
                    emit(text)
                return
            for raw in resp:                                # SSE: 一行一个 data: {...}
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except Exception:                           # noqa: BLE001
                    continue
                for piece in _chunks_of(obj):
                    if piece:
                        emit(piece)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise PlannerError(f"HTTP {exc.code}: {detail}") from exc
    except PlannerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PlannerError(str(exc)) from exc


async def ask_stream(planner: dict, messages: list[dict], *, on_chunk=None,
                     timeout: int = DEFAULT_TIMEOUT) -> str:
    """流式问一次, 每段增量 await on_chunk(text), 返回完整回答。"""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    DONE, FAIL = "__done__", "__fail__"

    def emit(piece):
        loop.call_soon_threadsafe(queue.put_nowait, (None, piece))

    def work():
        try:
            _stream_chat(planner, messages, lambda t: emit(t), timeout)
            loop.call_soon_threadsafe(queue.put_nowait, (DONE, ""))
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, (FAIL, str(exc)))

    task = asyncio.create_task(asyncio.to_thread(work))
    parts: list[str] = []
    while True:
        tag, piece = await queue.get()
        if tag == DONE:
            break
        if tag == FAIL:
            raise PlannerError(piece)
        parts.append(piece)
        if on_chunk is not None:
            await _maybe_await(on_chunk(piece))
    await task
    return "".join(parts)


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        await value
    return value


def _list_models(planner: dict, timeout: int = 30) -> list[str]:
    base = (planner.get("api_base") or "").rstrip("/")
    req = urllib.request.Request(base + "/models", headers=_headers(planner), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise PlannerError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PlannerError(str(exc)) from exc
    try:
        return [m.get("id", "") for m in data["data"] if isinstance(m, dict)]
    except (KeyError, TypeError):
        raise PlannerError("models 响应异常") from None


async def list_models(planner: dict, timeout: int = 30) -> list[str]:
    return await asyncio.to_thread(_list_models, planner, timeout)


# 常见的本地模型服务地址(llama.cpp / LM Studio / vLLM / Ollama 的 OpenAI 兼容口)
LOCAL_CANDIDATES = [
    "http://127.0.0.1:1234/v1",      # LM Studio
    "http://127.0.0.1:8080/v1",      # llama.cpp llama-server
    "http://127.0.0.1:8081/v1",
    "http://127.0.0.1:8000/v1",      # vLLM
    "http://127.0.0.1:11434/v1",     # Ollama
    "http://127.0.0.1:5000/v1",      # text-generation-webui
]


async def _probe(base: str, timeout: int) -> dict | None:
    try:
        models = [m for m in await list_models({"api_base": base}, timeout=timeout) if m]
    except Exception:  # noqa: BLE001
        return None
    return {"base": base, "models": models} if models else None


async def find_local(timeout: int = 2) -> list[dict]:
    """并发扫一遍常见端口, 返回能连上的本地模型服务(含可用模型列表)。"""
    results = await asyncio.gather(*[_probe(b, timeout) for b in LOCAL_CANDIDATES])
    return [r for r in results if r]
