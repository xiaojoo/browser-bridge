"""FastAPI 服务: 本地 HTTP + WebSocket 事件广播。

REST:
  GET  /             -> 内置聊天 UI
  GET  /api/status   -> 浏览器/登录/忙碌状态
  POST /api/start    -> 启动浏览器并进入登录等待(异步)
  POST /api/chat     -> 发消息, 返回 message_id; 结果经 WS 实时推送
  POST /api/new_chat -> 尽力新建对话(实验性)

WS   /ws             -> 事件推送(status/info/message_start/delta/message_end/error)
"""
import asyncio
import json
import locale
import logging
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import capture, config, engineer, events, planner, providers, settings, workspace
from .browser import BrowserManager

log = logging.getLogger("bridge")
manager = BrowserManager()

_clients: set[WebSocket] = set()
_tasks: set[asyncio.Task] = set()
_staged_files: dict[str, dict] = {}   # id -> {name, mime, data}


def _stage_file(data: bytes, name: str, mime: str) -> str:
    """暂存一个文件(内存), 返回可用于 /api/chat 的 id。"""
    fid = uuid.uuid4().hex
    _staged_files[fid] = {"name": name, "mime": mime, "data": data}
    while len(_staged_files) > config.MAX_STAGED_FILES:
        _staged_files.pop(next(iter(_staged_files)))
    return fid


def _spawn(coro) -> asyncio.Task:
    """创建后台任务并持有引用, 防止任务中途被 GC。"""
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def broadcast(payload: dict):
    """并行广播 + 单连接超时: 防止某个失效/半开连接挂起整个广播。"""
    if not _clients:
        log.debug("broadcast %s: no clients", payload.get("type"))
        return
    log.debug("broadcast %s to %d clients", payload.get("type"), len(_clients))
    async def send(ws):
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=2)
            return None
        except Exception as exc:
            log.debug("broadcast drop client: %s", exc)
            return ws
    dead = await asyncio.gather(*[send(w) for w in list(_clients)])
    for ws in dead:
        if ws is not None:
            _clients.discard(ws)


async def _on_manager_change(status: dict, **extra):
    payload = events.status_event(**status)
    for v in extra.values():
        if isinstance(v, dict) and v.get("type"):
            await broadcast(v)
    await broadcast(payload)


manager.on_change = _on_manager_change


class ChatRequest(BaseModel):
    text: str = Field(default="", max_length=2_000_000)   # 支持"解析文本"模式粘贴大段代码
    conversation_id: str | None = None
    file_ids: list[str] = Field(default_factory=list)


class StartRequest(BaseModel):
    provider: str | None = None


class OpenConvRequest(BaseModel):
    key: str = ""
    url: str = ""


class WorkspaceRootRequest(BaseModel):
    path: str = ""            # 绝对路径; 空 = 恢复默认工作区


class WorkspaceWrite(BaseModel):
    path: str = Field(min_length=1)
    content: str = ""


class EngineerRequest(BaseModel):
    task: str = Field(min_length=3, max_length=10_000)
    include: list[str] = Field(default_factory=list)   # 为空=自动包含全部文本文件
    plan: bool = True
    summary: bool = True


class SettingsPlanner(BaseModel):
    type: str = "web"
    api_base: str = ""
    api_model: str = ""
    api_key: str = ""
    api_temp: float = 0.2


class SettingsEngine(BaseModel):
    repo: str = ""
    auto_apply: str = ""
    confirm_apply: str = ""
    auto_test: str = ""
    test_cmd: str = ""


class SettingsRequest(BaseModel):
    planner: SettingsPlanner | None = None
    engine: SettingsEngine | None = None


async def _run_turn(text: str, files: list[dict] | None = None):
    """完整一轮: (附加文件) -> 发送 -> 捕获流式增量 -> 广播协议事件。"""
    mid = events.new_message_id()
    page = manager.page
    if page is None:
        await broadcast(events.error_event(mid, None, "浏览器未启动"))
        return

    await broadcast(events.message_start(mid, None))
    manager.busy = True
    await broadcast(events.status_event(**manager.status()))

    async def on_delta(kind, chunk, snapshot):
        nonlocal counters
        counters["deltas"] += 1
        if counters["deltas"] == 1:
            log.info("turn[%s] first delta kind=%s snap=%s", mid[:8], kind, snapshot)
        await broadcast(events.delta(mid, conv, kind, chunk, snapshot))

    async def on_warn(msg):
        await broadcast(events.info_event("捕获警告: " + msg))

    counters = {"deltas": 0}

    conv = None
    truncated = False
    errors: list[str] = []
    mode = manager.provider.capture_mode if manager.provider else "stream"
    stream_mode_on = mode == "stream"
    try:
        if stream_mode_on:
            await capture.enable_capture(page)
        if files:
            ok, errs = await manager.attach_files(files)
            if ok:
                await broadcast(events.info_event(f"已附加 {ok} 个文件"))
            if errs:
                await broadcast(events.info_event("附加文件失败: " + "; ".join(errs[:2])))
        try:
            await manager.send_text(text)
            log.info("turn[%s] send_text done", mid[:8])
        except Exception as exc:  # noqa: BLE001
            log.warning("turn[%s] send_text failed: %s", mid[:8], exc)
            await broadcast(events.error_event(mid, conv, f"发送失败: {exc}"))
            if stream_mode_on:
                await capture.disable_capture(page)
            return
        # 发送时可能另开了窗口(站点被限流) -> 捕获必须跟到新页面, 否则收不到输出
        if manager.page is not None and manager.page is not page:
            log.info("turn[%s] 页面已更换(另开窗口), 捕获切换到新页面", mid[:8])
            try:
                if stream_mode_on:
                    await capture.disable_capture(page)
            except Exception:  # noqa: BLE001
                pass
            page = manager.page
            if stream_mode_on:
                try:
                    await capture.enable_capture(page)
                except Exception:  # noqa: BLE001
                    log.warning("在新页面重新挂捕获失败", exc_info=True)
        conv = manager.conversation_id() or conv
        log.info("turn[%s] waiting capture mode=%s", mid[:8], mode)
        try:
            truncated, errors = await capture.wait_turn_end(
                page, on_delta, on_warn=on_warn, mode=mode,
                snapshot_selector=manager.provider.snapshot_selector)
            log.info("turn[%s] wait_turn_end done truncated=%s errs=%s deltas=%d",
                     mid[:8], truncated, errors, counters["deltas"])
        except capture.NoDataError as exc:
            log.warning("turn[%s] NoData: %s", mid[:8], exc)
            await broadcast(events.error_event(mid, conv, str(exc)))
            return
        finally:
            if stream_mode_on:
                await capture.disable_capture(page)

        # 站点有时不会在发送后清空输入框: 内容还留着就清掉(否则它会变成草稿, 下次又冒出来)
        await _cleanup_composer_after_turn(manager, text, mid)
    finally:
        manager.busy = False
        await broadcast(events.status_event(**manager.status()))

    if errors:
        await broadcast(events.info_event("本轮流中有警告: " + "; ".join(errors[:3])))
    await broadcast(events.message_end(mid, conv, truncated=truncated))


async def _cleanup_composer_after_turn(manager, text: str, mid: str):
    """一轮结束后, 站点没把输入框清空的话替它清掉。

    ChatGPT 这类站点会把没清空的输入框当"草稿"留着, 下次连接/新开窗口又还原出来 ——
    看着就像"上次的输入没发干净"。这一步放在**拿到回答之后**做, 所以不会误删还没提交的内容。
    """
    if not (text or "").strip():
        return
    try:
        if manager._norm(await manager._composer_text()) != manager._norm(text):
            return
        if await manager.clear_composer():
            log.info("turn[%s] 清掉了输入框里残留的已发送内容", mid[:8])
    except Exception:  # noqa: BLE001
        log.debug("composer cleanup skipped", exc_info=True)


