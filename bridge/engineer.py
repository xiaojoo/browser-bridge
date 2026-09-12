"""编码执行器(Phase 2): 规划 -> 分步实现 -> 校验落盘 -> 摘要。

模型不需要工具调用: 我们要求它只输出约定的变更清单 JSON(```json 代码块),
本地负责解析、路径校验与写盘。网页模型输出偶尔不稳定 -> 带错误信息重试一次。
"""
import difflib
import json
import logging
import re
from pathlib import Path

from . import capture, config, workspace

log = logging.getLogger("engineer")

PLAN_STEPS_MAX = 5
REPAIR_ATTEMPTS = 1          # 清单解析失败后的修复重试次数
CTX_TOTAL_BUDGET = 250_000   # 自动注入上下文的总字符预算
CTX_FILE_BUDGET = 100_000    # 单文件预算

_MANIFEST_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


# ---------------- 解析 ----------------
_OP_ZH = {"新建": "create", "创建": "create", "新增": "create", "添加": "create",
          "修改": "update", "更新": "update", "编辑": "update", "改为": "update",
          "删除": "delete", "移除": "delete", "删": "delete"}
_OP_EN = {"create": "create", "new": "create", "add": "create",
          "update": "update", "edit": "update", "modify": "update",
          "delete": "delete", "remove": "delete", "del": "delete"}


def _parse_loose(text: str):
    """兜底: 解析 '操作 路径 内容' 的表格/列表(模型不按协议时的退路)。

    只认**看着像文件路径**的路径列, 而且不带内容的"新建/修改"行一律不算 —— 因为
    以前任何以"删除/remove"开头的行都算一条文件改动, 于是代码里的 `remove(id: number) {`
    和表格里孤零零一个"删除"单元格都变成了"删除某个文件(路径为空)", 这些假清单又把
    真正的代码围栏整个挤掉: 97k 字的回答里 30 个文件一个都没认出来(踩过)。
    """
    ops = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    for line in lines:
        s = line.strip()
        if not s:
            continue
        cells = [c.strip() for c in s.replace("|", "\t").split("\t") if c.strip()]
        if not cells:
            continue
        head = cells[0].lower()
        if head in ("操作", "文件路径", "内容", "action", "path", "op", "type"):
            continue
        op = None
        for k, v in {**_OP_ZH, **_OP_EN}.items():
            if head.startswith(k):
                op = v
                break
        if not op or len(cells) < 2:
            continue
        path = cells[1].strip("`").strip()
        content = cells[2] if len(cells) > 2 else ""
        if not _looks_like_path(path):
            continue
        if op != "delete" and not content.strip():
            continue          # 表格里没带内容 -> 内容在代码围栏里, 不该由这一行来写
        ops.append({"op": op, "path": path, "content": content})
    if ops:
        return {"message": "loose-parse", "files": ops}
    return None


def _has_file_entries(obj) -> bool:
    """这是不是一份**可用的**变更清单?

    放宽的两点(都必须保留, 否则工程流程会走错):
      * `{"files": []}` 算清单 —— 它是模型在说"没有改动", 上层靠它决定要不要催它重答;
      * 条目是对象时至少得有一个带 path 的, 光有 `"files": ["dist"]`(package.json 的字段)
        不算清单, 否则真代码围栏会被它顶掉(踩过: 97k 字的回答 30 个文件全没认出来)。
    """
    files = obj.get("files") if isinstance(obj, dict) else None
    if not isinstance(files, list):
        return False
    if not files:
        return True
    return any(isinstance(f, dict) and str(f.get("path") or "").strip() for f in files)


def extract_manifest(text: str):
    """返回 (obj, source)。从回答里提取变更清单; 找不到返回 (None, reason)。"""
    text = strip_fence_labels(text)
    candidates = []
    for m in _MANIFEST_RE.finditer(text or ""):
        candidates.append(m.group(1))
    candidates.append((text or "").strip())
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if _has_file_entries(obj):
            return obj, "json-fence"
    # 兜底: 从任意 '{' 开始 raw_decode(能容忍 JSON 前后混有其它文字/围栏缺失)
    dec = json.JSONDecoder()
    text = text or ""
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except Exception:
            continue
        if _has_file_entries(obj):
            return obj, "raw-object"
    # 表格兜底只在**围栏之外**的正文里找: 代码块里的 `remove(id) {`、表格里一个"删除"单元格
    # 都不是变更清单(否则会把真正的代码围栏挤掉)。
    prose = re.sub(r"```.*?```", "\n", text, flags=re.S)
    loose = _parse_loose(prose)
    if loose:
        return loose, "loose-table"
    return None, "未找到含 files 数组的 JSON 清单"


def _plan_obj(text: str):
    """从模型回答里取出规划 JSON(容忍围栏缺失/前后有闲话)。"""
    raw = (text or "").strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            pass
    for m in _MANIFEST_RE.finditer(text or ""):
        try:
            return json.loads(m.group(1))
        except Exception:
            continue
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode((text or "")[i:])
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("steps"), list):
            return obj
    return None


def extract_plan(text: str) -> list:
    """返回 [{'text': 步骤, 'files': [相对路径]}]。兼容老的 {'steps': ['步骤1', ...]}。"""
    obj = _plan_obj(text)
    if not obj or not isinstance(obj.get("steps"), list):
        return []
    top_files = [str(f).strip().replace("\\", "/") for f in (obj.get("files") or []) if str(f).strip()]
    out = []
    for s in obj["steps"]:
        if isinstance(s, str):
            if s.strip():
                out.append({"text": s.strip(), "files": list(top_files), "change": True})
        elif isinstance(s, dict):
            t = str(s.get("text") or s.get("task") or s.get("step") or s.get("title") or "").strip()
            files = [str(f).strip().replace("\\", "/") for f in (s.get("files") or []) if str(f).strip()]
            change = s.get("change")
            if isinstance(change, str):
                change = change.strip().lower() not in ("false", "0", "no", "否", "不需要")
            if t:
                out.append({"text": t, "files": files or list(top_files),
                            "change": True if change is None else bool(change)})
    return out


