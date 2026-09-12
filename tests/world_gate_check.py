"""落盘: 从 ChatGPT 的回答里**机械整理**出文件改动 -> 用户自己决定写不写。

规矩(用户定的):
    - 本地模型**不写代码、也不判断该改什么**, 只由程序把回答里的代码块和文件名对上,
      内容**原样照抄**; 写不写由用户在卡片上勾选。
    - 卡片列在消息下面, 含每个文件的内容。

1. 后端: 闲聊 -> no_changes; 有代码但没认出文件名 -> no_changes; 两种情况**本地模型都被调用 0 次**。
        有代码 + 有文件名 -> 抽出文件, 内容与回答里**一模一样**, 依然 0 次调用。
2. 前端: 卡片列在消息下面(含内容, 默认勾选) -> 「全部跳过」不写盘 / 「写入选中的」只写勾选的。
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from bridge import engineer, planner, server, workspace  # noqa: E402

TMP_WS = ROOT / ".tmp" / "world-gate-ws"
URL = "http://127.0.0.1:8765/"

ANSWER_CODE = """先说明思路。

**修改文件: src/app.py**
```python
# src/app.py
print('hello')
```

新建一个:
```js title=js/app.js
console.log(1)
```

这段没认出文件名:
```python
print(2)
```
"""

called: list[str] = []


async def fake_ask(pcfg, prompt, system=None):
    called.append(prompt or "")
    return "```json\n{\"message\": \"\", \"files\": []}\n```"


def body(resp):
    return resp if isinstance(resp, dict) else json.loads(bytes(resp.body).decode("utf-8"))


def status_of(resp):
    return getattr(resp, "status_code", 200)


async def backend_checks() -> list[str]:
    bad: list[str] = []
    shutil.rmtree(TMP_WS, ignore_errors=True)
    TMP_WS.mkdir(parents=True)
    (TMP_WS / "src").mkdir()
    (TMP_WS / "src" / "app.py").write_bytes(b"print('old')\n")
    (TMP_WS / "js").mkdir()
    (TMP_WS / "README.txt").write_bytes(b"# hi\n")

    orig_root = workspace.ROOT
    orig_ask, orig_bc = planner.ask, server.broadcast

    async def noop(_payload):
        return None

    workspace.use_root(TMP_WS)
    planner.ask = fake_ask
    server.broadcast = noop
    try:
        # 1) 闲聊: 什么都不该发生
        called.clear()
        r = body(await server.api_world_apply(
            server.WorldApplyRequest(task="你好", text="你好！有什么可以帮你？", dry_run=True)))
        print("闲聊:", json.dumps({k: r.get(k) for k in ("no_changes", "reason")}, ensure_ascii=False))
        if not r.get("no_changes") or called:
            bad.append("闲聊没有被跳过或调用了本地模型: " + json.dumps([r, called], ensure_ascii=False)[:200])

        # 2) 有代码但没认出文件名 -> 也不写(不猜), 仍然 0 次调用
        called.clear()
        r = body(await server.api_world_apply(server.WorldApplyRequest(
            task="", text="给你一段代码:\n```python\nprint(3)\n```", dry_run=True)))
        print("没认出文件名:", json.dumps({k: r.get(k) for k in ("no_changes", "reason", "loose")},
                                        ensure_ascii=False))
        if not r.get("no_changes") or called:
            bad.append("没认出文件名时不该写入/不该叫模型: " + json.dumps(r, ensure_ascii=False)[:200])

        # 3) 有代码 + 文件名 -> 原样抽出, 本地模型 0 次
        called.clear()
        r = body(await server.api_world_apply(
            server.WorldApplyRequest(task="", text=ANSWER_CODE, dry_run=True)))
        files = r.get("files") or []
        print("抽出:", json.dumps([(f.get("op"), f.get("path"), f.get("size")) for f in files],
                                 ensure_ascii=False), "loose:", r.get("loose"))
        if called:
            bad.append("整理文件改动时调用了本地模型 " + str(len(called)) + " 次(它不该参与)")
        if [f.get("path") for f in files] != ["src/app.py", "js/app.js"]:
            bad.append("抽出的文件不对: " + json.dumps(files, ensure_ascii=False)[:300])
        else:
            if files[0].get("content") != "# src/app.py\nprint('hello')\n":
                bad.append("内容没有原样照抄(被改过): " + repr(files[0].get("content")))
            if files[0].get("op") != "update" or files[1].get("op") != "create":
                bad.append("op 判断不对(应为 update/create): " + json.dumps(files, ensure_ascii=False)[:200])
        if r.get("loose") != 1:
            bad.append("没认出文件名的代码块数不对(应 1): " + str(r.get("loose")))

        # 3b) 真实那种"每个文件一个小标题"的回答: 文件名不在工作区里, 也要认出来
        #     (踩过: ### 1. `package.json` 这种标题行以前不认 -> 39 段代码全部"没认出文件名")
        ANS2 = """项目：