async def _watch_browser_window():
    """每 3s 检查桥接窗口是否还在: 被关掉时自动把状态收敛回 idle 并提示。"""
    while True:
        await asyncio.sleep(3)
        try:
            await manager.ensure_alive()
        except Exception:  # noqa: BLE001
            log.debug("watchdog error", exc_info=True)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _spawn(_watch_browser_window())
    try:
        yield
    finally:
        for t in list(_tasks):
            t.cancel()


app = FastAPI(title="DeepSeek Browser Bridge", docs_url=None, redoc_url=None,
              lifespan=_lifespan)


@app.middleware("http")
async def _no_store_frontend(request, call_next):
    """前端资源一律不缓存: 改完 static/* 刷新就能看到, 不用手动清缓存。"""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".css", ".js")):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.get("/")
async def index():
    return FileResponse(config.STATIC_INDEX)


@app.get("/api/providers")
async def api_providers():
    return {"default": providers.DEFAULT_ID,
            "providers": [providers.info(p) for p in providers.PROVIDERS]}


# ---------- 本地工作区 ----------
class WorldApplyRequest(BaseModel):
    task: str = Field(default="", max_length=200_000)
    text: str = Field(default="", max_length=400_000)
    include: list[str] = Field(default_factory=list)
    dry_run: bool = False          # True = 只让本地模型给方案, 不写盘(给确认框看)


class WorldCommitRequest(BaseModel):
    files: list[dict] = Field(default_factory=list)
    message: str = ""


class WorldVerifyRequest(BaseModel):
    task: str = Field(default="", max_length=200_000)
    answer: str = Field(default="", max_length=400_000)
    applied: list[dict] = Field(default_factory=list)
    command: str = Field(default="", max_length=2000)   # 首选命令(可留空, 由本地模型挑)
    max_rounds: int = 4
    timeout: float = 300


class WorldTestRequest(BaseModel):
    command: str = Field(default="", max_length=2000)
    timeout: float = 180


class LocalChatRequest(BaseModel):
    """只用本地模型回一条(不经过网页模型): 正文 + 最近几轮上下文。"""
    text: str = Field(min_length=1, max_length=400_000)
    history: list[dict] = Field(default_factory=list)


def planner_ready(pcfg: dict) -> bool:
    """planner 能不能真的用来干活(本地模型不用 Key, API 模式必须有 Key)。"""
    return (pcfg.get("type") in ("api", "local")) and (
        pcfg.get("type") == "local" or bool(pcfg.get("api_key")))


PLANNER_NEEDED_HINT = ("需要先在设置里配一个「能对话的模型」: 规划模型类型选「本地模型」"
                       "(点「检测本地模型」自动填好) 或「API」(填 Key)。"
                       "这是全局设置, 所有会话共用, 换窗口不受影响。")


class ClientLogRequest(BaseModel):
    build: str = ""
    innerH: int = 0
    innerW: int = 0
    dpr: float | None = None
    wrapTop: int = 0
    wrapBottom: int = 0
    wrapClientH: int = 0
    wrapPos: str = ""
    mainBottom: int = 0
    headH: int | None = None
    composerPos: str | None = None
    composerH: str = ""
    padBottom: str = ""


@app.post("/api/client-log")
async def api_client_log(req: ClientLogRequest):
    """前端把布局实测值写进日志(排查用)。"""
    log.info("client-layout %s", req.model_dump_json())
    return {"ok": True}


@app.get("/api/workspace")
async def api_workspace_info():
    """当前工作区根目录(可切换成任意本地目录)。"""
    return workspace.info()


@app.post("/api/workspace/root")
async def api_workspace_root(req: WorkspaceRootRequest):
    try:
        info = workspace.set_root(req.path)
    except workspace.WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, **info}


@app.get("/api/workspace/browse")
async def api_workspace_browse(path: str = ""):
    """给"选目录"用: 只列子目录, 不读文件内容。"""
    return workspace.browse(path)


@app.get("/workspace/tree")
async def ws_tree(path: str = ""):
    try:
        items = workspace.list_all(path)
        return {"root": str(workspace.ROOT), "name": workspace.ROOT.name,
                "path": path or ".",
                "items": items}
    except workspace.WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/workspace/file")
async def ws_read(path: str = ""):
    try:
        data = workspace.read_file(path)
        return {"ok": True, **data}
    except workspace.WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404 if "不存在" in str(exc) else 400)


@app.post("/workspace/file")
async def ws_write(req: WorkspaceWrite):
    try:
        return workspace.write_file(req.path, req.content)
    except workspace.WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.delete("/workspace/file")
async def ws_delete(path: str = ""):
    try:
        return workspace.delete(path)
    except workspace.WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ---------- 编码执行器(Phase 2) ----------
async def _run_engineer(req: EngineerRequest):
    manager.busy = True
    try:
        await broadcast(events.status_event(**manager.status()))
        async def emit(stage: str, text: str, extra: dict):
            await broadcast(events.event("engineer", stage=stage, text=text, **extra))
        # 规划/摘要走配置的 planner(API), 失败回退网页 Provider; 实现始终走网页 Provider
        pcfg = settings.load().get("planner", {})
        async def ask_plan(prompt: str) -> str:
            if pcfg.get("type") in ("api", "local") and (pcfg.get("api_key") or pcfg.get("type") == "local"):
                try:
                    return await planner.ask(pcfg, prompt)
                except Exception as exc:  # noqa: BLE001
                    log.warning("planner api failed, fallback web: %s", exc)
                    await emit("progress", f"planner API 失败, 回退网页模型: {exc}", {})
            return await engineer.provider_ask(manager, prompt)
        result = await engineer.run(manager, req.task, req.include,
                                    emit=emit, plan=req.plan, summary=req.summary,
                                    ask_plan=ask_plan,
                                    repo=(settings.load().get("engine") or {}).get("repo", ""))
        if result.get("errors"):
            await broadcast(events.info_event(
                "工程任务有错误: " + "; ".join(result["errors"][:2])))
        else:
            await broadcast(events.info_event(
                f"工程任务完成: 应用 {len(result['applied'])} 项变更"))
    except Exception as exc:  # noqa: BLE001
        log.exception("engineer run failed")
        await broadcast(events.event("engineer", stage="error",
                                     text=f"执行器异常: {exc}", result={}))
    finally:
        manager.busy = False
        await broadcast(events.status_event(**manager.status()))


@app.post("/api/engineer/run")
async def api_engineer(req: EngineerRequest):
    if manager.state != "logged_in":
        return JSONResponse({"error": "浏览器未就绪/未登录"}, status_code=503)
    if manager.busy:
        return JSONResponse({"error": "正在执行任务(聊天或工程), 请稍候"}, status_code=409)
    _spawn(_run_engineer(req))
    return {"ok": True, "state": "queued"}


# ---------- 只用本地模型回一条(不进 ChatGPT/网页模型) ----------
async def _run_local_turn(text: str, history: list[dict], pcfg: dict):
    """走与网页模型完全相同的事件协议: message_start -> delta* -> message_end,
    所以消息列表里的显示、历史记录、World 模式的落盘+自测全都不用改。"""
    mid = events.new_message_id()
    conv = manager.conversation_id()
    await broadcast(events.message_start(mid, conv))
    manager.busy = True
    await broadcast(events.status_event(**manager.status()))
    got = False
    try:
        async def on_chunk(piece: str):
            nonlocal got
            got = True
            await broadcast(events.delta(mid, conv, "text", piece, False))

        answer = await planner.ask_stream(pcfg, planner.build_messages(text, history),
                                         on_chunk=on_chunk)
        if not got and answer:
            await broadcast(events.delta(mid, conv, "text", answer, True))
    except planner.PlannerError as exc:
        log.warning("local turn failed: %s", exc)
        await broadcast(events.error_event(mid, conv, "本地模型调用失败: " + str(exc)))
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("local turn crashed")
        await broadcast(events.error_event(mid, conv, "本地模型调用失败: " + str(exc)))
        return
    finally:
        manager.busy = False
        await broadcast(events.status_event(**manager.status()))
    await broadcast(events.message_end(mid, conv))


