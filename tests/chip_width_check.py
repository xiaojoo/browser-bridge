"""附件条目(.chiprow)宽度: 默认自适应, 超过最大宽度才截断。

顺带验证 #chips 是 flex 容器(多个条目会并排换行, 而不是各占一整行)。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"

SEED = r"""() => {
  showWelcome(false);
  staged = [
    { file: new File(["x"], "a.txt") },
    { file: new File(["x"], "b.py") },
    { file: new File(["x"], "一个非常非常长的文件名_用来测试是否会被截断_0123456789abcdefghijklmnopqrstuvwxyz.md") },
  ];
  renderChips();
  const rows = Array.from(document.querySelectorAll("#chips .chiprow"));
  return {
    chipsDisplay: getComputedStyle(document.getElementById("chips")).display,
    chipsWrap: getComputedStyle(document.getElementById("chips")).flexWrap,
    items: rows.map(r => {
      const nm = r.querySelector(".nm");
      const rect = r.getBoundingClientRect();
      return { w: Math.round(rect.width), top: Math.round(rect.top),
               name: nm.textContent.slice(0, 12), truncated: nm.scrollWidth > nm.clientWidth + 1 };
    }),
    style: (() => {
      const cs = getComputedStyle(rows[0]);
      return { maxWidth: cs.maxWidth, flex: cs.flexGrow + "/" + cs.flexShrink };
    })(),
  };
}"""


WRAP = r"""(long) => {
  showWelcome(false);
  const el = document.getElementById("input");
  el.value = long;                       // textarea
  autoGrowInput();
  const cs = getComputedStyle(el);
  const inp = { scrollW: el.scrollWidth, clientW: el.clientWidth,
                bg: cs.backgroundColor, color: cs.color, whiteSpace: cs.whiteSpace,
                overflowX: cs.overflowX };
  // 任务卡片里的请求/回复原文框
  handle({ type: "engineer", stage: "plan", steps: [{ text: "步骤一", files: [] }] });
  handle({ type: "engineer", stage: "step", index: 1, total: 1, files: [], chars: 10,
           prompt: long, text: "x" });
  const pre = document.querySelector("#conv .task-item pre");
  const pcs = getComputedStyle(pre);
  const card = { scrollW: pre.scrollWidth, clientW: pre.clientWidth,
                 bg: pcs.backgroundColor, whiteSpace: pcs.whiteSpace, overflowX: pcs.overflowX };
  // 旧的黑底日志框不该再存在
  const dark = Array.from(document.querySelectorAll("body *")).filter(e => {
    const c = getComputedStyle(e).backgroundColor;
    return c === "rgb(16, 20, 25)";
  }).length;
  return { inp: inp, card: card, darkBoxes: dark, legacyLog: !!document.getElementById("engineerLog") };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1180, "height": 760})
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)
        w = await page.evaluate(WRAP, "超长一段没有空格的文字" * 12 + "https://github.com/x/y" * 6)
        print("换行/配色:", json.dumps(w, ensure_ascii=False))
        if w["inp"]["scrollW"] > w["inp"]["clientW"] + 1:
            bad.append("输入框出现横向滚动: " + json.dumps(w["inp"], ensure_ascii=False))
        if w["inp"]["overflowX"] != "hidden":
            bad.append("输入框没有关掉横向滚动: " + w["inp"]["overflowX"])
        if w["inp"]["bg"] != "rgba(0, 0, 0, 0)":
            bad.append("输入框底色不是默认(可能有高亮): " + w["inp"]["bg"])
        if w["inp"]["whiteSpace"] != "pre-wrap":
            bad.append("输入框不是换行模式: " + w["inp"]["whiteSpace"])
        meta = await page.evaluate("""() => {
          const el = document.getElementById("input");
          return { tag: el.tagName, minH: getComputedStyle(el).minHeight, maxH: getComputedStyle(el).maxHeight,
                   value: el.value.length, placeholder: el.getAttribute("placeholder") || "",
                   h: Math.round(el.getBoundingClientRect().height), scrollH: el.scrollHeight };
        }""")
        print("输入框:", json.dumps(meta, ensure_ascii=False))
        if meta["tag"] != "TEXTAREA":
            bad.append("输入框不是原生 textarea: " + meta["tag"])
        if meta["minH"] != "60px" or meta["maxH"] != "180px":
            bad.append("输入框高度范围不对: " + json.dumps(meta, ensure_ascii=False))
        if meta["h"] > 181:
            bad.append("输入框长度没有限制住(超过 180 就会自己滚动): " + str(meta["h"]))
        if w["card"]["scrollW"] > w["card"]["clientW"] + 1:
            bad.append("任务卡片原文框出现横向滚动: " + json.dumps(w["card"], ensure_ascii=False))
        if w["darkBoxes"] or w["legacyLog"]:
            bad.append("仍有深色高亮框/旧日志框: " + json.dumps({k: w[k] for k in ("darkBoxes", "legacyLog")}))
        r = await page.evaluate(SEED)
        print(json.dumps(r, ensure_ascii=False))
        if r["chipsDisplay"] != "flex":
            bad.append("#chips 不是 flex 容器(条目会各占一整行): " + r["chipsDisplay"])
        short = r["items"][0]
        long_one = r["items"][2]
        if short["w"] >= 200:
            bad.append(f"短文件名没有自适应(宽度 {short['w']}px)")
        if short["truncated"]:
            bad.append("短文件名不该被截断")
        if long_one["w"] > 281:
            bad.append(f"长文件名没有受最大宽度限制: {long_one['w']}px")
        if not long_one["truncated"]:
            bad.append("超长文件名应该被省略号截断")
        if abs(r["items"][0]["top"] - r["items"][1]["top"]) > 2:
            bad.append("多个附件没有并排显示(换行了): " + json.dumps(r["items"], ensure_ascii=False))
        if "280px" not in r["style"]["maxWidth"]:
            bad.append("最大宽度不是 280px: " + json.dumps(r["style"], ensure_ascii=False))
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("CHIP_WIDTH_OK (附件条目默认自适应, 超过最大宽度截断, 多个并排换行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