def extract_steps(text: str) -> list:
    """兼容老接口: 只要步骤文字。"""
    return [s["text"] for s in extract_plan(text)]


# ---------------- 清单校验与落盘 ----------------
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:")


# ---------------- 落盘前的"疑似毁坏"防护 ----------------
# 背景: 整文件替换的写法下, 本地模型只要抽风就会把大文件写成一个残file ——
# 实际发生过两回: qml/components/EditorArea.qml 被写成根目录的 27 字节同名文件,
# 以及预览里的 "+167/-616 行 · 5541B"(把 600 多行的文件换成 5KB)。
# 一律拒绝会挡死正常的重构, 所以: 默认跳过 + 明确说明原因, 条目上带 "force": true 才硬来。
_DAMAGE_PREFIX = "疑似"
_TRUNCATE_MIN_OLD = 2000      # 旧文件至少这么大, 才谈得上"截断"
_TINY_NEW = 200               # 新内容小到这个程度就可疑
_FAT_OLD = 2000               # 判定"另一边那个同名文件是真文件"的门槛


def _read_old(op: str, path: str) -> str:
    """目标文件当前内容(读不到就当空)。"""
    if op == "delete":
        return ""
    try:
        return workspace.read_file(path)["text"].replace("\r\n", "\n")
    except Exception:  # noqa: BLE001
        return ""


def _basename_elsewhere(path: str) -> str:
    """工作区**子目录**里是否已有同名的大文件 —— 用来认"路径写错, 在根目录留了个残file"。"""
    name = path.rsplit("/", 1)[-1].lower()
    if not name:
        return ""
    for rel in workspace.walk_files():
        if rel == path or "/" not in rel:
            continue
        if rel.rsplit("/", 1)[-1].lower() != name:
            continue
        try:
            if (workspace.ROOT / rel).stat().st_size >= _FAT_OLD:
                return rel
        except OSError:
            continue
    return ""