@app.post("/api/local_chat")
async def api_local_chat(req: LocalChatRequest):
    """只用本地模型回答: 不经过网页模型, 也就不需要桥接浏览器窗口。"""
    pcfg = settings.load().get("planner", {})
    if not planner_ready(pcfg):
        return JSONResponse({"error": PLANNER_NEEDED_HINT}, status_code=400)
    if manager.busy:
        return JSONResponse({"error": "正在生成中, 请稍候"}, status_code=409)
    _spawn(_run_local_turn(req.text, req.history, pcfg))
    return {"ok": True, "state": "queued", "model": pcfg.get("api_model") or ""}


# ---------- 设置(planner 模型) ----------
@app.get("/api/settings")
async def api_settings_get():
    return settings.public()


@app.post("/api/settings")
async def api_settings_set(req: SettingsRequest):
    if req.planner is not None:
        patch = req.planner.model_dump(exclude_unset=True)
        # 选「本地模型」时把地址/模型补成真的本机服务(填错/没填都拦在这里, 不存一份用不了的配置)
        err = await settings.normalize_planner(patch)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        settings.save_planner(patch)
    if req.engine is not None:
        settings.save_engine(req.engine.model_dump(exclude_unset=True))
    if req.planner is None and req.engine is None:
        return JSONResponse({"error": "缺少配置"}, status_code=400)
    return settings.public()


_world_state: dict = {"apply": None, "verify": None}    # 最近一次落盘/验证的状态(页刷新后取)


@app.get("/api/world/state")
async def api_world_state():
    """页面刷新/新开时, 用它把"最近一轮本地执行"的结果补画出来(否则会停在旧文案上)。"""
    return {"ok": True, "apply": _world_state.get("apply"), "verify": _world_state.get("verify")}


@app.post("/api/world/apply")
async def api_world_apply(req: WorldApplyRequest):
    """World 模式: 用本地模型把网页模型(ChatGPT)的回答写成工作区文件改动。"""
    if not req.text.strip():
        return JSONResponse({"error": "没有可落盘的回答"}, status_code=400)
    pcfg = settings.load().get("planner", {})
    is_local = (pcfg.get("type") in ("api", "local")) and (
        pcfg.get("type") == "local" or bool(pcfg.get("api_key")))
    if not is_local:
        return JSONResponse({"error": "落盘/自测需要一个能读懂代码的模型: 打开设置 → 规划模型类型选"
                                      "「本地模型」(点「检测本地模型」自动填好) 或「API」(填 Key)。"
                                      "这是全局设置, 所有会话共用, 换窗口不受影响。"},
                            status_code=400)
    _world_state["apply"] = {"action": "start", "text": "正在把 ChatGPT 的回答落实成文件改动…",
                             "ts": time.time()}
    _world_state["verify"] = None
    await broadcast(events.event("world", stage="apply", action="start",
                                 text="正在把 ChatGPT 的回答落实成文件改动…"))
    repo = (settings.load().get("engine") or {}).get("repo", "")
    tree = workspace.walk_files()
    # 从回答里认出要改的文件, 把它们当前的内容一起给本地模型(否则它没法给出完整内容)
    targets = engineer.guess_paths(req.text, tree)
    contents: list[tuple[str, str]] = []
    for p in targets:
        try:
            data = workspace.read_file(p)
        except Exception:  # noqa: BLE001
            continue
        if not data.get("binary"):
            contents.append((p, data.get("text") or ""))
    if targets:
        log.info("world apply: 目标文件 %s", ", ".join(targets))
    # ChatGPT 只给了思路、没给代码时, 落盘的代码其实是本地模型自己写的 ——
    # 以前界面上看不出来, 用户会以为"ChatGPT 什么都没发, 怎么就写进去了"。
    self_authored = not _answer_has_code(req.text)
    self_note = ("ChatGPT 的回答里没有代码(只有说明/思路), 这些改动由本地模型按方案自己编写"
                 if self_authored else "")
    if self_authored:
        log.info("world apply: ChatGPT 的回答里没有代码, 改动由本地模型自编")
    prompt = engineer.apply_prompt(req.task, req.text, tree, repo, contents)
    try:
        ans = await planner.ask(pcfg, prompt)
    except planner.PlannerError as exc:
        return JSONResponse({"error": "本地模型调用失败: " + str(exc)}, status_code=502)
    obj, src = engineer.extract_manifest(ans or "")
    if obj is None:
        return JSONResponse({"error": "本地模型没能给出改动清单(" + src + ")",
                             "raw": (ans or "")[-2000:]}, status_code=422)
    if not (obj.get("files") or []):      # 空手而归: 再催一次
        try:
            hint = ("\n\n(回答里提到的文件是: " + ", ".join(targets) + ")") if targets else ""
            ans2 = await planner.ask(pcfg, prompt + hint + "\n\n" + engineer.EMPTY_MANIFEST_NUDGE)
            obj2, _src2 = engineer.extract_manifest(ans2 or "")
            if obj2 is not None and (obj2.get("files") or []):
                obj = obj2
        except planner.PlannerError:
            pass
        if not (obj.get("files") or []):
            log.info("world apply: 空清单, 本地模型原话: %s", (ans or "")[-400:].replace("\n", " "))
    if req.dry_run:                      # 只给方案: 让界面弹确认框
        files = engineer.preview_manifest(obj)
        contents = {str(it.get("path")): it.get("content") for it in (obj.get("files") or [])}
        for f in files:
            if not f.get("content"):
                f["content"] = contents.get(f.get("path"), "")
        log.info("world preview: %d file(s)", len(files))
        # 事件化: 刷新过的页面也能看到这一步(否则会一直停在"正在…")
        payload = {"action": "preview", "empty": not files, "text": obj.get("message") or "",
                   "selfAuthored": self_authored, "note": self_note,
                   "files": [{k: f.get(k) for k in
                              ("op", "path", "add", "del", "size", "unchanged", "error",
                               "force", "realOp")}
                             for f in files], "ts": time.time()}
        _world_state["apply"] = payload
        await broadcast(events.event("world", stage="apply", **payload))
        return {"ok": True, "dry_run": True, "message": obj.get("message") or "",
                "files": files, "empty": not files,
                "selfAuthored": self_authored, "note": self_note}
    applied, skipped, diffs = engineer.apply_manifest(obj)
    payload = {"action": "applied", "applied": applied, "skipped": skipped,
               "text": obj.get("message") or "", "selfAuthored": self_authored,
               "note": self_note, "ts": time.time()}
    _world_state["apply"] = payload
    await broadcast(events.event("world", stage="apply", **payload))
    log.info("world apply: %d applied, %d skipped", len(applied), len(skipped))
    return {"ok": True, "message": obj.get("message") or "", "applied": applied,
            "skipped": skipped, "diffs": diffs,
            "selfAuthored": self_authored, "note": self_note}


