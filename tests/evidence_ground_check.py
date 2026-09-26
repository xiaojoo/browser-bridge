r"""回归: 三条验收点全真的跑通了, 却被"证据核对"自己判死 —— 引用片段不能拿正则从整段里扫。

事故(2026-09-26 04:33, 工作区 H:/test, 留痕 .tmp/verify-audit/20260926-043316):
  模型逐条给了证据, 结构是 `[n] 命令 <cmd> 输出: "<quote>"`。三条验收点都真跑过、退出码全 0:
      PYCOMPILE_EXIT=0 / CHECKER_EXIT=0 / MODULE_IMPORTED_OK / ROTATE_RESULT=… / main guard 找到了
  但 `_grounding_problems()` 是拿 `_QUOTE_RE` 在**整段渲染出来的文字**上找引号 ——
  而 PowerShell 命令里全是引号(`$env:SDL_VIDEODRIVER='dummy'`), 于是命令里那些片段被当成
  "引用的输出", 报出 10 条"在真实输出里找不到", 结论 weak-evidence, 一次白跑。

现在: 结构化证据(列表)直接取它自己的 quote 字段; 只有自由文本证据才退回正则扫。
这条同时钉住反方向 —— 真编造的引用片段照样要被抓出来。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import server  # noqa: E402

AUDIT = ROOT / ".tmp" / "verify-audit" / "20260926-043316" / "verdict.json"

# 留痕不在(那目录只保留最近 50 份)就用抄下来的等价数据
ROUNDS = [
    {"round": 1, "action": "run", "code": 0, "kind": "behavior",
     "command": 'powershell -NoProfile -Command "python -m py_compile tetris3d.py; '
                "'PYCOMPILE_EXIT=' + $LASTEXITCODE\"",
     "output": "PYCOMPILE_EXIT=0\n"},
    {"round": 2, "action": "run", "code": 0, "kind": "behavior",
     "command": 'powershell -NoProfile -Command "$env:SDL_VIDEODRIVER=\'dummy\'; '
                "$env:SDL_AUDIODRIVER='dummy'; python verify/check_tetris3d.py; "
                "'CHECKER_EXIT=' + $LASTEXITCODE",
     "output": "MODULE_IMPORTED_OK\nROTATE_RESULT= [(-1, 0), (-1, 1), (-1, 2), (-1, 3)]\n"
               "NORMALIZE_RESULT= [(0, 0), (0, 1), (0, 2), (0, 3)]\nCHECKER_EXIT=0\n"},
]
CRITERIA = "- [1] py_compile 退出码 0\n- [2] 自检脚本退出码 0 且能看到断言原文\n"
QUOTES = ["PYCOMPILE_EXIT=0", "CHECKER_EXIT=0", "MODULE_IMPORTED_OK",
          'ROTATE_RESULT= [(-1, 0), (-1, 1), (-1, 2), (-1, 3)]']


def main() -> int:
    bad = []
    rounds, criteria, ev = ROUNDS, CRITERIA, None
    if AUDIT.exists():                                    # 优先用真留痕原文
        d = json.loads(AUDIT.read_text(encoding="utf-8"))
        rounds = [r for r in d["rounds"] if r.get("action") == "run"]
        criteria = d["checks"]
        ev = d["rounds"][-1].get("evidence") or ""
        quotes = re.findall(r'输出: "(.*?)"(?= ; |$)', ev)
        print("夹具: 留痕原文, %d 轮, %d 个引用片段" % (len(rounds), len(quotes)))
    else:
        rendered = " ; ".join('[%d] 命令 %s 输出: "%s"' % (i + 1, r["command"], q)
                              for i, (r, q) in enumerate(zip(rounds, QUOTES)))
        ev, quotes = rendered, QUOTES
        print("夹具: 等价重建数据(留痕已被清理), %d 轮, %d 个引用片段" % (len(rounds), len(quotes)))

    # 1) 修之前那种扫法会报一堆假问题 —— 现在结构化引用必须全被认下来
    old_style = server._grounding_problems(ev, criteria, rounds)          # 不传 quotes = 旧扫法
    new_style = server._grounding_problems(ev, criteria, rounds, quotes)  # 结构化字段
    print("1) 旧扫法报 %d 条: %s" % (len(old_style), (old_style[:1] or ["-"])[0][:60]))
    print("   结构化引用报 %d 条: %s" % (len(new_style), new_style))
    if not old_style:
        bad.append("复现不了: 旧扫法这次没报错, 说明夹具和事故现场不一样, 这条门禁失效了")
    if new_style:
        bad.append("三条验收点全真跑通, 结构化证据还是被拒了: " + "; ".join(new_style))

    # 2) 反方向: 编造的引用片段照样要抓出来
    fake = server._grounding_problems(ev, criteria, rounds,
                                      ["全部测试通过, 108 项断言 OK"])
    print("2) 编造引用 -> %s" % fake)
    if not any("找不到" in p for p in fake):
        bad.append("编造的输出片段没被抓出来 —— 这道防线被改没了")

    # 3) 自由文本证据仍走正则(事故里那种"自己写一句通过"不能放过)
    free = server._grounding_problems(
        "三条验收点均通过: Release 构建实跑成功并产出 SmartClip.exe", criteria, rounds)
    print("3) 自由文本没点名命令/没引用输出 -> %d 条问题" % len(free))
    if not free:
        bad.append("自由文本证据不再核对了(那次假通过事故的防线没了)")

    # 4) `_evidence_quotes` 只认结构化列表
    if server._evidence_quotes({"evidence": "一句话"}) is not None:
        bad.append("自由文本应该返回 None(退回正则扫), 不该返回空列表")
    got = server._evidence_quotes({"evidence": [{"check": 1, "command": "x", "quote": "OK"},
                                                {"check": 2, "command": "y"}]})
    print("4) 结构化取到的引用: %s" % got)
    if got != ["OK"]:
        bad.append("引用字段没取对: %s" % got)

    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("EVIDENCE_GROUND_OK (结构化证据按 quote 字段核对; 编造引用照样抓; "
          "自由文本仍走正则; 命令里的引号不再被当成引用)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
