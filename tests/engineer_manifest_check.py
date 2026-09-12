"""清单校验/落盘回归: 错误文案不再冒充路径 + 普通点文件(.gitignore)能写进去。

背景(用户实际日志):
  预览行「非法条目: invalid 非法路径: .gitignore +0/-0 行 · 0B」
  跳过行「未知 op: invalid」×2
两处都是同一个毛病: _check_op 把错误文案塞进了 path 槽, 而且 .gitignore 被
`path.startswith(".")` 整类拒掉, 于是点文件永远写不进去、日志还认不出是哪个文件。
本脚本不联网、不碰真实工作区(只改 workspace.ROOT 全局, 不落 settings)。
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import engineer, workspace  # noqa: E402

TMP_WS = ROOT / ".tmp" / "manifest-ws"


def main() -> int:
    bad = []
    shutil.rmtree(TMP_WS, ignore_errors=True)
    TMP_WS.mkdir(parents=True)
    (TMP_WS / "EditorArea.qml").write_text("// v1\n", encoding="utf-8")
    orig_root = workspace.ROOT
    workspace.ROOT = TMP_WS                  # 直接改全局, 别动 settings(用户配的 root)
    try:
        # 1) 校验: 错误只走 err 槽, path 槽只放真路径
        if engineer._check_op({"op": "update", "path": ".gitignore", "content": "x"}) != \
                ("update", ".gitignore", "x", None):
            bad.append("点文件 .gitignore 应该放行")
        for item in ({"op": "create", "path": "../escape.py", "content": "x"},
                     {"op": "update", "path": ".git/config", "content": "x"},
                     {"op": "update", "path": "C:/win.py", "content": "x"},
                     {"op": "update", "path": "", "content": "x"},
                     {"op": "invalid", "path": ".gitignore"}):
            op, path, content, err = engineer._check_op(item)
            if op or path or content or not err:
                bad.append("该拒的条目没拒干净: " + json.dumps(item, ensure_ascii=False)
                           + " -> " + repr((op, path, content, err)))
        op, path, content, err = engineer._check_op({"op": "update", "path": ".gitignore"})
        if not err or "缺少 content" not in err:
            bad.append("缺 content 的报错不对: " + repr(err))

        # 2) 预览: invalid 行要给出真路径 + 真原因, 且绝不写盘
        manifest = {"files": [
            {"op": "update", "path": "EditorArea.qml", "content": "// v2\n"},
            {"op": "create", "path": ".gitignore", "content": "build/\n"},
            {"op": "create", "path": "../escape.txt", "content": "x"}]}
        pv = engineer.preview_manifest(manifest)
        print("预览:", json.dumps(pv, ensure_ascii=False)[:300])
        if len(pv) != 3 or pv[2]["op"] != "invalid":
            bad.append("非法条目没有被标成 invalid: " + json.dumps(pv, ensure_ascii=False))
        elif pv[2].get("path") != "../escape.txt" or "非法路径" not in (pv[2].get("error") or ""):
            bad.append("invalid 行的 path/error 还是混的: " + json.dumps(pv[2], ensure_ascii=False))
        if not pv[1].get("size"):
            bad.append(".gitignore 的预览统计不对: " + json.dumps(pv[1], ensure_ascii=False))
        if (TMP_WS / "EditorArea.qml").read_text(encoding="utf-8") != "// v1\n":
            bad.append("预览竟然写盘了")

        # 3) commit: 界面把预览条目原样回传(含 invalid 那条), content 由服务端补回
        contents = {it["path"]: it["content"] for it in manifest["files"]}
        sent = [{"op": f["op"], "path": f["path"],
                 "content": f.get("content") or contents.get(f["path"], "")} for f in pv]
        applied, skipped, diffs = engineer.apply_manifest({"files": sent})
        print("落盘:", json.dumps(applied, ensure_ascii=False))
        print("跳过:", json.dumps(skipped, ensure_ascii=False))
        if not (TMP_WS / ".gitignore").exists():
            bad.append(".gitignore 还是没写进去")
        if (TMP_WS / "EditorArea.qml").read_text(encoding="utf-8") != "// v2\n":
            bad.append("正常条目没落盘")
        if len(applied) != 2:
            bad.append("应该落盘 2 项: " + json.dumps(applied, ensure_ascii=False))
        if not any("../escape.txt" in s for s in skipped):
            bad.append("跳过条目没带真路径(错误文案又冒充路径了): "
                       + json.dumps(skipped, ensure_ascii=False))
        if (TMP_WS.parent / "escape.txt").exists():
            bad.append("越界文件被写出来了")
        if not diffs or "add" not in diffs[0]:
            bad.append("diff 统计丢了: " + json.dumps(diffs, ensure_ascii=False))

        # 4) 毁坏防护: 疑似截断 / 疑似路径写错的残file 默认跳过, 带 force 才硬来
        #    (用户实际踩过: 700 行文件被换成一个 27 字节的残file, 还写到了项目根目录)
        (TMP_WS / "qml" / "components").mkdir(parents=True, exist_ok=True)
        big = "// 真正的大文件\n" * 300
        (TMP_WS / "qml" / "components" / "Big.qml").write_text(big, encoding="utf-8")
        st = engineer.preview_manifest({"files": [
            {"op": "update", "path": "qml/components/Big.qml", "content": "// 残file\n"},
            {"op": "update", "path": "Big.qml", "content": "// 残file\n"}]})
        print("毁坏防护:", json.dumps([{k: f.get(k) for k in ("op", "path", "force", "realOp")} for f in st],
                                     ensure_ascii=False))
        if st[0].get("op") != "warn" or "疑似截断" not in (st[0].get("error") or ""):
            bad.append("大幅截断的大文件没有被标成 warn: " + json.dumps(st[0], ensure_ascii=False)[:200])
        if st[1].get("op") != "warn" or "路径写错" not in (st[1].get("error") or ""):
            bad.append("根目录写残file没有被标成 warn: " + json.dumps(st[1], ensure_ascii=False)[:200])
        if st[1].get("realOp") != "update" or not st[1].get("force"):
            bad.append("warn 条目没带上真实 op / force: " + json.dumps(st[1], ensure_ascii=False)[:200])
        if (TMP_WS / "qml" / "components" / "Big.qml").read_text(encoding="utf-8") != big:
            bad.append("预览竟然写盘了(毁坏防护)")
        _, sk2, _ = engineer.apply_manifest({"files": [
            {"op": "update", "path": "qml/components/Big.qml", "content": "// 残file\n"}]})
        if not sk2 or "疑似截断" not in sk2[0]:
            bad.append("直接落盘时疑似截断没有被跳过: " + json.dumps(sk2, ensure_ascii=False))
        if (TMP_WS / "qml" / "components" / "Big.qml").read_text(encoding="utf-8") != big:
            bad.append("疑似截断竟然真的写进去了")
        _, sk3, _ = engineer.apply_manifest({"files": [
            {"op": "update", "path": "Big.qml", "content": "// 残file\n"}]})
        if not sk3:
            bad.append("根目录残file没有被跳过: " + json.dumps(sk3, ensure_ascii=False))
        if (TMP_WS / "Big.qml").exists():
            bad.append("根目录残file被写出来了")
        ap4, _, _ = engineer.apply_manifest({"files": [
            {"op": "update", "path": "qml/components/Big.qml", "content": "// 残file\n", "force": True}]})
        if len(ap4) != 1:
            bad.append("带 force 的确认写入没有生效: " + json.dumps(ap4, ensure_ascii=False))
    finally:
        workspace.ROOT = orig_root
        shutil.rmtree(TMP_WS, ignore_errors=True)

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("ENGINEER_MANIFEST_OK (错误文案只走 err 槽, 点文件可落盘, 越界仍被拦)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