@app.post("/api/world/commit")
async def api_world_commit(req: WorldCommitRequest):
    """按用户在确认框里勾选的条目落盘。"""
    if not req.files:
        return {"ok": True, "applied": [], "skipped": [], "message": "没有选中任何改动"}
    applied, skipped, diffs = engineer.apply_manifest({"files": req.files})
    payload = {"action": "applied", "applied": applied, "skipped": skipped,
               "text": req.message or "", "ts": time.time()}
    _world_state["apply"] = payload
    await broadcast(events.event("world", stage="apply", **payload))
    log.info("world commit: %d applied, %d skipped", len(applied), len(skipped))
    return {"ok": True, "message": req.message or "", "applied": applied,
            "skipped": skipped, "diffs": diffs}


@app.post("/api/world/test")
async def api_world_test(req: WorldTestRequest):
    """跑一次自测命令(在工作区目录下), 把输出交给前端决定是否回给网页模型。"""
    eng = settings.load().get("engine") or {}
    cmd = (req.command or eng.get("test_cmd") or "").strip()
    if not cmd:
        return {"ok": True, "skipped": True, "output": "", "code": 0}
    cwd = str(workspace.ROOT)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out = await asyncio.wait_for(proc.communicate(), timeout=max(5, min(req.timeout, 900)))
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "code": -1, "output": f"自测命令超时({int(req.timeout)}s): {cmd}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": -1, "output": f"自测命令无法执行: {exc}"}
    text = _decode_out(out[0] or b"")
    code = proc.returncode or 0
    return {"ok": code == 0, "code": code, "output": text[-20000:], "command": cmd}


_DENY_CMD = ("rm -rf", "rm -r ", "rd /s", "del /s", "del /q", "format ", "shutdown",
             "mkfs", "git push", "git reset --hard", "> /dev/sd", ":(){", "| sh", "| bash",
             "curl -s | ", "wget -q | ")


def _cmd_allowed(cmd: str) -> str:
    c = (cmd or "").strip()
    low = c.lower()
    if not c:
        return "命令为空"
    if len(c) > 500:
        return "命令过长"
    for d in _DENY_CMD:
        if d in low:
            return "命令被安全策略拦截(" + d.strip() + ")"
    return ""


def _decode_out(b: bytes) -> str:
    """Windows 控制台默认是 GBK/936, 直接按 utf-8 解会变成乱码。"""
    for enc in ("utf-8", locale.getpreferredencoding(False), "gbk", "cp936"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


# 这些是"命令本身跑不了/写错了"(比如在 Windows 上用 Linux 的 cat/sed, 路径写错, 语法错),
# 不是项目错误 —— 只有真正的编译/测试失败才算项目报错
_TOOL_FAIL = ("is not recognized", "not recognized as an internal", "command not found",
              "不是内部或外部命令", "找不到", "系统找不到指定", "no such file or directory",
              "the term '", "无法将", "term is not recognized",
              # PowerShell 自己的报错
              "parsererror", "at line:", "categoryinfo", "fullyqualifiederrorid",
              "此时不应有", "缺少表达式", "意外的标记", "无法识别", "不能识别",
              "unexpected token", "commandnotfoundexception", "is not defined",
              "意外的", "was unexpected at this time", "unexpected at this time",
              "syntax of the command is incorrect", "命令语法不正确", "not found",
              "cannot find path", "无法找到路径")


# 明显是 Linux 才有的命令(在 Windows 上必然跑不了), 与报错文案无关
_LINUX_ONLY = re.compile(
    r"(^|[;&|(\s])(cat|sed|grep|awk|ls|head|tail|chmod|chown|wc|uniq|touch|which|source|export)\b",
    re.I)


def _linux_style(cmd: str) -> bool:
    c = (cmd or "").strip()
    if not c:
        return False
    if re.search(r"\bdo\b", c) and re.search(r"\bdone\b", c):
        return True                                  # for ...; do ...; done 这种循环
    if re.search(r"\bfor\s+\w+\s+in\s", c):
        return True
    return bool(_LINUX_ONLY.search(c))


def _tool_failure(code: int, out: str, cmd: str = "") -> bool:
    if code == 0:
        return False
    if _linux_style(cmd):
        return True
    low = (out or "").lower()
    return any(k.lower() in low for k in _TOOL_FAIL)


# 只读文件的命令(Get-Content/type/dir…)即使退出码 0 也不产生"证据";
# 但同一轮里带了构建/测试动作的复合命令照样算。
_READONLY_CMD = re.compile(
    r"^\s*(?:(?:cmd(?:\.exe)?\s+/c\s+)|(?:powershell(?:\.exe)?[^;|&]*?-command\s+))?[\"']?\s*"
    r"(?:get-content|gc|type|dir|ls|cat|more|head|tail|findstr|select-string|"
    r"get-childitem|gci|tree|where|which)\b", re.I)
_BUILDY_CMD = re.compile(
    r"\b(?:cmake|ctest|make|ninja|msbuild|dotnet|g\+\+|clang|gcc|pytest|unittest|"
    r"npm|yarn|pnpm|node|cargo|gradle|mvn|build|test|--build)\b", re.I)


def _evidence_run(cmd: str, code: int) -> bool:
    """这条命令能不能当"验证证据": 成功退出, 且不是单纯地看文件。"""
    if code != 0:
        return False
    if _BUILDY_CMD.search(cmd or ""):
        return True
    return not _READONLY_CMD.match(cmd or "")


# ---------------------------------------------------------------- 证据的可信度
# 实际踩过的坑(用户看到"本地模型确认项目跑通了", 其实项目根本没验):
#   1) 命令写成复合/包装形式(`... ; 'CMAKE_EXIT=' + $LASTEXITCODE`), shell 退出码是 0,
#      真实失败藏在输出里 —— 于是"命令 ✓ 通过"是假的;
#   2) 它拿 `smartclip_verify`(工作区外的旧快照副本)当验证对象, 根本不是在验用户的项目;
#   3) done 的 evidence 是它自己写的一句话, 服务端只检查了"非空", 没人核对它引用的命令/输出。
# 下面这些函数就是把这三点钉死。
_EXIT_MARK_RE = re.compile(r"\b(?:[A-Z_]{2,24})?(?:EXITCODE|EXIT_CODE|EXIT)\s*[=:]\s*(-?\d+)")
_MASKED_FAIL_RE = re.compile(
    r"(CMake Error|NMAKE : fatal error|error MSB\d+|ninja: build stopped|"
    r"\*\*\* \[[^\]]*\] Error|make(?:\[\d+\])?: \*\*\*|FAILED\b|"
    r"Traceback \(most recent call last\))", re.I)
# 只是"编译/静态检查"的工具; 跑测试、跑刚构建出来的程序才算"真的执行了行为"
_COMPILE_RE = re.compile(
    r"\b(?:cmake|msbuild|ninja|nmake|make|g\+\+|clang\+\+|clang|gcc|cl\.exe|qmake|"
    r"qmlcachegen|qmllint|qmlformat|dotnet\s+build|gradle|mvn|tsc|javac)\b", re.I)
_ACTUALLY_RUNS_RE = re.compile(
    r"(ctest|pytest|unittest|npm\s+(?:run\s+)?test|yarn\s+test|\.exe\b|\.bat\b|"
    r"qmltestrunner|python\s+-m|run\b|--run|-c\s|check)", re.I)
_COMPILE_WORDS = ("编译", "构建", "compile", "build", "qmllint", "qmlcachegen", "静态检查", "语法")


def _wrapper_exit_code(out: str) -> int | None:
    """从输出里最后那个 `XXX_EXIT=N` / `EXITCODE=N` 取真实退出码(None = 输出里没有)。"""
    code = None
    for m in _EXIT_MARK_RE.finditer(out or ""):
        try:
            code = int(m.group(1))
        except ValueError:
            continue
    return code


def _masked_failure(out: str) -> str:
    """shell 退出码是 0, 但输出里明明写着构建/测试失败。"""
    m = _MASKED_FAIL_RE.search(out or "")
    return m.group(1) if m else ""


def _evidence_kind(cmd: str) -> str:
    """"behavior" 真的执行了行为; "build" 只是编译/静态检查; "" 什么都不算(读文件)。"""
    c = cmd or ""
    if not _evidence_run(c, 0):
        return ""
    if _COMPILE_RE.search(c) and not _ACTUALLY_RUNS_RE.search(c):
        return "build"
    return "behavior"


_PATH_OPT_RE = re.compile(r"(?:-S|-B|--source|--build|--prefix|-C|--config)\s+([A-Za-z0-9_.\-]+)")


def _missing_paths(cmd: str) -> list[str]:
    """命令里引用、但在**当前工作区根**下不存在的路径(多半是在看别的副本/旧快照)。"""
    cands: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9_.\-]+(?:[\\/][A-Za-z0-9_.\-]+)+", cmd or ""):
        tok = m.group(0).strip("\"'")
        if tok.startswith("-") or re.fullmatch(r"[A-Za-z]:", tok):
            continue
        cands.append(tok)
    # 光秃秃的目录名(`cmake -S smartclip_verify`)没有斜杠, 只在路径类选项后面认
    cands += [m.group(1) for m in _PATH_OPT_RE.finditer(cmd or "")]
    out: list[str] = []
    for tok in dict.fromkeys(cands):
        try:
            if not (workspace.ROOT / tok.replace("\\", "/")).resolve().exists():
                out.append(tok)
        except OSError:
            continue
    return out[:3]


