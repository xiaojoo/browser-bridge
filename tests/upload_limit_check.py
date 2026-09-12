"""文件上传受限: 站点那句提示要被认出来并回到页面上。

三段:
  1. _ATTACH_BLOCK_JS 的识别(真实无头浏览器 + 合成的"站点页面"): 认得出提示, 不误报
  2. BrowserManager.attach_files: 站点说不行 -> 不许再报"已附加 N 个文件"
  3. file_operator.send_file / /api/file/operate: 抛 UploadBlockedError, 并把原话弹到页面

不联网、不开桥接浏览器(用桩 page), 工作区切到 .tmp 下。
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import browser as browser_mod, config, file_operator, server, workspace  # noqa: E402
from bridge.browser import (BrowserManager, _ATTACH_BLOCK_JS, _ATTACH_DIAG_JS,  # noqa: E402
                            _FILE_INPUTS_JS, UPLOAD_LIMIT_HINT)

TMP_WS = ROOT / ".tmp" / "upload-limit-ws"
EN = "You've reached the limit of files you can upload to this conversation."
ZH = "本次对话上传文件数量已达上限, 无法继续上传"
URL = "http://127.0.0.1:8765/"


class StubLocator:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return self.page.inputs

    @property
    def last(self):
        return self

    def nth(self, i):
        self.page.used_input = i            # 记住到底喂了第几个文件框(回归点)
        return self

    async def set_input_files(self, payload):
        self.page.uploaded.append(payload)


class StubPage:
    """只实现 attach_files 用到的那几个接口。"""

    def __init__(self, notice="", inputs=1, landed=True, accepts=None):
        # 默认模仿 ChatGPT: 第一个框 accept="", 最后一个是 image/*,video/*
        self.accepts = accepts if accepts is not None else (["", "image/*,video/*"] if inputs else [])
        self.inputs = len(self.accepts)
        self.notice = notice
        self.landed = landed                # 站点附件区里到底出没出现这个文件
        self.used_input = None
        self.uploaded: list = []

    def is_closed(self):
        return False

    def locator(self, sel):
        assert sel == "input[type=file]", sel
        return StubLocator(self)

    async def evaluate(self, js, arg=None):
        if js is _FILE_INPUTS_JS:
            return [{"accept": a, "disabled": False} for a in self.accepts]
        if js is _ATTACH_DIAG_JS:
            names = list(arg or [])
            return {"url": "stub://page", "inputs": self.inputs, "inputsDisabled": 0,
                    "accept": self.accepts[-1] if self.accepts else "",
                    "inputInfo": [], "landed": {n: self.landed for n in names},
                    "landedHtml": {}, "progress": 0, "warn": []}
        if js is _ATTACH_BLOCK_JS:
            return self.notice
        raise AssertionError("意外的诊断脚本")


def mgr_with(page: StubPage) -> BrowserManager:
    m = BrowserManager()
    m.state = "logged_in"
    m.page = page
    return m


async def probe_cases() -> list[str]:
    """用合成的站点页面验 _ATTACH_BLOCK_JS。"""
    bad = []
    cases = [
        ("英文 toast", f'<div role="alert" style="padding:20px">{EN}</div>', True, EN),
        ("中文 toast", f'<div class="toast" style="padding:20px">{ZH}</div>', True, ZH),
        ("英文 banner", f'<div aria-live="polite" style="padding:12px">File uploads are not '
                        f'available right now.</div>', True, "File uploads are not available right now."),
        ("无关提示不误报", '<div class="alert" style="padding:20px">网络连接已断开</div>', True, ""),
        ("正文里的字样不误报", f'<div class="chat" style="padding:20px">上次你问的{ZH}是多少?</div>', True, ""),
        ("隐藏的提示不算", f'<div role="alert" style="display:none">{EN}</div>', True, ""),
    ]
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page()
        for name, html, saw, want in cases:
            await page.set_content(f"<html><body>{html}"
                                   f"<input type='file'></body></html>")
            got = await page.evaluate(_ATTACH_BLOCK_JS, {"sawInput": saw})
            ok = (got == want) if want else (got == "")
            print(f"  [{name}] -> {got!r}")
            if not ok:
                bad.append(f"{name}: 期望 {want!r}, 实际 {got!r}")

        # 结构信号: 输入框被禁用 / 入口消失
        await page.set_content("<body><input type='file' disabled></body>")
        got = await page.evaluate(_ATTACH_BLOCK_JS, {"sawInput": True})
        print("  [输入框被禁用] ->", repr(got))
        if "禁用" not in got:
            bad.append("输入框被禁用没认出来: " + repr(got))
        await page.set_content("<body><div>no input here</div></body>")
        got = await page.evaluate(_ATTACH_BLOCK_JS, {"sawInput": True})
        print("  [入口消失] ->", repr(got))
        if "消失" not in got:
            bad.append("上传入口消失没认出来: " + repr(got))
        got = await page.evaluate(_ATTACH_BLOCK_JS, {"sawInput": False})
        if got != "":
            bad.append("没让我们找输入框时不该给结构结论: " + repr(got))

        # 页面侧: info 事件要真的显示出来(警告"返回页面")
        pg = await b.new_page(viewport={"width": 1200, "height": 800})
        await pg.add_init_script("window.WebSocket = function(){return {close(){},send(){}};};")
        await pg.goto(URL, wait_until="networkidle", timeout=30_000)
        await pg.wait_for_timeout(500)
        msg = UPLOAD_LIMIT_HINT + ZH
        await pg.evaluate("(t) => handle({type: 'info', text: t})", msg)
        await pg.wait_for_timeout(200)
        shown = await pg.evaluate("() => Array.from(document.querySelectorAll('#toast .toast'))"
                                  ".map(e => e.textContent)")
        print("  页面上的提示:", json.dumps(shown, ensure_ascii=False))
        if not any(msg in s for s in shown):
            bad.append("info 事件没有在页面上显示出来: " + json.dumps(shown, ensure_ascii=False))
        await b.close()
    return bad


async def main() -> int:
    bad: list[str] = []
    print("[1] _ATTACH_BLOCK_JS 识别")
    bad += await probe_cases()

    print("[2] attach_files: 站点说不行就不能报成功")
    pg = StubPage()
    ok, errs = await mgr_with(pg).attach_files([{"name": "a.txt", "mime": "text/plain",
                                                "data": b"x"}])
    assert (ok, errs) == (1, []), (ok, errs)
    print(f"  正常: ok={ok} errs={errs} 喂的是第 {pg.used_input} 个文件框")
    # 【回归】文档不许再喂给 last(accept=image/*,video/*) —— 会被静默丢弃;
    # 要挑 accept="" 那个(第 0 个)
    if pg.used_input != 0:
        bad.append("又挑到了不收文档的那个文件框(应为第 0 个): " + str(pg.used_input))
    ok, errs = await mgr_with(StubPage(notice=ZH)).attach_files(
        [{"name": "a.txt", "mime": "text/plain", "data": b"x"}])
    print(f"  受限: ok={ok} errs={errs}")
    if ok != 0 or not errs or ZH not in errs[0] or UPLOAD_LIMIT_HINT not in errs[0]:
        bad.append("站点说上传受限时还报成功/没带站点原话: " + json.dumps([ok, errs], ensure_ascii=False))
    # 用户实际遇到的那种: 站点**什么都没说**, 附件区里也根本没有这个文件
    # (set_input_files 不报错 -> 以前记成"已附加", 消息发出去模型说"没看到附件")
    ok, errs = await mgr_with(StubPage(notice="", landed=False)).attach_files(
        [{"name": "RAG处理PDF面试指南.md", "mime": "text/markdown", "data": b"x"}])
    print(f"  静默丢弃: ok={ok} errs={errs}")
    if ok != 0 or not errs or "没有接受" not in errs[0]:
        bad.append("站点静默丢弃时没报出来: " + json.dumps([ok, errs], ensure_ascii=False))
    # 页面上没有一个文件框收得下这种类型 -> 直接说清楚(别喂了再假装成功)
    ok, errs = await mgr_with(StubPage(accepts=["image/*,video/*"])).attach_files(
        [{"name": "a.md", "mime": "text/markdown", "data": b"x"}])
    print(f"  类型都不收: ok={ok} errs={errs}")
    if ok != 0 or not errs or "不收这种类型" not in errs[0]:
        bad.append("类型都不收时没报出来: " + json.dumps([ok, errs], ensure_ascii=False))
    ok, errs = await mgr_with(StubPage(inputs=0)).attach_files(
        [{"name": "a.txt", "mime": "text/plain", "data": b"x"}])
    if ok != 0 or not errs:
        bad.append("没有文件输入框时应该报失败: " + json.dumps([ok, errs], ensure_ascii=False))
    print(f"  无输入框: ok={ok} errs={errs}")

    print("[2b] accept 打分: 图片仍走 image/* 那个框(别把原来的图片通路搞坏)")
    pg2 = StubPage(accepts=["", "image/*", "image/*,video/*"])
    await mgr_with(pg2).attach_files([{"name": "shot.png", "mime": "image/png", "data": b"x"}])
    print(f"  png -> 第 {pg2.used_input} 个文件框")
    if pg2.used_input != 2:
        bad.append("图片没有走 image/* 的文件框: " + str(pg2.used_input))

    print("[3] file_operator.send_file / 路由: 把站点原话回传")
    shutil.rmtree(TMP_WS, ignore_errors=True)
    TMP_WS.mkdir(parents=True)
    (TMP_WS / "a.txt").write_bytes(b"HELLO=1\n")
    orig_root = workspace.ROOT
    workspace.use_root(TMP_WS)
    try:
        m = mgr_with(StubPage(notice=EN))
        try:
            await file_operator.send_file(m, "a.txt")
            bad.append("站点受限时 send_file 应该抛 UploadBlockedError")
        except file_operator.UploadBlockedError as exc:
            print(f"  UploadBlockedError: {exc}")
            if EN not in str(exc):
                bad.append("UploadBlockedError 里没有站点原话: " + str(exc))
        except file_operator.FileOperatorError as exc:
            bad.append("抛成了普通 FileOperatorError(页面上就不会弹提示了): " + str(exc))

        # 非站点原因(文件不存在)仍然是普通错误, 不该冒充"上传受限"
        try:
            await file_operator.send_file(mgr_with(StubPage()), "nope.txt")
            bad.append("不存在的文件应该报错")
        except file_operator.UploadBlockedError:
            bad.append("文件不存在被误判成上传受限")
        except file_operator.FileOperatorError as exc:
            print(f"  文件不存在仍是普通错误: {exc}")

        # 路由: 受限时返回 upload_blocked + 广播给页面
        sent: list[dict] = []

        async def fake_broadcast(payload):
            sent.append(payload)

        orig_broadcast = server.broadcast
        server.broadcast = fake_broadcast
        server.manager = mgr_with(StubPage(notice=ZH))
        try:
            resp = await server.api_file_operate(
                server.FileOperateRequest(action="send_file", path="a.txt"))
        finally:
            server.broadcast = orig_broadcast
        body = json.loads(bytes(resp.body).decode("utf-8"))
        print("  路由返回:", json.dumps(body, ensure_ascii=False))
        if resp.status_code != 400 or not body.get("upload_blocked"):
            bad.append("路由没有把上传受限标出来: " + json.dumps(body, ensure_ascii=False))
        if not any(e.get("type") == "info" and ZH in (e.get("text") or "") for e in sent):
            bad.append("路由没有把站点原话广播到页面: " + json.dumps(sent, ensure_ascii=False))
    finally:
        workspace.use_root(orig_root)
        shutil.rmtree(TMP_WS, ignore_errors=True)

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("UPLOAD_LIMIT_OK (站点说上传受限 -> 原话带回界面, 不再假报已附加; 页面上能看到)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
