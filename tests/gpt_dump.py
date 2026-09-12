"""只读诊断: 列出 chatgpt 会话里的 assistant 消息节点, 并检查我们的快照选择器会命中哪个。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from bridge import browser, config, providers  # noqa: E402

p = providers.get("chatgpt")
profile = config.PROFILE_DIR / "chatgpt"

DUMP = """(snapshotSel) => {
  const msgs = Array.from(document.querySelectorAll('[data-message-author-role]'));
  const list = msgs.map((m, i) => ({
    i: i,
    role: m.getAttribute('data-message-author-role'),
    firstClass: String(m.className||'').split(' ')[0],
    hasMarkdown: !!m.querySelector('.markdown'),
    textLen: (m.innerText||'').length,
    head: (m.innerText||'').slice(0, 24)
  }));
  // 我们的快照选择器: 全部命中, 以及 last 命中项
  const nodes = Array.from(document.querySelectorAll(snapshotSel));
  const last = nodes.length ? nodes[nodes.length-1] : null;
  return {
    msgCount: msgs.length,
    list,
    nodeCount: nodes.length,
    selLast: last ? {
      tag: last.tagName,
      cls: String(last.className||'').slice(0,50),
      role: last.getAttribute && (last.getAttribute('data-message-author-role')||'') ,
      len: (last.innerText||'').length,
      head: (last.innerText||'').slice(0,24)
    } : null,
    snapshotSel: snapshotSel
  };
}"""


async def main() -> int:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=True,
            viewport={"width": 1280, "height": 860},
            user_agent=browser._UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            args=["--disable-blink-features=AutomationControlled", "--lang=zh-CN"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(p.url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(6000)
        info = await page.evaluate(DUMP, p.snapshot_selector)
        print(json.dumps(info, ensure_ascii=False, indent=1))
        await ctx.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
