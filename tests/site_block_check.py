"""站点不能输入时: 自动另开窗口重试一次, 两个窗口都不行就停下并把上限提示带回来。

检测脚本用真实浏览器跑(喂 ChatGPT 那样的额度提示页面), 重试逻辑用替身对象单测。
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

import bridge.browser as B  # noqa: E402
import bridge.browser as browser_mod  # noqa: E402

QUOTA_HTML = """<html><body>
<div class="banner">聊天已暂停，使用额度将在 19:59 重置</div>
<div>你已达到包含 文件或图像的 聊天次数上限。请发起新的纯文本聊天，或升级以立即继续。</div>
</body></html>"""

DISABLED_HTML = """<html><body>
<form><div id="prompt-textarea" contenteditable="false">不可输入</div></form>
</body></html>"""

NORMAL_HTML = """<html><body>
<div id="prompt-textarea" contenteditable="true">可以输入</div>
</body></html>"""

SELS = ["#prompt-textarea"]


class FakePage:
    def __init__(self, out=None, blocked=None):
        self._out = out
        self.keyboard = types.SimpleNamespace(type=self._type, press=self._press)
        self.sent = []
        self.blocked = blocked

    async def _type(self, text, delay=0):
        self.sent.append(text)

    async def _press(self, key):
        self.sent.append("<" + key + ">")


class FakeManager:
    """只保留 send_text 需要的部分, 用真实的 BrowserManager.send_text。"""
    page = None
    _ctx = None

    def __init__(self, blocked_seq):
        self._blocked = list(blocked_seq)
        self.opened = 0
        self.notified = 0
        self.page = FakePage()

    async def ensure_alive(self):
        return True

    def page_alive(self):
        return True

    async def blocked_reason(self):
        return self._blocked.pop(0) if self._blocked else ""

    async def open_fresh_window(self):
        self.opened += 1
        return True

    async def _notify(self, **kw):
        self.notified += 1

    async def _composer(self):
        return None                      # 走到这里说明没被拦(测试里当"没有输入框"处理)


FakeManager.send_text = B.BrowserManager.send_text


class FillManager:
    """模拟"键盘敲到一半被页面重绘打断"的场景, 用真实的 _fill_composer。"""
    provider = types.SimpleNamespace(composer_selectors=["#prompt-textarea"])

    def __init__(self, break_typing=True, inject_ok=True):
        self.box = ""
        self.break_typing = break_typing
        self.inject_ok = inject_ok
        self.typed = ""

        class Keyboard:
            def __init__(self, outer):
                self.outer = outer

            async def type(self, text, delay=0):
                # 只敲进去一半(模拟页面刷新把焦点弄丢)
                half = text[: max(1, len(text) // 2)] if self.outer.break_typing else text
                self.outer.typed += half
                self.outer.box += half

            async def press(self, key):
                self.outer.pressed = getattr(self.outer, "pressed", []) + [key]

        class Page:
            def __init__(self, outer):
                self.outer = outer
                self.keyboard = Keyboard(outer)

            async def evaluate(self, js, arg=None):
                if js is browser_mod._READ_JS:
                    return self.outer.box
                if js is browser_mod._FILL_JS:
                    if not self.outer.inject_ok:
                        return False
                    self.outer.box = (arg or {}).get("text", "")
                    return True
                return None

        self.page = Page(self)


FillManager._norm = staticmethod(browser_mod.BrowserManager._norm)
FillManager._composer_text = browser_mod.BrowserManager._composer_text
FillManager._fill_composer = browser_mod.BrowserManager._fill_composer


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page()
        for name, html, want in (("额度提示", QUOTA_HTML, True),
                                 ("输入框被禁用", DISABLED_HTML, True),
                                 ("正常页面", NORMAL_HTML, False)):
            await page.set_content(html)
            got = await page.evaluate(B._BLOCK_JS, {"sels": SELS})
            print(name, "->", repr(got[:80]))
            if want and not got:
                bad.append(f"{name} 没被识别出来")
            if not want and got:
                bad.append(f"{name} 被误判为不可输入: {got[:60]}")
            if name == "额度提示" and "上限" not in got:
                bad.append("没有把站点的原话带回来: " + got[:80])
        await b.close()

    # 0) 键盘输入被打断 -> 回读发现不完整 -> 重写(注入) -> 完整了才继续
    fm = FillManager(break_typing=True, inject_ok=True)
    ok_fill = await fm._fill_composer(None, "很长的一段中文消息" * 3)
    print("被打断后回读重写:", ok_fill, "| 最终内容长度:", len(fm.box))
    if not ok_fill or len(fm.box) != len("很长的一段中文消息" * 3):
        bad.append("键盘被打断后没有重写完整: " + str(len(fm.box)))

    # 注入也失败 -> 宁可报错也不发半条
    fm2 = FillManager(break_typing=True, inject_ok=False)
    try:
        await fm2._fill_composer(None, "很长的一段中文消息" * 3)
        bad.append("写不完整时应该报错而不是发半条")
    except RuntimeError as exc:
        print("写不完整:", str(exc)[:80])
        if "半条" not in str(exc):
            bad.append("报错信息没说清风险: " + str(exc)[:100])

    # 1) 第一个窗口就被限制 -> 另开窗口; 新窗口还是不行 -> 停下并带上限信息
    m = FakeManager(["你已达到包含 文件或图像的 聊天次数上限", "你已达到包含 文件或图像的 聊天次数上限"])
    try:
        await m.send_text("hello")
        bad.append("两个窗口都不能输入时应该报错停下")
    except RuntimeError as exc:
        print("两个窗口都不行:", str(exc)[:110])
        if "连续两个窗口都不行" not in str(exc) or "上限" not in str(exc):
            bad.append("报错信息里没有说明是连续两个窗口 + 上限: " + str(exc)[:120])
    if m.opened != 1:
        bad.append(f"应该只重开一次窗口, 实际 {m.opened}")
    if m.notified < 1:
        bad.append("重开窗口前没有提示用户")

    # 0b) 发送后输入框仍然留着内容 -> 只记日志, 不能报错
    class StickyManager(FakeManager):
        def __init__(self):
            super().__init__([""])
            self.page = types.SimpleNamespace(
                keyboard=types.SimpleNamespace(type=self._t, press=self._p))
            self.sent = []
            self.box = ""

        async def _t(self, text, delay=0):
            self.sent.append(text)
            self.box = text                      # 一直不清空

        async def _p(self, key):
            self.sent.append("<" + key + ">")

        async def _composer(self):
            return types.SimpleNamespace(click=self._click)

        async def _click(self):
            pass

        async def _composer_text(self):
            return self.box

        async def open_fresh_window(self):
            self.opened += 1
            return True

    StickyManager._norm = staticmethod(browser_mod.BrowserManager._norm)
    StickyManager._composer_text = browser_mod.BrowserManager._composer_text
    StickyManager.send_text = browser_mod.BrowserManager.send_text

    async def _fill(self, composer, text):      # 直接写进去(跳过输入细节)
        self.box = text

    StickyManager._fill_composer = _fill
    sm = StickyManager()
    try:
        ok_send = await sm.send_text("这条已经发出去了, 但输入框没清空")
        if not ok_send:
            bad.append("粘住输入框的场景应该仍然视为发送成功")
        if "<Enter>" not in sm.sent:
            bad.append("没有按下回车")
    except RuntimeError as exc:
        bad.append("输入框没清空时不该报错(会把已发出的消息误判为失败): " + str(exc)[:120])

    # 1b) 换窗口后: 捕获必须跟到新页面(否则收不到输出)
    src = open(str(Path(__file__).resolve().parent.parent / "bridge" / "server.py"),
               encoding="utf-8").read()
    if "页面已更换(另开窗口), 捕获切换到新页面" not in src:
        bad.append("turn 里没有在换窗口后把捕获切到新页面")
    bsrc = open(str(Path(__file__).resolve().parent.parent / "bridge" / "browser.py"),
                encoding="utf-8").read()
    if "新页面也要挂捕获" not in bsrc:
        bad.append("另开的新窗口没有挂捕获")

    # 2) 第一个窗口被限制 -> 新窗口可用 -> 继续走原来的发送流程
    m2 = FakeManager(["达到上限", ""])
    try:
        await m2.send_text("hello")
    except RuntimeError as exc:
        # 替身的 _composer 返回 None, 说明确实走到新窗口的发送流程了
        if "找不到输入框" not in str(exc):
            bad.append("新窗口重试后走到了意料之外的错误: " + str(exc)[:120])
    else:
        bad.append("替身没有输入框, 却成功发送了?")
    if m2.opened != 1:
        bad.append(f"应该重开一次窗口, 实际 {m2.opened}")

    # 3) 正常情况: 不重开窗口
    m3 = FakeManager([""])
    try:
        await m3.send_text("hello")
    except RuntimeError:
        pass
    if m3.opened:
        bad.append("没被限制时不该重开窗口")

    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("SITE_BLOCK_OK (站点不能输入 -> 另开窗口重试一次; 两个窗口都不行 -> 停下并返回上限提示)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
