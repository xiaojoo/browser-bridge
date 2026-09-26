r"""回归: "shell 退出码 0, 但程序根本没启动" 不能再被当成成功, 更不能被当成行为证据。

事故现场(2026-09-26 03:44, 工作区 H:/test/mimoclaw_workspace, 留痕 .tmp/verify-audit/20260926-034418):
  第 2 轮 `g++ -std=c++17 ...` —— PowerShell 报「无法将"g++"项识别为…」, 但整条复合命令的
       shell 退出码是 0, 而且它自己打印的 `BUILD_EXIT=` 后面是**空的**($LASTEXITCODE 为 null);
  第 4 轮 `Select-String ...\CMakeCache.txt` —— 路径不存在, 同样退出码 0。
  结果: 界面和留痕里全是「退出码 0 / ✓」, 其中 3 轮还被 `_evidence_kind` 判成 **behavior**(行为证据)。
  也就是说: 只要它第 3 轮肯说 done, 就会拿"g++ 不存在"当证据宣布通过并写盘。

根因是 `_tool_failure()` 第一行 `if code == 0: return False` —— 表里明明有"无法将"、
"commandnotfoundexception" 这些词, 但退出码是 0 时根本不看。
全程替身规划模型, 命令是真的在本机 PowerShell 里跑的。
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import planner, server, workspace  # noqa: E402

TMP_WS = ROOT / ".tmp" / "tool-fail-ws"
CHECKS = [{"id": 1, "expect": "排序输出严格升序", "how": "跑起来看"}]

# 留痕里那两段真实输出(截了尾巴, 形状没动)
GPP_MISSING = ("g++ : 无法将“g++”项识别为 cmdlet、函数、脚本文件或可运行程序的名称。请检查名称的拼写\n"
               "    + CategoryInfo          : ObjectNotFound: (g++:String) [], "
               "CommandNotFoundException\n"
               "    + FullyQualifiedErrorId : CommandNotFoundException\n"
               "BUILD_EXIT=\n")
PATH_MISSING = ("Select-String : 找不到路径“H:\\test\\mimoclaw_workspace\\build\\CMakeCache.txt”，"
                "因为该路径不存在。\n    + CategoryInfo : ObjectNotFound: ...\n"
                "    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands."
                "SelectStringCommand\nINSPECT_EXIT=0\n")
# 反向用例: 这些**不该**被当成"程序没启动"
NORMAL = [
    ("测试里出现 not found 字样", "running tests...\ntest_missing_key ... 404 not found ok\n12 passed\nEXIT=0"),
    ("grep 正常报没这个文件", "grep: settings.json: No such file or directory\nEXIT=1"),
    ("构建真的成功", "[2/5] Building main.obj\ntest_1.cpp\n    TEST PASS\nCMAKE_EXIT=0"),
    ("退出码标记是 0 不是空", "ok\nBUILD_EXIT=0\n"),
]


async def main() -> int:
    bad = []
    # ---------- 1) 纯函数: 认得出来的要认得出来, 认不得的不许乱认 ----------
    for name, out in (("g++ 不存在", GPP_MISSING), ("路径不存在", PATH_MISSING)):
        hit = server._never_ran(out)
        print("1) %-10s -> %r" % (name, hit[:48]))
        if not hit:
            bad.append("%s 没被认成「程序根本没启动」" % name)
    for name, out in NORMAL:
        hit = server._never_ran(out)
        print("1) %-10s -> %r" % (name, hit[:48]))
        if hit:
            bad.append("误伤: %s 被认成「程序没启动」(%r)" % (name, hit))
    if server._never_ran("nothing here\nRUN_EXIT=\n") == "":
        bad.append("`RUN_EXIT=` 后面是空的也没认出来")

    # ---------- 2) 端到端: 真的在本机 PowerShell 里跑一条不存在的命令 ----------
    shutil.rmtree(TMP_WS, ignore_errors=True)
    TMP_WS.mkdir(parents=True, exist_ok=True)
    (TMP_WS / "main.cpp").write_text("int main(){}\n", encoding="utf-8")
    orig_root, orig_ask = workspace.ROOT, planner.ask
    workspace.use_root(TMP_WS)
    bogus = ("powershell -NoProfile -Command \"gpt-no-such-tool-xyz --version; "
             "'TOOL_EXIT=' + $LASTEXITCODE\"")
    seq = iter([json.dumps({"checks": CHECKS, "action": "run", "command": bogus,
                            "verifies": [1]}, ensure_ascii=False)])
    prompts = []

    async def fake_ask(cfg, prompt, system=None):
        prompts.append(prompt)
        try:
            return next(seq)
        except StopIteration:
            return json.dumps({"action": "done", "message": "通过了",
                               "evidence": "它说 g++ 跑成功了"}, ensure_ascii=False)

    planner.ask = fake_ask
    try:
        r = await server._verify_core(server.WorldVerifyRequest(
            task="把这段 C++ 排序跑起来", answer="用 g++ 编译即可", applied=[],
            max_rounds=2, timeout=60))
    finally:
        planner.ask = orig_ask
        workspace.use_root(orig_root)
    r = r if isinstance(r, dict) else json.loads(r.body.decode("utf-8"))
    acts = [(x.get("action"), x.get("code"), x.get("kind")) for x in r["rounds"]]
    print("2) 端到端:", json.dumps({"ok": r.get("ok"), "reason": r.get("reason"),
                                    "real_error": r.get("real_error"), "acts": acts},
                                   ensure_ascii=False)[:300])
    if r.get("ok"):
        bad.append("一条不存在的命令居然被判成通过")
    if not any(a[0] == "tool-error" for a in acts):
        bad.append("那轮没被记成 tool-error(程序没启动 ≠ 项目报错): " + json.dumps(acts))
    if any(a[2] == "behavior" for a in acts):
        bad.append("「程序没启动」那一轮还被当成了行为证据: " + json.dumps(acts))
    if r.get("real_error"):
        bad.append("被算成了项目真实报错 —— 这台机器缺工具不是用户代码的问题")
    if not any("根本没启动" in p for p in prompts):
        bad.append("没把「程序根本没启动」这句回给模型, 它会继续烧轮数: "
                   + (prompts[-1][:200] if prompts else "一条提示都没发"))
    audit = r.get("audit") or ""
    vj = Path(audit) / "verdict.json"
    if audit and vj.exists():
        data = json.loads(vj.read_text(encoding="utf-8"))
        if not any(x.get("action") == "tool-error" for x in (data.get("rounds") or [])):
            bad.append("留痕里没有 tool-error 这一轮, 事后看不出是环境问题: " + str(audit))
    else:
        bad.append("这次验证没留痕: " + str(audit))
    shutil.rmtree(TMP_WS, ignore_errors=True)

    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("TOOL_FAIL_OK (退出码 0 + 程序没启动 = 按失败算并标 tool-error; 不算行为证据; "
          "不算项目报错; 明确回给模型; 留痕可复核; 正常输出里的 not found / No such file 不误伤)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
