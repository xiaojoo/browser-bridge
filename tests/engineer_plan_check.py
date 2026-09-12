"""World 模式工程流程: 本地模型规划 + 挑文件 -> 只把需要的文件发给网页模型。

全程本地 mock(替换 engineer.provider_ask), 不联网、不碰真实站点。
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import engineer, workspace  # noqa: E402

TMP_WS = ROOT / ".tmp" / "eng-ws"


class FakeManager:
    page = object()
    state = "logged_in"


def collect(store):
    async def emit(stage, text, extra):
        store.append((stage, text, extra))
    return emit


async def main() -> int:
    bad = []
    TMP_WS.mkdir(parents=True, exist_ok=True)
    (TMP_WS / "app.py").write_text("print('app v1')\n", encoding="utf-8")
    (TMP_WS / "utils.py").write_text("SECRET_UTILS = 1\n", encoding="utf-8")
    (TMP_WS / "notes.md").write_text("会议记录, 不该被发出去\n", encoding="utf-8")
    (TMP_WS / "sub").mkdir(exist_ok=True)
    (TMP_WS / "sub" / "deep.py").write_text("DEEP = 2\n", encoding="utf-8")

    sent: list[str] = []
    orig_ask = engineer.provider_ask
    orig_root = workspace.ROOT           # 跑完还原: use_root 不写设置, 别冲掉用户配的工作区

    def manifest(msg: str, path: str, content: str = "", op: str = "update") -> str:
        files = [{"op": op, "path": path}]
        if op != "delete":
            files[0]["content"] = content
        return json.dumps({"message": msg, "files": files}, ensure_ascii=False)

    async def fake_provider(manager, prompt):
        sent.append(prompt)
        if "子任务二" in prompt:
            return manifest("第二步", "sub/new.py", "NEW = 1\n", op="create")
        # 第一步: 一个正常改动 + 一个非法路径(触发 skipped, 供下一步做"日志回灌")
        return json.dumps({"message": "改好了", "files": [
            {"op": "update", "path": "app.py", "content": "print('app v2')\n"},
            {"op": "create", "path": "../escape.py", "content": "x"}]}, ensure_ascii=False)

    engineer.provider_ask = fake_provider
    try:
        workspace.use_root(TMP_WS)

        # ---------- 1) 本地模型给出"每步需要哪些文件" ----------
        plan = json.dumps({"steps": [
            {"text": "先改 app.py", "files": ["app.py"]},
            {"text": "子任务二: 处理边界", "files": []},
        ]}, ensure_ascii=False)
        events = []

        async def ask_plan(prompt):
            return "```json\n" + plan + "\n```"

        plan_prompts = []

        async def ask_plan2(prompt):
            plan_prompts.append(prompt)
            return "```json\n" + plan + "\n```"

        result = await engineer.run(FakeManager(), "把 app 里的打印改掉", [],
                                    emit=collect(events), plan=True, summary=False,
                                    ask_plan=ask_plan2, repo="https://github.com/me/proj")
        print("应用:", json.dumps(result["applied"], ensure_ascii=False))
        print("跳过:", json.dumps(result["skipped"], ensure_ascii=False))
        if len(sent) != 2:
            bad.append(f"应该只向网页模型发 2 次(每步一次): {len(sent)}")
        p1 = sent[0] if sent else ""
        if "### 文件 app.py" not in p1:
            bad.append("第一步没有把 app.py 内容发过去")
        for forbidden in ("SECRET_UTILS", "会议记录", "DEEP = 2"):
            if forbidden in p1:
                bad.append(f"第一步把不需要的文件也发了({forbidden})")
        if "### 文件 app.py" in (sent[1] if len(sent) > 1 else "") and "不需要" not in sent[1]:
            bad.append("第二步(规划说不需要文件)不该再带文件内容")
        if "上一步执行时遇到这些情况" not in (sent[1] if len(sent) > 1 else ""):
            bad.append("上一步的问题没有作为日志回灌给下一步")
        if not (TMP_WS / "app.py").read_text(encoding="utf-8").startswith("print('app v2')"):
            bad.append("网页模型给出的变更没有落盘")
        if not (TMP_WS / "sub" / "new.py").exists():
            bad.append("第二步的变更没有落盘")
        if not any("非法路径" in s for s in result["skipped"]):
            bad.append("非法路径没有被跳过: " + json.dumps(result["skipped"], ensure_ascii=False))
        plan_evt = [e for e in events if e[0] == "plan"]
        print("计划事件:", plan_evt[0][1] if plan_evt else None)
        if not plan_evt or "app.py" not in plan_evt[0][1]:
            bad.append("计划里没显示每步需要的文件: " + json.dumps(plan_evt, ensure_ascii=False))
        # 消息列表要能画任务卡片: 计划事件带结构化 steps; 每步发/收都有事件
        steps_payload = (plan_evt[0][2] or {}).get("steps") if plan_evt else None
        print("计划结构化:", json.dumps(steps_payload, ensure_ascii=False))
        if not steps_payload or len(steps_payload) != 2 or steps_payload[0].get("files") != ["app.py"]:
            bad.append("计划事件没有带结构化 steps: " + json.dumps(steps_payload, ensure_ascii=False))
        step_evts = [e for e in events if e[0] == "step"]
        answer_evts = [e for e in events if e[0] == "answer"]
        print("step 事件:", json.dumps([e[2] for e in step_evts], ensure_ascii=False)[:300])
        if len(step_evts) != 2 or len(answer_evts) != 2:
            bad.append(f"每步都该有 step/answer 事件: {len(step_evts)}/{len(answer_evts)}")
        if step_evts and (step_evts[0][2] or {}).get("files") != ["app.py"]:
            bad.append("step 事件没带上发送的文件: " + json.dumps(step_evts[0][2], ensure_ascii=False))
        if answer_evts and not (answer_evts[0][1] or "").strip():
            bad.append("answer 事件没带网页模型的回复文本")
        # 远程仓库地址要写进提示词
        if not plan_prompts or "github.com/me/proj" not in plan_prompts[0]:
            bad.append("规划提示词里没有远程仓库地址")
        impl_repo = [p for p in sent[:2] if "github.com/me/proj" in p]
        if len(impl_repo) < 1:
            bad.append("实现提示词里没有远程仓库地址")

        # ---------- 2) 老格式规划(只有步骤文字) -> 退回按选定范围发送 ----------
        sent.clear()

        async def ask_old(prompt):
            return '{"steps": ["改 app.py"]}'

        await engineer.run(FakeManager(), "再改一次", ["utils.py"],
                           emit=collect([]), plan=True, summary=False, ask_plan=ask_old)
        p = sent[0] if sent else ""
        print("老格式发送:", "utils.py" in p, "| 含 SECRET:", "SECRET_UTILS" in p)

        # ---------- 3) 该改却回了空清单 -> 催一次(并把真正的改动落盘) ----------
        sent.clear()
        calls = {"n": 0}

        async def fake_provider2(manager, prompt):
            sent.append(prompt)
            calls["n"] += 1
            if calls["n"] == 1:
                return manifest("确认无需修改", "app.py", "", op="update") if False else \
                       json.dumps({"message": "确认无需修改", "files": []}, ensure_ascii=False)
            return manifest("这次给出改动", "app.py", "print('app v3')\n")

        engineer.provider_ask = fake_provider2

        async def ask_one(prompt):
            return '{"steps": [{"text": "把 app.py 的打印改成 v3", "files": ["app.py"], "change": true}]}'

        res3 = await engineer.run(FakeManager(), "改成 v3", [], emit=collect([]),
                                  plan=True, summary=False, ask_plan=ask_one)
        print("催一次后发送次数:", len(sent), "应用:", json.dumps(res3["applied"], ensure_ascii=False))
        if len(sent) != 2:
            bad.append(f"该改却没改时应多催一次(共 2 次请求): {len(sent)}")
        if len(sent) > 1 and "没有给出任何文件变更" not in sent[1]:
            bad.append("催的那次没有说明原因")
        if not (TMP_WS / "app.py").read_text(encoding="utf-8").startswith("print('app v3')"):
            bad.append("催过之后仍然没有落盘")

        # ---------- 4) change=false 的步骤不该被催 ----------
        sent.clear()
        calls["n"] = 0

        async def fake_provider3(manager, prompt):
            sent.append(prompt)
            return json.dumps({"message": "已确认, 无需修改", "files": []}, ensure_ascii=False)

        engineer.provider_ask = fake_provider3

        async def ask_none(prompt):
            return '{"steps": [{"text": "确认一下现状, 不改文件", "files": [], "change": false}]}'

        await engineer.run(FakeManager(), "只看不改", [], emit=collect([]),
                           plan=True, summary=False, ask_plan=ask_none)
        print("change=false 发送次数:", len(sent))
        if len(sent) != 1:
            bad.append(f"change=false 的步骤不该催: {len(sent)} 次请求")
        if "SECRET_UTILS" not in p:
            bad.append("老格式规划(没给文件)时应退回发送选定文件: " + p[:200])
        if "会议记录" in p:
            bad.append("老格式也不该把没选中的文件发出去")
    finally:
        engineer.provider_ask = orig_ask
        workspace.use_root(orig_root)
        shutil.rmtree(TMP_WS, ignore_errors=True)

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("ENGINEER_PLAN_OK (本地模型给步骤+挑文件, 只把需要的文件发给网页模型, 问题日志回灌)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
