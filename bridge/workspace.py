"""本地工作区: 读写都被限制在当前根目录内(路径越界一律拒绝)。

根目录默认是 workspace/, 可以在界面上改成任意本地目录(记在 .bridge_settings.json)。
"""
import os
from pathlib import Path

from . import config, settings

ROOT: Path = config.WORKSPACE_DIR


class WorkspaceError(Exception):
    """带消息的业务异常(由路由转成 400/404)。"""


def default_root() -> Path:
    return config.WORKSPACE_DIR


def _load_root() -> Path:
    """启动时按设置恢复上次选的工作区目录; 失效就退回默认。"""
    try:
        saved = (settings.load().get("workspace") or {}).get("root") or ""
    except Exception:  # noqa: BLE001
        saved = ""
    if saved:
        p = Path(saved).expanduser()
        if p.is_dir():
            return p.resolve()
    return default_root()


def ensure_root() -> Path:
    if ROOT == default_root():          # 只有默认目录才自动建目录/放 README
        ROOT.mkdir(parents=True, exist_ok=True)
        readme = ROOT / "README.txt"
        if not readme.exists():
            readme.write_text(
                "这是 Web LLM Bridge 的本地工作区。\n"
                "把要交给 AI 的代码/文件放进来, 或在左侧「工作区」面板里读取/编辑。\n"
                "AI 回答里的代码块可一键保存到这里(路径会被校验, 无法越出本目录)。\n"
                "想换成别的目录: 点侧栏「工作区」标题, 选一个本地文件夹即可。\n",
                encoding="utf-8")
    return ROOT


def info() -> dict:
    return {"root": str(ROOT), "name": ROOT.name or str(ROOT),
            "default": str(default_root()), "custom": ROOT != default_root()}


def set_root(path_str: str) -> dict:
    """切换工作区根目录(必须是一个已存在的目录; 空字符串 = 恢复默认)。"""
    global ROOT
    raw = (path_str or "").strip().strip('"')
    if not raw:
        ROOT = default_root()
        settings.save_workspace_root("")
        ensure_root()
        return info()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (ROOT / p)                       # 相对路径按当前根解析, 方便手输
    try:
        p = p.resolve()
    except OSError as exc:
        raise WorkspaceError(f"路径无法解析: {raw}") from exc
    if not p.exists():
        raise WorkspaceError(f"目录不存在: {p}")
    if not p.is_dir():
        raise WorkspaceError(f"不是目录: {p}")
    try:
        ROOT = p
        if not any(ROOT.iterdir()):          # 空目录也能用
            pass
    except PermissionError as exc:
        raise WorkspaceError(f"没有读取权限: {p}") from exc
    settings.save_workspace_root(str(p))
    return info()


def use_root(path) -> str:
    """临时换根: 只改内存里的 ROOT, **不写设置**(测试/临时用)。返回旧根, 用完自己还原。

    别拿 set_root 干这事: 它会把工作区根写进设置, 跑一遍测试就把用户配的项目目录冲掉了
    (set_root("") 更狠 —— 直接把用户的 root 清空)。
    """
    global ROOT
    old = str(ROOT)
    p = Path(path)
    ROOT = p.resolve() if p.is_absolute() else (ROOT / p).resolve()
    return old


def drives() -> list[str]:
    """本机可用的盘符(Windows)。"""
    out: list[str] = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            try:
                if os.path.exists(d):
                    out.append(d)
            except OSError:
                continue
    return out


def browse(path_str: str = "") -> dict:
    """给"选目录"用的只列子目录接口(仅本机 127.0.0.1 可用)。

    path 为空 = "这台电脑"视图: 只给盘符, 不列当前工作区里的子目录。
    """
    drv = drives()
    raw = (path_str or "").strip().strip('"')
    if not raw:
        return {"path": "", "parent": "", "dirs": [], "drives": drv,
                "atDrives": True, "isRoot": True}
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (ROOT / p)
    try:
        p = p.resolve()
    except OSError:
        p = default_root()
    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else default_root()
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            try:
                if child.is_dir() and not child.name.startswith("."):
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError:
        pass
    parent = str(p.parent)
    at_drive_root = p.parent == p
    return {"path": str(p), "parent": "" if at_drive_root else parent,
            "dirs": dirs, "drives": drv, "atDrives": False, "isRoot": at_drive_root}


