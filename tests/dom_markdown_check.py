"""DOM 快照 -> Markdown -> 渲染 的链路校验(本地 mock 站点 DOM, 不碰真实站点)。

现实问题: DOM 捕获模式(ChatGPT)原来用 innerText, 站点里的标题/加粗/列表/代码块结构全被压平,
界面上看到的是一堆没有排版的纯文本。这里验证新的转换器能保住结构, 并且前端渲染器能正确显示。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge.capture import _SNAPSHOT_JS  # noqa: E402

URL = "http://127.0.0.1:8765/"

# 模拟 ChatGPT/DeepSeek 助手消息的 DOM 结构
SITE_DOM = """
<div class="markdown prose">
  <h3>1. 软件开发 / 工程实现</h3>
  <ul>
    <li>设计<b>项目架构</b>与模块拆分</li>
    <li>写代码(<code>Python / C++ / Rust</code>)并加上<em>注释</em>
      <ul><li>嵌套项: 单元测试</li></ul>
    </li>
  </ul>
  <p>先看这段:</p>
  <pre><code>def fib(n):
    return n if n &lt; 2 else fib(n-1) + fib(n-2)</code></pre>
  <ol><li>复现问题</li><li>定位根因</li></ol>
  <table><tr><th>方案</th><th>代价</th></tr><tr><td>A</td><td>低</td></tr></table>
</div>
"""

CHECK_MD = r"""(text) => {
  const html = renderMarkdown(text);
  const box = document.createElement("div");
  box.innerHTML = html;
  return {
    html: html.slice(0, 400),
    headings: box.querySelectorAll("h3").length,
    strong: box.querySelectorAll("strong").length,
    em: box.querySelectorAll("em").length,
    bullets: Array.from(box.querySelectorAll("p")).filter(p => p.textContent.startsWith("• ")).length,
    ordered: Array.from(box.querySelectorAll("p")).filter(p => /^\d+\. /.test(p.textContent)).length,
    codeBlocks: box.querySelectorAll("pre.code").length,
    codeText: (box.querySelector("pre.code") || {}).textContent || "",
    tableRow: /方案 | 代价/.test(box.textContent),
    noRawMarkers: !/\*\*|-\s\[|\*[^*]+\*/.test(box.textContent),
  };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        site = await browser.new_page()
        await site.set_content("<body>" + SITE_DOM + "</body>")
        snap = await site.evaluate(_SNAPSHOT_JS, "div.markdown")
        md = snap["text"]
        print("转换出的 Markdown:\n" + md)
        for token, name in (("### ", "三级标题"), ("- ", "无序列表"), ("**项目架构**", "加粗"),
                            ("*注释*", "斜体"), ("```", "代码块"), ("1. 复现问题", "有序列表"),
                            ("方案 | 代价", "表格"), ("嵌套项", "嵌套列表")):
            if token not in md:
                bad.append(f"Markdown 里缺少{name}: {token!r}")
        if "设计项目架构" not in md.replace("**", ""):
            bad.append("列表文本丢失")

        app = await browser.new_page()
        await app.goto(URL, wait_until="networkidle", timeout=30_000)
        await app.wait_for_timeout(600)
        r = await app.evaluate(CHECK_MD, md)
        print("渲染结果:", json.dumps(r, ensure_ascii=False))
        if r["headings"] != 1:
            bad.append("标题未渲染为 <h3>")
        if r["strong"] < 1 or r["em"] < 1:
            bad.append("加粗/斜体未渲染")
        if r["bullets"] < 3:
            bad.append(f"无序列表未渲染成 • 行: {r['bullets']}")
        if r["ordered"] != 2:
            bad.append(f"有序列表序号丢失: {r['ordered']}")
        if r["codeBlocks"] != 1 or "def fib(n)" not in r["codeText"]:
            bad.append("代码块未渲染")
        if not r["tableRow"]:
            bad.append("表格内容丢失")
        if not r["noRawMarkers"]:
            bad.append("界面上仍能看到原始 Markdown 记号")
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("DOM_MARKDOWN_OK (标题/加粗/斜体/列表/嵌套/代码块/表格 都保住并正确渲染)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
