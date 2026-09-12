"""纯本地文件操作器: ChatGPT 决定做什么, 这里只负责做。

职责只有两个:

    read_file  - 从当前工作区安全读取文件(文本给内容, 二进制只给信息)
    send_file  - 从当前工作区读取文件字节, 交给 BrowserManager.attach_files() 附加到网页

不调用本地模型、planner、engineer, 不规划、不判断意图、不决定下一步 —— 那些是上层(ChatGPT)的事。

路径一律按"工作区相对路径"解析, 交给 workspace._abs() 做越界检查,
所以 ../、绝对路径(D:\\...)都会被拒绝(异常 -> FileOperatorError -> HTTP 400)。
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from . import config, workspace
from .browser import UPLOAD_LIMIT_HINT


class FileOperatorError(Exception):
    """文件操作业务异常(路由转成 400)。"""


class UploadBlockedError(FileOperatorError):
    """站点侧不让传文件(上传次数/额度上限、类型不支持…)。

    单独一类是因为路由会把它**弹到页面上**, 而不是只回一个 JSON —— 用户得知道
    "文件没传上去"是站点拦的, 不是本地出错。
    """


def _normalize_path(path: str) -> str:
    """把外部传入的路径统一成工作区相对路径(Windows/Linux 分隔符都收)。"""
    if not isinstance(path, str):
        raise FileOperatorError("path 必须是字符串")

    value = path.strip().strip('"').strip("'").replace("\\", "/")

    # 去掉开头的 ./ 和多余的 /。
    parts = [part for part in value.split("/") if part not in ("", ".")]

    if any(part == ".." for part in parts):
        raise FileOperatorError(f"禁止访问工作区外部路径: {path}")

    normalized = "/".join(parts)
    if not normalized:
        raise FileOperatorError("path 不能为空")
    return normalized


def _resolve_file(path: str) -> tuple[str, Path]:
    """解析到工作区内的真实 Path; 返回 (相对路径, 绝对路径)。"""
    rel = _normalize_path(path)

    try:
        absolute = workspace._abs(rel)  # noqa: SLF001  (工作区越界检查在这里)
    except workspace.WorkspaceError as exc:
        raise FileOperatorError(str(exc)) from exc

    if not absolute.exists():
        raise FileOperatorError(f"文件不存在: {rel}")
    if not absolute.is_file():
        raise FileOperatorError(f"目标不是文件: {rel}")
    return rel, absolute


def _mime_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def read_file(path: str) -> dict:
    """读取工作区内的文件。

    文本文件: {"binary": False, "text": "...", "size": n, "path": "..."}
    二进制文件: {"binary": True, "size": n, "path": "..."}
    (沿用 workspace.read_file() 的 2MB 文本上限; 二进制不塞进对话。)
    """
    rel, _ = _resolve_file(path)

    try:
        result = workspace.read_file(rel)
    except workspace.WorkspaceError as exc:
        raise FileOperatorError(str(exc)) from exc

    if not isinstance(result, dict):
        raise FileOperatorError("workspace.read_file() 返回了无效结果")
    return result


def prepare_send_file(path: str) -> dict:
    """读出文件字节, 组装成 BrowserManager.attach_files() 要的格式。

    单文件上限沿用 config.MAX_FILE_MB(与 /api/chat_files 一致) —— 这里必须拦,
    否则整个文件会被读进内存再交给 Playwright。
    """
    rel, absolute = _resolve_file(path)

    try:
        size = absolute.stat().st_size
    except OSError as exc:
        raise FileOperatorError(f"读取文件信息失败: {rel}: {exc}") from exc

    if size > config.MAX_FILE_MB * 1024 * 1024:
        raise FileOperatorError(f"文件超过 {config.MAX_FILE_MB}MB 上限: {rel}")

    try:
        data = absolute.read_bytes()
    except OSError as exc:
        raise FileOperatorError(f"读取文件失败: {rel}: {exc}") from exc

    return {"name": absolute.name, "mime": _mime_type(absolute),
            "data": data, "path": rel, "size": len(data)}


async def send_file(manager, path: str) -> dict:
    """把工作区文件附加到当前网页的输入框(附件区)。

    注意这是 attach, 不是 submit:
        本地文件 -> input[type=file] -> 站点上传/预览
    不按 Enter。要不要真发出去(以及发什么文字)由上层协议决定,
    需要时再用 BrowserManager.send_text("") 发纯文件消息。
    """
    if manager is None:
        raise FileOperatorError("BrowserManager 不可用")
    if getattr(manager, "state", "") != "logged_in":
        raise FileOperatorError("浏览器未就绪/未登录")

    try:
        alive = await manager.ensure_alive()
    except Exception as exc:  # noqa: BLE001
        raise FileOperatorError(f"浏览器连接检查失败: {exc}") from exc
    if not alive:
        raise FileOperatorError("桥接浏览器窗口已关闭, 请先启动并登录")

    file_data = prepare_send_file(path)

    try:
        ok, errors = await manager.attach_files([{
            "name": file_data["name"],
            "mime": file_data["mime"],
            "data": file_data["data"],
        }])
    except Exception as exc:  # noqa: BLE001
        raise FileOperatorError(f"上传文件失败: {file_data['path']}: {exc}") from exc

    if ok != 1:
        # 站点自己说"传不上去"时给专门的异常 -> 路由会把站点原话弹到页面上
        notice = ""
        probe = getattr(manager, "upload_blocked_reason", None)
        if probe is not None:
            try:
                notice = await probe()
            except Exception:  # noqa: BLE001
                notice = ""
        if notice:
            raise UploadBlockedError(UPLOAD_LIMIT_HINT + notice)
        detail = "; ".join(str(item) for item in errors) if errors else "未知错误"
        raise FileOperatorError(f"文件未能附加到浏览器: {file_data['path']}: {detail}")

    return {"ok": True, "action": "send_file", "path": file_data["path"],
            "name": file_data["name"], "mime": file_data["mime"],
            "size": file_data["size"], "attached": True}
