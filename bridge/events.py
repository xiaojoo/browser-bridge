"""与桌面端约定的协议事件(全部为可 JSON 序列化的 dict)。

事件结构统一为 { "type": ..., "ts": <epoch>, ... 字段 }。
"""
import time
import uuid


def new_message_id() -> str:
    return uuid.uuid4().hex


def event(type_: str, **kw) -> dict:
    return {"type": type_, "ts": time.time(), **kw}


def status_event(**kw) -> dict:
    return event("status", **kw)


def info_event(text: str) -> dict:
    return event("info", text=text)


def message_start(mid: str, conversation_id) -> dict:
    return event("message_start", message_id=mid, conversation_id=conversation_id)


def delta(mid: str, conversation_id, kind: str, text: str, snapshot: bool = False) -> dict:
    """kind: text | reasoning ; snapshot=True 表示该 text 是整条内容快照(应替换而非追加)。"""
    return event(
        "delta", message_id=mid, conversation_id=conversation_id,
        kind=kind, text=text, snapshot=snapshot,
    )


def message_end(mid: str, conversation_id, truncated: bool = False) -> dict:
    return event("message_end", message_id=mid, conversation_id=conversation_id,
                 truncated=truncated)


def error_event(mid: str, conversation_id, text: str) -> dict:
    return event("error", message_id=mid, conversation_id=conversation_id, text=text)
