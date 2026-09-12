"""连接后必须切到"新对话"页, 不能停在上次那个会话上(用户诉求: 每次连接都是新窗口)。

后端这一半在 browser.py: `_launch()` 登录成功后调 `open_fresh_chat()`。
这里用替身验证它的决策(不联网、不开浏览器):
  - 站点还停在上次那个会话(URL 里有 /c/convA) -> 必须切到新对话
  - 已经在新会话页(URL 里没有会话 id)        -> 一根汗毛都不动
  - 第一次点不动(SPA 还没渲染出按钮)          -> 要再试一次
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import providers  # noqa: E402
from bridge.browser import BrowserManager  # noqa: E402


class FakePage:
    def __init__(self, url: str):
        self.url = url

    def is_closed(self) -> bool:
        return False


async def main() -> int:
    bad: list[str] = []
    mgr = BrowserManager()
    mgr.provider = providers.get("chatgpt")          # conversation_pattern = "/c/"
    calls: list[str] = []

    async def ok_new_chat() -> bool:
        calls.append("new_chat")
        mgr.page = FakePage("https://chatgpt.com/")   # 切开之后 URL 里就没有会话 id
        return True

    # 1) 站点还停在上次那个会话 -> 必须主动切到新对话
    mgr.page = FakePage("https://chatgpt.com/c/convA")
    mgr.new_chat = ok_new_chat
    got = await mgr.open_fresh_chat()
    print("停在上次会话:", got, calls)
    if not got or calls != ["new_chat"]:
        bad.append("连上后没有把上次那个会话切成新对话: " + repr((got, calls)))
    if mgr.conversation_id() is not None:
        bad.append("切开之后本地还认为停在某个会话上: " + str(mgr.conversation_id()))

    # 2) 已经在新会话页 -> 不要乱动
    calls.clear()
    mgr.page = FakePage("https://chatgpt.com/")
    got = await mgr.open_fresh_chat()
    print("已在新会话页:", got, calls)
    if not got or calls:
        bad.append("已经在新会话页却又点了一次新对话: " + repr((got, calls)))

    # 3) 第一次点不动 -> 隔一会儿再试一次(SPA 常见)
    calls.clear()

    async def flaky_new_chat() -> bool:
        calls.append("try")
        return False

    mgr.page = FakePage("https://chatgpt.com/c/convA")
    mgr.new_chat = flaky_new_chat
    got = await mgr.open_fresh_chat()
    print("点不动时:", got, calls)
    if got or len(calls) != 2:
        bad.append("新对话点不动时没有重试一次: " + repr((got, calls)))

    # 4) 接线: _launch() 登录成功后确实调了它(不然上面这些决策根本不会跑)
    src = (ROOT / "bridge" / "browser.py").read_text(encoding="utf-8")
    launch = src.split("async def _launch", 1)[1].split("async def ", 1)[0]
    if "open_fresh_chat()" not in launch:
        bad.append("_launch() 里没有在登录成功后调用 open_fresh_chat()")

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("FRESH_WINDOW_OK (连上后主动切到新对话页, 不乱动已在新会话页的情况, 失败会重试)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