def _damage_reason(op: str, path: str, content: str) -> str:
    """这条写入会不会把项目改坏? 返回原因(空 = 放行)。"""
    new = content or ""
    old = _read_old(op, path)
    if op == "update" and old:
        if len(old) >= _TRUNCATE_MIN_OLD and len(new) < max(_TINY_NEW, len(old) // 3):
            return (f"{_DAMAGE_PREFIX}截断: {path} 现在 {len(old)} 字符, 新内容只有 {len(new)} 字符"
                    f" —— 整文件替换不该大幅缩水。确认真要这样改, 就在这条上加 \"force\": true")
        return ""
    # update 到不存在的文件 = 事实上的新建; 在根目录放一个很小的同名残file, 基本都是路径写错
    if op in ("update", "create") and not old and "/" not in path and len(new) < _TINY_NEW:
        other = _basename_elsewhere(path)
        if other:
            return (f"{_DAMAGE_PREFIX}路径写错: 工作区里 {other} 已经是同名文件, "
                    f"这里却要在根目录新建一个只有 {len(new)} 字符的 {path}"
                    f"(确认真要新建就在这条上加 \"force\": true)")
    return ""


def _check_op(item: dict) -> tuple[str, str, str | None, str | None]:
    """校验一条清单条目, 返回 (op, path, content, err); err 非空 = 这条要跳过。

    错误文案必须走 err 这一槽: 以前它被塞进 path 槽, 下游就把
    「非法路径: .gitignore」当成文件名播出去, 日志里全是叫人看不懂的行。
    """
    op = str(item.get("op", "")).lower()
    if op not in ("create", "update", "delete"):
        return "", "", None, f"未知 op: {op}"
    raw = str(item.get("path", "")).strip().replace("\\", "/")
    path = raw.lstrip("/")
    # 只拦真正危险的路径: 空/越界/绝对路径/仓库内部(.git)。
    # 普通点文件(.gitignore、.github/…、.env)是正常目标, 不该一律拒绝。
    if (not path or raw.startswith("/") or _WIN_ABS_RE.match(raw)
            or ".." in path.split("/") or ".git" in path.split("/")):
        return "", "", None, f"非法路径: {item.get('path')}"
    content = item.get("content")
    if op in ("create", "update") and not isinstance(content, str):
        return "", "", None, f"{op} 缺少 content: {path}"
    if not item.get("force"):            # force: true = 已经确认过, 跳过毁坏防护
        reason = _damage_reason(op, path, content)
        if reason:
            return "", "", None, reason
    return op, path, content, None


def _item_path(item) -> str:
    """日志/预览里"这条指的是哪个文件"; 拿不到路径就退回整条目, 绝不返回错误文案。"""
    try:
        return str(item.get("path") or "").strip() or str(item)
    except AttributeError:
        return str(item)


def _diff_stats(old: str, new: str) -> dict:
    """预览与落盘共用的一份行数/字节数统计。"""
    sm = difflib.SequenceMatcher(None, old.splitlines(keepends=True), new.splitlines(keepends=True))
    add = dele = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            add += j2 - j1
        if tag in ("delete", "replace"):
            dele += i2 - i1
    return {"oldLines": len(old.splitlines()), "newLines": len(new.splitlines()),
            "add": add, "del": dele, "size": len(new.encode("utf-8")),
            "preview": new[:4000], "unchanged": bool(old) and old == new}


def preview_manifest(obj: dict) -> list[dict]:
    """只看不改: 给确认框用的预览(每个文件的操作/行数/新旧内容片段)。

    毁坏防护拦下的条目走 "warn": 带上统计和 force 标记 —— 界面上默认**不勾选**,
    用户看清"要缩水多少"之后可以自己勾上硬来(直接落盘时则一律跳过)。
    """
    out: list[dict] = []
    for item in obj.get("files") or []:
        op, path, content, err = _check_op(item)
        if err:
            entry: dict = {"op": "invalid", "path": _item_path(item), "error": err}
            if err.startswith(_DAMAGE_PREFIX):
                real_op = str(item.get("op", "")).lower()
                shown = _item_path(item)
                entry = {"op": "warn", "path": shown, "error": err, "force": True,
                         "realOp": real_op, "content": item.get("content") or ""}
                entry.update(_diff_stats(_read_old(real_op, shown), item.get("content") or ""))
            out.append(entry)
            continue
        if op == "delete":
            old = _read_old(op, path)
            out.append({"op": op, "path": path, "oldLines": len(old.splitlines()), "newLines": 0,
                        "preview": "", "exists": bool(old)})
            continue
        out.append({"op": op, "path": path, **_diff_stats(_read_old(op, path), content or "")})
    return out


def apply_manifest(obj: dict):
    """逐条校验并落盘; 返回 (applied, skipped, diffs)。"""
    applied: list[dict] = []
    skipped: list[str] = []
    diffs: list[dict] = []
    for item in obj.get("files") or []:
        op, path, content, err = _check_op(item)
        if err:
            skipped.append(f"{_item_path(item)}: {err}")
            continue
        try:
            if op == "delete":
                workspace.delete(path)
                applied.append({"path": path, "op": "delete"})
            else:
                old = _read_old(op, path)
                if content.replace("\r\n", "\n") != old:
                    res = workspace.write_file(path, content)
                    applied.append({"path": path, "op": op, "size": res["size"]})
                    if old:
                        st = _diff_stats(old, content)
                        diffs.append({"path": path, "add": st["add"], "del": st["del"]})
                    else:
                        diffs.append({"path": path, "add": len(content.splitlines()),
                                      "del": 0})
                else:
                    diffs.append({"path": path, "add": 0, "del": 0})   # 无变化
        except workspace.WorkspaceError as exc:
            skipped.append(f"{path}: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("apply %s", path)
            skipped.append(f"{path}: {exc}")
    return applied, skipped, diffs


# ---------------- Provider 调用 ----------------
async def provider_ask(manager, prompt: str) -> str:
    """在网页对话里发送 prompt 并捕获完整回答文本(不广播到聊天 UI)。"""
    page = manager.page
    mode = manager.provider.capture_mode
    stream_on = mode == "stream"
    buf: list[str] = []

    def on_delta(kind, text, snapshot):
        if kind == "text":
            if snapshot:
                buf.clear()
                buf.append(text)
            else:
                buf.append(text)

    try:
        if stream_on:
            await capture.enable_capture(page)
        await manager.send_text(prompt)
        try:
            _, _ = await capture.wait_turn_end(
                page, on_delta, mode=mode,
                snapshot_selector=manager.provider.snapshot_selector)
        except capture.NoDataError as exc:
            log.warning("engineer provider_ask NoData: %s", exc)
            return ""
    finally:
        if stream_on:
            await capture.disable_capture(page)
    return "".join(buf)


# ---------------- 提示词 ----------------
def repo_line(repo: str = "") -> str:
    return ("\n\n远程仓库: " + repo.strip() + "（需要看代码时可以自行拉取/查看; 也可以只说要哪些文件, 由本地补发）"
            ) if (repo or "").strip() else ""


def plan_prompt(task: str, tree_lines: list[str], repo: str = "") -> str:
    return (
        "你是资深工程师, 负责拆解任务并挑出实现每一步时**真正需要看**的文件。\n"
        "用户任务:\n" + task +
        repo_line(repo) +
        "\n\n本地工作区文件:\n" + "\n".join(tree_lines[:400] or ["(空)"]) +
        "\n\n只输出一个 ```json``` 代码块, 形如:\n"
        '{"steps": [{"text": "这一步要改什么(写清楚改哪个文件、怎么改)", '
        '"files": ["这一步需要读的文件相对路径"], "change": true}], "note": "一句话"}\n'
        f"硬性要求:\n"
        f"1. 按依赖顺序、不超过 {PLAN_STEPS_MAX} 步;\n"
        "2. 每一步都必须是**要动手改代码/文件的动作**(改哪个文件、改成什么样), change 填 true;\n"
        "3. 不要出现『查看/确认/分析/检查/了解/运行验证』这类只读步骤 —— 需要看的文件就放进同一步的 files 里, 边看边改;\n"
        "4. files 只列这一步真正要读的文件(宁少勿多, 不要把全部文件都列上), 路径必须来自上面的文件列表;\n"
        "5. 只有纯粹输出结论、确实不需要改任何文件时才允许 change=false(尽量少用)。\n"
        "代码块之外不要输出任何文字。"
    )


def implement_prompt(step: str, tree_lines: list[str], context: str, prev_log: str = "",
                     repo: str = "", label: str = "") -> str:
    return (
        "【Web LLM Bridge 工程任务" + ((" · 第 " + label + " 步") if label else "") + "】\n" + step + "\n\n" +
        "You are an automated coding agent. This is a FILE-CHANGE PROTOCOL task: "
        "actually writing files is done by an external system, so NEVER say you cannot write "
        "files and NEVER give explanations or tables. "
        "If this step asks for a change, you MUST return the full new content of every changed file "
        "in the manifest — do not reply with analysis only.\n\n"
        "实现子任务:\n" + step +
        repo_line(repo) +
        "\n\n本地工作区文件:\n" + "\n".join(tree_lines[:400] or ["(空)"]) +
        (("\n\n相关文件内容(只给了这一步需要的):\n" + context) if context else
         "\n\n(这一步没有附带已有文件内容, 如需读取请说明需要哪个文件)") +
        (("\n\n" + prev_log) if prev_log else "") +
        "\n\n只输出一个 ```json``` 代码块, 严格按照这个形状(把占位符替换为实际内容, "
        "content 为完整文件内容, 内部换行用 \\n):\n"
        "```json\n"
        '{"message": "一句话说明", "files": [\n'
        '  {"op": "create", "path": "新文件的相对路径", "content": "完整内容"},\n'
        '  {"op": "update", "path": "已存在文件路径", "content": "替换后的完整内容"},\n'
        '  {"op": "delete", "path": "要删除的文件路径"}\n'
        "  ]}\n"
        "```\n"
        "规则: 只允许操作上面列出的工作区文件; create/update 必须给完整 content(整文件替换); "
        "不要省略已有代码; delete 不含 content。代码块之外不要输出任何文字。"
    )


def repair_prompt(original: str, err: str, raw_snip: str) -> str:
    return (
        "Your previous answer did not follow the FILE-CHANGE PROTOCOL. You do not need to "
        "write files yourself — an external system applies them, so just output the protocol.\n"
        "原因: " + err +
        "\n你的原回答(节选):\n" + raw_snip[-500:] +
        "\n请重新只输出一个 ```json``` 代码块(含 files 数组, 元素字段 op/path/content), "
        "不要再输出任何解释或表格。"
    )


# 扩展名表: **长的必须排在它自己的前缀前面**(html 在 h 前、json 在 js 前; vue 别漏) ——
# 否则 "index.html" 会被匹配成 "index.h", "src/App.vue" 直接认不出来(踩过)。
_PATH_EXTS = ("cmake", "scss", "hpp", "json", "jsx", "tsx", "cpp", "cxx", "html", "swift",
              "svelte", "astro", "prisma", "gradle", "qml", "toml", "yaml", "yml", "java",
              "php", "txt", "css", "vue", "ts", "js", "cc", "mm", "md", "py", "kt", "kts",
              "go", "rs", "rb", "cs", "h", "m", "c", "sql", "ini", "sh", "ps1", "bat",
              "xml", "lua", "pro", "pri")
_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.(?:" + "|".join(_PATH_EXTS) + ")")


# 代码围栏: ```lang\n...\n```
_FENCE_RE = re.compile(r"```[ \t]*([^\n`]*)\n(.*?)(?:```|\Z)", re.S)
_COMMENT_HEADS = ("#", "//", "/*", "*", "<!--", "--", ";", "%")

# 站点有时把语言名渲染成**围栏正文的第一行**:
#     ```
#     JSON
#     { "name": "..." }
#     ```
# 原样落盘的话每个文件第一行都会多出 "JSON"/"HTML"/"TypeScript", package.json 直接不是合法
# JSON(踩过: 写进去的工程全废)。只认这些"孤零零一个语言名"的行, 别的一个字不动。
_LANG_LABELS = {
    "json", "jsonc", "html", "htm", "xml", "css", "scss", "sass", "less", "stylus",
    "typescript", "ts", "tsx", "javascript", "js", "jsx", "mjs", "cjs", "vue", "svelte",
    "astro", "python", "py", "java", "kotlin", "kt", "kts", "go", "golang", "rust", "rs",
    "c", "cpp", "c++", "cxx", "csharp", "cs", "php", "ruby", "rb", "swift", "lua", "sql",
    "yaml", "yml", "toml", "ini", "conf", "cfg", "sh", "bash", "zsh", "shell", "powershell",
    "ps1", "bat", "cmd", "dockerfile", "makefile", "markdown", "md", "text", "txt",
    "plaintext", "plain", "console", "log", "http", "graphql", "gql", "prisma", "gradle",
    "qml", "pro", "pri", "diff", "patch", "csv", "env", "nginx", "sass", "wasm", "asm",
}

_FENCE_HEAD_RE = re.compile(r"```[ \t]*[^\n`]*\n[ \t]*([A-Za-z][A-Za-z0-9+#._-]{0,19})[ \t]*\n")


def strip_fence_labels(text: str) -> str:
    """去掉围栏正文第一行那个"孤零零的语言名"(站点渲染成这样, 不是文件内容)。"""
    def repl(m):
        label = m.group(1).lower().rstrip(":：")
        return "```\n" if label in _LANG_LABELS else m.group(0)
    return _FENCE_HEAD_RE.sub(repl, text or "")


def _resolve_any(raw: str, tree_set: set, base: dict) -> str:
    """把一段文本里抠出来的路径对齐到工作区; 对不上就按"新文件"原样收下(仅限看着像文件名的)。"""
    tokens = str(raw or "").strip().strip("`*\"'()[]<>").replace("\\", "/").split()
    if not tokens:
        return ""
    cand = tokens[0].lstrip("./")
    if not cand:
        return ""
    if cand in tree_set:
        return cand
    hit = base.get(cand.split("/")[-1])
    if hit:
        return hit
    return cand if _PATH_RE.fullmatch(cand) else ""


def _looks_like_path(p: str) -> bool:
    """这个字符串是不是一个"像文件"的路径(必须带已知扩展名, 不能是绝对路径/上跳路径)。"""
    p = (p or "").strip().strip("`*\"'")
    if not p or len(p) > 300:
        return False
    p = p.replace("\\", "/")
    if p.startswith("./") or p.startswith("../") or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return False
    return bool(_PATH_RE.fullmatch(p))


def _path_from_info(info: str) -> str:
    """围栏的信息串里找文件名: ```python main.py / ```main.py / ```py title=src/a.py"""
    low = (info or "").lower()
    for key in ("title=", "filename=", "file=", "path=", "name="):
        i = low.find(key)
        if i >= 0:
            m = _PATH_RE.search(info[i + len(key):])
            if m:
                return m.group(0)
    m = _PATH_RE.search(info or "")
    return m.group(0) if m else ""


def _is_manifest_block(body: str) -> bool:
    """这段围栏本身是不是"变更清单 JSON"(而不是某个文件的代码)?

    只看**形状**: files 是数组, 且条目是对象。这样 package.json 里的 `"files": ["dist"]` 仍然
    算文件内容(它确实是那个文件), `{"files":[{"op":"delete","path":""}]}` 不会被评为"一段没认出
    文件名的代码"; 而 **tsconfig.json 里的 `"files": []` 必须当成文件内容**, 不能当成清单
    (踩过: 那一版 tsconfig.json 整个没写进去)。
    """
    t = (body or "").strip()
    if not t.startswith("{"):
        return False
    try:
        obj = json.loads(t)
    except Exception:
        return False
    files = obj.get("files") if isinstance(obj, dict) else None
    return isinstance(files, list) and any(isinstance(f, dict) for f in files)


def _dedupe_items(items: list[dict]) -> list[dict]:
    """同一路径出现多次: 留最后一次(通常是改过之后的完整版本), 顺序按首次出现。"""
    out: dict = {}
    order: list = []
    for it in items:
        p = it.get("path") or ""
        if not p:
            continue
        if p not in out:
            order.append(p)
        out[p] = it
    return [out[p] for p in order]


_CREATE_WORDS = ("新建", "创建", "新增", "添加", "create", "add ", "new file")

# 围栏前那几行里"在说某个文件"的说法: 工作区里没有这个路径时, 只有跟着这些词才认它
# (不然"我建议你用 Qt 的 ScrollView"这种句子里的词就会被误当成文件名)
_FILE_TALK_WORDS = _CREATE_WORDS + (
    "修改", "更新", "替换", "删除", "改成", "改为", "写入", "重写", "补上", "改一下",
    "文件", "路径", "file", "path", "update", "modify", "change", "replace", "delete",
    "rewrite", "patch", "fix")


def _looks_like_name_line(ln: str, path: str) -> bool:
    """这一行是不是"就写了这一个文件名"(标题/加粗/项目符号/编号/行尾冒号)?

    网页模型的回答几乎都这么写, 所以这条规则很值钱:
        ### 1. `package.json`      **src/App.vue**      - src/api/request.ts:
    但一行里还夹着别的说明就不算(例如"### 使用 package.json 里的配置")。
    """
    head = re.sub(r"^[\s>#*\-•·]+", "", (ln or "").strip())     # 标题/项目符号
    head = re.sub(r"^\d+[.、)]\s*", "", head)                    # 编号
    head = head.strip().strip("`*_'\"")
    head = head.rstrip(":：").strip()
    if len(head) > 120:
        return False
    head = head.replace("`", "").replace("*", "").strip()
    if not path or not head.endswith(path):
        return False
    rest = head[: -len(path)].strip(" :：,，-—()（）[]【】")
    return rest in ("", "文件", "file", "文件名", "路径", "path", "new file")


def _nearby_path(before: str, tree_set: set, base: dict) -> str:
    """围栏**前面几行**里提到的文件: 工作区里真实存在的优先;

    不存在的路径只有在"这行确实在说某个文件"时才认(否则宁可不猜)。三种认法:
      1. 路径就在工作区里(最可信);
      2. 同行有"修改/新建/文件/path"这类说法;
      3. 这一行**就是在写一个文件名**(标题行/加粗/编号: `### 8. src/App.vue`)。
    只看叶子行, 而且调用方已经把其它代码围栏从这段文字里剔掉了 —— 否则上一段围栏信息串里的
    文件名会被这一段"抢"走。
    """
    lines = [ln for ln in (before or "").splitlines() if ln.strip()]
    for ln in reversed(lines[-6:]):
        cands = [c for c in (_resolve_any(t, tree_set, base) for t in _PATH_RE.findall(ln)) if c]
        if not cands:
            continue
        intree = [c for c in cands if c in tree_set]
        if intree:
            return intree[-1]
        if any(w in ln.lower() for w in _FILE_TALK_WORDS):
            return cands[-1]
        named = [c for c in cands if _looks_like_name_line(ln, c)]
        if named:
            return named[-1]
    return ""


def extract_code_files(answer: str, tree_lines: list[str]) -> tuple[list[dict], int]:
    """从网页模型的回答里**机械地**抽出"要写哪个文件、内容是什么" —— 不经过任何模型。

    这是"本地模型不写代码"的关键: 只做整理/对号入座, 内容**原样照抄**回答里的代码,
    一个字都不改、不补、不猜。写不写由用户在卡片上勾选。

    返回 (items, loose):
        items: [{"op": "update|create|delete", "path": ..., "content": ..., "source": ...}]
        loose: 有几段代码**没认出文件名**(不猜, 让用户/网页模型写明文件名)

    认文件名的顺序:
        1. 回答里直接给了变更清单 ```json {"files":[...]}``` -> 用它(网页模型自己写的协议)
        2. 代码围栏的信息串里带路径(```python main.py)
        3. 围栏里第一行注释带路径(# main.py / // main.cpp)
        4. 围栏**前面 6 行**里提到的、工作区里真实存在的路径
    """
    a = strip_fence_labels(answer or "")      # 去掉"围栏第一行的语言名", 免得它变成文件第一行
    tree_set = set(tree_lines or [])
    base = {p.split("/")[-1]: p for p in tree_set}

    obj, _src = extract_manifest(a)
    if obj and (obj.get("files") or []):          # 网页模型自己给了清单 -> 直接用
        items: list[dict] = []
        for it in obj["files"]:
            if not isinstance(it, dict):
                continue
            p = _resolve_any(str(it.get("path") or ""), tree_set, base)
            if not p:
                continue
            op = str(it.get("op") or "").lower()
            items.append({"op": op if op in ("create", "update", "delete") else "update",
                          "path": p,
                          "content": it.get("content") if isinstance(it.get("content"), str) else "",
                          "force": bool(it.get("force")), "source": "manifest"})
        if items:
            return _dedupe_items(items), 0
        # 清单里一条都没对上(路径全是空的/对不上工作区) -> 不拿它当结论, 继续按代码围栏认文件名
        log.info("清单解析出来 %d 条但一条都没对上工作区, 改按代码围栏整理", len(obj["files"]))

    items, loose = [], 0
    matches = list(_FENCE_RE.finditer(a))
    prev_end = 0
    for m in matches:
        prose_before = a[prev_end:m.start()]          # 两段围栏之间的正文(不含围栏自身)
        prev_end = m.end()
        info, body = (m.group(1) or "").strip(), m.group(2) or ""
        if not body.strip():
            continue
        if _is_manifest_block(body):
            continue              # 清单本身不是"某个文件的代码", 也别算成没认出文件名的代码块
        path = _resolve_any(_path_from_info(info), tree_set, base)
        if not path:
            first = body.strip().splitlines()[0] if body.strip() else ""
            if first.lstrip().startswith(_COMMENT_HEADS):
                path = _resolve_any(first, tree_set, base)
        if not path:
            path = _nearby_path(prose_before, tree_set, base)
        if not path:
            loose += 1                            # 认不出就不猜
            continue
        items.append({"op": "update" if path in tree_set else "create", "path": path,
                      "content": body.rstrip("\n") + "\n", "source": "fence"})
    return _dedupe_items(items), loose


def guess_paths(answer: str, tree_lines: list[str], limit: int = 6) -> list[str]:
    """回答里出现过的文件路径(含只写在说明文字里的), 按出现顺序、去重。"""
    tree = set(tree_lines or [])
    base = {p.split("/")[-1]: p for p in tree}
    out: list[str] = []
    for m in _PATH_RE.finditer(answer or ""):
        raw = m.group(0).replace("\\", "/").lstrip("./")
        cand = raw if raw in tree else base.get(raw.split("/")[-1], "")
        if cand and cand not in out:
            out.append(cand)
        if len(out) >= limit:
            break
    return out


def checks_text(checks) -> str:
    """把模型给的验收点渲染成一段可沿用的文字(第 1 轮定下, 之后每轮原样带上)。"""
    out: list[str] = []
    for i, c in enumerate(checks or [], 1):
        if isinstance(c, dict):
            exp = str(c.get("expect") or c.get("text") or c.get("check") or "").strip()
            how = str(c.get("how") or "").strip()
            if exp:
                out.append(f"- [{c.get('id') or i}] {exp}" + (f"(验法: {how})" if how else ""))
        elif isinstance(c, str) and c.strip():
            out.append(f"- [{i}] {c.strip()}")
    return "\n".join(out)


def verify_prompt(task: str, applied: str, tree_lines: list[str], last_cmd: str,
                  last_output: str, round_no: int, max_rounds: int, repo: str = "",
                  criteria: str = "", preferred_cmd: str = "") -> str:
    """本地模型自己验证: 先把需求变成验收点, 再找证据、改代码, 直到通过。"""
    return (
        "你是本地工程助手, 正在验证一个真实项目**是否真的满足了这次的需求** —— 不是「能编译就算完」。\n"
        "**运行环境是 Windows + PowerShell/cmd, 不是 Linux**: 没有 cat/sed/grep/ls/head/tail; "
        "看代码用 powershell -NoProfile -Command Get-Content -Raw 加路径, 或用 type; 不要用 for/do/done 这类循环。\n"
        "\n【1. 先把需求变成验收点】从下面的【用户需求】里提炼 1~3 条**可判定**的验收点: "
        "每条写成「什么输入/操作 → 期望结果」, 要能在命令行上判真假; 放进 checks 字段(id/expect/how)。"
        "验收点一旦定下就沿用, 之后每轮都报同一套, 不要中途换题。\n"
        "【2. 再找证据】优先跑项目自带的入口(先看 CMakeLists.txt 有没有 ctest/enable_testing、"
        "package.json 的 test、pytest.ini/pyproject.toml、Makefile, 判断该跑什么); "
        "需求是**行为性**的(去重、排序、边界、快捷键、数据格式…)就**必须真正执行这个行为**: "
        "跑自带测试、运行刚构建出来的可执行文件、或写一个最小的临时验证程序; "
        "项目没有测试入口时, 临时验证程序放在 `.verify/` 目录(用项目已有的编译器/解释器), 跑完删掉。\n"
        "**构建通过 ≠ 需求满足**: 它只证明能编译, 不能当行为性验收点的证据。\n"
        "**只看文件不算验证**: Get-Content/type/dir 是了解代码, 不是验证; 要读代码就和真正要跑的命令放同一轮, "
        "不要把读文件当成一轮的成果。\n"
        "\n【3. 每轮只做一件事】\n"
        "- run: 跑一条命令, 并用 verifies 说明它在验哪几条验收点(如 [1,2])。\n"
        "- done: **只有拿到证据才能用** —— evidence 必须写清: 哪条验收点、**这次真跑过的**那条命令、"
        "以及输出里的**原文片段**(照抄, 不要改写/不要凭印象写); 推荐用列表形式逐条给。\n"
        "**你不能改代码**(本地模型只负责跑命令、看输出、报结论): 命令失败时先自查是不是命令/路径/语法"
        "写错了(换一条 Windows 命令重试 run); 确认是代码本身的问题时, 用 done 把「问题在哪个文件的"
        "什么位置、要怎么改」说清楚, 让用户决定 —— 不要给出 files 去改文件, 服务端也会拦住。"
        "【4. 证据的三条硬要求(达不到就当没验过)】\n"
        "a) 证据只能引用**这次真的跑过、并且退出码为 0** 的命令与输出; 不许拿别处的构建、别的目录/别的副本"
        "(比如项目的旧快照)当证据, 也不许把\"应该没问题\"写成证据。\n"
        "b) 行为性验收点必须**真的执行行为**(跑自带测试、跑刚构建出来的程序、写最小验证程序), "
        "`qmllint`/编译通过只能证明语法, 不能当行为证据 —— 这一点由服务端强制校验。\n"
        "c) 命令用 **复合/包装写法**(例如 `... ; 'EXITCODE=' + $LASTEXITCODE`、`... | Select-Object`)时, "
        "shell 的退出码经常是 0 而真实结果在输出里: 服务端会按输出里最后那个 `XXX_EXIT=N` 判断, "
        "所以输出里必须真的打印退出码, 别用 `cmd /c exit 0` 之类的假命令冒充。\n"
        "没跑过任何命令、或每条命令都失败时不许用 done; 实在验不了的验收点在 reason 里如实说明, 不要假装通过。\n"
        f"(第 {round_no}/{max_rounds} 轮)" + repo_line(repo) +
        "\n\n只输出一个 json 代码块:\n"
        '{"checks": [{"id": 1, "expect": "输入/操作 → 期望结果", "how": "打算怎么验"}], '
        '"action": "run|done", "command": "要执行的命令(run 时必填)", "verifies": [1], '
        '"evidence": [{"check": 1, "command": "这次跑过的命令", "quote": "输出里的一行原文"}], '
        '"reason": "一句话理由", "message": "一句话说明"}\n'
        "规则: 命令必须是在项目目录下可执行的构建/测试/运行命令, 不要做删除、推送、安装系统之类的事。\n"
        + (("\n【已定验收点(沿用它)】\n" + criteria) if criteria else "") +
        (("\n\n【项目约定的验证命令(合适就优先用它, 不合适就自己挑)】\n" + preferred_cmd.strip())
         if (preferred_cmd or "").strip() else "") +
        "\n\n【用户需求】\n" + (task or "(略)")[:3000] +
        "\n\n【刚做的改动】\n" + (applied or "(无)")[:3000] +
        "\n\n【项目文件】\n" + "\n".join(tree_lines[:400] or ["(空)"]) +
        (("\n\n【上一轮命令】\n" + (last_cmd or "(没有执行任何命令)") +
          "\n【输出(截断)】\n" + (last_output or "")[:6000])
         if (last_cmd or last_output) else "\n\n(还没跑过任何命令)")
    )


def extract_verify(text: str):
    """解析本地模型的验证决策(json 对象, 带 action 字段)。"""
    raw = (text or "").strip()
    def ok(o):
        return isinstance(o, dict) and str(o.get("action") or "").strip().lower() in ("run", "fix", "done")
    if raw.startswith("{"):
        try:
            o = json.loads(raw)
            if ok(o):
                o["action"] = str(o["action"]).strip().lower()
                return o
        except Exception:
            pass
    for m in _MANIFEST_RE.finditer(text or ""):
        try:
            o = json.loads(m.group(1))
        except Exception:
            continue
        if ok(o):
            o["action"] = str(o["action"]).strip().lower()
            return o
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch != "{":
            continue
        try:
            o, _ = dec.raw_decode((text or "")[i:])
        except Exception:
            continue
        if ok(o):
            o["action"] = str(o["action"]).strip().lower()
            return o
    return None


def summary_prompt(ops_summary: str) -> str:
    return (
        "已对本地工作区应用如下变更:\n" + ops_summary +
        "\n请用中文、不超过 4 行总结: 改动要点、潜在风险、建议的下一步。直接输出文字, 不要代码块。"
    )


# ---------------- 编排 ----------------
_TEXT_EXT = set(".py .js .ts .jsx .tsx .json .md .html .css .java .go .rs .c .cpp .h .rb .php "
                ".sql .yml .yaml .toml .ini .sh .bat .ps1 .xml .csv .txt .qml .pro .cmake .lua .kt".split())


def is_text_path(f: str) -> bool:
    return f.endswith(".txt") or Path(f).suffix.lower() in _TEXT_EXT


def read_context(files: list[str]) -> tuple[str, list[str]]:
    """只读这些文件, 返回 (拼好的内容, 实际读到的文件名)。超预算的会被截断/跳过。"""
    used = 0
    chunks: list[str] = []
    names: list[str] = []
    for f in files:
        if not is_text_path(f):
            continue
        try:
            data = workspace.read_file(f)
        except Exception:
            continue
        if data.get("binary"):
            continue
        text = (data["text"] or "")[:CTX_FILE_BUDGET]
        budget = CTX_TOTAL_BUDGET - used
        if budget <= 0:
            break
        if len(text) > budget:
            text = text[:budget] + "\n…(截断)"
        used += len(text)
        names.append(f)
        chunks.append(f"### 文件 {f}\n" + text)
    return "\n\n".join(chunks), names


async def build_context(manager, include: list[str]) -> tuple[list[str], str]:
    """返回 (tree_lines, context_text)。include 为空则自动注入可读文本文件。"""
    tree = workspace.walk_files()
    ctx, _ = read_context(include or workspace.walk_files())
    return tree, ctx



async def run(manager, task: str, include: list[str], *,
              emit, plan: bool = True, summary: bool = True, ask_plan=None, repo: str = ""):
    """执行一次工程运行。emit(stage: str, text: str, extra: dict)。

    ask_plan: 可选, 用于"规划/摘要"的模型调用(prompt) -> 文本;
             默认回退到网页 Provider(provider_ask)。"实现"始终走网页 Provider。
    """
    result: dict = {"ok": False, "applied": [], "skipped": [],
                    "steps": [], "errors": [], "summary": ""}
    if manager.page is None or manager.state != "logged_in":
        result["errors"].append("浏览器未连接/未登录")
        await emit("error", "浏览器未连接/未登录, 请先启动并登录", {})
        return result

    ask = ask_plan or (lambda prompt: provider_ask(manager, prompt))
    tree_lines = workspace.walk_files()
    global_ctx, _ = read_context(include or workspace.walk_files())   # 规划没给文件时的兜底

    # 1) 规划(本地模型): 步骤 + 每一步真正需要看的文件
    steps = [{"text": task, "files": []}]
    plan_gives_files = False
    if plan:
        await emit("progress", "规划中…(本地/规划模型)", {})
        ans = await ask(plan_prompt(task, tree_lines, repo))
        if not ans:
            result["errors"].append("规划请求无返回")
            await emit("error", "规划请求无返回(检查站点对话是否正常)", {})
            return result
        found = extract_plan(ans)
        if found:
            steps = found[:PLAN_STEPS_MAX]
            plan_gives_files = any(s.get("files") for s in steps)
            await emit("plan", "\n".join(
                f"{i + 1}. {s['text']}" + ("  [需要文件: " + ", ".join(s["files"][:4]) + ("]" if len(s["files"]) <= 4 else ", …]") if s.get("files") else "")
                for i, s in enumerate(steps)),
                {"steps": [{"text": s["text"], "files": s.get("files") or []} for s in steps]})
        else:
            await emit("progress", "规划未解析, 按单步执行", {})

    # 2) 分步实现(网页模型写代码): 只把这一步需要的文件发过去
    manifest_fail = False
    prev_log = ""
    for idx, step in enumerate(steps, 1):
        step_text = step["text"] if isinstance(step, dict) else str(step)
        want = (step.get("files") if isinstance(step, dict) else []) or []
        await emit("progress", f"实现中 ({idx}/{len(steps)}): {step_text[:60]}", {})
        if plan_gives_files and want:
            context, sent = read_context(want)
            note = f"本步只发送 {len(sent)} 个文件: " + ", ".join(sent)
        elif plan_gives_files:
            context, sent = "", []
            note = "本步不需要读取已有文件"
        else:
            context, sent = global_ctx, []
            note = "规划未指定文件, 按选定范围发送"
        if note:
            await emit("progress", note, {"files": sent})
        prompt = implement_prompt(step_text, tree_lines, context, prev_log, repo,
                                  label=f"{idx}/{len(steps)}")
        await emit("step", f"[{idx}/{len(steps)}] {step_text[:80]}",
                   {"index": idx, "total": len(steps), "files": sent,
                    "chars": len(prompt), "prompt": prompt[:4000]})
        ans = await provider_ask(manager, prompt)
        await emit("answer", (ans or "").strip()[:4000],
                   {"index": idx, "chars": len(ans or "")})
        obj, src = extract_manifest(ans)
        attempt = 0
        while obj is None and attempt < REPAIR_ATTEMPTS:
            attempt += 1
            await emit("progress", f"清单解析失败, 第 {attempt} 次修复重试…", {})
            ans2 = await provider_ask(manager, repair_prompt(step_text, src, ans))
            obj, src = extract_manifest(ans2 or ans)
        # 这一步本该改动, 却回了空清单 -> 催它给出真正的文件内容
        wants_change = bool(step.get("change", True)) if isinstance(step, dict) else True
        if obj is not None and wants_change and not (obj.get("files") or []) and attempt < REPAIR_ATTEMPTS:
            attempt += 1
            nudged = ('你上一条回复没有给出任何文件变更, 但这一步要求实现改动。'
                      '请直接给出改动后的**完整文件内容**(仍用同一个 JSON 清单协议), 不要只给分析。\n\n'
                      + implement_prompt(step_text, tree_lines, context, prev_log, repo,
                                         label=f"{idx}/{len(steps)}"))
            await emit("progress", "这一步没给出改动, 让网页模型重写一次…", {"index": idx})
            ans2 = await provider_ask(manager, nudged)
            obj2, src2 = extract_manifest(ans2 or "")
            if obj2 is not None and (obj2.get("files") or []):
                obj, src = obj2, src2
        if obj is None:
            manifest_fail = True
            result["errors"].append(f"步骤{idx} 变更清单无法解析")
            raw_tail = (ans or "")[-600:]
            prev_log = f"上一步({idx})的变更清单无法解析({src})。回答结尾: {raw_tail[-300:]}"
            await emit("error",
                       f"步骤{idx} 变更清单无法解析({src})。回答结尾: {raw_tail}",
                       {"raw": (ans or "")[-2000:]})
            continue
        applied, skipped, diffs = apply_manifest(obj)
        result["applied"].extend(applied)
        result["skipped"].extend(skipped)
        msg = obj.get("message") or f"步骤{idx}完成"
        await emit("apply", f"[{idx}] {msg}", {"applied": applied, "skipped": skipped,
                                               "diffs": diffs})
        # 这一轮的问题(跳过/落盘报错)作为日志回灌给下一步, 让网页模型带着问题继续
        prev_log = ""
        if skipped:
            prev_log = "上一步执行时遇到这些情况(请在本步修正):\n" + "\n".join(f"- {s}" for s in skipped[:5])

    # 3) 摘要
    if summary and result["applied"]:
        ops_txt = "\n".join(
            f"- {a['op']} {a['path']}" + (f" ({a.get('size')}B)" if a.get("size") else "")
            for a in result["applied"])
        try:
            s = await ask(summary_prompt(ops_txt))
            if s:
                result["summary"] = s.strip()
                await emit("summary", s.strip(), {})
        except Exception as exc:  # noqa: BLE001
            log.warning("summary failed: %s", exc)

    result["ok"] = bool(result["applied"]) and not manifest_fail
    await emit("done", f"执行完成: 应用 {len(result['applied'])} 项, "
                       f"跳过 {len(result['skipped'])} 项, 错误 {len(result['errors'])} 项",
               {"result": result})
    return result
