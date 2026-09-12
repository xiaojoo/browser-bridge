"""engineer 单元测试: 清单解析 + 校验落盘(离线, 不联网)。

注意: 落盘一律在临时目录里做 —— 以前它直接往**用户配置的工作区**(settings 里的 root,
比如 H:\\steward)里写 eng_unit_tmp/, 跑一次测试就往人家项目里塞文件。
"""
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge import engineer, workspace  # noqa: E402

TMP_WS = ROOT / ".tmp" / "engineer-unit-ws"

GOOD = '''先说明: 我会直接给出清单。
```json
{"message": "新增 fib", "files": [
  {"op": "create", "path": "eng_unit_tmp/app.py", "content": "def fib(n):\\n    return n if n < 2 else fib(n-1)+fib(n-2)\\n"},
  {"op": "update", "path": "eng_unit_tmp/readme.md", "content": "# demo"}
]}
```'''

BAD = "我不会照做, 因为不能修改文件。"

NO_FENCE = """JSON
{"message": "创建完成", "files": [{"op": "create", "path": "eng_unit_tmp/no_fence.txt", "content": "ok"}]}
结尾还有解释文字。"""

STEPS = '''```json
{"steps": ["建目录结构", "实现核心函数", "补测试"]}
```'''


def main() -> int:
    # 解析
    obj, src = engineer.extract_manifest(GOOD)
    assert obj and len(obj["files"]) == 2, (obj, src)
    obj2, src2 = engineer.extract_manifest(BAD)
    assert obj2 is None and src2, (obj2, src2)
    steps = engineer.extract_steps(STEPS)
    assert steps == ["建目录结构", "实现核心函数", "补测试"], steps
    obj3, src3 = engineer.extract_manifest(NO_FENCE)
    assert obj3 and len(obj3["files"]) == 1, (obj3, src3)

    # 落盘(在临时工作区里做, 绝不碰用户配的项目目录)
    shutil.rmtree(TMP_WS, ignore_errors=True)
    TMP_WS.mkdir(parents=True)
    orig_root = workspace.ROOT
    workspace.use_root(TMP_WS)
    try:
        applied, skipped, diffs = engineer.apply_manifest(obj)
        assert len(applied) == 2 and not skipped, (applied, skipped)
        data = workspace.read_file("eng_unit_tmp/app.py")
        assert "fib" in data["text"], data
        # 越界路径应被拒
        bad = {"files": [{"op": "create", "path": "../../evil.py", "content": "x"}]}
        _, skipped2, _ = engineer.apply_manifest(bad)
        assert skipped2, f"越界应被跳过: {skipped2}"
    finally:
        workspace.use_root(orig_root)
        shutil.rmtree(TMP_WS, ignore_errors=True)

    print("ENGINEER_UNIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
