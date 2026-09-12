"""回归: 那次"本地模型确认项目跑通了"的假通过不能再发生 —— 复现用户事故现场。

事故现场(来自真实记录):
  1) ChatGPT 只回了"需要改 3 个点"的思路, 一个字符代码都没给;
  2) 本地模型自己把 qml/components/EditorArea.qml 整份重写(预览行 "+167/-616 行 · 5541B"),
     项目根目录还多出一个 27 字节的同名残file;
  3) 验证阶段拿工作区**外面的旧副本** smartclip_verify 当验证对象跑 cmake, 命令写成
     `... ; Write-Output ('CMAKE_EXIT=' + $LASTEXITCODE)` —— shell 退出码恒为 0, 满屏 ✓ 其实全失败;
  4) done 的证据是它自己写的一句话("smartclip_verify 的 Release 构建实跑成功并产出 SmartClip.exe,
     且改动后的 EditorArea.qml 已被 qmlcachegen 实际编译进模块"), 没有任何一条能被核对。

本脚本断言: 这四步现在都拦得住, 且验证过程留痕可复核。全程替身, 不联网。
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import engineer, planner, server, workspace  # noqa: E402

TMP_WS = ROOT / ".tmp" / "fake-pass-ws"
BIG = "// 行号与内容对齐\n" * 320          # 640 行, 跟事故里的文件规模相近
STUB = "// 残file\n" * 30                  # ~300 字符(远小于 1/3)
INCIDENT_EVIDENCE = ("三条验收点均通过: smartclip_verify 的 Release 构建实跑成功并产出 SmartClip.exe, "
                     "且改动后的 EditorArea.qml 已被 qmlcachegen 实际编译进模块")
WRAP_CMD = ("powershell -NoProfile -Command \"cmake -S smartclip_verify -B .verify/build; "
            "Write-Output ('CMAKE_EXIT=' + $LASTEXITCODE)\"")
TASK = "选中内容显示成蓝色; 右键菜单背景改成灰黑色; 菜单在鼠标右键处弹出"
CHECKS = [{"id": 1, "expect": "选中文本显示成蓝色", "how": "跑起来看"},
          {"id": 2, "expect": "右键菜单背景是灰黑色", "how": "跑起来看"},
          {"id": 3, "expect": "菜单在鼠标右键处弹出", "how": "跑起来看"}]


def _same(p, q) -> bool:
    return os.path.normcase(str(p)) == os.path.normcase(str(q))


async def main() -> int:
    bad = []
    shutil.rmtree(TMP_WS, ignore_errors=True)
    (TMP_WS / "qml" / "components").mkdir(parents=True)
    (TMP_WS / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
    target = TMP_WS / "qml" / "components" / "EditorArea.qml"
    target.write_text(BIG, encoding="utf-8")

    orig_root = workspace.ROOT
    orig_ask = planner.ask
    workspace.use_root(TMP_WS)
    try:
        # ---------- (2) 落盘: 大面积截断 + 根目录残file 都必须被拦下, 不许动真文件 ----------
        applied, skipped, _ = engineer.apply_manifest({"files": [
            {"op": "update", "path": "qml/components/EditorArea.qml", "content": STUB},
            {"op": "update", "path": "EditorArea.qml", "content": "// v3\n// 又改了一次\n"}]})
        print("落盘:", json.dumps({"applied": applied, "skipped": skipped}, ensure_ascii=False)[:400])
        if applied:
            bad.append("疑似毁坏的写入居然落盘了: " + json.dumps(applied, ensure_ascii=False))
        if len(skipped) != 2 or not any("疑似截断" in s for s in skipped) or \
                not any("路径写错" in s for s in skipped):
            bad.append("两条疑似毁坏没被分别拦下: " + json.dumps(skipped, ensure_ascii=False))
        if target.read_text(encoding="utf-8") != BIG:
            bad.append("真文件被截断了")
        if (TMP_WS / "EditorArea.qml").exists():
            bad.append("根目录残file被写出来了")
        pv = engineer.preview_manifest({"files": [
            {"op": "update", "path": "qml/components/EditorArea.qml", "content": STUB}]})
        if pv[0].get("op") != "warn" or not pv[0].get("force") or pv[0].get("realOp") != "update":
            bad.append("确认框没把这条标成可 force 的 warn: " + json.dumps(pv[0], ensure_ascii=False)[:200])

        # ---------- (3)(4) 验证: 包装命令藏的退出码 / 旧副本 / 自编证据 -> 都不算通过 ----------
        seq = iter([
            json.dumps({"checks": CHECKS, "action": "run", "command": WRAP_CMD, "verifies": [1, 2, 3]},
                       ensure_ascii=False),
            json.dumps({"action": "done", "message": "三条验收点均通过", "evidence": INCIDENT_EVIDENCE},
                       ensure_ascii=False),
        ])
        prompts = []

        async def fake_ask(cfg, prompt, system=None):
            prompts.append(prompt)
            try:
                return next(seq)
            except StopIteration:
                return json.dumps({"action": "done", "evidence": INCIDENT_EVIDENCE}, ensure_ascii=False)

        planner.ask = fake_ask
        try:
            r = await server.api_world_verify(server.WorldVerifyRequest(
                task=TASK, answer="可以, 按你这个 EditorArea.qml, 需要改 3 个点…", applied=[],
                max_rounds=3, timeout=120))
        finally:
            planner.ask = orig_ask
        r = r if isinstance(r, dict) else json.loads(r.body.decode("utf-8"))
        runs = [x for x in r["rounds"] if x.get("action") == "run"]
        print("事故复现:", json.dumps({"ok": r.get("ok"), "reason": r.get("reason"),
                                       "codes": [x.get("code") for x in runs]}, ensure_ascii=False))
        if r.get("ok"):
            bad.append("这次事故被判成了通过: " + json.dumps(r, ensure_ascii=False)[:250])
        if not runs or runs[0].get("code") != 1:
            bad.append("包装命令藏起来的真实退出码(CMAKE_EXIT=1)没被认出来: "
                       + json.dumps(runs, ensure_ascii=False)[:250])
        if not r.get("real_error"):
            bad.append("包装命令的失败没有被算成真实报错")
        out0 = str(runs[0].get("output") or "") if runs else ""
        if "shell 退出码是 0" not in out0 or "CMAKE_EXIT" not in out0:
            bad.append("没说清「退出码被包装命令藏起来了」: " + out0[:200])
        if "工作区根是" not in out0 or "smartclip_verify" not in out0:
            bad.append("没点破「它在工作区外的旧副本上验证」: " + out0[:250])
        if not any("旧快照副本" in p for p in prompts):
            bad.append("这个提醒没有回给模型(提示词里没有): "
                       + (prompts[1][:200] if len(prompts) > 1 else "没有第 2 轮"))
        if not any("没有证据不能算通过" in p for p in prompts):
            bad.append("没有把「这套证据不成立」写回给模型")
        audit = r.get("audit") or ""
        vj = Path(audit) / "verdict.json"
        if not audit or not vj.exists():
            bad.append("这次验证没有留痕, 事后无法复核: " + str(audit))
        else:
            data = json.loads(vj.read_text(encoding="utf-8"))
            if not _same(data.get("root", ""), TMP_WS):
                bad.append("留痕里的工作区根不对: " + str(data.get("root")))
            if not any(x.get("command") == WRAP_CMD for x in (data.get("rounds") or [])):
                bad.append("留痕里没有那条命令: " + json.dumps(data.get("rounds"), ensure_ascii=False)[:200])

        # ---------- 只有编译证据 + 自编证据(事故第 4 步) -> weak-evidence ----------
        seq2 = iter([
            json.dumps({"checks": [CHECKS[2]], "action": "run", "command": "cmake --version",
                        "verifies": [1]}, ensure_ascii=False),
            json.dumps({"action": "done", "message": "三条验收点均通过", "evidence": INCIDENT_EVIDENCE},
                       ensure_ascii=False),
        ])
        build_prompts = []

        async def fake_ask2(cfg, prompt, system=None):
            build_prompts.append(prompt)
            try:
                return next(seq2)
            except StopIteration:
                return json.dumps({"action": "done", "evidence": INCIDENT_EVIDENCE}, ensure_ascii=False)

        planner.ask = fake_ask2
        try:
            r2 = await server.api_world_verify(server.WorldVerifyRequest(
                task=TASK, answer="需要改 3 个点…", applied=[], max_rounds=3, timeout=60))
        finally:
            planner.ask = orig_ask
        r2 = r2 if isinstance(r2, dict) else json.loads(r2.body.decode("utf-8"))
        print("自编证据:", json.dumps({"ok": r2.get("ok"), "reason": r2.get("reason"),
                                       "behavior_ok": r2.get("behavior_ok"),
                                       "build_ok": r2.get("build_ok")}, ensure_ascii=False))
        if r2.get("ok"):
            bad.append("自编证据被接受了: " + json.dumps(r2, ensure_ascii=False)[:250])
        if r2.get("reason") != "weak-evidence":
            bad.append("自编证据/只有编译证据没判成 weak-evidence: "
                       + json.dumps(r2, ensure_ascii=False)[:250])
        if not any("证据不被接受" in p for p in build_prompts):
            bad.append("没有把「证据不成立」的具体原因写回给模型")
        if r2.get("behavior_ok") or not r2.get("build_ok"):
            bad.append("证据种类记错了: "
                       + json.dumps({k: r2.get(k) for k in ("behavior_ok", "build_ok")}, ensure_ascii=False))
    finally:
        workspace.use_root(orig_root)
        shutil.rmtree(TMP_WS, ignore_errors=True)

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("FAKE_PASS_OK (截断/残file落盘被拦; 包装命令藏的退出码按失败算; 旧副本验证被点破; "
          "自编证据/只有编译证据不接受; 每轮验证留痕可复核)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