ROOT = _load_root()
ensure_root()


def _abs(rel: str) -> Path:
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        return ROOT
    p = (ROOT / rel).resolve()
    try:
        p.relative_to(ROOT)
    except ValueError as exc:
        raise WorkspaceError(f"路径越界: {rel}") from exc
    return p


def list_dir(rel: str = "") -> list[dict]:
    """列出目录项(不递归)。目录在前; 跳过常见噪音目录/文件。"""
    d = _abs(rel)
    if not d.exists():
        raise WorkspaceError(f"不存在: {rel or '.'}")
    if not d.is_dir():
        raise WorkspaceError(f"不是目录: {rel or '.'}")
    items: list[dict] = []
    for child in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        relp = child.relative_to(ROOT).as_posix()
        if child.is_dir():
            if child.name in config.WORKSPACE_SKIP_DIRS or child.name.startswith("."):
                continue
            items.append({"path": relp, "kind": "dir", "size": None})
        else:
            if child.name.startswith("."):
                continue
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            items.append({"path": relp, "kind": "file", "size": size})
    return items


def read_file(rel: str) -> dict:
    p = _abs(rel)
    if not p.exists():
        raise WorkspaceError(f"不存在: {rel}")
    if not p.is_file():
        raise WorkspaceError(f"不是文件: {rel}")
    data = p.read_bytes()
    if len(data) > config.WORKSPACE_READ_MAX:
        raise WorkspaceError(f"文件过大(>{config.WORKSPACE_READ_MAX // 1024 // 1024}MB): {rel}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"binary": True, "size": len(data), "path": rel}
    return {"binary": False, "text": text, "size": len(data), "path": rel}


def write_file(rel: str, content: str) -> dict:
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise WorkspaceError("缺少文件名")
    if len(content) > config.WORKSPACE_WRITE_MAX:
        raise WorkspaceError(f"内容过长(>{config.WORKSPACE_WRITE_MAX}字符)")
    p = _abs(rel)
    if p.exists() and p.is_dir():
        raise WorkspaceError(f"目标是目录: {rel}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": rel, "size": len(content.encode("utf-8"))}


def delete(rel: str) -> dict:
    p = _abs(rel)
    if not p.exists():
        raise WorkspaceError(f"不存在: {rel}")
    if p.is_dir():
        try:
            next(p.iterdir())
            raise WorkspaceError(f"目录非空, 请先清空: {rel}")
        except StopIteration:
            pass
        p.rmdir()
    else:
        p.unlink()
    return {"ok": True, "path": rel}


def walk_files(rel: str = "") -> list[str]:
    """递归列出工作区内文本候选文件的相对路径(跳过噪音/隐藏)。"""
    out: list[str] = []
    d = _abs(rel)
    if not d.exists():
        return out
    stack = [d]
    while stack:
        cur = stack.pop()
        for child in sorted(cur.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir():
                if child.name in config.WORKSPACE_SKIP_DIRS or child.name.startswith("."):
                    continue
                stack.append(child)
            elif not child.name.startswith("."):
                out.append(child.relative_to(ROOT).as_posix())
    return out


def list_all(rel: str = "") -> list[dict]:
    """递归列出整棵树: 每层目录与文件(带完整相对路径), 跳过噪音目录。"""
    items: list[dict] = []
    d = _abs(rel or "")

    def rec(node: Path):
        try:
            children = sorted(node.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if child.name.startswith("."):
                continue
            rp = child.relative_to(ROOT).as_posix()
            if child.is_dir():
                if child.name in config.WORKSPACE_SKIP_DIRS:
                    continue
                items.append({"path": rp, "kind": "dir", "size": None})
                rec(child)
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                items.append({"path": rp, "kind": "file", "size": size})

    rec(d)
    return items