```
vue3-mall/
├── package.json
```

### 1. `package.json`
```json
{"name": "vue3-mall"}
```

### 2. `index.html`
```html
<div id="app"></div>
```

### 8. `src/App.vue`
```vue
<template><div/></template>
```

### 运行方式
```bash
cd vue3-mall
```
"""
        items2, loose2 = engineer.extract_code_files(ANS2, ["README.txt"])
        paths2 = [i["path"] for i in items2]
        print("小标题式回答:", json.dumps(paths2, ensure_ascii=False)[:200], "| loose:", loose2)
        if paths2 != ["package.json", "index.html", "src/App.vue"]:
            bad.append("小标题式的回答没认全文件(html/vue 也别漏): " + json.dumps(paths2, ensure_ascii=False))
        if any(i["op"] != "create" for i in items2):
            bad.append("工作区里没有的文件应该是 create: " + json.dumps(items2, ensure_ascii=False)[:200])
        if loose2 != 2:                      # 目录树 + bash 命令, 都不该被当成文件
            bad.append("目录树/命令块不该被当成文件(loose 应 2): " + str(loose2))

        # 3c) 卡片上的"写入工作区"一键落盘: 走 /api/world/apply_all, 内容=最近一次整理出来的
        for p in ("src/app.py", "js/app.js"):
            fp = TMP_WS / p
            if fp.exists():
                fp.unlink()
        allr = body(await server.api_world_apply_all())
        got_all = [a.get("path") for a in allr.get("applied", [])]
        print("一键写入:", json.dumps(got_all, ensure_ascii=False))
        if got_all != ["src/app.py", "js/app.js"]:      # _last_extract = 上一段(dry_run)整理出来的那两条
            bad.append("一键写入没有把整理出来的文件写进去: " + json.dumps(allr, ensure_ascii=False)[:200])
        if (TMP_WS / "src" / "app.py").read_text(encoding="utf-8") != "# src/app.py\nprint('hello')\n":
            bad.append("一键写入的内容不是回答里的原文: "
                       + repr((TMP_WS / "src" / "app.py").read_text(encoding="utf-8")))
        if called:
            bad.append("一键写入调用了本地模型: " + str(len(called)))

        # 4) 自动模式(不确认) = 直接写入**整理出来的**那些, 依然不叫本地模型
        called.clear()
        r4 = body(await server.api_world_apply(
            server.WorldApplyRequest(task="", text=ANSWER_CODE, dry_run=False)))
        got = (TMP_WS / "src" / "app.py").read_text(encoding="utf-8")
        print("自动写入:", json.dumps([a.get("path") for a in r4.get("applied", [])], ensure_ascii=False))
        if called or "print('hello')" not in got:
            bad.append("自动模式没写入/叫了模型: " + json.dumps([called, got], ensure_ascii=False)[:200])

        # 5) 验证门槛: 没改动没代码 -> 拒绝
        called.clear()
        vr = await server.api_world_verify(
            server.WorldVerifyRequest(task="你好", answer="你好！", applied=[]))
        if status_of(vr) != 400 or called:
            bad.append("没东西可验证时还跑了验证: " + str(status_of(vr)))
    finally:
        workspace.use_root(orig_root)
        planner.ask, server.broadcast = orig_ask, orig_bc
        shutil.rmtree(TMP_WS, ignore_errors=True)
    return bad


STUB = """(() => {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
    { status: 200, headers: { "Content-Type": "application/json" } }));
  const orig = window.fetch.bind(window);
  window.__w = [];
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.indexOf("/api/world/apply_all") === 0) {
      window.__w.push(["apply_all", JSON.parse((o && o.body) || "{}")]);
      return json({ ok: true, applied: [{ op: "create", path: "package.json", size: 120 },
                                        { op: "create", path: "src/App.vue", size: 80 }], skipped: [] });
    }
    if (s.indexOf("/api/world/apply") === 0) {
      window.__w.push(["apply", JSON.parse((o && o.body) || "{}")]);
      if (window.__firstNoChanges) {           // 第一次故意回"没认出文件名"
        window.__firstNoChanges = false;
        return json({ ok: true, no_changes: true, loose: 2, files: [],
                      text: "这条回答里没有可写入的文件(回答里有代码, 但没认出这些代码属于哪个文件)" });
      }
      return json({ ok: true, dry_run: true, empty: false, loose: 1, files: [
        { op: "update", path: "src/app.py", add: 2, del: 1, size: 20, unchanged: false,
          preview: "print('hello')\\n", content: "print('hello')\\n" },
        { op: "create", path: "js/app.js", add: 1, del: 0, size: 12, unchanged: false,
          preview: "console.log(1)\\n", content: "console.log(1)\\n" }] });
    }
    if (s.indexOf("/api/world/commit") === 0) {
      window.__w.push(["commit", JSON.parse((o && o.body) || "{}")]);
      return json({ ok: true, applied: [{ op: "update", path: "src/app.py", size: 20 }], skipped: [] });
    }
    if (s.indexOf("/api/world/verify") === 0) { window.__w.push(["verify", null]); return json({ ok: true, rounds: [] }); }
    if (s.indexOf("/api/world/state") === 0) return json({ ok: true, apply: null, verify: null });
    if (s.indexOf("/api/settings") === 0) return json({ engine: { confirm_apply: "1" }, planner: {} });
    if (s.indexOf("/api/status") === 0)
      return json({ provider: "chatgpt", state: "logged_in", busy: false, error: null,
                    started: true, conversation_id: "convA", conversation_url: "" });
    if (s.indexOf("/api/conversations") === 0)
      return json({ ok: true, provider: "chatgpt", current: "convA", items: [] });
    if (s.indexOf("/workspace/tree") === 0)
      return json({ ok: true, root: "H:\\\\tmp", name: "tmp", default: "", custom: false, items: [] });
    return orig(u, o);
  };
  window.WebSocket = function () { return { close() {}, send() {} }; };
})();"""

CARD = """() => {
  const card = Array.from(document.querySelectorAll("#conv .task-card"))
    .find(c => c.querySelector("b").textContent.indexOf("识别到的文件改动") >= 0);
  if (!card) return { found: false };
  return {
    found: true,
    title: card.querySelector("b").textContent,
    rows: Array.from(card.querySelectorAll(".task-item")).map(it => ({
      text: it.querySelector(".tt").textContent,
      checked: !!it.querySelector("input[type=checkbox]") && it.querySelector("input[type=checkbox]").checked,
      content: (it.querySelector("details pre") || {}).textContent || "",
    })),
    buttons: Array.from(card.querySelectorAll(".task-btns button")).map(b => b.textContent),
    verify: !!card.querySelector(".task-check input") && card.querySelector(".task-check input").checked,
    loose: (card.querySelector(".task-list .hint2") || {}).textContent || "",
    overlay: document.getElementById("applyOverlay").classList.contains("show"),
  };
}"""


async def frontend_checks() -> list[str]:
    bad: list[str] = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)

        # (a) 全部跳过 -> 一个都不写
        page = await b.new_page(viewport={"width": 1280, "height": 860})
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)
        await page.evaluate("""() => { worldPipeline("改一下", "把 src/app.py 改成 print('hello')"); }""")
        await page.wait_for_timeout(600)
        c = await page.evaluate(CARD)
        print("卡片:", json.dumps({k: c.get(k) for k in ("found", "buttons", "verify")}, ensure_ascii=False))
        print("  行:", json.dumps(c.get("rows"), ensure_ascii=False)[:300])
        if not c.get("found"):
            bad.append("消息下面没有出现「识别到的文件改动」卡片")
        else:
            if len(c["rows"]) != 2 or not all(r["checked"] for r in c["rows"]):
                bad.append("卡片没有列出 2 个文件/默认没勾选: " + json.dumps(c["rows"], ensure_ascii=False))
            if not all(("print('hello')" in r["content"]) or ("console.log" in r["content"]) for r in c["rows"]):
                bad.append("卡片上没有文件内容: " + json.dumps(c["rows"], ensure_ascii=False)[:300])
            if c["buttons"] != ["写入选中的", "全部跳过"]:
                bad.append("卡片按钮不对: " + json.dumps(c["buttons"], ensure_ascii=False))
            if not c["verify"]:
                bad.append("卡片默认没有勾「写入后自测」")
            if not c["loose"]:
                bad.append("没认出文件名的代码块没有提示: " + repr(c["loose"]))
        if c.get("overlay"):
            bad.append("还在弹确认框(应该只在消息下面出卡片)")
        await page.evaluate("""() => {
          const card = Array.from(document.querySelectorAll("#conv .task-card"))
            .find(c => c.querySelector("b").textContent.indexOf("识别到的文件改动") >= 0);
          card.querySelector(".task-btns button:not(.pri)").click();     // 全部跳过
        }""")
        await page.wait_for_timeout(400)
        skip = await page.evaluate("""() => ({
          calls: window.__w.map(x => x[0]),
          foot: Array.from(document.querySelectorAll("#conv .task-card .task-foot"))
                  .map(e => e.textContent).join(" | ") })""")
        print("跳过后:", json.dumps(skip, ensure_ascii=False)[:200])
        if "commit" in skip["calls"]:
            bad.append("点了「全部跳过」还是写盘了: " + json.dumps(skip["calls"]))
        if "跳过" not in skip["foot"]:
            bad.append("跳过后卡片没说明: " + skip["foot"])
        await page.close()

        # (b) 取消勾选第二个 -> 只写勾选的那个
        page2 = await b.new_page(viewport={"width": 1280, "height": 860})
        await page2.add_init_script(STUB)
        await page2.goto(URL, wait_until="networkidle", timeout=30_000)
        await page2.wait_for_timeout(700)
        await page2.evaluate("""() => { worldPipeline("改一下", "x"); }""")
        await page2.wait_for_timeout(600)
        await page2.evaluate("""() => {
          const card = Array.from(document.querySelectorAll("#conv .task-card"))
            .find(c => c.querySelector("b").textContent.indexOf("识别到的文件改动") >= 0);
          card.querySelectorAll(".task-item input[type=checkbox]")[1].checked = false;
          card.querySelector(".task-btns button.pri").click();           // 写入选中的
        }""")
        await page2.wait_for_timeout(900)
        r2 = await page2.evaluate("""() => ({
          calls: window.__w.map(x => x[0]),
          commit: (window.__w.find(x => x[0] === "commit") || [null, {}])[1],
          foot: Array.from(document.querySelectorAll("#conv .task-card .task-foot"))
                  .map(e => e.textContent).join(" | ") })""")
        paths = [f.get("path") for f in (r2["commit"] or {}).get("files", [])]
        print("写入选中的:", json.dumps({"paths": paths, "calls": r2["calls"]}, ensure_ascii=False))
        if paths != ["src/app.py"]:
            bad.append("没有只写入勾选的文件: " + json.dumps(paths, ensure_ascii=False))
        if "verify" not in r2["calls"]:
            bad.append("勾了自测却没去验证: " + json.dumps(r2["calls"]))
        await page2.close()

        # (c) 认不出文件名时: 卡片上要给「重新整理这条回答」的口子(规则修过 / 补了文件名后重试,
        #     不用把整段回答再发一遍)
        page3 = await b.new_page(viewport={"width": 1280, "height": 860})
        await page3.add_init_script("window.__firstNoChanges = true;")
        await page3.add_init_script(STUB)
        await page3.goto(URL, wait_until="networkidle", timeout=30_000)
        await page3.wait_for_timeout(700)
        await page3.evaluate("""() => { worldPipeline("建项目", "（一段没写文件名的代码）"); }""")
        await page3.wait_for_timeout(700)
        first = await page3.evaluate("""() => ({
          foot: Array.from(document.querySelectorAll("#conv .task-card .task-foot")).map(e => e.textContent),
          retry: !!Array.from(document.querySelectorAll("#conv .task-card .task-btns button"))
                    .find(b => b.textContent.indexOf("重新整理") >= 0) })""")
        print("认不出文件名时:", json.dumps(first, ensure_ascii=False)[:220])
        if not first["retry"]:
            bad.append("认不出文件名时没有「重新整理这条回答」按钮: " + json.dumps(first, ensure_ascii=False))
        await page3.evaluate("""() => {
          const b = Array.from(document.querySelectorAll("#conv .task-card .task-btns button"))
            .find(x => x.textContent.indexOf("重新整理") >= 0);
          if (b) b.click();
        }""")
        await page3.wait_for_timeout(1000)
        second = await page3.evaluate(CARD)
        print("点重新整理后:", json.dumps({k: second.get(k) for k in ("found", "buttons")},
                                         ensure_ascii=False)[:200],
              "| 行数:", len(second.get("rows") or []))
        if not second.get("found") or len(second.get("rows") or []) != 2:
            bad.append("点「重新整理」没有出文件卡片: " + json.dumps(second, ensure_ascii=False)[:200])
        await page3.close()

        # (d) 事件推来的"只读预览卡"底部要有「写入工作区(N 个)」一键按钮
        page4 = await b.new_page(viewport={"width": 1280, "height": 860})
        await page4.add_init_script(STUB)
        await page4.goto(URL, wait_until="networkidle", timeout=30_000)
        await page4.wait_for_timeout(700)
        await page4.evaluate("""() => {
          // 先有一段真实问答(否则这条记录只有卡片, 刷新时会被"只有卡片的坏段落"清理掉)
          transcript = [{ role: "user", text: "把商城项目写进工作区" },
                        { role: "assistant", text: "这是完整项目源码, 按文件名创建即可。" }];
          histConv = liveConvKey(); histProvider = providerKey();
          histFlush();
          const files = [];
          for (let i = 1; i <= 30; i++) {
            files.push({ op: "create", path: "src/f" + i + ".ts", add: 30, del: 0, size: 659 });
          }
          handle({ type: "world", stage: "apply", action: "preview", loose: 3, files: files });
        }""")
        await page4.wait_for_timeout(400)
        prev = await page4.evaluate("""() => {
          const card = Array.from(document.querySelectorAll("#conv .task-card"))
            .find(c => c.querySelector("b").textContent.indexOf("本地落盘") >= 0);
          if (!card) return { found: false };
          return { found: true,
                   folded: card.classList.contains("folded"),
                   buttons: Array.from(card.querySelectorAll(".task-btns button")).map(b => b.textContent),
                   rows: Array.from(card.querySelectorAll(".task-item .tt")).map(e => e.textContent),
                   count: (card.querySelector(".tcount") || {}).textContent || "",
                   foot: card.querySelector(".task-foot").textContent };
        }""")
        print("预览卡:", json.dumps({k: prev.get(k) for k in ("folded", "buttons", "count")},
                                   ensure_ascii=False), "| 行数:", len(prev.get("rows") or []))
        if not prev.get("fetched_none") and not prev.get("folded"):
            bad.append("预览卡没有默认折叠")
        if len(prev.get("rows") or []) != 30:
            bad.append("预览卡没有列出完整的文件清单(应 30 行): " + str(len(prev.get("rows") or [])))
        if prev.get("buttons") != ["写入工作区(30 个)"]:
            bad.append("预览卡上没有「写入工作区」按钮: " + json.dumps(prev.get("buttons"), ensure_ascii=False))
        await page4.evaluate("""() => {
          const card = Array.from(document.querySelectorAll("#conv .task-card"))
            .find(c => c.querySelector("b").textContent.indexOf("本地落盘") >= 0);
          card.querySelector(".task-btns button").click();
        }""")
        await page4.wait_for_timeout(800)
        done = await page4.evaluate("""() => ({
          calls: window.__w.map(x => x[0]),
          foot: Array.from(document.querySelectorAll("#conv .task-card .task-foot"))
                  .map(e => e.textContent).join(" | ") })""")
        print("点了写入工作区:", json.dumps(done, ensure_ascii=False)[:220])
        if ("apply_all" not in done["calls"]) or ("已写入 2 个文件" not in done["foot"]):
            bad.append("点「写入工作区」没落到 /api/world/apply_all 或没回报结果: "
                       + json.dumps(done, ensure_ascii=False)[:220])

        # 刷新后也要还是"折叠 + 完整清单 + 写入按钮"(历史里别被砍成 12 行、折叠状态/按钮也别丢)
        await page4.reload(wait_until="networkidle")
        await page4.wait_for_timeout(1400)
        after_reload = await page4.evaluate("""() => {
          const card = Array.from(document.querySelectorAll("#conv .task-card"))
            .find(c => c.querySelector("b").textContent.indexOf("本地落盘") >= 0);
          if (!card) return { found: false };
          return { found: true, folded: card.classList.contains("folded"),
                   buttons: Array.from(card.querySelectorAll(".task-btns button")).map(b => b.textContent),
                   rows: card.querySelectorAll(".task-item .tt").length };
        }""")
        print("刷新后预览卡:", json.dumps(after_reload, ensure_ascii=False))
        if not after_reload.get("found"):
            bad.append("刷新后预览卡丢了")
        else:
            if not after_reload.get("folded"):
                bad.append("刷新后预览卡不是折叠的")
            if after_reload.get("rows") != 30:
                bad.append("刷新后文件清单不完整(应 30 行): " + str(after_reload.get("rows")))
            if after_reload.get("buttons") != ["写入工作区(30 个)"]:
                bad.append("刷新后「写入工作区」按钮丢了: " + json.dumps(after_reload.get("buttons"),
                                                                  ensure_ascii=False))
        await page4.close()
        await b.close()
    return bad


async def main() -> int:
    print("[1] 后端: 只整理, 不调模型")
    bad = await backend_checks()
    print("[2] 前端: 卡片 + 用户决定")
    bad += await frontend_checks()
    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("WORLD_GATE_OK (文件改动只由程序从回答里整理、内容原样照抄; 本地模型 0 次调用; "
          "写不写由用户在消息下面的卡片上勾选)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
