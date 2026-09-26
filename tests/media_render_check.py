"""回答里的图片: 从 markdown 到"屏幕上真的看得见"这一整条路的自检。

为什么要单独一条: 为了显示图片, 前端现在会**自己拼 <img> 标签**, 而 alt/src 来自站点文本。
属性值转义漏一个引号, 站点就能往我们的页面里塞事件处理器。所以除了"显示得出来",
这条还盯三件注入面 + 一件"图是不是真的从本地服务取到的" + 一件"连续几张是不是真的排成
一行 3 张"(布局是量出来的: 同一容器、顶边对齐、整行宽度被吃掉, 不是看 CSS 顺不顺眼),
最后两件是"抓图的代价": 坏链要有总预算、记过的坏链第二遍不许再联网。

打的是 8765 上那个真服务(要它把 transcripts/media/ 挂出来), 页面用替身状态。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import transcript  # noqa: E402

URL = "http://127.0.0.1:8765/"
FIX = "media/_check/box.png"


def solid_png(w: int = 800, h: int = 600, rgb=(220, 40, 40)) -> bytes:
    """自己造一张**有尺寸**的纯色 PNG(不依赖网络, 也不要 PIL)。

    原来用的是 1x1 的红点: 大图的弹框按"不超过原图"来渲染, 1x1 的图在弹框里就只有 3x3 个
    像素(边框 1px 各一边), 于是"大图明显比缩略图大"这条根本量不到 —— 尺子量不出来的东西
    不能算测过。
    """
    import struct
    import zlib

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


PNG = solid_png()

STUB = """(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.fetch = (u) => {
    const s = String(u);
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "_check", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0) return json({ ok: true, items: [], current: "_check" });
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    return orig(u);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""


async def main() -> int:
    bad = []
    p = transcript.ROOT / FIX
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(PNG)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1200, "height": 860})
            await page.add_init_script(STUB)
            await page.goto(URL, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(700)

            # 1) 本地路径 -> 真的能加载出来(证明 /transcripts/media 这条挂载是通的)
            r = await page.evaluate("""(fix) => {
              showWelcome(false);
              handle({ type: "message_start" });
              handle({ type: "delta", kind: "text",
                       text: "先看效果:\\n\\n![一个红方块](" + fix + ")\\n\\n就这样。" });
              handle({ type: "message_end" });
              return new Promise(res => setTimeout(() => {
                const i = document.querySelector("#conv img.md-img");
                res(i ? { src: i.getAttribute("src"), alt: i.alt, w: i.naturalWidth,
                          complete: i.complete } : null);
              }, 1200));
            }""", FIX)
            print("1) 渲染出的图片: %s" % json.dumps(r, ensure_ascii=False))
            if not r:
                bad.append("![...](media/…) 没有渲染成 <img> —— 图片这条路根本没通")
            else:
                if r["src"] != "/transcripts/" + FIX:
                    bad.append("本地图片没被指到挂载点: " + str(r["src"]))
                if not r["complete"] or r["w"] < 1:
                    bad.append("<img> 在但没加载成功(naturalWidth=%s) —— 挂载或路径不对" % r["w"])
                if r["alt"] != "一个红方块":
                    bad.append("alt 丢了: " + str(r["alt"]))

            # 2) 一行 3 张: 连续图片行(中间夹空行, 就是 capture 真实输出的样子)必须并成
            #    同一个 .md-imgs, 前 3 张顶边对齐 = 真的同一行, 第 4 张落到下一行。
            #    只看最后一个容器: 上一步那张图自己就是一个组, 用 querySelector 会抓到它。
            lay = await page.evaluate("""(fix) => {
              const one = "![IMG](" + fix + ")";
              showWelcome(false);
              handle({ type: "message_start" });
              handle({ type: "delta", kind: "text", text: "先来四张:\\n\\n"
                + [1,2,3,4].map(i => one.replace("IMG", i)).join("\\n\\n") + "\\n\\n就这样。" });
              handle({ type: "message_end" });
              return new Promise(res => setTimeout(() => {
                const gs = Array.from(document.querySelectorAll("#conv .md-imgs"));
                const g = gs[gs.length - 1];
                if (!g) return res(null);
                const kids = Array.from(g.querySelectorAll("img.md-img"));
                const box = (e) => e.getBoundingClientRect();
                res({ n: kids.length, groups: gs.length,
                      rowW: Math.round(box(g).width),
                      tops: kids.map(i => Math.round(box(i).top)),
                      w: kids.map(i => Math.round(box(i).width)),
                      h: kids.map(i => Math.round(box(i).height)),
                      // 第一行三格的右边界离容器右边界还剩多少 px(留白就是这里)
                      spare: kids.length < 3 ? -1
                             : Math.round(box(g).right - box(kids[2]).right) });
              }, 1200));
            }""", FIX)
            print("2) 一行 3 张: %s" % json.dumps(lay, ensure_ascii=False))
            if not lay:
                bad.append("四张连续图片行没有被并成一个 .md-imgs —— 还会竖着排")
            else:
                if lay["n"] != 4:
                    bad.append("四张连续图片行被拆成了 %d 张一组(应 4 张同一容器, 换行靠 CSS 不是拆容器)"
                               % lay["n"])
                else:
                    if len(set(lay["tops"][:3])) != 1:
                        bad.append("前 3 张不在同一行(顶边 %s)" % lay["tops"])
                    if lay["tops"][3] <= lay["tops"][0]:
                        bad.append("第 4 张没有换到下一行(顶边 %s)" % lay["tops"])
                    if max(lay["w"][:3]) - min(lay["w"][:3]) > 2:
                        bad.append("同一行三格宽度不等: %s" % lay["w"])
                    if lay["spare"] > 2:
                        bad.append("一行没吃掉整行宽度, 右侧留白 %dpx(行宽 %d)"
                                   % (lay["spare"], lay["rowW"]))

            # 3) 注入面: javascript: / 引号跑出去 / data: 撑爆 —— 判"挂进 DOM 后有没有事件属性",
            #    不是在 HTML 源码上正则找 on...= (转义后的 alt 文本里本来就带着 "onclick=" 字样,
            #    那样量会把自己吓一跳, 是假阳性)
            inj = await page.evaluate("""() => {
              const cases = {
                js:   '![x](javascript:alert(1))',
                q:    '![a" onclick="alert(1)](media/_check/box.png)',
                src:  '![a](media/_check/box.png\\" onerror=\\"alert(1))',
                data: '![a](data:image/png;base64,' + 'A'.repeat(4000) + ')',
              };
              const host = document.createElement("div");
              document.body.appendChild(host);
              const out = {};
              for (const k in cases) {
                const html = renderMarkdown(cases[k]);
                host.innerHTML = html;
                const els = Array.from(host.querySelectorAll("*"));
                out[k] = { html: html.slice(0, 150),
                           imgs: host.querySelectorAll("img").length,
                           handlerAttrs: els.filter(e => Array.from(e.attributes)
                              .some(a => /^on[a-z]+$/i.test(a.name))).length,
                           inlineScript: !!host.querySelector("script,iframe,object,embed") };
              }
              host.remove();
              return out;
            }""")
            print("3) 注入面:")
            for k, v in inj.items():
                print("   %-5s img=%d 事件属性=%d script=%-5s %s"
                      % (k, v["imgs"], v["handlerAttrs"], v["inlineScript"], v["html"][:80]))
                if v["handlerAttrs"] or v["inlineScript"]:
                    bad.append("%s 用例挂进 DOM 后出现了事件属性/可执行元素" % k)
            if inj["js"]["imgs"]:
                bad.append("javascript: 地址还是生成了 <img>")
            if inj["data"]["imgs"]:
                bad.append("data: 地址被当成图片输出(几 MB base64 会撑爆一行 JSONL)")
            if inj["q"]["imgs"] != 1:
                bad.append("合法的本地图片被引号检查误伤了")

            # 4) 点缩略图 -> 本页弹框看大图。判四件: 弹框开了; 大图明显比缩略图大;
            #    **正文里那几张缩略图一个像素都没动**(原来是在原处撑开, 会把整列正文顶走);
            #    关闭有三条路(按钮 / Esc / 点遮罩), 且不会跳到别的地址。
            url0 = page.url
            thumb = page.locator("#conv .md-imgs").last.locator("img.md-img").nth(1)
            before = await page.evaluate("""() => {
              const g = [...document.querySelectorAll("#conv .md-imgs")].pop();
              const k = [...g.querySelectorAll("img.md-img")];
              const r = (e) => e.getBoundingClientRect();
              const tops = k.map(i => Math.round(r(i).top));
              return {rects: k.map((i, n) => [Math.round(r(i).left), Math.round(r(i).width),
                                              tops[n] - tops[0]]),   // 用相对顶: 滚动不影响
                      src: k[1].currentSrc || k[1].src, alt: k[1].alt};
            }""")
            await thumb.click()
            await page.wait_for_timeout(700)
            open1 = await page.evaluate("""() => {
              const ov = document.querySelector("#imgOverlay"), v = document.querySelector("#imgView");
              const r = v.getBoundingClientRect(), vr = document.documentElement;
              return {shown: ov.classList.contains("show"), disp: getComputedStyle(ov).display,
                      src: v.getAttribute("src"), alt: v.alt,
                      natural: v.naturalWidth + "x" + v.naturalHeight,
                      cap: document.querySelector("#imgCap").textContent,
                      size: document.querySelector("#imgSize").textContent,
                      w: Math.round(r.width), h: Math.round(r.height),
                      vh: vr.clientHeight, vw: vr.clientWidth,
                      tabs: document.querySelectorAll("a[target]").length};
            }""")
            moved = await page.evaluate("""() => {
              const k = [...[...document.querySelectorAll("#conv .md-imgs")]
                    .pop().querySelectorAll("img.md-img")];
              const r = (e) => e.getBoundingClientRect();
              const tops = k.map(i => Math.round(r(i).top));
              return k.map((i, n) => [Math.round(r(i).left), Math.round(r(i).width),
                                      tops[n] - tops[0]]);
            }""")
            print("4) 弹框 shown=%s display=%s 大图 %d×%d (原图 %s, 视口 %d×%d) 缩略图 %s | 标题=%r 尺寸=%r"
                  % (open1["shown"], open1["disp"], open1["w"], open1["h"], open1["natural"],
                     open1["vw"], open1["vh"], json.dumps(moved), open1["cap"][:14], open1["size"]))
            if not open1["shown"] or open1["disp"] != "flex":
                bad.append("点缩略图没开出弹框")
            else:
                if open1["src"] != before["src"]:
                    bad.append("弹框里显示的不是点的那张图: %s vs %s" % (open1["src"], before["src"]))
                if open1["w"] < moved[1][2] * 1.8 or open1["w"] > open1["vw"]:
                    bad.append("大图宽度不对: %d (缩略图 %d, 视口 %d)"
                               % (open1["w"], moved[1][2], open1["vw"]))
                if open1["h"] > open1["vh"]:
                    bad.append("大图比视口还高(%d > %d): 弹框会自己滚" % (open1["h"], open1["vh"]))
                if moved != before["rects"]:
                    bad.append("开弹框把正文里的缩略图挪动了: %s -> %s"
                               % (json.dumps(before["rects"]), json.dumps(moved)))
                if not open1["size"] or "×" not in open1["size"]:
                    bad.append("标题栏没写出图的真实像素尺寸: %r" % open1["size"])
                if open1["cap"] != before["alt"]:
                    bad.append("标题栏没带上 alt 文字: %r vs %r" % (open1["cap"], before["alt"]))
            # 4b) 换四种视口量溢出: 看大图的弹框**任何方向都不该出现滚动条**
            #     (图 702 宽被 .dialog 的 660 夹出横向条、竖向溢出 21px 就是这么来的)
            sweep = []
            for (vw, vh) in ((1400, 900), (1280, 720), (1000, 1200), (900, 600)):
                await page.set_viewport_size({"width": vw, "height": vh})
                await page.wait_for_timeout(300)
                sweep.append(await page.evaluate("""() => {
                  const ov = document.querySelector('#imgOverlay'),
                        dl = document.querySelector('.img-dialog'),
                        bd = ov.querySelector('.dlg-body'), r = dl.getBoundingClientRect();
                  return {vw: innerWidth, vh: innerHeight,
                          vOver: bd.scrollHeight - bd.clientHeight,
                          hOver: bd.scrollWidth - bd.clientWidth,
                          ovOver: ov.scrollHeight - ov.clientHeight,
                          oy: getComputedStyle(bd).overflowY,
                          out: r.top < 0 || r.left < 0 || r.bottom > innerHeight || r.right > innerWidth,
                          box: [Math.round(r.left), Math.round(r.top),
                                Math.round(r.right), Math.round(r.bottom)]};
                }"""))
            await page.set_viewport_size({"width": 1200, "height": 860})
            await page.wait_for_timeout(300)
            print("4b) 四种视口的弹框溢出: %s" % json.dumps(sweep, ensure_ascii=False))
            for s in sweep:
                if s["vOver"] or s["hOver"] or s["ovOver"]:
                    bad.append("%dx%d 时弹框里出现滚动条: 竖向 %dpx 横向 %dpx 遮罩 %dpx"
                               % (s["vw"], s["vh"], s["vOver"], s["hOver"], s["ovOver"]))
                if s["oy"] != "hidden":
                    bad.append("%dx%d 时正文容器还能滚(overflow-y=%s)" % (s["vw"], s["vh"], s["oy"]))
                if s["out"]:
                    bad.append("%dx%d 时弹框超出视口: %s" % (s["vw"], s["vh"], s["box"]))
            await page.click("#imgClose")
            await page.wait_for_timeout(200)
            if await page.evaluate("()=>document.querySelector('#imgOverlay').classList.contains('show')"):
                bad.append("关闭按钮没关掉弹框")
            await thumb.click()
            await page.wait_for_timeout(400)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
            if await page.evaluate("()=>document.querySelector('#imgOverlay').classList.contains('show')"):
                bad.append("Esc 没关掉弹框")
            await thumb.click()
            await page.wait_for_timeout(400)
            await page.mouse.click(30, 30)          # 点遮罩空白处(图在中间, 不会误点到大图)
            await page.wait_for_timeout(200)
            if await page.evaluate("()=>document.querySelector('#imgOverlay').classList.contains('show')"):
                bad.append("点遮罩空白处没关掉弹框")
            if page.url != url0:
                bad.append("看图跳走了: %s -> %s" % (url0, page.url))

            # 5) 抓取幂等: 同一段文本反复同步不该反复下载(包括进程重启、_cache 空了之后)
            from bridge import media  # noqa: E402
            remote = URL + "transcripts/" + FIX          # 用刚挂出来的那张真图当"远程"地址
            t = "看图 ![a](%s)" % remote
            orig_fetch = media._fetch_urllib
            net = {"n": 0}

            def counting(url):
                net["n"] += 1
                return orig_fetch(url)

            media._fetch_urllib = counting
            try:
                media._cache.clear()
                out1, s1 = await media.harvest(None, "chatgpt", "_check", t)
                n1 = net["n"]
                media._cache.clear()               # 模拟服务重启: 内存缓存和索引都没了, 只能靠盘上那份
                media._index.clear()
                media._INDEX_LOADED = False
                out2, s2 = await media.harvest(None, "chatgpt", "_check", t)
                n2 = net["n"]
            finally:
                media._fetch_urllib = orig_fetch
            print("5) 冷启动联网 %d 次(saved=%s); 清掉缓存再同步同一份: 联网 %d 次, 文本一致=%s"
                  % (n1, s1.get("saved"), n2 - n1, out2 == out1))
            if s1.get("saved") != 1 or "media/chatgpt/_check/" not in out1:
                bad.append("第一次没抓下来或没改写成本地路径: %s | %s" % (s1, out1[-90:]))
            if n2 - n1 != 0:
                bad.append("文件已在盘上却还是去联网抓(没查本地存在): 多抓 %d 次" % (n2 - n1))
            if out1 != out2:
                bad.append("同一份文本两次同步结果不稳定: 会多写一个版本")

            # 6) 时间预算: 站点正文里混着失效外链(实测一条 google 图标代理链就要等满超时),
            #    一次同步不能被一堆坏链拖成长任务 —— 它常常占着互斥, 也占着这一轮落盘。
            many = " ".join("![x%d](https://bad.example/x%d.png)" % (i, i) for i in range(25))
            orig_u, orig_budget = media._fetch_urllib, media.BUDGET_S
            media.BUDGET_S = 3.0

            def slow(url):
                time.sleep(1.0)
                raise OSError("模拟: 连不上")

            media._fetch_urllib = slow
            media._bad.clear()
            try:
                tb0 = time.time()
                out3, s3 = await media.harvest(None, "chatgpt", "_check", many)
                el = time.time() - tb0
            finally:
                media._fetch_urllib, media.BUDGET_S = orig_u, orig_budget
            print("6) 25 张坏链(每张要 1s): %.1fs 就返回了, 统计=%s" % (el, s3))
            if s3.get("skipped", 0) < 1:
                bad.append("超预算后还在往下试: skipped=%s(应该有一张张记成没试)" % s3.get("skipped"))
            if el > 12:
                bad.append("25 张坏链让一次同步花了 %.0fs —— 预算没起作用" % el)
            if out3.count("https://bad.example") != 25:
                bad.append("没抓下来的图片地址被改坏了(原地址必须一条不少地留着)")

            # 6b) 已经记成坏链的地址: 一遍都不许再去碰它(同一段会话会被反复同步很多遍,
            #     每次重等一遍超时就是拿互斥换一堆注定失败的请求)。这里把预算放开,
            #     只判一件事 —— 有没有再联网、用了多久。
            media._bad.clear()
            media._bad.update({("https://bad.example/x%d.png" % i): time.time() for i in range(25)})
            media.BUDGET_S = 40.0
            net2 = {"n": 0}

            def counting_slow(url):
                net2["n"] += 1
                raise OSError("不该再被调用")

            media._fetch_urllib = counting_slow
            tb0 = time.time()
            out4, s4 = await media.harvest(None, "chatgpt", "_check", many)
            again = time.time() - tb0
            media._fetch_urllib, media.BUDGET_S = orig_u, orig_budget
            media._bad.clear()
            print("6b) 全都记成坏链后再同步一遍: %.2fs, 联网 %d 次, 统计=%s 文本一致=%s"
                  % (again, net2["n"], s4, out4 == out3))
            if net2["n"]:
                bad.append("已知坏链还去联网: %d 次" % net2["n"])
            if again > 1:
                bad.append("全都记成坏链的一批还是要 %.1fs —— 失败没有被记住" % again)
            if s4.get("failed") != min(25, media.MAX_PER_CALL):
                bad.append("第二遍的失败数不对: %s(应 %d —— 一次最多看 %d 张, 其余的原地址也保留)"
                           % (s4.get("failed"), min(25, media.MAX_PER_CALL), media.MAX_PER_CALL))
            media._bad.clear()

            await browser.close()
    finally:
        import shutil
        shutil.rmtree(p.parent, ignore_errors=True)              # media/_check/
        shutil.rmtree(transcript.ROOT / "media" / "chatgpt" / "_check", ignore_errors=True)
        idx = transcript.ROOT / "media" / "index.json"
        try:                                                    # 索引里也别留测试条目
            d = json.loads(idx.read_text(encoding="utf-8"))
            d = {k: v for k, v in d.items() if "_check" not in v}
            idx.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8", newline="\n")
        except Exception:
            pass

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("MEDIA_RENDER_OK (本地图片真加载出来 / 连续图并成一行且一行最多 3 张 / "
          "javascript: 与 data: 不放行 / 引号跑不出事件属性 / 点开是本页弹框且不动正文 / "
          "按钮·Esc·点遮罩三条关闭路都能关 / 已是本地路径就不再联网 / 坏链有预算且不重等)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
