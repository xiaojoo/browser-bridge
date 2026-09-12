"""DOM 快照捕获不能把"上一轮的回答"当成本轮答案。

真实故障(bridge 日志 22:19:55~22:20:03):
    send_text: 14 chars | 修改后测试通过的完整代码输出
    first delta kind=text snap=True
    wait_turn_end done truncated=False errs=[] deltas=2
新回答的节点先是冒了个头(半截文本), 随后页面重绘、那条节点消失, 快照退回"本轮之前那条回答";
收尾时 maybe_final() 又把这条旧回答整段回传 —— 于是消息列表里新问题配上了上一轮的答案,
看起来就是"错位 / 滞后了一条"。

三个场景:
  1) 假页面按时间重演上面那段(无浏览器, 时间线精确);
  2) 真页面(无头 Chromium, 真选择器 + 真 toMarkdown)重演同一段: 节点加了又删, 再回来;
  3) 站点根本没给出新回答(页面上始终只有上一轮那条): 必须如实报"没捕获到内容",
     不能把上一轮的回答当本轮结果。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import capture, config, providers  # noqa: E402

PREV = "上一轮的回答: 对，这个现象说明滚动条本身已经实现了，但当前实现仍然受条件控制…"
PARTIAL = "好的，我按你要求给出完整代码输出：\n```qml\nFlickable {"          # 刚冒头的半截
REAL = "好的，下面是修改后测试通过的完整文件：\n```qml\nimport QtQuick\n" + "x" * 400
SEL = providers.get("chatgpt").snapshot_selector


class FakePage:
    """按时间推进的假页面: 时间线 = [(相对秒, 快照), ...]。"""

    def __init__(self, timeline):
        self.timeline = timeline
        self.t0 = time.monotonic()
        self.snapshots = 0

    async def evaluate(self, script, arg=None):
        if "__dsCap" in script:                     # enable/disable/drain: 本轮没有网络流
            return [] if "return" in script else None
        if "querySelectorAll(expr)" in script:      # _SNAPSHOT_JS
            self.snapshots += 1
            elapsed = time.monotonic() - self.t0
            snap = self.timeline[0][1]
            for at, s in self.timeline:
                if elapsed >= at:
                    snap = s
            return dict(snap)
        return None


async def run_case(page, mode, timeout=14.0):
    deltas = []

    async def on_delta(kind, text, snapshot):
        deltas.append(text)
        await asyncio.sleep(0)

    t0 = time.monotonic()
    err = None
    try:
        await asyncio.wait_for(capture.wait_turn_end(
            page, on_delta, mode=mode, snapshot_selector=SEL), timeout=timeout)
    except capture.NoDataError as exc:
        err = exc
    return deltas, time.monotonic() - t0, err


async def case1_fake(bad):
    timeline = [
        (0.0, {"count": 1, "text": PREV, "generating": False}),      # 本轮之前: 上一轮的回答
        (0.6, {"count": 2, "text": PARTIAL, "generating": False}),   # 新回答节点冒头(半截)
        (1.2, {"count": 1, "text": PREV, "generating": False}),      # 重绘: 节点消失, 快照退回旧回答
        (3.2, {"count": 2, "text": REAL, "generating": False}),      # 真正的完整回答
    ]
    deltas, dur, err = await run_case(FakePage(timeline), "dom")
    print(f"[1] 假页面: {dur:.1f}s 回传 {len(deltas)} 次 -> {[d[:18] for d in deltas]}")
    if PREV in deltas:
        bad.append("[1] 把上一轮的回答当成本轮答案回传(消息会滞后一条)")
    if not deltas or deltas[-1] != REAL:
        bad.append(f"[1] 最终答案不是本轮的真回答: {(deltas[-1][:30] if deltas else '空')}")
    if dur > 12:
        bad.append(f"[1] 这一轮拖得太久: {dur:.1f}s")


async def case2_real_dom(bad):
    seed = """(prev) => {
      document.body.innerHTML =
        '<div data-message-author-role="user"><div class="markdown">问题一</div></div>' +
        '<div data-message-author-role="assistant"><div class="markdown"></div></div>';
      document.querySelector('[data-message-author-role="assistant"] .markdown').textContent = prev;
    }"""
    add = """(text) => {
      const d = document.createElement('div');
      d.setAttribute('data-message-author-role', 'assistant');
      const m = document.createElement('div');
      m.className = 'markdown';
      m.textContent = text;
      d.appendChild(m);
      document.body.appendChild(d);
    }"""
    drop_last = """() => {
      const all = document.querySelectorAll('[data-message-author-role="assistant"]');
      all[all.length - 1].remove();
    }"""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("about:blank")
        await page.evaluate(seed, PREV)
        probe = await page.evaluate("(expr) => document.querySelectorAll(expr).length", SEL)
        print(f"[2] 初始页面匹配节点数: {probe}")

        task = asyncio.create_task(run_case(page, "dom"))

        async def step(after, fn, *args):
            await asyncio.sleep(after)
            await fn(*args)

        await step(0.7, page.evaluate, add, PARTIAL)     # 新回答冒头
        await step(0.5, page.evaluate, drop_last)        # 重绘: 节点消失, 快照退回旧回答
        await step(1.8, page.evaluate, add, REAL)        # 真回答回来
        deltas, dur, err = await task
        await browser.close()

    print(f"[2] 真页面: {dur:.1f}s 回传 {len(deltas)} 次 -> {[d[:18] for d in deltas]}")
    flat = lambda s: "".join(s.split())                       # 真 markdown 转换会规整空白
    if PREV in deltas:
        bad.append("[2] 快照退回旧回答时被当成本轮答案")
    if not deltas or flat(deltas[-1]) != flat(REAL):
        bad.append(f"[2] 最终答案不是本轮的真回答: {(deltas[-1][:30] if deltas else '空')}")


async def case3_no_answer(bad):
    """站点没给新回答(页面上始终只有上一轮那条): 要报错, 不能拿旧的顶替。"""
    old = config.NO_DATA_ERROR_S
    config.NO_DATA_ERROR_S = 3
    try:
        page = FakePage([(0.0, {"count": 1, "text": PREV, "generating": False})])
        deltas, dur, err = await run_case(page, "dom")
    finally:
        config.NO_DATA_ERROR_S = old
    print(f"[3] 一直没新回答: {dur:.1f}s 回传 {len(deltas)} 次, 报错={type(err).__name__}")
    if deltas:
        bad.append("[3] 没有新回答却回传了内容: " + str([d[:20] for d in deltas]))
    if err is None:
        bad.append("[3] 没有新回答时应该如实报错(而不是安静地留着上一轮的答案)")


async def main() -> int:
    bad = []
    await case1_fake(bad)
    await case2_real_dom(bad)
    await case3_no_answer(bad)
    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("SNAPSHOT_STALE_OK (快照暂时退回上一轮回答时不冒充本轮答案; 真回答到了照样收全; 没回答就报错)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