def _wrong_copy_note(cmd: str) -> str:
    miss = _missing_paths(cmd)
    if not miss:
        return ""
    return ("\n[提醒] 工作区根是 " + str(workspace.ROOT) + ", 但这条命令引用的 " +
            ", ".join(miss) + " 在根目录下不存在 —— 不要拿别的目录/旧快照副本当验证对象, "
            "请在当前工作区里验。")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _cmd_tokens(cmd: str) -> list[str]:
    """命令里"有辨识度"的词(程序名 + 路径样式的词), 用来核对证据有没有点名它。"""
    out: list[str] = []
    for i, raw in enumerate(re.split(r"\s+", (cmd or "").strip())):
        t = raw.strip("\"'`")
        if not t or t.startswith("-"):
            continue
        if i == 0 or "/" in t or "\\" in t or re.search(r"\.[A-Za-z0-9]{1,6}$", t):
            out.append(t)
            stem = t.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if len(stem) >= 3 and stem != t:
                out.append(stem)
    return list(dict.fromkeys(out))


_QUOTE_RE = re.compile(r"[「『“\"'`]([^「」『』“”\"'`\n]{4,160})[」』”\"'`]")


def _evidence_text(obj: dict) -> str:
    """evidence 允许是字符串, 也允许是逐条列表 —— 统一成可核对的一段文字。

    没引用输出时写成"(没引用输出)", 不要留一对空引号: 空引号在扫描"引用片段"时
    会和后面那条的引号配成一对, 凭空造出一个根本不存在的引用。
    """
    ev = obj.get("evidence")
    if isinstance(ev, list):
        parts: list[str] = []
        for i, e in enumerate(ev, 1):
            if isinstance(e, dict):
                cid = e.get("check") or e.get("id") or i
                quote = str(e.get("quote") or e.get("output") or e.get("why") or "").strip()
                parts.append("[" + str(cid) + "] 命令 " + str(e.get("command") or "") +
                             (" 输出: \"" + quote + "\"" if quote else " (没引用输出)"))
            else:
                parts.append(str(e))
        return " ; ".join(parts)
    return str(ev or "")


def _grounding_problems(evidence: str, criteria: str, rounds: list[dict]) -> list[str]:
    """把"自己写一句证据"钉死在这次真跑过的命令和真实输出上。"""
    clean = re.sub(r'(""|\'\'|「」|『』|“”|``)', "", evidence or "")   # 先去掉空引号
    ev = _norm(clean)
    ok_runs = [r for r in rounds if r.get("action") == "run" and not r.get("code")]
    problems: list[str] = []
    tokens: list[str] = []
    for r in ok_runs:
        tokens += _cmd_tokens(str(r.get("command") or ""))
    if not any(_norm(t) in ev for t in tokens if len(t) >= 3):
        problems.append("证据里没有点名这次真跑过的命令(不能拿别处/没跑过的构建当证据)")
    outs = _norm("\n".join(str(r.get("output") or "") for r in ok_runs))
    for q in _QUOTE_RE.findall(clean):
        nq = _norm(q)
        if len(nq) >= 6 and nq not in outs:
            problems.append("证据里引用的输出「" + q[:50] + "」在这次的真实输出里找不到")
    for cid in re.findall(r"\[(\d+)\]", criteria or ""):
        # 逐条证据列表本身就带 "[1] 命令 …", 也接受"验收点 1"这种写法
        if f"[{cid}]" in (evidence or ""):
            continue
        if not re.search(r"(?:验收点|check|检查)\s*\[?" + re.escape(cid) + r"\]?", evidence or "", re.I):
            problems.append("验收点 [" + cid + "] 在证据里没被提到")
    return problems


def _criteria_need_behavior(criteria: str) -> bool:
    """验收点里只要有一条是行为性的(不是"能编译就行"), 就必须有真的执行行为的证据。"""
    for line in (criteria or "").splitlines():
        ln = line.strip().lower()
        if ln and not any(w in ln for w in _COMPILE_WORDS):
            return True
    return False


def _answer_has_code(answer: str) -> bool:
    """ChatGPT 的回答里到底有没有代码(围栏代码块, 或看着像代码的行)。"""
    a = answer or ""
    if "```" in a:
        return True
    for ln in a.splitlines():
        s = ln.rstrip()
        if len(s) - len(s.lstrip()) >= 4 and re.search(r"[;{}=]|\)\s*$", s.strip()):
            return True
    return False


_AUDIT_DIR = config.BASE_DIR / ".tmp" / "verify-audit"     # 审计留痕(应用自己的目录, 不进用户项目)
_AUDIT_KEEP = 50


