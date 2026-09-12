"""生成 UI 预览截图(含模拟对话), 用于人工比对样式。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"
OUT = Path(r"H:\browser-bridge\.tmp\ui_preview.png")

SAMPLE = r"""**调试建议**

1. 添加日志来跟踪数据流
2. 检查 `InMemoryExpertStore` 的实现

```python
def transfer_nvme_to_ram(task):
    record = nvme.get(task)
    if record:
        ram.put(record)
    return record
```

> 建议先用日志确认断点在哪一环。"""


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900},
                                      device_scale_factor=1.5)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)
        # 模拟: 用户消息 + 流式完成的助手消息 + 思考过程
        await page.evaluate("""(raw) => {
          const addUser = (t) => { const r = document.createElement('div');
            r.className='user-row'; r.innerHTML='<div class="user-bubble"></div>';
            r.firstChild.textContent = t; conv.appendChild(r); };
          addUser('NVMe → RAM 的 transfer 没有真正执行，帮我看看怎么调试。');
          const current = { row: null, body: null, raw: '', reason: '先给出断点排查顺序，再贴最小复现。' };
          const row = document.createElement('div'); row.className='message';
          const body = document.createElement('div'); body.className='assistant-body';
          row.appendChild(body); conv.appendChild(row);
          current.row = row; current.body = body;
          current.raw = raw;
          const reasonHtml = '<details class="reasoning-details"><summary>思考过程</summary>' +
            '<div class="rtext">' + current.reason + '</div></details>';
          body.innerHTML = reasonHtml + renderMarkdown(current.raw);
          conv.scrollTop = conv.scrollHeight;
        }""", SAMPLE)
        await page.wait_for_timeout(600)
        await page.screenshot(path=str(OUT), full_page=False)
        await browser.close()
    print("saved:", OUT)


asyncio.run(main())
