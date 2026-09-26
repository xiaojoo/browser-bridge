"""站点上下文到上限 -> 自动另开窗口时, 本地模型把上一段汇总成"接力上下文"带过去。

不联网、不开桥接浏览器: planner / 页面读取都用替身。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import planner, server  # noqa: E402
from bridge.browser import BrowserManager  # noqa: E402

CFG = {"engine": {}, "planner": {"type": "api", "api_base": "http://x/v1",
                                 "api_model": "stub", "api_key": "k"}}
MSGS = [
    {"role": "user", "text": "帮我把 qml/EditorArea.qml 的滚动条改成常显"},
    {"role": "assistant", "text": "改法: ScrollBar.vertical.policy: ScrollBar.AlwaysOn"},
    {"role": "user", "text": "现在报错: qmllint 说 ScrollBar 未定义, 版本 Qt 6.5"},
]

asked: list[str] = []
infos: list[str] = []


class StubPage:
    def __init__(self):
        self.keys: list[str] = []

    def is_closed(self):
        return False

    async def evaluate(self, js, arg=None):
        return ""

    @property
    def keyboard(self):
        page = self

        class Kb:
            async def press(self, k):
                page.keys.append(k)
        return Kb()


async def main() -> int:
    bad: list[str] = []
    orig_load, orig_ask, orig_bc = server.settings.load, planner.ask, server.broadcast
    server.settings.load = lambda: json.loads(json.dumps(CFG))

    async def fake_ask(pcfg, prompt, system=None):
        asked.append(prompt)
        return "目标: 让 EditorArea.qml 滚动条常显(Qt 6.5)。\n- 已确认: 用 ScrollBar.AlwaysOn\n- 待办: 解决 qmllint 报 ScrollBar 未定义"

    async def fake_broadcast(payload):
        if payload.get("type") == "info":
            infos.append(payload.get("text") or "")

    planner.ask, server.broadcast = fake_ask, fake_broadcast

    try:
        # ---------- 1) 汇总本身 ----------
        asked.clear()
        infos.clear()
        m = BrowserManager()
        m.state = "logged_in"
        m.page = StubPage()

        async def read_conv():
            return {"ok": True, "count": len(MSGS), "messages": MSGS}

        m.read_conversation = read_conv
        ctx = await server._handoff_context(m)
        print("接力上下文:\n" + json.dumps(ctx, ensure_ascii=False)[:400])
        if not ctx.startswith("【上一窗口的交接"):
            bad.append("接力上下文没有明确的头: " + ctx[:80])
        if "ScrollBar.AlwaysOn" not in ctx or "Qt 6.5" not in ctx:
            bad.append("汇总结果没有带过来: " + ctx[:200])
        if not asked or "ScrollBar.AlwaysOn" not in asked[0] or "【用户】" not in asked[0]:
            bad.append("喂给本地模型的对话不完整: " + (asked[0][-300:] if asked else "(没调用)"))

        # ---------- 2) 太长: 留头 + 尾 ----------
        long_msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                      "text": ("开头" if i == 0 else "结尾" if i == 39 else "中间") + "甲" * 4000}
                     for i in range(40)]
        digest = server._conversation_digest(long_msgs)
        print("超长摘要:", len(digest), "字, 含省略标记:", "中间省略" in digest)
        if "中间省略" not in digest or len(digest) > 61000:
            bad.append("超长对话没有正确截断: " + str(len(digest)))
        if "开头" not in digest or "结尾" not in digest:
            bad.append("截断后丢了头或尾: " + digest[:60] + " / " + digest[-60:])

        # ---------- 3) 站点读不回来 -> 用 bridge 记下的最近几轮 ----------
        asked.clear()
        server._recent_msgs.clear()
        server._remember_turn("本地记的问题", "本地记的回答")
        m2 = BrowserManager()
        m2.state = "logged_in"
        m2.page = StubPage()

        async def read_fail():
            return {"ok": False, "error": "该站点不支持"}

        m2.read_conversation = read_fail
        ctx2 = await server._handoff_context(m2)
        if "本地记的问题" not in (asked[0] if asked else "") or not ctx2:
            bad.append("站点读不回来时没有退回本地记录: " + str(asked)[:200])

        # ---------- 4) 没配规划模型 -> 明确提示, 不硬编 ----------
        infos.clear()
        server.settings.load = lambda: {"engine": {}, "planner": {"type": "web"}}
        ctx3 = await server._handoff_context(m2)
        print("没配模型:", repr(ctx3), "|", json.dumps(infos, ensure_ascii=False)[:160])
        if ctx3 or not any("没配" in t for t in infos):
            bad.append("没配规划模型时应该明确提示: " + json.dumps([ctx3, infos], ensure_ascii=False)[:200])
        server.settings.load = lambda: json.loads(json.dumps(CFG))

        # ---------- 5) send_text: 换窗口时把汇总拼在消息前面 ----------
        asked.clear()
        infos.clear()
        filled: list[str] = []
        reasons = ["上下文长度已达上限"]

        async def fake_blocked():
            return reasons.pop(0) if reasons else ""

        async def fake_open():
            return True

        async def fake_fill(composer, text):
            filled.append(text)

        async def fake_composer():
            return object()

        async def fake_composer_text():
            return ""                       # 站点已清空 = 发出去了

        async def fake_on_change(status, **extra):
            info = extra.get("info")        # 线上由 server._on_manager_change 广播成 info 事件
            if isinstance(info, dict) and info.get("text"):
                infos.append(info["text"])

        m3 = BrowserManager()
        m3.state = "logged_in"
        m3.page = StubPage()
        m3.on_change = fake_on_change
        m3.read_conversation = read_conv
        m3.on_context_limit = server._handoff_context      # 线上是 server 在启动时挂上的
        m3.blocked_reason = fake_blocked
        m3.open_fresh_window = fake_open
        m3._fill_composer = fake_fill
        m3._composer = fake_composer
        m3._composer_text = fake_composer_text
        ok = await m3.send_text("请继续刚才的改动")
        print("换窗口后发出去的内容:", json.dumps(filled, ensure_ascii=False)[:200])
        if not ok:
            bad.append("换窗口重试没有成功")
        if len(filled) != 1:
            bad.append("应该只发一条(重试那一条): " + json.dumps(filled, ensure_ascii=False)[:200])
        elif not (filled[0].startswith("【上一窗口的交接") and filled[0].endswith("请继续刚才的改动")):
            bad.append("汇总没有拼在新消息前面: " + filled[0][:200])
        if not any("汇总" in t for t in infos):
            bad.append("界面上没有提示「已汇总带过去」: " + json.dumps(infos, ensure_ascii=False)[:200])
        # 线上那条 manager 必须真的挂上了接力回调(不然功能等于没接)。
        # 不比对函数对象: 线上挂的是一层 lambda(它要多传 arm_relay=True), 对象必然不是
        # _handoff_context 本身。所以查这个回调的函数体里到底调不调 _handoff_context
        # —— 没挂上、或挂成了别的东西, 都过不了。
        cb = server.manager.on_context_limit
        names = getattr(getattr(cb, "__code__", None), "co_names", ())
        if not (callable(cb) and "_handoff_context" in names):
            bad.append("server.manager 没有挂上 on_context_limit 接力回调: %r (函数体引用 %s)"
                       % (cb, names))
    finally:
        server.settings.load, planner.ask, server.broadcast = orig_load, orig_ask, orig_bc
        server._recent_msgs.clear()

    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("HANDOFF_OK (上下文到上限自动换窗口时, 本地模型汇总上一段并拼在新消息前面带过去; "
          "读不回对话 / 没配模型都有明确退路)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
