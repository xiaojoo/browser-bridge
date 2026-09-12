"""World 模式: 本地模型自己验证项目(挑命令/看输出/自己改)。

全程替身: 不联网、不跑真命令(命令用 cmd /c exit N 之类)。

注意: 这里测的是**验证循环本身**(命令/证据/轮次/假通过防线), 不是"没东西可验证就别跑"那道
门槛(那道门由 `tests/world_gate_check.py` 覆盖)。所以下面每个调用都显式带 `force=True` ——
它的含义就是界面上那个「这次不改文件, 只让它去项目里自测」: 用户明确要求验证, 与落盘无关。
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import engineer, planner, server, workspace  # noqa: E402

TMP_WS = ROOT / ".tmp" / "verify-ws"


async def main() -> int:
    bad = []
    TMP_WS.mkdir(parents=True, exist_ok=True)
    (TMP_WS / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
    (TMP_WS / "main.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

    seq = iter([
        json.dumps({"checks": [{"id": 1, "expect": "项目能编译通过", "how": "cmake --build"},
                               {"id": 2, "expect": "改动后 main 返回 1", "how": "跑一下 main"}],
                    "action": "run", "command": "cmd /c exit 1", "reason": "先构建看看"},
                   ensure_ascii=False),
        json.dumps({"action": "fix", "message": "补上头文件", "files": [
            {"op": "update", "path": "main.cpp", "content": "int main(){return 1;}\n"}]}),
        json.dumps({"action": "run", "command": "cmd /c exit 0", "reason": "再构建"}),
        json.dumps({"action": "done", "message": "项目已能构建通过",
                    "evidence": [{"check": 1, "command": "cmd /c exit 0", "quote": ""},
                                 {"check": 2, "command": "cmd /c exit 0", "quote": ""}]},
                   ensure_ascii=False),
    ])
    prompts = []
    orig_ask = planner.ask

    async def fake_ask(cfg, prompt, system=None):
        prompts.append(prompt)
        try:
            return next(seq)
        except StopIteration:
            return json.dumps({"action": "done", "message": "没了"})

    planner.ask = fake_ask
    events = []
    orig_broadcast = server.broadcast
    orig_root = workspace.ROOT           # 跑完还原: use_root 不写设置, 别冲掉用户配的工作区

    async def fake_broadcast(payload):
        events.append(payload)

    server.broadcast = fake_broadcast
    try:
        workspace.use_root(TMP_WS)
        res = await server.api_world_verify(server.WorldVerifyRequest(
            task="修一下 main", answer="把返回值改成 1", applied=[{"op": "update", "path": "main.cpp"}],
            command="", max_rounds=4, timeout=20, force=True))
    finally:
        planner.ask = orig_ask
        server.broadcast = orig_broadcast
        workspace.use_root(orig_root)
    res = res if isinstance(res, dict) else json.loads(res.body.decode("utf-8"))
    print("验证结果:", json.dumps({k: res.get(k) for k in ("ok", "gave_up")}, ensure_ascii=False))
    print("轮次:", json.dumps([{k: r.get(k) for k in ("round", "action", "command", "code")} for r in res["rounds"]],
                             ensure_ascii=False))
    if not res.get("ok"):
        bad.append("本地模型验证没有判为通过: " + json.dumps(res, ensure_ascii=False)[:300])
    acts = [r.get("action") for r in res["rounds"]]
    # 【硬规矩】本地模型**不写代码**: 它给的 fix 会被拦成 fix-blocked, 文件内容一个字都不变
    if acts != ["run", "fix-blocked", "run", "done"]:
        bad.append("验证轮次不对(应 run->fix-blocked->run->done): " + json.dumps(acts, ensure_ascii=False))
    if not any(r.get("code") == 1 for r in res["rounds"]):
        bad.append("第一轮应该拿到退出码 1")
    if "int main(){return 1;}" in (TMP_WS / "main.cpp").read_text(encoding="utf-8"):
        bad.append("本地模型写代码竟然落盘了(它不许写代码)")
    if not any(r.get("action") == "fix-blocked" and (r.get("suggested") or []) for r in res["rounds"]):
        bad.append("被拦下的改动没有作为'建议'记下来")
    if not any("CMakeLists.txt" in p for p in prompts):
        bad.append("验证提示词里没有项目文件列表")
    if not any(e.get("stage") == "verify" and e.get("action") == "run-done" for e in events):
        bad.append("没有向前端推送验证事件: " + json.dumps(events[:3], ensure_ascii=False)[:300])
    if not any(e.get("action") == "fix-blocked" for e in events):
        bad.append("拦住本地模型写代码时没有通知界面")
    if not any(e.get("action") == "finished" for e in events):
        bad.append("验证结束时没有推送 finished 事件(刷新过的页面会一直转圈)")
    st = await server.api_world_state()
    print("world/state:", json.dumps(st, ensure_ascii=False)[:200])
    if not (st.get("verify") or {}).get("action") == "finished" or not st["verify"].get("ok"):
        bad.append("world/state 没有记下最近一次验证结果: " + json.dumps(st, ensure_ascii=False)[:200])
    if res.get("changed"):
        bad.append("本地模型没写任何代码, 却报了 changed: " + json.dumps(res.get("changed"), ensure_ascii=False))

    # 失败场景: error_log 要能直接发给网页模型(含命令/退出码/输出)
    seq2 = iter([
        json.dumps({"action": "run", "command": "cmd /c echo build-error-here & exit 1", "reason": "构建"}),
        json.dumps({"action": "fix", "message": "试着改一下", "files": [
            {"op": "update", "path": "main.cpp", "content": "int main(){return 2;}\n"}]}),
    ])

    async def fake_ask2(cfg, prompt, system=None):
        try:
            return next(seq2)
        except StopIteration:
            return json.dumps({"action": "fix", "message": "再改一下", "files": [
                {"op": "update", "path": "main.cpp", "content": "int main(){return 3;}\n"}]})

    planner.ask = fake_ask2
    try:
        workspace.use_root(TMP_WS)
        r3 = await server.api_world_verify(server.WorldVerifyRequest(
            task="修 main", answer="改成 2", max_rounds=2, timeout=20, force=True))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    r3 = r3 if isinstance(r3, dict) else json.loads(r3.body.decode("utf-8"))
    print("失败日志:", json.dumps(r3.get("error_log", "")[:220], ensure_ascii=False))
    if r3.get("ok"):
        bad.append("这个场景不该判为通过")
    elog = r3.get("error_log") or ""
    if "cmd /c echo build-error-here" not in elog or "退出码 1" not in elog or "build-error-here" not in elog:
        bad.append("error_log 里缺命令/退出码/输出: " + elog[:300])

    # 命令跑不了(比如 Linux 的 cat) -> 归为环境问题, 不当作项目报错
    seq3 = iter([
        json.dumps({"action": "run", "command": "cat missing.qml", "reason": "看文件"}),
        json.dumps({"action": "done", "message": "改用 Get-Content 看过了"}),
    ])

    async def fake_ask3(cfg, prompt, system=None):
        try:
            return next(seq3)
        except StopIteration:
            return json.dumps({"action": "done", "message": "ok"})

    planner.ask = fake_ask3
    try:
        workspace.use_root(TMP_WS)
        r4 = await server.api_world_verify(server.WorldVerifyRequest(
            task="x", answer="y", applied=[], max_rounds=3, timeout=20, force=True))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    r4 = r4 if isinstance(r4, dict) else json.loads(r4.body.decode("utf-8"))
    acts4 = [r.get("action") for r in r4["rounds"]]
    print("命令跑不了时:", json.dumps(acts4, ensure_ascii=False), "| real_error:", r4.get("real_error"))
    if "tool-error" not in acts4:
        bad.append("Linux 命令失败没有被识别成环境问题: " + json.dumps(acts4, ensure_ascii=False))
    if r4.get("real_error"):
        bad.append("环境问题被当成了项目报错")

    # PowerShell 解析错误(用户日志里的那段"此时不应有 f")也要算环境问题
    async def fake_ask4(cfg, prompt, system=None):
        return json.dumps({"action": "run", "command": "for f in a b; do cat $f; done", "reason": "看文件"})

    planner.ask = fake_ask4
    try:
        workspace.use_root(TMP_WS)
        r5 = await server.api_world_verify(server.WorldVerifyRequest(
            task="x", answer="y", applied=[], max_rounds=1, timeout=20, force=True))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    r5 = r5 if isinstance(r5, dict) else json.loads(r5.body.decode("utf-8"))
    acts5 = [r.get("action") for r in r5["rounds"]]
    print("PowerShell 语法错:", json.dumps(acts5, ensure_ascii=False), "| real_error:", r5.get("real_error"))
    if acts5 != ["tool-error"] or r5.get("real_error"):
        bad.append("PowerShell 语法/命令错误被当成了项目报错: " + json.dumps(r5, ensure_ascii=False)[:200])

    # 输出编码: GBK 的字节要能正确解码(不能是乱码)
    gbk = "‘sed’ 不是内部或外部命令".encode("gbk")
    dec = server._decode_out(gbk)
    print("解码:", dec)
    if "不是内部或外部命令" not in dec:
        bad.append("GBK 输出解码成了乱码: " + dec)

    # 危险命令必须被拦住
    planner.ask = fake_ask2 = None
    async def fake_danger(cfg, prompt, system=None):
        return json.dumps({"action": "run", "command": "rm -rf /", "reason": "x"})
    planner.ask = fake_danger
    try:
        workspace.use_root(TMP_WS)
        r2 = await server.api_world_verify(server.WorldVerifyRequest(task="x", answer="y", max_rounds=2,
                                                                   force=True))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    r2 = r2 if isinstance(r2, dict) else json.loads(r2.body.decode("utf-8"))
    print("危险命令:", json.dumps(r2["rounds"][0], ensure_ascii=False)[:200])
    if r2["rounds"][0].get("action") != "blocked" or r2.get("gave_up") is not True:
        bad.append("危险命令没有被拦住: " + json.dumps(r2, ensure_ascii=False)[:300])
    if r2.get("reason") != "blocked":
        bad.append("被拦截时的原因没说清: " + json.dumps(r2.get("reason"), ensure_ascii=False))

    # 需求驱动的验收点: 第 1 轮给的 checks 要沿用到后面的提示词里, 不能中途换题
    seq6 = iter([
        json.dumps({"checks": [{"id": 1, "expect": "同一内容复制两次 -> 历史里只有一条",
                                "how": "跑 ClipboardStore 的临时验证程序"}],
                    "action": "run", "command": "cmd /c exit 0", "verifies": [1]}, ensure_ascii=False),
        json.dumps({"action": "done", "message": "验收点 1 通过",
                    "evidence": "验收点 [1]: 跑 cmd /c exit 0, 退出码 0; 输出里没有重复项"},
                   ensure_ascii=False),
    ])
    prompts6 = []

    async def fake_ask6(cfg, prompt, system=None):
        prompts6.append(prompt)
        try:
            return next(seq6)
        except StopIteration:
            return json.dumps({"action": "done", "evidence": "没了"})

    planner.ask = fake_ask6
    try:
        workspace.use_root(TMP_WS)
        r6 = await server.api_world_verify(server.WorldVerifyRequest(
            task="给剪贴板历史去重", answer="用 QSet 去重", applied=[], max_rounds=3, timeout=20,
            force=True))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    r6 = r6 if isinstance(r6, dict) else json.loads(r6.body.decode("utf-8"))
    print("验收点:", json.dumps(r6.get("checks"), ensure_ascii=False))
    if not r6.get("checks") or "只有一条" not in r6["checks"]:
        bad.append("模型给的验收点没有被记下来: " + json.dumps(r6, ensure_ascii=False)[:200])
    if len(prompts6) < 2 or "只有一条" not in prompts6[1]:
        bad.append("第 2 轮的提示词没有沿用已定验收点: " + (prompts6[1][:200] if len(prompts6) > 1 else "没有第 2 轮"))
    if not r6.get("ok") or r6.get("reason") != "ok":
        bad.append("有验收点+证据的验证应该判通过: " + json.dumps(r6, ensure_ascii=False)[:250])

    # 只有读文件就宣布 done -> 不接受(读文件不算验证); 轮数用完也要如实说原因
    seq7 = iter([
        json.dumps({"checks": [{"id": 1, "expect": "去重生效", "how": "-"}],
                    "action": "run",
                    "command": 'powershell -NoProfile -Command "Get-Content -Raw main.cpp"'},
                   ensure_ascii=False),
        json.dumps({"action": "done", "message": "我看过代码了, 应该没问题"}, ensure_ascii=False),
    ])
    prompts7 = []

    async def fake_ask7(cfg, prompt, system=None):
        prompts7.append(prompt)
        try:
            return next(seq7)
        except StopIteration:
            return json.dumps({"action": "run",
                               "command": 'powershell -NoProfile -Command "Get-Content -Raw main.cpp"'})

    planner.ask = fake_ask7
    try:
        workspace.use_root(TMP_WS)
        r7 = await server.api_world_verify(server.WorldVerifyRequest(
            task="给剪贴板历史去重", answer="用 QSet 去重", applied=[], max_rounds=3, timeout=20,
            force=True))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    r7 = r7 if isinstance(r7, dict) else json.loads(r7.body.decode("utf-8"))
    acts7 = [r.get("action") for r in r7["rounds"]]
    print("只读文件场景:", json.dumps(acts7, ensure_ascii=False), "| reason:", r7.get("reason"),
          "|", json.dumps(r7.get("last_output", ""), ensure_ascii=False)[:120])
    if r7.get("ok"):
        bad.append("只看文件就宣布 done 不该判通过: " + json.dumps(r7, ensure_ascii=False)[:250])
    if "no-evidence" not in acts7:
        bad.append("看文件当验证没有被拦下来: " + json.dumps(acts7, ensure_ascii=False))
    if r7.get("reason") not in ("no-evidence", "rounds-exhausted"):
        bad.append("只读文件场景的原因不对: " + json.dumps(r7.get("reason"), ensure_ascii=False))
    if not any("没有证据不能算通过" in p for p in prompts7):
        bad.append("没有把它打回去让它真跑一条命令(提示词里没有这句)")
    if not r7.get("checks"):
        bad.append("验收点没被记住: " + json.dumps(r7, ensure_ascii=False)[:200])

    # 命令都成功跑过、但轮数用完也没宣布通过 -> 不能再说成"命令在这台机器上跑不了"
    async def fake_ask8(cfg, prompt, system=None):
        return json.dumps({"action": "run", "command": "cmd /c exit 0", "verifies": [1]})

    ev8 = []

    async def cap8(payload):
        ev8.append(payload)

    planner.ask = fake_ask8
    server.broadcast = cap8
    try:
        workspace.use_root(TMP_WS)
        r8 = await server.api_world_verify(server.WorldVerifyRequest(
            task="x", answer="y", applied=[], max_rounds=2, timeout=20, force=True))
    finally:
        planner.ask = orig_ask
        server.broadcast = orig_broadcast
        workspace.use_root(orig_root)
    r8 = r8 if isinstance(r8, dict) else json.loads(r8.body.decode("utf-8"))
    fin8 = [e for e in ev8 if e.get("action") == "finished"][-1]
    print("轮数用完:", json.dumps({"reason": r8.get("reason"), "text": fin8.get("text")}, ensure_ascii=False))
    if r8.get("reason") != "rounds-exhausted":
        bad.append("轮数用完了原因却写成别的: " + json.dumps(r8.get("reason"), ensure_ascii=False))
    if "跑不了" in (fin8.get("text") or ""):
        bad.append("命令明明都成功了, 结束语还在赖环境: " + json.dumps(fin8, ensure_ascii=False)[:200])
    if (server._world_state.get("verify") or {}).get("reason") != "rounds-exhausted":
        bad.append("world/state 没记下结束原因: "
                   + json.dumps(server._world_state.get("verify"), ensure_ascii=False)[:200])

    # ---------- 假通过防线: 用户实际踩过的四种"本地模型确认跑通了" ----------
    async def verify_once(fake, rounds=3):
        planner.ask = fake
        try:
            workspace.use_root(TMP_WS)
            r = await server.api_world_verify(server.WorldVerifyRequest(
                task="给剪贴板历史去重", answer="用 QSet 去重", applied=[], max_rounds=rounds, timeout=20,
                force=True))
        finally:
            planner.ask = orig_ask
            workspace.use_root(orig_root)
        return r if isinstance(r, dict) else json.loads(r.body.decode("utf-8"))

    # (1) 包装命令的假成功: shell 退出码 0, 真实退出码写在输出里 -> 必须按失败算
    async def fake_masked(cfg, prompt, system=None):
        return json.dumps({"checks": [{"id": 1, "expect": "构建成功", "how": "cmake"}],
                           "action": "run",
                           "command": 'python -c "print(\'BUILD_EXIT=1\')"',
                           "verifies": [1]}, ensure_ascii=False)

    r11 = await verify_once(fake_masked, rounds=2)
    k11 = [(x.get("action"), x.get("code")) for x in r11["rounds"]]
    print("包装命令的假成功:", json.dumps(k11, ensure_ascii=False), "| reason:", r11.get("reason"))
    if not any(c == 1 for _, c in k11):
        bad.append("shell 退出码 0、输出里写着 BUILD_EXIT=1 的命令没被按失败算: "
                   + json.dumps(k11, ensure_ascii=False))
    if not r11.get("real_error") or r11.get("ok"):
        bad.append("包装命令的假成功被当成了成功: " + json.dumps(r11, ensure_ascii=False)[:250])
    if not any("shell 退出码是 0" in str(x.get("output") or "") for x in r11["rounds"]):
        bad.append("没把「真实退出码被藏起来了」这件事说清楚")

    # (2) 只有编译证据 -> 行为性验收点不算通过(构建通过 ≠ 需求满足)
    build_notes = []

    async def fake_build_only(cfg, prompt, system=None):
        build_notes.append(prompt)
        if len(build_notes) == 1:
            return json.dumps({"checks": [{"id": 1, "expect": "右键菜单在鼠标处弹出", "how": "跑程序看"}],
                               "action": "run", "command": "cmake --version", "verifies": [1]},
                              ensure_ascii=False)
        return json.dumps({"action": "done", "message": "构建通过, 应该没问题",
                           "evidence": "验收点 [1]: 跑 cmake --version, 退出码 0, 说明菜单没问题"},
                          ensure_ascii=False)

    r12 = await verify_once(fake_build_only)
    acts12 = [x.get("action") for x in r12["rounds"]]
    print("只有编译证据:", json.dumps(acts12, ensure_ascii=False), "| reason:", r12.get("reason"),
          "| behavior_ok:", r12.get("behavior_ok"), "| build_ok:", r12.get("build_ok"))
    if r12.get("ok"):
        bad.append("只有编译证据就宣布行为性验收点通过: " + json.dumps(r12, ensure_ascii=False)[:250])
    if "weak-evidence" not in acts12 or r12.get("reason") != "weak-evidence":
        bad.append("只有编译证据没有被拦下来: " + json.dumps(r12, ensure_ascii=False)[:250])
    if not any("只跑了编译" in p for p in build_notes):
        bad.append("没有把「编译不算行为证据」写进打回提示词")

    # (3) 证据里引用的输出必须是真实输出(不能凭空编一句"ALL TESTS PASSED")
    fake_notes = []

    async def fake_quote(cfg, prompt, system=None):
        fake_notes.append(prompt)
        if len(fake_notes) == 1:
            return json.dumps({"checks": [{"id": 1, "expect": "去重生效", "how": "跑测试"}],
                               "action": "run", "command": "cmd /c exit 0", "verifies": [1]},
                              ensure_ascii=False)
        return json.dumps({"action": "done", "message": "都过了",
                           "evidence": "验收点 [1]: 跑 cmd /c exit 0, 输出「ALL TESTS PASSED」"},
                          ensure_ascii=False)

    r13 = await verify_once(fake_quote)
    acts13 = [x.get("action") for x in r13["rounds"]]
    print("凭空引用输出:", json.dumps(acts13, ensure_ascii=False), "| reason:", r13.get("reason"))
    if r13.get("ok"):
        bad.append("凭空引用的输出被当成了证据: " + json.dumps(r13, ensure_ascii=False)[:250])
    if not any("真实输出里找不到" in p for p in fake_notes):
        bad.append("没有识破「引用的输出不在真实输出里」")

    # (4) 没有验收点就宣布 done -> 不接受(没有判定标准)
    nc = {"n": 0}

    async def fake_no_checks(cfg, prompt, system=None):
        nc["n"] += 1
        if nc["n"] == 1:
            return json.dumps({"action": "run", "command": "cmd /c exit 0"})
        return json.dumps({"action": "done", "message": "没问题了",
                           "evidence": "跑 cmd /c exit 0 退出码 0"})

    r14 = await verify_once(fake_no_checks)
    acts14 = [x.get("action") for x in r14["rounds"]]
    print("没有验收点:", json.dumps(acts14, ensure_ascii=False), "| reason:", r14.get("reason"))
    if r14.get("ok"):
        bad.append("没有验收点也判通过: " + json.dumps(r14, ensure_ascii=False)[:250])
    if "no-checks" not in acts14 or r14.get("reason") != "no-checks":
        bad.append("没定验收点没有被拦下来: " + json.dumps(r14, ensure_ascii=False)[:250])

    # (5) 拿工作区外的旧快照副本当验证对象 -> 当场点破; 每轮验证要留痕可复核
    async def fake_wrong_copy(cfg, prompt, system=None):
        return json.dumps({"checks": [{"id": 1, "expect": "去重生效", "how": "跑测试"}],
                           "action": "run",
                           "command": "cmake -S smartclip_verify -B .verify/build", "verifies": [1]},
                          ensure_ascii=False)

    r15 = await verify_once(fake_wrong_copy, rounds=2)
    lo = str(r15.get("last_output") or "")
    print("别的副本:", json.dumps({"reason": r15.get("reason"), "root": r15.get("root")}, ensure_ascii=False))
    if "工作区根是" not in lo or "smartclip_verify" not in lo:
        bad.append("没有点破「在别的副本上验证」: " + lo[:250])
    audit = r15.get("audit") or ""
    print("审计留痕:", audit)
    if not audit or not (Path(audit) / "verdict.json").exists():
        bad.append("验证没有留痕(事后没法复核它跑过什么): " + str(audit))
    else:
        vj = json.loads((Path(audit) / "verdict.json").read_text(encoding="utf-8"))
        if str(TMP_WS).lower() not in str(vj.get("root", "")).lower():
            bad.append("留痕里记的工作区根不对: " + str(vj.get("root")))
        if not vj.get("rounds"):
            bad.append("留痕里没有命令/输出: " + json.dumps(vj, ensure_ascii=False)[:200])

    # 临时验证脚手架: 它自己造的 .verify/ 要收掉; 本来就有的目录一根汗毛都不动
    (TMP_WS / ".verify").mkdir(parents=True, exist_ok=True)
    (TMP_WS / ".verify" / "keep.txt").write_text("用户的", encoding="utf-8")
    planner.ask = fake_ask8
    try:
        workspace.use_root(TMP_WS)
        await server.api_world_verify(server.WorldVerifyRequest(task="x", answer="y", max_rounds=1,
                                                               timeout=20, force=True))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    if not (TMP_WS / ".verify" / "keep.txt").exists():
        bad.append("本来就在的 .verify/ 被误删了")
    shutil.rmtree(TMP_WS / ".verify", ignore_errors=True)
    (TMP_WS / ".verify").mkdir()
    (TMP_WS / ".verify" / "check.py").write_text("print(1)", encoding="utf-8")
    if not server._cleanup_verify_dir(TMP_WS / ".verify", False) or (TMP_WS / ".verify").exists():
        bad.append("验证阶段自己造的 .verify/ 没有被收掉")
    # "证据"的判定: 看文件不算, 构建/测试算
    if server._evidence_run('powershell -NoProfile -Command "Get-Content -Raw a.cpp"', 0):
        bad.append("读文件被当成了验证证据")
    if server._evidence_run("cmd /c type src\\a.cpp", 0):
        bad.append("cmd /c type 读文件被当成了验证证据")
    if not server._evidence_run("cmake --build build_verify --config Release", 0):
        bad.append("构建命令没被当成验证证据")
    if not server._evidence_run('powershell -NoProfile -Command "Get-Content a; cmake --build ."', 0):
        bad.append("读文件+构建的复合命令应该算证据")
    if server._evidence_run("cmake --build build_verify", 1):
        bad.append("退出码非 0 的命令被当成了证据")
    # "证据种类": 编译/静态检查 != 真的执行了行为
    if server._evidence_kind("cmake --build build_verify --config Release") != "build":
        bad.append("cmake 构建没有被认成「编译证据」")
    if server._evidence_kind("qmllint x.qml") != "build":
        bad.append("qmllint 没有被认成「编译证据」")
    if server._evidence_kind("build/Debug/SmartClip.exe") != "behavior":
        bad.append("跑刚构建出来的程序应该算「行为证据」")
    if server._evidence_kind("pytest tests/") != "behavior":
        bad.append("跑测试应该算「行为证据」")
    if server._evidence_kind('powershell -NoProfile -Command "Get-Content -Raw a.cpp"') != "":
        bad.append("读文件不该有「证据种类」")
    # 证据接地: 点名这次跑过的命令 + 逐条验收点 + 引用的输出必须是原文
    r_ok = [{"action": "run", "command": "pytest tests/", "code": 0, "output": "2 passed in 0.4s"}]
    if server._grounding_problems("验收点 [1]: 跑 pytest tests/ 输出「2 passed in 0.4s」", "- [1] 去重生效", r_ok):
        bad.append("合规的证据被误判成不接地")
    if not server._grounding_problems("我看过代码了, 应该没问题", "- [1] 去重生效", r_ok):
        bad.append("没点名命令的「证据」没被拦下来")
    if not server._grounding_problems("验收点 [1]: 跑 pytest tests/ 输出「ALL TESTS PASSED」",
                                      "- [1] 去重生效", r_ok):
        bad.append("引用假输出的「证据」没被拦下来")
    if not server._grounding_problems("跑 pytest tests/ 都过了", "- [1] 去重生效\n- [2] 边界正确", r_ok):
        bad.append("漏掉某条验收点的「证据」没被拦下来")
    # 包装命令里藏着的真实退出码 / 工作区外的路径
    if server._wrapper_exit_code("...\nCMAKE_EXIT=2\n") != 2:
        bad.append("没从输出里认出真实退出码")
    if server._wrapper_exit_code("all good\nEXITCODE=0\n") != 0:
        bad.append("退出码 0 被认成了失败")
    if not server._missing_paths("cmake -S smartclip_verify -B .verify/build"):
        bad.append("没认出命令里工作区下不存在的路径(旧快照副本)")
    # ChatGPT 只给思路、没给代码时, 界面必须如实说"代码是本地模型自编的"
    if server._answer_has_code("可以, 按你这个 EditorArea.qml, 需要改 3 个点:\n"
                               "1. 设置 selectionColor 和 selectedTextColor\n"
                               "2. 给菜单定制样式\n3. 用鼠标坐标 popup()"):
        bad.append("纯说明的回答被当成了「带代码」")
    if not server._answer_has_code('把这段换成:\n```qml\nselectionColor: "#3d78b8"\n```'):
        bad.append("带代码块的回答没被认出来")

    # 验证失败时先逼本地模型自己修: 一次都没动手改 -> 追加自修轮数, 并把"必须用 fix"写进提示词
    async def fake_ask9(cfg, prompt, system=None):
        prompts9.append(prompt)
        return json.dumps({"action": "run", "command": "cmd /c echo boom & exit 1"})

    prompts9 = []
    planner.ask = fake_ask9
    try:
        workspace.use_root(TMP_WS)
        r9 = await server.api_world_verify(server.WorldVerifyRequest(
            task="给剪贴板历史去重", answer="用 QSet 去重", applied=[], max_rounds=2, timeout=20,
            force=True))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    r9 = r9 if isinstance(r9, dict) else json.loads(r9.body.decode("utf-8"))
    print("没动手就收工:", json.dumps({"rounds": len(r9["rounds"]), "repair_rounds": r9.get("repair_rounds"),
                                      "local_fixes": r9.get("local_fixes"), "reason": r9.get("reason")},
                                     ensure_ascii=False))
    if len(r9["rounds"]) <= 2:
        bad.append("一次都没动手改, 却没追加自修轮数: " + json.dumps(r9["rounds"], ensure_ascii=False)[:200])
    # 追加的轮数现在是"换命令继续找证据", 不许再逼它改代码
    if not any("换一条能真正跑出证据的命令" in p for p in prompts9):
        bad.append("没拿出结论时提示词里没有让它换命令找证据")
    if any("直接用 fix" in p or "必须自己用 fix" in p for p in prompts9):
        bad.append("提示词还在让本地模型自己用 fix 改代码: " + str([p[-160:] for p in prompts9])[:200])
    if r9.get("local_fixes") or r9.get("tried_local_fix"):
        bad.append("一次都没修过却报了 local_fixes: " + json.dumps(r9, ensure_ascii=False)[:200])
    if r9.get("reason") != "real-error":
        bad.append("有真实报错时原因不对: " + json.dumps(r9.get("reason"), ensure_ascii=False))

    # 本地模型想改代码 -> 拦下来(不落盘), 只如实记下"它想改几次"
    seq10 = iter([
        json.dumps({"action": "run", "command": "cmd /c echo still-bad & exit 1"}),
        json.dumps({"action": "fix", "message": "改了", "files": [
            {"op": "update", "path": "main.cpp", "content": "int main(){return 9;}\n"}]}),
    ])

    async def fake_ask10(cfg, prompt, system=None):
        try:
            return next(seq10)
        except StopIteration:
            return json.dumps({"action": "run", "command": "cmd /c echo still-bad & exit 1"})

    planner.ask = fake_ask10
    try:
        workspace.use_root(TMP_WS)
        r10 = await server.api_world_verify(server.WorldVerifyRequest(
            task="x", answer="y", applied=[], max_rounds=3, timeout=20, force=True))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    r10 = r10 if isinstance(r10, dict) else json.loads(r10.body.decode("utf-8"))
    print("自己修过:", json.dumps({"local_fixes": r10.get("local_fixes"),
                                   "tried_local_fix": r10.get("tried_local_fix"),
                                   "reason": r10.get("reason"),
                                   "changed": [a.get("path") for a in (r10.get("changed") or [])]},
                                  ensure_ascii=False))
    if not r10.get("local_fixes") or not r10.get("tried_local_fix"):
        bad.append("它想改代码却没记下次数: " + json.dumps(r10, ensure_ascii=False)[:250])
    if "int main(){return 9;}" in (TMP_WS / "main.cpp").read_text(encoding="utf-8"):
        bad.append("本地模型的自修竟然落盘了(它不许写代码)")
    if not any(r.get("action") == "fix-blocked" for r in r10.get("rounds") or []):
        bad.append("想改代码的轮次没有被标成 fix-blocked: " + json.dumps(r10.get("rounds"), ensure_ascii=False)[:200])

    shutil.rmtree(TMP_WS, ignore_errors=True)
    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("WORLD_VERIFY_OK (本地模型只跑命令/看输出, 不写代码; 按需求定验收点->找证据; "
          "证据要点名真跑过的命令+引用真实输出; 编译不算行为证据; 包装命令藏的退出码按失败算; "
          "拿工作区外的旧副本验证会被点破; 每轮验证留痕可复核; 危险命令被拦)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
