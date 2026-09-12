"""回归: 站点把语言名渲染成代码块正文第一行时, 它不能被写进文件。

真实事故: ChatGPT 页面上的代码块长这样(语言名在 <pre> 里, 不在 <code> 里):

    ```
    JSON
    { "name": "vue3-mall-pro" }
    ```

我们按"整段照抄"落盘, 于是**每个文件的第一行都多出 JSON / HTML / TypeScript / vue** ——
`package.json` 直接不是合法 JSON, 写进工作区的整个工程是废的。

盯三件事:
  1) `extract_code_files` 抽出的内容里没有那一行, package.json 仍是合法 JSON;
  2) 真的"第一行就是一个词"的普通文本文件不受影响(只认语言名);
  3) 真页面(无头 Chromium): `<pre>` 里塞了语言名头部、`<code>` 里才是正文时, 转出来的
     markdown 也不该带上那一行。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import capture, engineer  # noqa: E402

TREE = ["package.json", "src/main.ts", "index.html", "notes.txt", "src/App.vue"]

ANSWER = """### package.json

```
JSON
{
  "name": "vue3-mall-pro",
  "scripts": { "dev": "vite" }
}
```

### src/main.ts

```
TypeScript
import { createApp } from 'vue'
```
"""

PLAIN = """### notes.txt

```
这是一个普通文本文件
第二行
```
"""


def main() -> int:
    bad = []

    items, _ = engineer.extract_code_files(ANSWER, TREE)
    got = {it["path"]: it["content"] for it in items}
    print("抽出:", sorted(got))
    if "package.json" not in got:
        bad.append("package.json 没被认出来")
    else:
        first = got["package.json"].splitlines()[0]
        print("package.json 第一行:", repr(first))
        if first.strip().lower() == "json":
            bad.append("文件第一行还是语言名 JSON")
        try:
            json.loads(got["package.json"])
        except Exception as exc:  # noqa: BLE001
            bad.append(f"package.json 不是合法 JSON: {exc}")
    if "src/main.ts" in got and got["src/main.ts"].splitlines()[0].strip().lower() == "typescript":
        bad.append("src/main.ts 第一行还是语言名 TypeScript")

    plain, _ = engineer.extract_code_files(PLAIN, TREE)
    if not plain or plain[0]["content"].splitlines()[0] != "这是一个普通文本文件":
        bad.append("普通文本的第一行被误删了: " + json.dumps(plain, ensure_ascii=False)[:160])

    # 清单(fenced JSON)里带语言名时也要认出来
    man = '```\nJSON\n{"message": "x", "files": [{"op": "create", "path": "src/App.vue", "content": "<template/>"}]}\n```'
    obj, src = engineer.extract_manifest(man)
    print("带语言名的清单:", src)
    if not obj or not obj.get("files"):
        bad.append("带语言名的变更清单没认出来: " + str(src))

    async def real_dom():
        html = """<div data-message-id="m1" data-message-author-role="assistant">
          <div class="markdown">
            <pre><div class="code-head">JSON</div><code>{
  "name": "vue3-mall-pro"
}</code></pre>
          </div>
        </div>"""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto("about:blank")
            await page.evaluate("(h) => { document.body.innerHTML = h; }", html)
            out = await page.evaluate(capture._CONV_JS, {"selector": '[data-message-author-role]'})
            await browser.close()
        text = (out.get("messages") or [{}])[0].get("text", "")
        print("真页面转出来的正文:", repr(text))
        return text

    text = asyncio.run(real_dom())
    if "JSON" in text.splitlines()[1] if len(text.splitlines()) > 1 else False:
        bad.append("真页面里 <pre> 的语言名头部还是被当成代码了")
    if '"name"' not in text:
        bad.append("真页面没转出代码正文")

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("EXTRACT_LANG_LABEL_OK (语言名不会被写进文件; 普通文本不受影响; 真页面的 <pre>/<code> 也对)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
