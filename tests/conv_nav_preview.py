"""右侧「输入导航」样式预览: 短会话/长会话 x 收起/展开, 各一张截图(裁到导航那一块)。

用法: 先起服务(python main.py), 再 python tests/conv_nav_preview.py
输出: .tmp/convnav_short_closed.png / _short_open.png / _long_closed.png / _long_open.png / _page.png
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

URL = "http://127.0.0.1:8765/"
OUT = Path(r"H:\browser-bridge\.tmp")

STUB = r"""(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/providers") === 0)
      return json({ default: "chatgpt", providers: [{ id: "chatgpt", name: "ChatGPT Web", short: "GPT",
        capture_mode: "dom", conversation_pattern: "/c/" }] });
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/api/chat") === 0) return json({ ok: true });
    if (s.indexOf("/workspace/tree") === 0) return json({ ok: true, root: "H:\\tmp", name: "ws", items: [] });
    if (s.indexOf("/api/workspace") === 0) return json({ ok: true, root: "H:\\tmp", name: "ws", custom: true });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""

# n 条输入 + 长短不一的回答; 让 active 落在中间那条(和参考图一样, 中间那格是蓝色)
SEED = r"""(n) => {
  showWelcome(false);
  transcript = []; convEl.innerHTML = "";
  const qs = ["Windows PowerShell.txt", "中文", "import torch.txt",
              "帮我看看这个 PowerShell 脚本为什么删不掉文件", "再帮我看看 requirements 里要不要锁版本"];
  for (let i = 1; i <= n; i++) {
    const q = qs[(i - 1) % qs.length] + (i > qs.length ? " (" + i + ")" : "");
    transcript.push({ role: "user", text: q });
    addUserMsg(q, transcript.length - 1);
    transcript.push({ role: "assistant", text: "### 回答 " + i + "\n\n" + "内容".repeat(60) });
    renderStoredMessage(transcript[transcript.length - 1], transcript.length - 1);
  }
  convWrap.scrollTop = 0;
  syncScrollbar(); layoutConvNav();
  const rows = convEl.querySelectorAll(".user-row[data-mi]");
  convWrap.scrollTop = Math.max(0, rows[Math.floor(n / 2)].offsetTop - 40);   // active = 中间那条
  syncScrollbar(); layoutConvNav();
  return transcript.length;
}"""


async def shot(page, name, pad=(30, 30), full=False):
    await page.screenshot(path=str(OUT / name))
    print("saved:", OUT / name)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(800)

        for n, tag in ((3, "short"), (30, "long"), (60, "dense")):
            await page.evaluate(SEED, n)
            await page.wait_for_timeout(500)
            await page.mouse.move(700, 500)
            await page.wait_for_timeout(450)
            box = await page.evaluate("""() => { const r = document.getElementById("convNav").getBoundingClientRect();
              return { x: Math.round(r.x) - 70, y: Math.round(r.y) - 16,
                       width: Math.round(r.width) + 110, height: Math.round(r.height) + 32 }; }""")
            await page.screenshot(path=str(OUT / f"convnav_{tag}_closed.png"), clip=box)
            print("saved:", OUT / f"convnav_{tag}_closed.png")
            await page.hover("#convNavCard")
            await page.wait_for_timeout(700)
            card = await page.evaluate("""() => { const r = document.getElementById("convNavCard").getBoundingClientRect();
              return { x: Math.round(r.x) - 26, y: Math.round(r.y) - 26, width: Math.round(r.width) + 70,
                       height: Math.min(Math.round(r.height), 300) + 52 }; }""")
            await page.screenshot(path=str(OUT / f"convnav_{tag}_open.png"), clip=card)
            print("saved:", OUT / f"convnav_{tag}_open.png")
            await page.mouse.move(700, 500)
            await page.wait_for_timeout(400)
        await page.screenshot(path=str(OUT / "convnav_page.png"))
        print("saved:", OUT / "convnav_page.png")
        await b.close()


asyncio.run(main())
