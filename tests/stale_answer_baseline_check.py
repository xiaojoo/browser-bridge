"""回归: 发送后不能把"上一轮的回答"当成本轮答案(页面上出现两份同样的回答)。

真实故障(bridge 日志):
    18:29:05 turn[02bdf697] wait_turn_end done ... deltas=23        <- 上一轮的答案
    18:29:31 send_text: 2 chars | 确认
    18:29:33 turn[168ee8d0] first delta kind=text snap=True         <- 1.3 秒就"有回答"了
用户看到的现象: ChatGPT 还在思考中, 页面上却又出现了一遍上一条回答。

原因: baseline(上一轮回答的快照)是在 send **之后**才读的。发送会让站点重绘整段对话,
那一刻读回来可能是空(节点暂时全没了), 于是页面把上一轮那条回答渲染回来时, 文本"和
baseline 不一样", 就被当成本轮答案回传 —— 上一条回答在页面上变成两份。

本测试覆盖:
  1) 假页面重演: 发送瞬间读回空 -> 旧回答重新渲染 -> 真回答到来(不能回传旧回答);
  2) 旧回答节点一直挂着(带 data-message-id): 节点身份相同 -> 不能当本轮答案;
  3) 短回答(如"好的")逐字重复: 页面上真多出一条节点 -> 算本轮(不能一概否掉);
  4) 站点一直"生成中"却迟迟不吐内容: 不能拿 NO_DATA_ERROR_S 误报, 要接着等;
  5) 真页面(无头 Chromium, 真选择器 + 真 toMarkdown)重演: 新节点先空着, 再逐段出字;
  6) 静态检查: server.py 必须在 send 之前读 baseline 并传进 wait_turn_end;
  7) "生成中"指示卡住不动: 不能挂到整轮超时, 要收工并标记 truncated。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import capture, config, providers  # noqa: E402

PREV = ("对，前一版确实更像'功能骨架'：目录都在，但真正的业务逻辑还差一层。"
        "下面我按模块把缺口列一遍，你确认后我再给完整实现。")
REAL = ("好的，我按你确认的方案给出完整实现：\n```ts\n// src/store/cart.ts\n"
        "export const useCart = () => ({ items: [] });\n```\n其余文件同样按这个结构补齐。" + "尾巴" * 60)
SEL = providers.get("chatgpt").snapshot_selector


def norm(s):
    return "".join((s or "").split())


class FakePage:
    """按时间推进的假页面: 时间线 = [(相对秒, 快照), ...]。"""

    def __init__(self, timeline):
        self.timeline = timeline
        self.t0 = time.monotonic()

    async def evaluate(self, script, arg=None):
        if "__dsCap" in script:                     # enable/disable/drain
            return [] if "return" in script else None
        if "querySelectorAll(expr)" in script:      # _SNAPSHOT_JS
            elapsed = time.monotonic() - self.t0
            snap = self.timeline[0][1]
            for at, s in self.timeline:
                if elapsed >= at:
                    snap = s
            return dict(snap)
        return None


async def run_case(page, baseline, mode="dom", timeout=14.0):
    deltas = []

    async def on_delta(kind, text, snapshot):
        deltas.append(text)
        await asyncio.sleep(0)

    err = None
    truncated = None
    try:
        truncated, _errs = await asyncio.wait_for(capture.wait_turn_end(
            page, on_delta, mode=mode, snapshot_selector=SEL, baseline=baseline), timeout=timeout)
    except capture.NoDataError as exc:
        err = exc
    return deltas, err, truncated


async def case1_fake_blank_baseline(bad):
    """发送瞬间读回空(旧代码的 baseline 就是空的) -> 旧回答重新渲染时不能当本轮答案。"""
    baseline = {"count": 1, "text": PREV, "id": "m1", "generating": False}
    timeline = [
        (0.0, {"count": 1, "text": "", "id": "", "generating": True}),        # 站点重绘: 暂时读回空
        (0.5, {"count": 1, "text": PREV, "id": "m1", "generating": True}),    # 旧回答又渲染回来
        (1.6, {"count": 2, "text": REAL, "id": "m2", "generating": False}),   # 真回答
    ]
    deltas, err, _tr = await run_case(FakePage(timeline), baseline)
    print(f"[1] 重绘后旧回答回来: 回传 {len(deltas)} 次, 末段={norm(deltas[-1])[:16] if deltas else '空'}")
    if any(norm(d) == norm(PREV) for d in deltas):
        bad.append("[1] 把上一轮的回答当成本轮答案回传了(页面会出现两份同样的回答)")
    if not deltas or norm(deltas[-1]) != norm(REAL):
        bad.append("[1] 最终答案不是本轮的真回答")
    if err:
        bad.append(f"[1] 不该报错: {err}")


async def case2_same_node(bad):
    """旧回答节点一直挂着(同一 data-message-id): 一律不能当本轮答案。"""
    baseline = {"count": 1, "text": PREV, "id": "m1", "generating": False}
    timeline = [
        (0.0, {"count": 1, "text": PREV, "id": "m1", "generating": False}),
        (0.4, {"count": 1, "text": PREV + " ", "id": "m1", "generating": False}),   # 只差空白
        (0.8, {"count": 2, "text": REAL, "id": "m2", "generating": False}),
    ]
    deltas, err, _tr = await run_case(FakePage(timeline), baseline)
    print(f"[2] 同一节点: 回传 {len(deltas)} 次")
    if any(norm(d) == norm(PREV) for d in deltas):
        bad.append("[2] 同一个回答节点(同 id)被当成本轮答案")
    if not deltas or norm(deltas[-1]) != norm(REAL):
        bad.append("[2] 最终答案不是本轮的真回答")


async def case3_short_repeat(bad):
    """短回答(如"好的")逐字重复, 但页面上真多出一条回答节点 -> 算本轮。"""
    baseline = {"count": 1, "text": "好的", "id": "m1", "generating": False}
    timeline = [(0.0, {"count": 2, "text": "好的", "id": "m2", "generating": False})]
    deltas, err, _tr = await run_case(FakePage(timeline), baseline)
    print(f"[3] 短回答重复: 回传 {len(deltas)} 次 -> {deltas}")
    if not deltas or norm(deltas[-1]) != "好的":
        bad.append("[3] 真的新回答(短文本, 节点数增加)被误判成上一轮")


async def case4_long_thinking(bad):
    """站点一直"生成中"迟迟不吐内容: 不能按 NO_DATA_ERROR_S 误报, 要接着等。"""
    old = config.NO_DATA_ERROR_S
    config.NO_DATA_ERROR_S = 1.0
    try:
        baseline = {"count": 1, "text": PREV, "id": "m1", "generating": False}
        timeline = [
            (0.0, {"count": 2, "text": "", "id": "m2", "generating": True}),      # 思考中(空节点)
            (2.6, {"count": 2, "text": REAL, "id": "m2", "generating": False}),
        ]
        deltas, err, _tr = await run_case(FakePage(timeline), baseline, timeout=12)
    finally:
        config.NO_DATA_ERROR_S = old
    print(f"[4] 思考 2.6s (NO_DATA 窗口 1s): 回传 {len(deltas)} 次, 报错={type(err).__name__ if err else None}")
    if err:
        bad.append(f"[4] 站点还在生成中就按 NO_DATA 报错了: {err}")
    if not deltas or norm(deltas[-1]) != norm(REAL):
        bad.append("[4] 思考结束后没拿到真回答")


async def case5_real_dom(bad):
    """真页面: 上一轮节点挂着 -> 新节点空着(思考中) -> 逐段出字。"""
    seed = """(prev) => {
      document.body.innerHTML =
        '<div data-message-id="m1" data-message-author-role="assistant">' +
        '<div class="markdown"><p></p></div></div>';
      document.querySelector('[data-message-id="m1"] .markdown p').textContent = prev;
    }"""
    add_placeholder = """() => {
      const stop = document.createElement('button');
      stop.id = 'stopbtn';
      stop.setAttribute('aria-label', '停止流式传输');
      stop.textContent = '停止';
      document.body.appendChild(stop);
      const d = document.createElement('div');
      d.setAttribute('data-message-id', 'm2');
      d.setAttribute('data-message-author-role', 'assistant');
      const m = document.createElement('div');
      m.className = 'markdown';
      d.appendChild(m);
      document.body.appendChild(d);
    }"""
    fill = """(text) => {
      document.querySelector('[data-message-id="m2"] .markdown').innerHTML = '<p></p>';
      document.querySelector('[data-message-id="m2"] .markdown p').textContent = text;
      const b = document.getElementById('stopbtn');
      if (b) b.remove();
    }"""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("about:blank")
        await page.evaluate(seed, PREV)
        baseline = await capture.read_snapshot(page, SEL)
        print(f"[5] 发送前 baseline: {len(norm(baseline.get('text')))} 字 id={baseline.get('id')!r} "
              f"count={baseline.get('count')}")
        if norm(baseline.get("text")) != norm(PREV):
            bad.append("[5] baseline 没读对")

        task = asyncio.create_task(run_case(page, baseline, timeout=14))
        await asyncio.sleep(0.5)
        await page.evaluate(add_placeholder)         # 思考中: 新节点空着 + 停止按钮可见
        await asyncio.sleep(1.2)
        await page.evaluate(fill, REAL)              # 正文到达
        deltas, err, _tr = await task
        await browser.close()

    print(f"[5] 真页面: 回传 {len(deltas)} 次 -> {[norm(d)[:12] for d in deltas]}")
    if any(norm(d) == norm(PREV) for d in deltas):
        bad.append("[5] 真页面上把上一轮的回答当成了本轮答案")
    if not deltas or norm(deltas[-1]) != norm(REAL):
        bad.append("[5] 真页面的最终答案不对")
    if err:
        bad.append(f"[5] 不该报错: {err}")


def case6_server_order(bad):
    """静态检查: baseline 必须在 send_text 之前读, 而且要传进 wait_turn_end。"""
    src = (Path(__file__).resolve().parent.parent / "bridge" / "server.py").read_text(encoding="utf-8")
    i_read = src.find("baseline = await capture.read_snapshot")
    i_send = src.find("await manager.send_text(text)")
    i_pass = src.find("baseline=baseline")
    print(f"[6] 静态: 读 baseline@{i_read} < send@{i_send}, 传入@{i_pass}")
    if i_read < 0 or i_send < 0 or i_pass < 0:
        bad.append("[6] server.py 里缺 baseline 的读取/传递")
    elif not (i_read < i_send < i_pass):
        bad.append("[6] baseline 不是先读后传的顺序(必须 send 之前读, send 之后传入)")


async def case7_stuck_generating(bad):
    """已经有内容, 但"生成中"指示一直亮着不动: 不能挂到整轮超时, 要收工(并标记 truncated)。"""
    old = config.SNAPSHOT_STUCK_S
    config.SNAPSHOT_STUCK_S = 1.0
    try:
        baseline = {"count": 1, "text": PREV, "id": "m1", "generating": False}
        timeline = [(0.0, {"count": 2, "text": REAL, "id": "m2", "generating": True})]
        deltas, err, tr = await run_case(FakePage(timeline), baseline, timeout=8)
    finally:
        config.SNAPSHOT_STUCK_S = old
    print(f"[7] 生成指示卡住: 回传 {len(deltas)} 次, truncated={tr}, 报错={type(err).__name__ if err else None}")
    if err:
        bad.append(f"[7] 卡住时不该报错: {err}")
    if tr is not True:
        bad.append("[7] 生成指示卡住却没标记 truncated(会一直等到整轮超时)")
    if not deltas or norm(deltas[-1]) != norm(REAL):
        bad.append("[7] 卡住时最后的答案不对")


async def main() -> int:
    bad = []
    await case1_fake_blank_baseline(bad)
    await case2_same_node(bad)
    await case3_short_repeat(bad)
    await case4_long_thinking(bad)
    await case5_real_dom(bad)
    await case7_stuck_generating(bad)
    case6_server_order(bad)
    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("STALE_ANSWER_BASELINE_OK (发送前的 baseline 说话算数: 上一轮的回答任何时候都不会被当成本轮答案; "
          "真回答照样收全; 站点还在生成就不误报)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
