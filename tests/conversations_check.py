"""站点会话历史读取/切换校验(本地 mock 站点, 不碰真实站点)。

覆盖:
- 有 a[href] 的站点(ChatGPT 型): 按 conversation_pattern 提取 id/key/url, 识别当前会话
- 没有链接的站点(DeepSeek 型): 兜底从历史容器里取可点条目, 打标记后可以点它切换
- open_conversation: 有 url 走跳转, 没有 url 走点击
"""
import asyncio
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import providers  # noqa: E402
from bridge.browser import BrowserManager  # noqa: E402

LINKED = """<!doctype html><meta charset="utf-8"><title>linked</title>
<style>#listbox a{display:block;padding:4px 0}</style>
<div id="listbox" style="max-height:60px;overflow:auto;display:block">
  <nav aria-label="Chat history">
    <a href="/c/aaa111">整理一下这周的实验记录</a>
    <a href="/c/bbb222" aria-current="page">DMA 调试思路</a>
    <a href="/c/ccc333">写个 fib 缓存</a>
    <a href="/c/ddd444">更早的会话</a>
    <a href="/c/eee555">再早一点</a>
  </nav>
</div>
<div id="prompt-textarea" contenteditable="true"></div>
<script>
  // 滚到底再"懒加载"两条
  const box = document.getElementById("listbox");
  let loaded = 0;
  box.addEventListener("scroll", () => {
    if (box.scrollTop + box.clientHeight < box.scrollHeight - 2) return;
    if (loaded >= 2) return;
    const nav = box.querySelector("nav");
    for (let i = 1; i <= 2; i++) {
      const a = document.createElement("a");
      a.href = "/c/lazy" + (++loaded);
      a.textContent = "懒加载会话 " + loaded;
      nav.appendChild(a);
    }
  });
</script>"""

UNLINKED = """<!doctype html><meta charset="utf-8"><title>unlinked</title>
<aside aria-label="历史会话">
  <div class="conv-item">本周实验记录</div>
  <div class="conv-item active">DMA 调试</div>
  <div class="conv-item">fib 缓存</div>
</aside>
<div id="prompt-textarea" contenteditable="true"></div>
<script>
  document.querySelectorAll(".conv-item").forEach(el => {
    el.addEventListener("click", () => {
      document.querySelectorAll(".conv-item").forEach(x => x.classList.remove("active"));
      el.classList.add("active");
      window.__opened = el.textContent.trim();
    });
  });
</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = (UNLINKED if "unlinked" in self.path else LINKED).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def mock_provider(url, pattern):
    return providers.Provider(
        id="mock", name="Mock Web", url=url, short="MK",
        composer_selectors=("#prompt-textarea",),
        capture_mode="dom", conversation_pattern=pattern,
    )


async def main() -> int:
    srv, root = start_server()
    bad = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            mgr = BrowserManager()
            mgr.page = page
            mgr._ctx = await browser.new_context()
            mgr.state = "logged_in"

            # 1) 有链接的站点
            mgr.provider = mock_provider(root + "/", "/c/")
            await page.goto(root + "/c/bbb222", wait_until="domcontentloaded")
            items = await mgr.conversations()
            print("1) 链接型:", items)
            ids = [i["id"] for i in items]
            if ids != ["aaa111", "bbb222", "ccc333", "ddd444", "eee555"]:
                bad.append("会话列表解析不对: " + str(ids))
            if any(not i["url"] for i in items):
                bad.append("链接型会话应带 url")
            active = [i for i in items if i["active"]]
            if len(active) != 1 or active[0]["id"] != "bbb222":
                bad.append("当前会话(aria-current)识别失败: " + str(active))
            if mgr.conversation_id() != "bbb222":
                bad.append("conversation_id 与列表 id 不一致: " + str(mgr.conversation_id()))
            ok = await mgr.open_conversation("aaa111", root + "/c/aaa111")
            print("   打开第一条:", ok, page.url)
            if not ok or not page.url.endswith("/c/aaa111"):
                bad.append("按 url 切换会话失败: " + page.url)
            if mgr.conversation_id() != "aaa111":
                bad.append("切换后 conversation_id 未更新: " + str(mgr.conversation_id()))

            # 1b) 加载更多: 先把站点列表滚到底, 再读一次
            more = await mgr.conversations(more=True)
            print("1b) 加载更多:", [i["title"] for i in more])
            if len(more) <= len(items):
                bad.append(f"滚到底后没有读出更多会话: {len(items)} -> {len(more)}")
            if not any("懒加载会话" in i["title"] for i in more):
                bad.append("没拿到站点懒加载出来的新会话: " + str([i["title"] for i in more]))

            # 2) 没有链接的站点(靠点击)
            mgr.provider = mock_provider(root + "/unlinked", "/c/")
            await page.goto(root + "/unlinked", wait_until="domcontentloaded")
            items2 = await mgr.conversations()
            print("2) 点击型:", items2)
            titles = [i["title"] for i in items2]
            if titles != ["本周实验记录", "DMA 调试", "fib 缓存"]:
                bad.append("兜底列表解析不对: " + str(titles))
            if any(i["url"] for i in items2):
                bad.append("点击型会话不该有 url")
            if [i["title"] for i in items2 if i["active"]] != ["DMA 调试"]:
                bad.append("点击型的当前会话识别失败")
            target = items2[2]
            ok2 = await mgr.open_conversation(target["key"], "")
            opened = await page.evaluate("() => window.__opened || null")
            print("   点击切换:", ok2, opened)
            if not ok2 or opened != "fib 缓存":
                bad.append(f"点击切换会话失败: ok={ok2} opened={opened}")

            # 3) 页面没有会话列表时返回空列表, 不报错
            await page.set_content("<div>nothing here</div>")
            empty = await mgr.conversations()
            print("3) 无列表:", empty)
            if empty != []:
                bad.append("没有列表时应返回空: " + str(empty))

            await browser.close()
    finally:
        srv.shutdown()

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("CONVERSATIONS_OK (链接型/点击型会话列表解析与切换都正常)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
