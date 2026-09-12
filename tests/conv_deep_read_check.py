"""回归: 整段对话必须读全(站点是虚拟列表 + 长回答能有十几万像素高)。

真实故障(用户: "这边获取的消息也不完整"):
同一会话连续读 4 次, 条数是 10/8/6/6 —— 而且都只拿到最后 2~3 轮。原因两条:
  1) `_CONV_JS` 只读 DOM 一次, 而站点只挂载视口附近的消息, 别的一律不在 DOM 里;
  2) 旧实现"滚到顶再读一次": 这条会话最后一条回答有 97710 字 ≈ 131000 像素高,
     每步只往上挪 0.8 屏(860px)永远走不到头; 就算滚到顶, 站点还会**异步**去取更早的历史。
代价: 那条 54937 字的回答(里面有 vite.config.ts / tsconfig*.json 的内容)整个读不到,
     卡片自然"少了文件"。

本测试用一个**模拟 ChatGPT 的虚拟列表**页面(只挂载视口 ±700px 的消息, 绝对定位保持滚动高度):
  1) 先确认这个假页面真的只挂载一小部分(否则测试没有意义);
  2) `read_conversation(deep=True)` 必须一次读全 8 条、顺序正确、id 都在;
  3) 读完把滚动位置放回底部(不把用户的窗口停在半空);
  4) 单次读(`deep=False`)确实会漏 —— 证明这个测试盯的就是那个坑。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import capture  # noqa: E402

SEL = "[data-message-author-role]"

# heights 里故意放一条 120000px 的"巨型回答"和一条 20000px 的, 复刻真实对话的形状
HEIGHTS = [300, 400, 20000, 120000, 500, 300, 200, 250]
TEXTS = [f"消息 {i} " + ("x" * (len(f"消息 {i}") + 4)) for i in range(len(HEIGHTS))]

FIXTURE = """(spec) => {
  const wrap = document.createElement('div');
  wrap.id = 'scroller';
  wrap.style.cssText = 'position:absolute;top:0;left:0;right:0;bottom:0;overflow-y:auto';
  const inner = document.createElement('div');
  inner.id = 'inner';
  inner.style.cssText = 'position:relative';
  wrap.appendChild(inner);
  document.body.appendChild(wrap);
  const heights = spec.heights, texts = spec.texts;
  const offs = []; let acc = 0;
  for (const h of heights) { offs.push(acc); acc += h; }
  inner.style.height = acc + 'px';
  window.__nodes = {};
  window.__mounts = [];
  const render = () => {
    const top = wrap.scrollTop, bottom = top + wrap.clientHeight, pad = 700;
    for (let i = 0; i < heights.length; i++) {
      const lo = offs[i], hi = lo + heights[i];
      const visible = hi >= top - pad && lo <= bottom + pad;
      const id = 'm' + i;
      let el = window.__nodes[id];
      if (visible && !el) {
        el = document.createElement('div');
        el.setAttribute('data-message-id', id);
        el.setAttribute('data-message-author-role', i % 2 === 0 ? 'user' : 'assistant');
        el.dataset.idx = String(i);
        el.style.cssText = 'position:absolute;left:0;right:0;top:' + lo + 'px;height:' + heights[i] + 'px';
        const md = document.createElement('div');
        md.className = 'markdown';
        md.textContent = texts[i];
        el.appendChild(md);
        inner.appendChild(el);
        window.__nodes[id] = el;
      } else if (!visible && el) {
        el.remove();
        delete window.__nodes[id];
      }
    }
    // DOM 顺序必须和文档顺序一致(站点也是这样的)
    Array.from(inner.children)
      .sort((a, b) => (+a.dataset.idx) - (+b.dataset.idx))
      .forEach(el => inner.appendChild(el));
    window.__mounts.push(Array.from(inner.children).map(e => e.getAttribute('data-message-id')));
  };
  wrap.addEventListener('scroll', render);
  window.__render = render;
  render();
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 900, "height": 800})
        await page.goto("about:blank")
        await page.evaluate(FIXTURE, {"heights": HEIGHTS, "texts": TEXTS})
        await page.wait_for_timeout(150)

        mounted = await page.evaluate(
            "() => Array.from(document.querySelectorAll('%s')).length" % SEL)
        mounts = await page.evaluate("() => window.__mounts")
        print(f"假页面: 共 {len(HEIGHTS)} 条, 视口里只挂载了 {mounted} 条")
        if mounted >= len(HEIGHTS):
            bad.append("假页面没有真的虚拟化, 这个测试没有意义")

        deep = await capture.read_conversation(page, SEL, deep=True)
        ids = [m.get("id") for m in deep.get("messages") or []]
        print("deep 读回:", deep.get("count"), "条 ->", ids)
        if deep.get("count") != len(HEIGHTS):
            bad.append(f"整段读回 {deep.get('count')} 条, 应为 {len(HEIGHTS)} 条")
        if ids != [f"m{i}" for i in range(len(HEIGHTS))]:
            bad.append("读回的顺序/id 不对: " + str(ids))
        if any(not (m.get("text") or "").strip() for m in deep.get("messages") or []):
            bad.append("读回的消息里有空正文")

        pos = await page.evaluate("""() => {
            const el = document.getElementById('scroller');
            return { top: Math.round(el.scrollTop), h: Math.round(el.scrollHeight),
                     ch: Math.round(el.clientHeight) };
        }""")
        print("读完的滚动位置:", pos)
        if pos["top"] + pos["ch"] < pos["h"] - 5:
            bad.append("读完没有把窗口滚回底部: " + str(pos))

        # 再做一次: 第二次读必须同样完整(不能因为缓存/位置变化而变少)
        again = await capture.read_conversation(page, SEL, deep=True)
        if [m.get("id") for m in again.get("messages") or []] != ids:
            bad.append("第二次读的结果和第一次不一样: " + str([m.get("id") for m in again.get("messages") or []]))

        one = await capture.read_conversation(page, SEL, deep=False)
        print("单次读(deep=False)只有:", one.get("count"), "条 <- 这就是以前的坑")
        if one.get("count") >= len(HEIGHTS):
            bad.append("单次读不该读到全部 —— 说明假页面没虚拟化")
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("CONV_DEEP_READ_OK (虚拟列表里的消息一次读全: 顺序/id 都对, 读完回到底部; 单次读确实会漏)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
