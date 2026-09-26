"""字体阶梯自检: 把整个界面上每一个"真的挂着自己的文字"的节点扫一遍, 判它的
(字号, 行高) 是不是落在 ChatGPT 那几档上。

为什么写成"扫一遍"而不是逐条 assert 十个选择器: 逐条列的那种门禁只会检查我列出来的
那几处, 而字体这件事的失败方式恰恰是"某处没人想到"。ChatGPT 全站只有四档
(12/16 最小注释 · 14/20 外壳 · 16/26 正文 · 12.25/20 代码, 标题 18/28·20/28·24/32 都是 600,
品牌行 18/26 字距 -0.27px), 这组数是 2026-09-26 用 .tmp/gpt_type_census.py 在它真实页面上
扫出来的, 见 memory/project-chatgpt-style-values.md。我们原来有 15 档。

例外只有三个"圆徽里的字母/图钉": 它们不是文字角色, 12px 会在 18/22px 的圆里挤出去。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from media_render_check import STUB  # noqa: E402
from handoff_ui_check import preview_payload  # noqa: E402  复用那份长笔记的替身

URL = "http://127.0.0.1:8765/"

# (字号, 行高) —— 全部来自 ChatGPT 页面量到的值:
#   12/16 最小注释 · 14/20 外壳 · 16/26 正文 · 16/24 引用块 · 14/26 段内代码(w500)
#   12.25/20 代码块 · 18/26 品牌行 · 18/28·20/28·24/32 标题(w600)
LADDER = {("12px", "16px"), ("14px", "20px"), ("16px", "26px"), ("12.25px", "20px"),
          ("18px", "28px"), ("20px", "28px"), ("24px", "32px"), ("18px", "26px"),
          ("14px", "26px"), ("16px", "24px")}
# 圆徽里的首字母 / 图钉: ChatGPT 没有对应角色(它的头像是图片), 尺寸受圆的直径限制,
# 12px 会在 18px / 22px 的圆里挤出去。按探针给的路径片段认。
EXCEPT = {"rbAv", "pi-av", "pinmark", "pAvatar"}

MD = """# 一级
## 二级
### 三级

正文里有 `行内代码` 和 **加粗**。

> 引用一行

- 列表项

| 甲 | 乙 |
| --- | --- |
| 1 | 2 |

```python
def f():
    return 1
```
"""

CENSUS = """() => {
  const path = (e) => {
    const p = [];
    for (let x = e; x && x.tagName && p.length < 3; x = x.parentElement) {
      let s = x.tagName.toLowerCase();
      if (x.id) s += '#' + x.id;
      else if (x.className && typeof x.className === 'string') {
        const c = x.className.trim().split(/\\s+/)[0]; if (c) s += '.' + c;
      }
      p.unshift(s);
    }
    return p.join('>');
  };
  const seen = new Map();
  for (const n of document.querySelectorAll('*')) {
    const own = [...n.childNodes].filter(x => x.nodeType === 3 && x.textContent.trim());
    if (!own.length) continue;
    const c = getComputedStyle(n);
    const k = [c.fontSize, c.lineHeight, c.fontWeight, c.letterSpacing].join('|');
    let r = seen.get(k);
    if (!r) { r = {size: c.fontSize, lh: c.lineHeight, weight: c.fontWeight,
                   ls: c.letterSpacing, n: 0, where: [], samples: []}; seen.set(k, r); }
    r.n++;
    if (r.where.length < 6) { r.where.push(path(n)); r.samples.push(n.textContent.trim().slice(0, 14)); }
  }
  return [...seen.values()].sort((a, b) => b.n - a.n);
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page(viewport={"width": 1400, "height": 900})
        await page.add_init_script(STUB)
        await page.route("**/api/handoff/preview",
                         lambda r: r.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(preview_payload("x.jsonl"))))
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(900)
        # 把能出现的文字都摆出来: 侧栏 + 一段完整 markdown 回答 + 用户气泡 + 错误行 + 弹框
        await page.evaluate("""(md) => {
          showWelcome(false);
          addUserMsg("帮我把显卡状态检查做成每周跑一次的脚本", 0);
          handle({ type: "message_start" });
          handle({ type: "delta", kind: "text", text: md });
          handle({ type: "message_end" });
          handle({ type: "error", text: "示例错误提示" });
        }""", MD)
        await page.wait_for_timeout(500)
        await page.click("#btnHandoff", force=True)     # 弹框里的字也要扫(dialog 是 display:none 时
        await page.click("#hoRun", force=True)          #   computed style 仍然读得到, 不用真的打开)
        await page.wait_for_timeout(1200)
        rows = await page.evaluate(CENSUS)
        print("界面上共 %d 种字体组合:" % len(rows))
        for r in rows:
            off = (r["size"], r["lh"]) not in LADDER
            where = " ".join(r["where"])
            exc = any(e in where for e in EXCEPT)
            print("  %-9s %-7s w%-4s ls%-7s x%-4d %s %s"
                  % (r["size"], r["lh"], r["weight"], r["ls"], r["n"],
                     "OFF" if (off and not exc) else ("例外" if off else "  ok"),
                     "|".join(r["where"])[:78]))
            if off and not exc:
                bad.append("%s/%s 不在 ChatGPT 的阶梯上: %s"
                           % (r["size"], r["lh"], " ".join(r["where"][:3])))
        heavy = [r for r in rows if r["weight"] not in ("400", "500", "600")
                 and not any(e in " ".join(r["where"]) for e in EXCEPT)]
        for r in heavy:
            print("  字重超档 w%s: %s" % (r["weight"], " ".join(r["where"][:3])))
        if heavy:
            bad.append("有 %d 处字重超过 ChatGPT 用的 600: %s"
                       % (len(heavy), " ".join(heavy[0]["where"])))
        grey = await page.evaluate("""() => {
          const hit = (e) => { const c = getComputedStyle(e);
            return [...e.childNodes].some(x => x.nodeType === 3 && x.textContent.trim())
              && /rgb\\(138, 143, 152\\)/.test(c.color); };
          return [...document.querySelectorAll('*')].filter(hit).slice(0, 3)
                 .map(e => e.tagName + '.' + (e.className || '') + '|' + e.textContent.trim().slice(0, 10));
        }""")
        if grey:
            bad.append("还有旧灰字 #8A8F98(ChatGPT 的次级灰是 #8F8F8F): %s" % grey)
        await b.close()

    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("TYPE_SCALE_OK (全站字号只落在 ChatGPT 那几档: 12/16 · 14/20 · 16/26 · 12.25/20 · "
          "标题 18/28·20/28·24/32 · 品牌 18/26; 字重不超过 600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