def _audit_verify(rounds: list[dict], payload: dict, criteria: str, verify_dir) -> str:
    """把这一轮验证留痕: 命令/退出码/输出/结论 + `.verify/` 脚手架。

    以前 `.verify/` 被静默删掉, 事后谁也复核不了"它到底跑过什么、凭什么说通过"。
    """
    try:
        base = _AUDIT_DIR / time.strftime("%Y%m%d-%H%M%S")
        d, n = base, 1
        while d.exists():                     # 同一秒内跑多次也不能互相覆盖
            d = base.with_name(base.name + "-" + str(n))
            n += 1
        d.mkdir(parents=True)
        keys = ("round", "action", "command", "code", "kind", "output",
                "reason", "evidence", "applied", "skipped", "text")
        slim = [{k: (str(v)[-4000:] if k == "output" else v) for k, v in r.items() if k in keys}
                for r in rounds]
        (d / "verdict.json").write_text(json.dumps(
            {"ts": time.time(), "root": str(workspace.ROOT), "checks": criteria,
             "verdict": {k: payload.get(k) for k in ("ok", "reason", "text")}, "rounds": slim},
            ensure_ascii=False, indent=2), encoding="utf-8")
        if verify_dir is not None and verify_dir.exists():
            shutil.copytree(verify_dir, d / "verify-dir", dirs_exist_ok=True)
        for old in sorted([p for p in _AUDIT_DIR.iterdir() if p.is_dir()])[:-_AUDIT_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
        return str(d)
    except Exception:  # noqa: BLE001 —— 留痕失败绝不能影响验证本身
        log.exception("verify audit failed")
        return ""


def _cleanup_verify_dir(path, existed_before: bool) -> bool:
    """收掉验证阶段自己造的临时脚手架(.verify/)。本来就有的话一根汗毛都不动。"""
    if existed_before or not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def _repair_note(rounds: list[dict]) -> str:
    """验证失败、本地模型却一次都没动手改时, 逼它自己修的提示词。"""
    last = next((r for r in reversed(rounds) if r.get("action") == "run" and r.get("code")), None)
    return ("[验证失败了, 但你一次都没有动手改过代码] 这不是\"命令跑不了\"那么简单, 是刚才的改动没做对 —— "
            "请用 fix 把相关文件改对(files 里给**完整**内容), 然后重新跑一条命令证明它成立; "
            "不要只换命令重跑、也不要只写解释。\n"
            "失败的命令: " + str((last or {}).get("command") or "(见上一轮)") +
            "\n输出(截断):\n" + str((last or {}).get("output") or "")[-3000:])


async def _run_cmd(cmd: str, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=str(workspace.ROOT), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        out = await asyncio.wait_for(proc.communicate(), timeout=max(5, min(timeout, 900)))
    except asyncio.TimeoutError:
        proc.kill()
        return -1, f"命令超时({int(timeout)}s): {cmd}"
    return (proc.returncode or 0), _decode_out(out[0] or b"")[-20000:]


@app.post("/api/world/verify")
async def api_world_verify(req: WorldVerifyRequest):
    """本地模型自己验证项目: 挑构建/测试命令 -> 看输出 -> 自己改 -> 直到通过。"""
    if not (req.task.strip() or req.answer.strip() or req.applied):
        return JSONResponse({"error": "没有可验证的内容"}, status_code=400)
    pcfg = settings.load().get("planner", {})
    if not ((pcfg.get("type") in ("api", "local")) and
            (pcfg.get("type") == "local" or pcfg.get("api_key"))):
        return JSONResponse({"error": "需要先配置本地/规划模型"}, status_code=400)
    repo = (settings.load().get("engine") or {}).get("repo", "")
    applied_txt = "\n".join(f"- {a.get('op')} {a.get('path')}" for a in (req.applied or []))
    rounds: list[dict] = []
    last_cmd, last_out = "", ""
    criteria = ""            # 第 1 轮从需求里定下的验收点, 之后每轮沿用(免得它中途换题)
    ran_ok = False           # 有没有命令真的跑出过证据(只读文件不算) —— 没有就不接受 done
    behavior_ok = False      # 有没有**真的执行了行为**的证据(跑测试/跑刚构建出来的程序)
    build_ok = False         # 是不是只有编译/静态检查的证据
    max_rounds = max(1, min(req.max_rounds, 6))
    gave_up = False
    real_error_seen = False  # 有没有"命令本身没问题、但失败了"的情况
    local_fixes = 0          # 本地模型自己动手改的次数(0 = 一次都没试过)
    repair_extra = 0         # 没动手改就想收工时, 额外再给它的自修轮数
    repair_budget = 2
    verify_dir = workspace.ROOT / ".verify"       # 临时验证程序的地盘(它自己造的脚手架)
    verify_dir_existed = verify_dir.exists()
    i = 0
    while True:
        if i >= max_rounds + repair_extra:
            # 失败了却一次都没动手改 -> 别急着甩给网页模型, 先逼它自己修(本地能修的就本地修)
            if real_error_seen and local_fixes == 0 and repair_extra == 0:
                repair_extra = repair_budget
                last_cmd, last_out = "", _repair_note(rounds)
                await broadcast(events.event("world", stage="verify", action="repair",
                                             text="本地模型还没动手改过, 再让它自己修 " + str(repair_budget) + " 轮"))
                continue
            gave_up = True
            break
        i += 1
        tree = workspace.walk_files()
        prompt = engineer.verify_prompt(req.task, applied_txt, tree, last_cmd, last_out, i,
                                        max_rounds + repair_extra, repo,
                                        criteria=criteria, preferred_cmd=req.command)
        try:
            ans = await planner.ask(pcfg, prompt)
        except planner.PlannerError as exc:
            _cleanup_verify_dir(verify_dir, verify_dir_existed)
            return JSONResponse({"error": "本地模型调用失败: " + str(exc)}, status_code=502)
        obj = engineer.extract_verify(ans or "")
        if obj is None:
            gave_up = True
            rounds.append({"round": i, "action": "parse-fail", "text": (ans or "")[-500:]})
            await broadcast(events.event("world", stage="verify", round=i, action="parse-fail",
                                         text="本地模型没给出可解析的验证决策"))
            break
        if not criteria and obj.get("checks"):
            criteria = engineer.checks_text(obj.get("checks"))
            if criteria:
                await broadcast(events.event("world", stage="verify", round=i, action="checks",
                                             checks=obj.get("checks"), text=criteria))
        action = obj.get("action")
        await broadcast(events.event("world", stage="verify", round=i, action=action,
                                     text=obj.get("message") or obj.get("reason") or "",
                                     command=obj.get("command") or ""))
        if action == "run":
            cmd = (obj.get("command") or req.command or "").strip()
            why = _cmd_allowed(cmd)
            if why:
                rounds.append({"round": i, "action": "blocked", "command": cmd, "reason": why})
                await broadcast(events.event("world", stage="verify", round=i, action="blocked",
                                             command=cmd, text=why))
                gave_up = True
                break
            code, out = await _run_cmd(cmd, req.timeout)
            if code == 0:
                # 包装命令的假成功: shell 退出码 0, 真实结果在输出里(用户看到过满屏 ✓ 其实全失败)
                real = _wrapper_exit_code(out)
                masked = _masked_failure(out)
                if real:
                    code = real
                    out += ("\n\n[这条命令的 shell 退出码是 0, 但它自己在输出里打印的退出码是 " +
                            str(real) + " —— 按失败算: 不要用复合命令把真实退出码藏起来]")
                elif masked:
                    code = 1
                    out += ("\n\n[这条命令的 shell 退出码是 0, 但输出里是构建/测试失败(" + masked +
                            ") —— 按失败算]")
            note_extra = _wrong_copy_note(cmd) if code != 0 else ""
            if note_extra:
                out += note_extra          # 写进这条命令的输出里: 提示词、留痕、界面都能看到
            if _tool_failure(code, out, cmd):
                # 命令在这个环境里根本跑不了(比如 Linux 的 cat/sed): 不是项目错误
                rounds.append({"round": i, "action": "tool-error", "command": cmd,
                               "code": code, "output": out[-1500:]})
                await broadcast(events.event("world", stage="verify", round=i, action="tool-error",
                                             command=cmd, code=code, text=out[-600:]))
                last_cmd, last_out = cmd, (
                    "[这条命令在本机跑不了(不是项目错误)]\n" + out[-1500:] +
                    "\n注意: 这台机器是 Windows + PowerShell, 没有 cat/sed/grep/ls; "
                    "看文件请用 powershell -NoProfile -Command \"Get-Content -Raw 路径\"; "
                    "请改用 Windows 上可用的命令, 或直接做构建/测试。")
                continue
            kind = _evidence_kind(cmd) if code == 0 else ""
            rounds.append({"round": i, "action": "run", "command": cmd, "code": code,
                           "kind": kind, "output": out[-4000:]})
            if _evidence_run(cmd, code):
                ran_ok = True
                if kind == "behavior":
                    behavior_ok = True
                elif kind == "build":
                    build_ok = True
            elif code != 0:
                real_error_seen = True       # 命令本身没问题却失败了 = 改动还没做对, 该它自己修
            await broadcast(events.event("world", stage="verify", round=i, action="run-done",
                                         command=cmd, code=code, text=out[-1500:]))
            # 失败时把话说明白: 这很可能是**你刚才的改动**不对, 用 fix 自己改, 别只换命令重跑
            last_cmd, last_out = cmd, (out if code == 0 else (
                "这条命令失败了, 先判断是哪种:\n"
                "(1) 命令本身在这台机器上用不了 / 路径或语法写错 -> 换一条 Windows 命令重试(run);\n"
                "(2) 命令没问题、输出确实是编译/测试/验证失败 -> **那就是你刚才的改动不对, 直接用 fix 把文件改对**"
                "(你手上有完整内容, 别只换命令重跑、也别只解释), 改完再跑一条命令证明它成立。\n\n" + out))
            continue
        if action == "fix":
            local_fixes += 1                 # 它自己动手了
            applied, skipped, _ = engineer.apply_manifest({"files": obj.get("files") or [],
                                                            "message": obj.get("message") or ""})
            rounds.append({"round": i, "action": "fix", "applied": applied, "skipped": skipped,
                           "text": obj.get("message") or ""})
            await broadcast(events.event("world", stage="verify", round=i, action="fix-done",
                                         applied=applied, skipped=skipped,
                                         text=obj.get("message") or ""))
            applied_txt += "\n" + "\n".join(f"- {a.get('op')} {a.get('path')}" for a in applied)
            continue
        if action == "done":
            ev_txt = _evidence_text(obj)
            # 没证据的"通过"不算通过: 只读过文件 / 一条命令都没成功过, 就打回去重来
            if not ran_ok:
                note = ("[你说 done, 但这次验证里没有任何一条命令真的跑出过结果(只读文件不算验证)] "
                        "没有证据不能算通过: 请真的跑一条能产生证据的命令(项目自带测试 / 刚构建出来的产物 / "
                        ".verify/ 下的临时验证程序), 再决定用 run 还是 done; 确实验不了就在 reason 里如实说明。")
                rounds.append({"round": i, "action": "no-evidence",
                               "text": obj.get("message") or obj.get("reason") or ""})
                await broadcast(events.event("world", stage="verify", round=i, action="no-evidence",
                                             text="没跑出任何可当证据的命令, 不接受「完成」"))
                last_cmd, last_out = "", note
                continue
            # 没有验收点就没有判定标准 —— 上一版这里放行, 于是"它说通过就算通过"
            if not (criteria or "").strip():
                note = ("[你还没有定验收点] 先把需求提炼成 1~3 条**可判定**的验收点(checks: 什么操作 → "
                        "期望结果), 再拿证据说明每一条都成立; 没有验收点的「通过」没有任何判定标准, 不接受。")
                rounds.append({"round": i, "action": "no-checks",
                               "text": obj.get("message") or obj.get("reason") or ""})
                await broadcast(events.event("world", stage="verify", round=i, action="no-checks",
                                             text="没有验收点, 不接受「完成」"))
                last_cmd, last_out = "", note
                continue
            if not ev_txt.strip():
                note = ("[你说 done, 但没给 evidence] 验收点已经定下了: 请逐条写清是哪条验收点、跑了什么命令、"
                        "并把输出里的**原文片段**照抄进来; 给不出证据就先别用 done。")
                rounds.append({"round": i, "action": "no-evidence",
                               "text": obj.get("message") or obj.get("reason") or ""})
                await broadcast(events.event("world", stage="verify", round=i, action="no-evidence",
                                             text="宣布完成但没给证据, 不接受"))
                last_cmd, last_out = "", note
                continue
            # 证据必须落到"这次真跑过的命令 + 真实输出"上; 行为性验收点不能只有编译证据
            problems = _grounding_problems(ev_txt, criteria, rounds)
            if not problems and _criteria_need_behavior(criteria) and not behavior_ok:
                problems.append("验收点是行为性的, 但这次只跑了编译/静态检查" +
                                ("(qmllint/cmake 之类)" if build_ok else "") +
                                " —— 构建通过不能当行为证据, 要真的跑测试或运行刚构建出来的程序")
            if problems:
                note = ("[证据不被接受] " + "; ".join(problems) + "。请按 "
                        '"evidence": [{"check": 1, "command": "这次真跑过的命令", "quote": "输出里的原文片段"}] '
                        "逐条给证据(引用的命令必须这次真跑过、退出码为 0; 引用的输出必须是原文; "
                        "行为性验收点必须真的执行行为, 编译不算)。")
                rounds.append({"round": i, "action": "weak-evidence", "evidence": ev_txt,
                               "text": obj.get("message") or obj.get("reason") or ""})
                await broadcast(events.event("world", stage="verify", round=i, action="weak-evidence",
                                             text="证据对不上这次真跑过的命令/输出, 不接受「完成」"))
                last_cmd, last_out = "", note
                continue
            rounds.append({"round": i, "action": "done", "evidence": ev_txt,
                           "text": obj.get("message") or obj.get("reason") or ""})
            break
    ok = bool(rounds) and rounds[-1].get("action") == "done"
    # 把失败信息拼成一段可直接发给网页模型的日志
    parts: list[str] = []
    real_error = False
    for r in rounds:
        act = r.get("action")
        if act == "tool-error":
            continue                     # 命令跑不了, 不算项目错误, 也不发给网页模型
        if act == "run" and r.get("code"):
            real_error = True
        if act == "run" and r.get("code"):
            parts.append("$ " + str(r.get("command")) + "\n(退出码 " + str(r.get("code")) + ")\n" +
                         (r.get("output") or "").strip()[-6000:])
        elif act == "run":
            parts.append("$ " + str(r.get("command")) + "\n(通过)")
        elif act == "blocked":
            parts.append("$ " + str(r.get("command")) + "\n[命令被安全策略拦截] " + str(r.get("reason")))
        elif act == "parse-fail":
            parts.append("[本地模型没能给出可解析的验证决策]")
        elif act == "fix":
            files = ", ".join(str(a.get("op")) + " " + str(a.get("path")) for a in (r.get("applied") or []))
            parts.append("[本地模型自己改过: " + (files or "无文件变化") + "]")
    if not parts and last_out:
        parts.append("$ " + last_cmd + "\n" + last_out[-6000:])
    changed = [a for r in rounds if r.get("action") == "fix" for a in (r.get("applied") or [])]
    repair_rounds = max(0, i - max_rounds)     # 额外逼它"自己修"的轮数
    error_log = "\n\n".join(parts[-6:])
    # 结束语必须说清"为什么没通过": 以前只要没有非零退出码就一律说"命令在这台机器上跑不了",
    # 于是"轮数用完/只读了文件/没给证据"都被赖成了环境问题。
    acts = {r.get("action") for r in rounds}
    if ok:
        reason = "ok"
    elif real_error:
        reason = "real-error"
    elif "blocked" in acts:
        reason = "blocked"
    elif "parse-fail" in acts:
        reason = "parse-fail"
    elif "no-checks" in acts:
        reason = "no-checks"
    elif "weak-evidence" in acts:
        reason = "weak-evidence"
    elif "no-evidence" in acts:
        reason = "no-evidence"
    elif "run" not in acts and "fix" not in acts:
        reason = "tool-error"
    else:
        reason = "rounds-exhausted"
    tail = {
        "ok": "本地模型确认项目跑通了(有验收点 + 证据)",
        "real-error": ("本地模型自己改了 " + str(local_fixes) + " 次也没能修好(有真实报错)"
                       if local_fixes else "没能自己搞定(有真实报错)"),
        "blocked": "验证被安全策略拦住了(命令被拒), 没能验证",
        "parse-fail": "本地模型没给出可解析的验证决策, 验证没做完",
        "no-checks": "验证没做完: 它没先定出可判定的验收点, 「通过」没有判定标准",
        "weak-evidence": "不接受「完成」: 它给的证据对不上这次真跑过的命令/输出(或只有编译证据)",
        "no-evidence": "验证没做完: 它没能跑出任何可当证据的命令(只读文件不算验证), 也没给验收证据",
        "tool-error": "没能完成验证(命令在这台机器上跑不了)",
        "rounds-exhausted": f"验证没做完: {len(rounds)} 轮用完也没拿出证据宣布通过(不是项目报错)",
    }[reason]
    payload = {"action": "finished", "round": len(rounds), "ok": ok, "text": tail,
               "reason": reason, "ts": time.time(),
               "root": str(workspace.ROOT), "behavior_ok": behavior_ok, "build_ok": build_ok}
    # 留痕要在清脚手架之前做(以前 .verify/ 一删, 事后就复核不了它到底跑过什么)
    audit = _audit_verify(rounds, payload, criteria, verify_dir) if rounds else ""
    payload["audit"] = audit
    if _cleanup_verify_dir(verify_dir, verify_dir_existed):
        log.info("world verify: 清掉了临时验证目录 %s", verify_dir)
    _world_state["verify"] = payload
    await broadcast(events.event("world", stage="verify", **payload))
    log.info("world verify: ok=%s real_error=%s reason=%s rounds=%d fixes=%d behavior=%s build=%s audit=%s",
             ok, real_error, reason, len(rounds), local_fixes, behavior_ok, build_ok, audit or "-")
    return {"ok": ok, "gave_up": gave_up, "rounds": rounds, "changed": changed,
            "real_error": real_error, "reason": reason, "checks": criteria,
            "local_fixes": local_fixes, "tried_local_fix": bool(local_fixes),
            "repair_rounds": repair_rounds, "behavior_ok": behavior_ok, "build_ok": build_ok,
            "root": str(workspace.ROOT), "audit": audit,
            "last_command": last_cmd, "last_output": last_out[-8000:],
            "error_log": error_log}


@app.get("/api/local-models")
async def api_local_models():
    """扫一下本机常见的本地模型服务(LM Studio / llama.cpp / vLLM / Ollama)。"""
    found = await planner.find_local()
    return {"ok": True, "found": found}


@app.post("/api/settings/test")
async def api_settings_test(req: SettingsRequest):
    if req.planner is None:
        return JSONResponse({"error": "缺少 planner 配置"}, status_code=400)
    p = req.planner.model_dump(exclude_unset=True)
    if p.get("type") == "local":                       # 地址填错/没填 -> 先补成本机真有的服务
        err = await settings.normalize_planner(p)
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
    is_local = (p.get("type") == "local") or any(
        s in (p.get("api_base") or "") for s in ("127.0.0.1", "localhost", "0.0.0.0"))
    from_request = bool(p.get("api_key"))          # 界面里刚填的 key 优先(不用先保存)
    if not from_request:
        p["api_key"] = settings.load()["planner"].get("api_key", "")
    if not p.get("api_key") and not is_local:
        return JSONResponse({"error": "需要先填写 api_key"}, status_code=400)
    try:
        models = await planner.list_models(p)
        return {"ok": True, "models": models[:60],
                "key_source": "request" if from_request else "saved"}
    except planner.PlannerError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/status")
async def api_status():
    return manager.status()


@app.post("/api/start")
async def api_start(req: StartRequest | None = None):
    req = req or StartRequest()
    target = providers.get(req.provider)
    if (manager._ctx is not None  # noqa: SLF001
            and manager.provider.id != target.id and manager.busy):
        return JSONResponse({"error": "正在生成中, 请稍后再切换 Provider"}, status_code=409)
    # 已在启动/登录流程且目标一致 -> 幂等返回
    if (manager._ctx is not None  # noqa: SLF001
            and manager.provider.id == target.id):
        return {"ok": True, "state": manager.state}
    if (manager._ctx is None
            and manager.state in ("launching", "waiting_login")
            and manager.provider.id == target.id):
        return {"ok": True, "state": manager.state}
    _spawn(manager.ensure_started(target.id))
    return {"ok": True, "state": "launching"}


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    if manager.state != "logged_in":
        return JSONResponse({"error": "浏览器未就绪/未登录"}, status_code=503)
    if not await manager.ensure_alive():          # 窗口被关掉时给明确提示, 而不是"找不到输入框"
        return JSONResponse({"error": "桥接浏览器窗口已关闭, 请点左下角「启动并登录」重新打开"},
                            status_code=503)
    if manager.busy:
        return JSONResponse({"error": "正在生成中, 请稍候"}, status_code=409)
    if not (req.text.strip() or req.file_ids):
        return JSONResponse({"error": "文本或文件至少提供一项"}, status_code=400)
    files = []
    for fid in req.file_ids:
        p = _staged_files.pop(fid, None)
        if p:
            files.append(p)
    _spawn(_run_turn(req.text, files))
    return {"ok": True, "state": "queued"}


@app.post("/api/chat_files")
async def api_chat_files(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return JSONResponse({"error": "空文件"}, status_code=400)
    if len(data) > config.MAX_FILE_MB * 1024 * 1024:
        return JSONResponse({"error": f"文件超过 {config.MAX_FILE_MB}MB 上限"}, status_code=413)
    name = file.filename or "file"
    mime = file.content_type or "application/octet-stream"
    fid = _stage_file(data, name, mime)
    return {"id": fid, "name": name, "size": len(data)}


@app.post("/api/new_chat")
async def api_new_chat():
    if manager.state != "logged_in":
        return JSONResponse({"error": "浏览器未就绪/未登录"}, status_code=503)
    if not await manager.ensure_alive():
        return JSONResponse({"error": "桥接浏览器窗口已关闭, 请点左下角「启动并登录」重新打开"},
                            status_code=503)
    ok = await manager.new_chat()
    return {"ok": ok, "provider": manager.provider.name,
            "conversation_id": manager.conversation_id()}


@app.get("/api/conversations")
async def api_conversations(more: int = 0):
    """站点自己的会话历史列表(读不到就是空列表, 前端会退回本地记录)。
    more=1: 先把站点列表滚到底再读, 用来翻出更早的会话。"""
    if manager.state != "logged_in":
        return JSONResponse({"error": "浏览器未就绪/未登录"}, status_code=503)
    if not await manager.ensure_alive():
        return JSONResponse({"error": "桥接浏览器窗口已关闭, 请点左下角「启动并登录」重新打开"},
                            status_code=503)
    items = await manager.conversations(more=bool(more))
    return {"ok": True, "provider": manager.provider.id,
            "current": manager.conversation_id(), "items": items, "more": bool(more)}


@app.post("/api/conversations/open")
async def api_open_conversation(req: OpenConvRequest):
    if manager.state != "logged_in":
        return JSONResponse({"error": "浏览器未就绪/未登录"}, status_code=503)
    if manager.busy:
        return JSONResponse({"error": "正在生成中, 请稍候"}, status_code=409)
    if not await manager.ensure_alive():
        return JSONResponse({"error": "桥接浏览器窗口已关闭, 请点左下角「启动并登录」重新打开"},
                            status_code=503)
    ok = await manager.open_conversation(req.key, req.url)
    if not ok:
        return JSONResponse({"error": "切换会话失败(站点结构可能已改版)"}, status_code=502)
    return {"ok": True, "current": manager.conversation_id()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        await ws.send_json(events.status_event(**manager.status()))
        while True:
            data = await ws.receive_text()
            # 预留: 未来可直接在 WS 上收 chat 命令
            if data == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _clients.discard(ws)


# 静态资源挂载在 API/WS 路由之后: 前面已有的路由优先匹配,
# 其余路径交给 StaticFiles 提供分离的 index.html + css/app.css + js/app.js
app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")
