"""输入框: 支持把文件拖进来 + 从剪贴板粘贴文件(截图/复制的文件)。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"

STUB = r"""(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0)
      return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/workspace/tree") === 0)
      return json({ ok: true, root: "H:\\tmp", name: "tmp", default: "", custom: false, items: [] });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

DROP = r"""() => {
  const dt = new DataTransfer();
  dt.items.add(new File(["hello-drop"], "拖进来的.txt", { type: "text/plain" }));
  dt.items.add(new File([new Uint8Array([1,2,3])], "img.png", { type: "image/png" }));
  const box = document.querySelector(".composer");
  box.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: dt }));
  const hint = box.classList.contains("drop-on");
  box.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt }));
  return { dropOnDuringDrag: hint };
}"""

PASTE = r"""() => {
  const dt = new DataTransfer();
  dt.items.add(new File(["pasted"], "粘贴.png", { type: "image/png" }));
  const ev = new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dt });
  const el = document.getElementById("input");
  el.focus();
  el.dispatchEvent(ev);
  return { prevented: ev.defaultPrevented };
}"""

TEXT_PASTE = r"""() => {
  const dt = new DataTransfer();
  dt.setData("text/plain", "纯文本粘贴内容");
  const el = document.getElementById("input");
  el.focus();
  const ev = new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dt });
  el.dispatchEvent(ev);      // 交给浏览器原生处理(合成事件不会真的插入, 这里只验证没被拦)
  return { prevented: ev.defaultPrevented };
}"""


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 820})
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)

        # 1) 拖入两个文件
        r1 = await page.evaluate(DROP)
        await page.wait_for_timeout(400)
        st = await page.evaluate(r"""() => ({
          chips: Array.from(document.querySelectorAll("#chips .chiprow .nm")).map(e => e.textContent),
          dropClass: document.querySelector(".composer").classList.contains("drop-on"),
          imgs: document.querySelectorAll("#chips .chiprow img").length,
        })""")
        print("拖入后:", json.dumps(st, ensure_ascii=False), "拖拽中高亮:", r1["dropOnDuringDrag"])
        if st["chips"] != ["拖进来的.txt", "img.png"]:
            bad.append("拖入的文件没有进入输入框: " + json.dumps(st, ensure_ascii=False))
        if not r1["dropOnDuringDrag"]:
            bad.append("拖拽经过输入框时没有高亮提示")
        if st["dropClass"]:
            bad.append("拖放结束后高亮没有取消")
        if st["imgs"] != 1:
            bad.append("图片没有缩略图: " + json.dumps(st, ensure_ascii=False))

        # 2) 粘贴文件(截图)
        r2 = await page.evaluate(PASTE)
        await page.wait_for_timeout(400)
        chips2 = await page.evaluate("() => Array.from(document.querySelectorAll('#chips .chiprow .nm')).map(e => e.textContent)")
        print("粘贴文件后:", json.dumps(chips2, ensure_ascii=False), "拦截默认:", r2["prevented"])
        if "粘贴.png" not in chips2:
            bad.append("从剪贴板粘贴的文件没有进入输入框: " + json.dumps(chips2, ensure_ascii=False))
        if not r2["prevented"]:
            bad.append("粘贴文件时没有拦住默认行为(可能同时插入了乱码文本)")

        # 3) 纯文本粘贴不该被拦
        r3 = await page.evaluate(TEXT_PASTE)
        if r3["prevented"]:
            bad.append("纯文本粘贴被拦截了(应该保持原生粘贴)")

        # 4) 清空后重新验证: 点 × 能移除
        await page.evaluate("() => document.querySelector('#chips .chiprow .rm').click()")
        await page.wait_for_timeout(200)
        chips3 = await page.evaluate("() => document.querySelectorAll('#chips .chiprow').length")
        if chips3 != 2:
            bad.append("点 × 没有移除条目: " + str(chips3))

        # 5) 【回归】拖到**输入框(textarea)**上: dragover/drop 会冒泡到 .composer,
        #    以前 .composer 和 #input 各挂了一次监听 -> 同一个文件被加两次
        #    (用户实际遇到的: "拖动文件到输入框, 出现两个同样的文件")。
        #    注意上面第 1 步是往 .composer 上派发的, 所以以前这条路径没被覆盖到。
        before = await page.evaluate("() => document.querySelectorAll('#chips .chiprow').length")
        hl = await page.evaluate(r"""() => {
          const dt = new DataTransfer();
          dt.items.add(new File(["once"], "再拖一次.txt", { type: "text/plain" }));
          const el = document.getElementById("input");
          el.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: dt }));
          const on = document.querySelector(".composer").classList.contains("drop-on");
          el.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt }));
          return { highlight: on };
        }""")
        await page.wait_for_timeout(400)
        names5 = await page.evaluate("() => Array.from(document.querySelectorAll('#chips .chiprow .nm')).map(e => e.textContent)")
        n_new = names5.count("再拖一次.txt")
        print(f"拖到输入框上: {before} -> {len(names5)} 个, 新文件出现 {n_new} 次, 拖拽中高亮 {hl['highlight']}")
        print("  chips:", json.dumps(names5, ensure_ascii=False))
        if n_new != 1 or len(names5) != before + 1:
            bad.append(f"拖到输入框上被加了 {n_new} 次(应 1 次): " + json.dumps(names5, ensure_ascii=False))
        if not hl["highlight"]:
            bad.append("在输入框上拖着的时候没有高亮提示")
        await browser.close()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("DROP_PASTE_OK (文件可拖入输入框, 剪贴板文件可粘贴, 纯文本粘贴不受影响)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
