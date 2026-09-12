"""站点"不能输入了"的判定: 只看提示类元素 + 输入框是否真的禁用。

真实事故(日志里): ChatGPT 的**回答正文**里出现了"使用额度"几个字, 而当时的探测扫的是整页
innerText -> 误判成"站点不让输入了" -> 白白另开一个新窗口(消息记到新会话名下, 看起来就是
"消息丢了")。这里用合成页面把这条钉死。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge.browser import _BLOCK_JS  # noqa: E402

CFG = {"sels": ["#prompt-textarea", 'div[contenteditable="true"]']}

# 一段"回答正文": 里面故意塞满会被旧实现误判的字眼
ANSWER = """
<div class="markdown" style="padding:20px">
  <p>这个报错已经很明确了：不是 vLLM 启动问题。如使用额度 20:42，当前的文件视图用于数据分析和设置。</p>
  <p>如果还是不行，请稍后重试；次数上限相关参数建议改小。</p>
  <p>You've reached the limit of files you can upload to this conversation.</p>
</div>
"""

CASES = [
    ("回答正文里的字眼不算", f"<body>{ANSWER}<div id='prompt-textarea' contenteditable='true' "
                            f"style='width:400px;height:40px'>x</div></body>", ""),
    ("输入框被禁用要认出来", "<body><div id='prompt-textarea' contenteditable='false' "
                             "style='width:400px;height:40px'>x</div></body>", "输入框已被禁用"),
    ("真·额度提示(alert)", "<body><div role='alert' style='padding:12px'>已达到使用额度，将在 20:42 重置</div>"
                            "<div id='prompt-textarea' contenteditable='true' "
                            "style='width:400px;height:40px'>x</div></body>", "使用额度"),
    ("无关的 alert 不算", "<body><div class='alert' style='padding:12px'>网络连接已断开</div>"
                          "<div id='prompt-textarea' contenteditable='true' "
                          "style='width:400px;height:40px'>x</div></body>", ""),
    ("隐藏的提示不算", "<body><div role='alert' style='display:none'>已达到使用额度</div>"
                       "<div id='prompt-textarea' contenteditable='true' "
                       "style='width:400px;height:40px'>x</div></body>", ""),
]


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page()
        for name, html, want in CASES:
            await page.set_content("<html>" + html + "</html>")
            got = await page.evaluate(_BLOCK_JS, CFG)
            ok = (want in got) if want else (got == "")
            print(f"  [{name}] -> {got!r}  {'OK' if ok else 'FAIL'}")
            if not ok:
                bad.append(f"{name}: 期望 {want!r}, 实际 {got!r}")
        await b.close()
    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("BLOCK_DETECT_OK (回答正文里的'使用额度/稍后重试'不再被误判成站点限制; "
          "输入框被禁用/真的弹了额度提示仍然认得出来)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
