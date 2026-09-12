"""World 模式新流程: 原始消息发给 ChatGPT -> 回答显示 -> 本地模型落盘 -> 自测 -> 报错回传。

后端用替身本地模型(不联网), 前端用 stub 拦截请求。
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

URL = "http://127.0.0.1:8765/"
TMP_WS = ROOT / ".tmp" / "world-flow-ws"

# 让页面以为已连接(不然 send() 会直接返回)
STUB = """(() => {
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
    return orig(u, o);
  };
  window.WebSocket = function () { return { close: function () {}, send: function () {} }; };
})();"""


async def main() -> int:
    bad = []
    TMP_WS.mkdir(parents=True, exist_ok=True)
    (TMP_WS / "EditorArea.qml").write_text("// v1\n", encoding="utf-8")

    # ---------- 后端: 落盘 ----------
    captured = {}
    orig_ask = planner.ask
    orig_root = workspace.ROOT           # 跑完还原: use_root 不写设置, 别冲掉用户配的工作区

    async def fake_ask(cfg, prompt, system=None):
        captured["prompt"] = prompt
        return json.dumps({"message": "按 ChatGPT 的方案改好了", "files": [
            {"op": "update", "path": "EditorArea.qml", "content": "// v2 加了滚动条\n"}]},
            ensure_ascii=False)

    planner.ask = fake_ask
    try:
        workspace.use_root(TMP_WS)
        res = await server.api_world_apply(server.WorldApplyRequest(
            task="内容区滚动条隐藏了, 让它显示出来", text="把 EditorArea.qml 改成 ScrollView + ThinScrollBar"))
        body = res if isinstance(res, dict) else json.loads(res.body.decode("utf-8"))
        print("落盘:", json.dumps(body, ensure_ascii=False))
        if not body.get("ok") or not body.get("applied"):
            bad.append("落盘接口没有应用任何改动: " + json.dumps(body, ensure_ascii=False))
        if "v2" not in (TMP_WS / "EditorArea.qml").read_text(encoding="utf-8"):
            bad.append("文件没有被写入")
        if "ChatGPT 的回答" not in captured.get("prompt", "") and "ChatGPT" not in captured.get("prompt", ""):
            bad.append("落盘提示词里没带上 ChatGPT 的回答")
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)

    # ---------- 后端: 从"说明文字"里认出目标文件, 并把当前内容喂给本地模型 ----------
    (TMP_WS / "qml" / "components").mkdir(parents=True, exist_ok=True)
    (TMP_WS / "qml" / "components" / "EditorArea.qml").write_text("// 旧内容\nScrollBar.AsNeeded\n", encoding="utf-8")
    (TMP_WS / "Main.qml").write_text("// main\n", encoding="utf-8")
    workspace.use_root(TMP_WS)
    try:
        tree = workspace.walk_files()
        guessed = engineer.guess_paths(
            "只需要修改 qml/components/EditorArea.qml, 把 ScrollBar.AsNeeded 改成 AlwaysOn; 不用改 Main.qml",
            tree)
        print("认出的文件:", json.dumps(guessed, ensure_ascii=False))
        if "qml/components/EditorArea.qml" not in guessed:
            bad.append("没能从说明文字里认出目标文件: " + json.dumps(guessed, ensure_ascii=False))
    finally:
        workspace.use_root(orig_root)

    seen_prompt = {}
    orig_ask2 = planner.ask

    async def fake_ask_ctx(cfg, prompt, system=None):
        seen_prompt["p"] = prompt
        return json.dumps({"message": "按说明改好", "files": [
            {"op": "update", "path": "qml/components/EditorArea.qml",
             "content": "// 新内容\nScrollBar.AlwaysOn\n"}]}, ensure_ascii=False)

    planner.ask = fake_ask_ctx
    try:
        workspace.use_root(TMP_WS)
        res_ctx = await server.api_world_apply(server.WorldApplyRequest(
            task="让滚动条常显", text="只需要修改 qml/components/EditorArea.qml, 把 AsNeeded 改成 AlwaysOn"))
    finally:
        planner.ask = orig_ask2
        workspace.use_root(orig_root)
    res_ctx = res_ctx if isinstance(res_ctx, dict) else json.loads(res_ctx.body.decode("utf-8"))
    p = seen_prompt.get("p", "")
    print("提示词含当前内容:", "【当前文件内容: qml/components/EditorArea.qml】" in p,
          "| 含 AlwaysOn 说明:", "AlwaysOn" in p)
    if "【当前文件内容: qml/components/EditorArea.qml】" not in p:
        bad.append("没把目标文件的当前内容给本地模型(它没法给出完整内容)")
    if "// 旧内容" not in p:
        bad.append("提示词里没有目标文件的真实内容")
    if not res_ctx.get("applied"):
        bad.append("这种'说明文字+替换'的回答没有被落实: " + json.dumps(res_ctx, ensure_ascii=False)[:200])

    # ---------- 后端: 改动更新不该被误判为"跳过"(老的 tuple.size bug) ----------
    # 注意: 这一段以前漏了切根 —— 于是它直接往**用户配置的工作区**(H:\steward 之类)里
    # 写了一个 27 字节的 EditorArea.qml, 在人家项目根目录留了个残file。
    workspace.use_root(TMP_WS)
    try:
        res2 = engineer.apply_manifest({"files": [
            {"op": "update", "path": "EditorArea.qml", "content": "// v3\n// 又改了一次\n"}]})
    finally:
        workspace.use_root(orig_root)
    print("更新落盘:", json.dumps({"applied": res2[0], "skipped": res2[1], "diffs": res2[2]}, ensure_ascii=False))
    if res2[1]:
        bad.append("更新已存在文件时被误判为跳过(旧 diff bug): " + json.dumps(res2[1], ensure_ascii=False))
    if not res2[2] or res2[2][0].get("add") is None:
        bad.append("diff 统计不对: " + json.dumps(res2[2], ensure_ascii=False))

    # ---------- 后端: 预览(dry_run) + 按勾选落盘(commit) ----------
    workspace.use_root(TMP_WS)
    try:
        pv = engineer.preview_manifest({"files": [
            {"op": "update", "path": "EditorArea.qml", "content": "// v4\n"},
            {"op": "create", "path": "New.qml", "content": "// new\n"},
            {"op": "create", "path": "../escape.txt", "content": "x"}]})
        print("预览:", json.dumps([{k: f.get(k) for k in ("op", "path", "add", "del", "unchanged")} for f in pv],
                                 ensure_ascii=False))
        if len(pv) != 3 or pv[2]["op"] != "invalid":
            bad.append("预览/非法路径处理不对: " + json.dumps(pv, ensure_ascii=False))
        before = (TMP_WS / "EditorArea.qml").read_text(encoding="utf-8")
        if "v4" in before:
            bad.append("预览竟然写盘了")
        # commit 只应用勾选的那一条
        com = await server.api_world_commit(server.WorldCommitRequest(files=[
            {"op": "update", "path": "EditorArea.qml", "content": "// v4\n"}]))
        com = com if isinstance(com, dict) else json.loads(com.body.decode("utf-8"))
        print("commit:", json.dumps(com, ensure_ascii=False)[:200])
        if not com.get("applied") or "v4" not in (TMP_WS / "EditorArea.qml").read_text(encoding="utf-8"):
            bad.append("commit 没有按勾选落盘: " + json.dumps(com, ensure_ascii=False))
        if (TMP_WS / "New.qml").exists():
            bad.append("没勾选的文件也被写入了")
    finally:
        workspace.use_root(orig_root)
        (TMP_WS / "EditorArea.qml").write_text("// v1\n", encoding="utf-8")

    # ---------- 后端: 自测命令 ----------
    ok_run = await server.api_world_test(server.WorldTestRequest(command="cmd /c exit 0", timeout=20))
    bad_run = await server.api_world_test(server.WorldTestRequest(command="cmd /c exit 3", timeout=20))
    skip = await server.api_world_test(server.WorldTestRequest(command="", timeout=20))
    print("自测:", json.dumps({"ok": ok_run, "bad": bad_run, "skip": skip}, ensure_ascii=False)[:300])
    if not ok_run.get("ok") or ok_run.get("code") != 0:
        bad.append("自测(成功命令)判定不对: " + json.dumps(ok_run, ensure_ascii=False))
    if bad_run.get("ok") or bad_run.get("code") != 3:
        bad.append("自测(失败命令)判定不对: " + json.dumps(bad_run, ensure_ascii=False))
    if not skip.get("skipped"):
        bad.append("没配自测命令时应 skipped")

    # ---------- 前端 ----------
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script(STUB)
        await page.goto(URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(700)
        await page.evaluate(r"""() => {
          window.__calls = [];
          showWelcome(false);
          switchMode("world");
          post = async (url, body) => {
            window.__calls.push([url, body]);
            if (url === "/api/chat") return { ok: true };
            if (url === "/api/world/apply") {
              if (body && body.dry_run) return { ok: true, dry_run: true, message: "按方案改好了", files: [
                { op: "update", path: "EditorArea.qml", add: 3, del: 1, size: 18, unchanged: false,
                  preview: "// 新内容 A", content: "// 新内容 A\n" },
                { op: "create", path: "Thin.qml", add: 5, del: 0, size: 22, unchanged: false,
                  preview: "// 新内容 B", content: "// 新内容 B\n" }] };
              return { ok: true, message: "按方案改好了",
                       applied: [{ op: "update", path: "EditorArea.qml", size: 18 }], skipped: [] };
            }
            if (url === "/api/world/commit") return { ok: true, message: "按勾选写入",
              applied: (body.files || []).map(f => ({ op: f.op, path: f.path, size: 18 })), skipped: [] };
            if (url === "/api/world/verify") return { ok: false, gave_up: true, real_error: true,
              reason: "real-error", local_fixes: 2, tried_local_fix: true,
              last_command: "cmake --build build",
              error_log: "$ cmake --build build\n(退出码 1)\nE   AssertionError: 滚动条不可见",
              changed: [{ op: "update", path: "EditorArea.qml" }],
              rounds: [{ round: 1, action: "run", command: "cmake --build build", code: 1 },
                       { round: 2, action: "fix", applied: [{ op: "update", path: "EditorArea.qml" }] },
                       { round: 3, action: "run", command: "cmake --build build", code: 1 }] };
            return {};
          };
          // 设置: 落盘前确认 = 开
          const origFetch = window.fetch;
          window.fetch = (u, o) => String(u).indexOf("/api/settings") === 0
            ? Promise.resolve(new Response(JSON.stringify({ planner: {}, engine: { confirm_apply: "1" } }),
                { status: 200, headers: { "Content-Type": "application/json" } }))
            : origFetch(u, o);
        }""")
        await page.evaluate(r"""() => {
          document.getElementById("input").value = "当前项目中, EditorArea.qml 内容区滚动条隐藏了, 让它显示出来";
        }""")
        await page.evaluate("() => send()")
        await page.wait_for_timeout(400)
        sent = await page.evaluate("() => window.__calls.filter(c => c[0] === '/api/chat')")
        print("发给 chatgpt:", json.dumps(sent, ensure_ascii=False)[:200])
        if not sent or "EditorArea.qml" not in (sent[0][1] or {}).get("text", ""):
            bad.append("World 模式没有把原始消息发给网页模型: " + json.dumps(sent, ensure_ascii=False)[:200])
        if "FILE-CHANGE PROTOCOL" in (sent[0][1] or {}).get("text", ""):
            bad.append("还在发旧的工程协议提示词")

        # 模拟回答回来 -> 应该弹出确认框
        await page.evaluate(r"""() => {
          handle({ type: "message_start" });
          handle({ type: "delta", kind: "text", text: "把 EditorArea.qml 换成 ScrollView + ThinScrollBar 即可。" });
          handle({ type: "message_end" });
        }""")
        await page.wait_for_timeout(900)
        dlg = await page.evaluate(r"""() => ({
          open: document.getElementById("applyOverlay").classList.contains("show"),
          items: Array.from(document.querySelectorAll("#applyList .apply-item")).map(it => ({
            op: it.querySelector(".ai-op").textContent,
            path: it.querySelector(".ai-path").textContent,
            meta: it.querySelector(".ai-meta").textContent,
            checked: it.querySelector("input").checked,
          })),
          note: document.getElementById("applyNote").textContent,
          hasVerifyRow: !!document.getElementById("applyVerify"),
          sendDisabled: document.getElementById("btnSend").disabled,
          sendTitle: document.getElementById("btnSend").title,
        })""")
        print("确认框:", json.dumps(dlg, ensure_ascii=False))
        file_rows = [i for i in dlg["items"] if i["op"] != "自测"]
        if not dlg["open"] or len(file_rows) != 2:
            bad.append("落盘前没有弹出确认框(应含 2 个文件 + 自测步骤): " + json.dumps(dlg, ensure_ascii=False))
        elif not all(i["checked"] for i in dlg["items"]):
            bad.append("确认框里默认应勾选: " + json.dumps(dlg["items"], ensure_ascii=False))
        if not dlg["hasVerifyRow"]:
            bad.append("确认框里没有「自测」这一步: " + json.dumps(dlg, ensure_ascii=False))
        if not dlg["sendDisabled"]:
            bad.append("本地执行期间发送按钮应该置灰")
        # 取消勾选第二个文件 -> 只应写入第一个
        await page.evaluate("""() => {
          const boxes = document.querySelectorAll("#applyList .apply-item input");
          boxes[1].checked = false;
        }""")
        await page.click("#applyOk")
        await page.wait_for_timeout(1500)
        st = await page.evaluate(r"""() => ({
          cards: Array.from(document.querySelectorAll("#conv .task-card")).map(c => ({
            title: c.querySelector("b").textContent,
            count: (c.querySelector(".tcount") || {}).textContent || "",
            rows: Array.from(c.querySelectorAll(".task-item .tt")).map(e => e.textContent),
            foot: c.querySelector(".task-foot").textContent,
          })),
          bubbles: Array.from(document.querySelectorAll("#conv .user-bubble")).map(e => e.textContent.slice(0, 24)),
          answerShown: Array.from(document.querySelectorAll("#conv .assistant-body"))
                            .some(e => e.textContent.indexOf("ThinScrollBar") >= 0),
          calls: window.__calls.map(c => c[0]),
        })""")
        print("界面:", json.dumps(st, ensure_ascii=False)[:900])
        if not st["answerShown"]:
            bad.append("ChatGPT 的回答没有作为消息显示")
        titles = [c["title"] for c in st["cards"]]
        if "本地落盘(本地模型写入工作区)" not in titles or "本地验证(按需求验收点找证据)" not in titles:
            bad.append("缺少落盘/验证卡片: " + json.dumps(titles, ensure_ascii=False))
        else:
            land = [c for c in st["cards"] if c["title"].startswith("本地落盘")][0]
            if not any("EditorArea.qml" in r for r in land["rows"]):
                bad.append("落盘卡片没显示写入的文件: " + json.dumps(land, ensure_ascii=False))
            vcard = [c for c in st["cards"] if c["title"].startswith("本地验证")][0]
            if "没搞定" not in vcard.get("count", ""):
                bad.append("验证卡片状态不对: " + json.dumps(vcard, ensure_ascii=False))
        verify_call = [c for c in st["calls"] if c == "/api/world/verify"]
        if not verify_call:
            bad.append("落盘后没有调用本地模型验证: " + json.dumps(st["calls"], ensure_ascii=False))
        commits = await page.evaluate("() => window.__calls.filter(c => c[0] === '/api/world/commit')")
        print("提交:", json.dumps(commits, ensure_ascii=False)[:200])
        if not commits:
            bad.append("确认框点「应用勾选的」后没有调用 commit")
        else:
            paths = [f["path"] for f in commits[0][1]["files"]]
            if paths != ["EditorArea.qml"]:
                bad.append("只该写入勾选的文件, 实际: " + json.dumps(paths, ensure_ascii=False))
        fixes = [c for c in st["calls"] if c == "/api/chat"]
        if len(fixes) < 2:
            bad.append("自测报错没有自动回传给 ChatGPT: " + json.dumps(st["calls"], ensure_ascii=False))
        back = await page.evaluate("() => window.__calls.filter(c => c[0] === '/api/chat').slice(-1)[0][1].text")
        if "AssertionError" not in back or "退出码 1" not in back:
            bad.append("回传的报错里没有自测输出: " + str(back)[:300])
        if "cmake --build build" not in back:
            bad.append("回传的消息里没有失败的构建命令: " + str(back)[:300])
        if "EditorArea.qml" not in back:
            bad.append("回传的消息里没说本地改过哪些文件: " + str(back)[:300])
        if not any("自测报错" in b for b in st["bubbles"]):
            bad.append("消息列表里没有提示已回传报错: " + json.dumps(st["bubbles"], ensure_ascii=False))

        # 场景2: 本地模型没给出改动 -> 说明原因, 但仍然去验证
        await page.evaluate(r"""() => {
          window.__calls = [];
          showWelcome(false);
          post = async (url, body) => {
            window.__calls.push([url, body]);
            if (url === "/api/chat") return { ok: true };
            if (url === "/api/world/apply") return { ok: true, dry_run: true, empty: true,
              message: "这次回答里没有具体代码改动", files: [] };
            if (url === "/api/world/verify") return { ok: true, gave_up: false, rounds: [
              { round: 1, action: "done", text: "项目本身能构建通过" }], last_output: "" };
            return {};
          };
          worldTask = "只看不改的任务";
          handle({ type: "message_start" });
          handle({ type: "delta", kind: "text", text: "建议你先看看 ThinScrollBar.qml" });
          handle({ type: "message_end" });
        }""")
        await page.wait_for_timeout(1200)
        # 没有文件改动时也会弹确认框(只有自测这一步), 勾着执行 -> 应该去验证
        dlg2 = await page.evaluate("""() => ({
          open: document.getElementById("applyOverlay").classList.contains("show"),
          verifyChecked: (document.getElementById("applyVerify") || {}).checked,
        })""")
        if not dlg2["open"]:
            bad.append("没有文件改动时也应该弹步骤确认框: " + json.dumps(dlg2, ensure_ascii=False))
        await page.click("#applyOk")
        await page.wait_for_timeout(1200)
        st2 = await page.evaluate(r"""() => ({
          calls: window.__calls.map(c => c[0]),
          cards: Array.from(document.querySelectorAll("#conv .task-card")).map(c => ({
            title: c.querySelector("b").textContent,
            count: (c.querySelector(".tcount") || {}).textContent || "",
            foot: c.querySelector(".task-foot").textContent })),
        })""")
        print("空清单场景:", json.dumps(st2, ensure_ascii=False)[:400])
        if "/api/world/verify" not in st2["calls"]:
            bad.append("本地模型没给出改动时, 也应该去验证项目: " + json.dumps(st2["calls"], ensure_ascii=False))
        if not any(("没有需要写入的文件" in c["foot"]) or ("没有具体文件改动" in c["foot"])
                   for c in st2["cards"]):
            bad.append("没有说明为什么没落盘: " + json.dumps(st2["cards"], ensure_ascii=False))
        if not any(c["count"] == "通过" for c in st2["cards"]):
            bad.append("验证卡片没有标通过: " + json.dumps(st2["cards"], ensure_ascii=False))

        # 实时事件(真实环境由 WebSocket 推送): 验证卡片逐行长出来
        await page.evaluate(r"""() => {
          handle({ type: "world", stage: "verify", round: 1, action: "run",
                   command: "cmake --build build", text: "" });
          handle({ type: "world", stage: "verify", round: 1, action: "run-done",
                   command: "cmake --build build", code: 1, text: "error: 找不到 ThinScrollBar" });
        }""")
        await page.wait_for_timeout(300)
        live = await page.evaluate(r"""() => Array.from(document.querySelectorAll("#conv .task-card"))
            .map(c => c.querySelector("b").textContent + ": " +
                      Array.from(c.querySelectorAll(".task-item .tt")).map(e => e.textContent).join(" | "))""")
        print("实时事件:", json.dumps(live, ensure_ascii=False)[:300])
        if not any("cmake --build build" in l for l in live):
            bad.append("验证事件没有写进卡片: " + json.dumps(live, ensure_ascii=False)[:250])

        # real_error=false(只是本地模型命令跑不通) -> 不回传 ChatGPT
        await page.evaluate(r"""() => {
          window.__calls = [];
          showWelcome(false);
          post = async (url, body) => {
            window.__calls.push([url, body]);
            if (url === "/api/chat") return { ok: true };
            if (url === "/api/world/apply") return { ok: true, dry_run: true, files: [
              { op: "update", path: "a.qml", add: 1, del: 0, size: 5, unchanged: false,
                preview: "x", content: "x" }] };
            if (url === "/api/world/commit") return { ok: true, applied: [{ op: "update", path: "a.qml" }], skipped: [] };
            if (url === "/api/world/verify") return { ok: false, gave_up: true, real_error: false,
              error_log: "$ cat x.qml\n(退出码 1)\n'cat' 不是内部或外部命令",
              rounds: [{ round: 1, action: "tool-error", command: "cat x.qml", code: 1 }] };
            return {};
          };
          worldTask = "环境问题场景";
          handle({ type: "message_start" });
          handle({ type: "delta", kind: "text", text: "看看这个" });
          handle({ type: "message_end" });
        }""")
        await page.wait_for_timeout(900)
        await page.click("#applyOk")
        await page.wait_for_timeout(1200)
        st3 = await page.evaluate(r"""() => ({
          calls: window.__calls.map(c => c[0]),
          foots: Array.from(document.querySelectorAll("#conv .task-card .task-foot")).map(e => e.textContent),
        })""")
        print("环境问题场景:", json.dumps(st3, ensure_ascii=False)[:300])
        if st3["calls"].count("/api/chat") != 0:
            bad.append("只是本地命令跑不通时不该回传 ChatGPT: " + json.dumps(st3["calls"], ensure_ascii=False))
        if not any("不是项目报错" in f for f in st3["foots"]):
            bad.append("没有如实说明是环境问题: " + json.dumps(st3["foots"], ensure_ascii=False))

        # 场景: 只用本地模型 + 验证有真实报错 -> 本地模型自己修不动时也不许回传 ChatGPT
        await page.evaluate(r"""() => {
          window.__calls = [];
          showWelcome(false);
          const keep = replyChoice;
          replyChoice = "local";                   // 只用本地模型
          post = async (url, body) => {
            window.__calls.push([url, body]);
            if (url === "/api/chat") return { ok: true };
            if (url === "/api/world/apply") return { ok: true, dry_run: true, files: [
              { op: "update", path: "a.qml", add: 1, del: 0, size: 5, unchanged: false,
                preview: "x", content: "x" }] };
            if (url === "/api/world/commit") return { ok: true, applied: [{ op: "update", path: "a.qml" }], skipped: [] };
            if (url === "/api/world/verify") return { ok: false, gave_up: true, real_error: true,
              reason: "real-error", local_fixes: 2, tried_local_fix: true,
              error_log: "$ cmake --build build\n(退出码 1)\nAssertionError: 去重没生效",
              rounds: [{ round: 1, action: "run", command: "cmake --build build", code: 1 },
                       { round: 2, action: "fix", applied: [{ op: "update", path: "a.qml" }] }] };
            return {};
          };
          worldTask = "只用本地模型场景";
          handle({ type: "message_start" });
          handle({ type: "delta", kind: "text", text: "看看这个" });
          handle({ type: "message_end" });
          window.__restoreReply = () => { replyChoice = keep; };
        }""")
        await page.wait_for_timeout(900)
        await page.click("#applyOk")
        await page.wait_for_timeout(1200)
        st4 = await page.evaluate(r"""() => {
          window.__restoreReply();
          return {
            calls: window.__calls.map(c => c[0]),
            foots: Array.from(document.querySelectorAll("#conv .task-card .task-foot")).map(e => e.textContent),
          };
        }""")
        print("只用本地模型场景:", json.dumps(st4, ensure_ascii=False)[:300])
        if st4["calls"].count("/api/chat") != 0:
            bad.append("只用本地模型时不该回传 ChatGPT: " + json.dumps(st4["calls"], ensure_ascii=False))
        if not any("不回传" in f for f in st4["foots"]):
            bad.append("没有说明报错留在卡片里: " + json.dumps(st4["foots"], ensure_ascii=False))

        # 场景: 刷新过的页面(没有本地执行链路)收到落盘/验证事件 -> 卡片状态要正确, 不能停在"正在…"
        await page.evaluate(r"""() => {
          window.__calls = [];
          worldBusy = false;                 // 模拟刷新后的新页面
          worldCards = [];
          convEl.innerHTML = "";
          transcript.forEach(renderStoredMessage);   // 像刷新后那样按历史重画
          showWelcome(false);
          const ev = (o) => handle(Object.assign({ type: "world" }, o));
          ev({ stage: "apply", action: "start", text: "正在把 ChatGPT 的回答落实成文件改动…" });
          ev({ stage: "apply", action: "preview", text: "识别到目标文件",
               files: [{ op: "update", path: "qml/models/ClipboardModel.qml", add: 3, del: 1, size: 420 }] });
          ev({ stage: "apply", action: "applied", text: "改好了",
               applied: [{ op: "update", path: "qml/models/ClipboardModel.qml", size: 420 }], skipped: [] });
          ev({ stage: "verify", action: "finished", ok: true, text: "本地模型确认项目跑通了" });
        }""")
        await page.wait_for_timeout(400)
        ev_state = await page.evaluate(r"""() => ({
          cards: Array.from(document.querySelectorAll("#conv .task-card")).map(c => ({
            title: c.querySelector("b").textContent,
            foot: c.querySelector(".task-foot").textContent,
            spinning: c.querySelector(".tspin") && c.querySelector(".tspin").style.display !== "none",
          })),
        })""")
        print("事件驱动(模拟刷新后):", json.dumps(ev_state, ensure_ascii=False)[:400])
        feet = [c["foot"] for c in ev_state["cards"]]
        if any("正在把 ChatGPT 的回答落实成文件改动" in f for f in feet):
            bad.append("落盘完成后仍停在正在中: " + json.dumps(feet, ensure_ascii=False))
        if not any("应用 1 项" in f for f in feet):
            bad.append("没有显示落盘结果: " + json.dumps(feet, ensure_ascii=False))
        if any(c["spinning"] for c in ev_state["cards"]):
            bad.append("事件驱动下不该有转圈: " + json.dumps(ev_state, ensure_ascii=False))

        # 刷新后: 卡片还在(从历史重画)
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(1200)
        reloaded = await page.evaluate(r"""() => ({
          cards: Array.from(document.querySelectorAll("#conv .task-card")).map(c => ({
            title: c.querySelector("b").textContent,
            rows: Array.from(c.querySelectorAll(".task-item .tt")).map(e => e.textContent),
          })),
        })""")
        spinning2 = await page.evaluate(r"""() => Array.from(document.querySelectorAll("#conv .task-card"))
            .filter(c => c.querySelector(".tspin") && c.querySelector(".tspin").style.display !== "none")
            .map(c => c.querySelector("b").textContent)""")
        print("刷新后:", json.dumps(reloaded, ensure_ascii=False)[:200], "| 还在转的:", json.dumps(spinning2, ensure_ascii=False))
        if spinning2:
            bad.append("刷新后历史卡片还在转圈: " + json.dumps(spinning2, ensure_ascii=False))
        # 存档里也不该带 busy=true
        busy_flags = await page.evaluate(r"""() => {
          const all = JSON.parse(localStorage.getItem("wlb.history.v3") || "{}");
          const out = [];
          Object.values(all).forEach(p => Object.values(p).forEach(e =>
            (e.messages || []).forEach(m => { if (m.role === "world") out.push(!!(m.card || {}).busy); })));
          return out;
        }""")
        if any(busy_flags):
            bad.append("历史存档里带了 进行中 标记: " + json.dumps(busy_flags, ensure_ascii=False))
        names = " ".join(c["title"] for c in reloaded["cards"])
        if "本地落盘" not in names or "本地验证" not in names:
            bad.append("刷新后本地执行过程的卡片丢了: " + json.dumps(reloaded, ensure_ascii=False)[:200])
        elif not any("cmake --build" in r for c in reloaded["cards"] for r in c["rows"]):
            bad.append("刷新后卡片里的执行细节丢了: " + json.dumps(reloaded, ensure_ascii=False)[:250])

        # 结束后: 发送按钮恢复 + 卡片写进了历史(localStorage)
        after = await page.evaluate(r"""() => ({
          sendDisabled: document.getElementById("btnSend").disabled,
          spinning: Array.from(document.querySelectorAll("#conv .task-card"))
                      .filter(c => (c.querySelector(".tspin") || {}).style
                                   && c.querySelector(".tspin").style.display !== "none")
                      .map(c => c.querySelector("b").textContent),
          history: (() => {
            try {
              const all = JSON.parse(localStorage.getItem("wlb.history.v3") || "{}");
              const prov = Object.keys(all)[0] || "";
              const entries = Object.values(all[prov] || {});
              const msgs = entries.length ? (entries[entries.length - 1].messages || []) : [];
              return msgs.filter(m => m.role === "world").map(m => m.card && m.card.title);
            } catch (e) { return ["err:" + e.message]; }
          })(),
        })""")
        print("收尾:", json.dumps(after, ensure_ascii=False)[:400])
        if after["sendDisabled"]:
            bad.append("执行结束后发送按钮应该恢复可用")
        if after["spinning"]:
            bad.append("本地执行结束后还有卡片在转圈: " + json.dumps(after["spinning"], ensure_ascii=False))
        titles = [t for t in after["history"] if t]
        if not titles or "本地落盘" not in " ".join(titles):
            bad.append("本地执行的过程没有写进历史(刷新会丢): " + json.dumps(after["history"], ensure_ascii=False)[:250])
        await browser.close()

    shutil.rmtree(TMP_WS, ignore_errors=True)
    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WORLD_FLOW_OK (原样发消息 -> 回答显示 -> 本地落盘 -> 自测 -> 报错回传)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
