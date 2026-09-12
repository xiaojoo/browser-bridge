"""回归: 假"变更清单"不能把真正的代码围栏挤掉。

真实故障(97k 字的商城回答, 30 个文件一个都没认出来):
    world apply: 跳过(没认出可写入的文件): 回答里有代码, 但没认出这些代码属于哪个文件 (loose=0)
回答里 30 段代码都带着 `### src/pages/Home.vue` 这样的文件名标题, 本该全都认出来, 结果是 0。

根因: `_parse_loose` 把**整篇回答(含代码围栏)**当"操作/路径/内容"表格扫, 于是
    `remove(id: number) {`(TypeScript 代码) -> "删除某个文件(路径为空)"
    表格里孤零零一个"删除"单元格        -> 又一条
三条假 op 让 `extract_manifest` 以为"这回答给了变更清单", `extract_code_files` 于是直接走清单
分支; 清单里路径全是空 -> 一条都对不上 -> 返回 0 个文件, 连围栏都没看。

本测试盯住:
  1) 代码里的 remove(...) / 表格里的"删除" 不算变更清单, 围栏照常整理;
  2) package.json 里的 "files": ["dist"] 不算变更清单(它不是 {path, content} 清单);
  3) 清单里条目全对不上时, 不能拿"0 个文件"当结论 -> 继续按围栏认文件名;
  4) 真的清单还能用(json-fence), 真的松散表格还能用(loose-table);
  5) 手上那条真实回答(若 .tmp/last_answer.txt 还在)必须能整理出 >=25 个文件且路径都不为空。
"""
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import engineer  # noqa: E402

TREE = ["src/pages/Home.vue", "src/main.ts", "package.json", "src/stores/cart.ts"]

# 1) 代码里的 remove(...) 曾经变成"删除文件"
CODE_WITH_REMOVE = """这里给出改动：

### src/stores/cart.ts

```ts
export const useCart = () => ({
  remove(id: number) {
    return id;
  },
});
```

### src/pages/Home.vue

```vue
<template><div>home</div></template>
```
"""

# 2) package.json 里的 files 字段不是变更清单
PKG_FILES = """先把 package.json 贴一下：

```json
{
  "name": "mall",
  "files": ["dist", "src"]
}
```

### src/main.ts

```ts
console.log('hi');
```
"""

# 3) 表格里孤零零一个"删除"单元格
TABLE_WORD = """这一版要处理的情况：

| 情况 | 说明 |
| --- | --- |
| 删除 | 用户删除条目 |
| 新增 | 用户新增条目 |

### src/pages/Home.vue

```vue
<template><div>home</div></template>
```
"""

# 4) 真清单(全对不上)不能顶掉围栏
BAD_MANIFEST = """```json
{"message": "改完了", "files": [{"op": "delete", "path": ""}]}
```

### src/main.ts

```ts
export const a = 1;
```
"""

# 5) 真清单(能对上)照旧走清单
GOOD_MANIFEST = """```json
{"message": "新增", "files": [{"op": "create", "path": "src/stores/new.ts", "content": "export const n = 1;"}]}
```
"""

# 6) 真松散表格照旧能用(围栏之外)
LOOSE_TABLE = """| 操作 | 文件 | 内容 |
| --- | --- | --- |
| 新建 | src/stores/loose.ts | export const l = 1; |
"""

# 7) tsconfig.json 里的 "files": [] 是**文件内容**, 不是变更清单(踩过: 整个文件没写进去)
TSCONFIG = """### tsconfig.json

```
{
  "files": [],
  "references": [{ "path": "./tsconfig.app.json" }]
}
```

### src/main.ts

```ts
export const a = 1;
```
"""


def check(name, text, *, want_paths, want_loose=None, manifest_src=None):
    obj, src = engineer.extract_manifest(text)
    items, loose = engineer.extract_code_files(text, TREE)
    paths = [it["path"] for it in items]
    print(f"[{name}] manifest={src} items={len(items)} loose={loose} -> {paths}")
    bad = []
    for p in want_paths:
        if p not in paths:
            bad.append(f"{name}: 没认出 {p}(认出的是 {paths})")
    if want_loose is not None and loose != want_loose:
        bad.append(f"{name}: loose={loose}, 期望 {want_loose}")
    if manifest_src is not None and src != manifest_src:
        bad.append(f"{name}: manifest 来源={src}, 期望 {manifest_src}")
    if any(not it.get("path") for it in items):
        bad.append(f"{name}: 出现了空路径条目")
    return bad


def main() -> int:
    bad = []
    bad += check("1 代码里的 remove", CODE_WITH_REMOVE,
                 want_paths=["src/stores/cart.ts", "src/pages/Home.vue"], want_loose=0,
                 manifest_src="未找到含 files 数组的 JSON 清单")
    bad += check("2 package.json 的 files", PKG_FILES,
                 want_paths=["src/main.ts"], want_loose=0,
                 manifest_src="未找到含 files 数组的 JSON 清单")
    bad += check("3 表格里的删除", TABLE_WORD,
                 want_paths=["src/pages/Home.vue"], want_loose=0,
                 manifest_src="未找到含 files 数组的 JSON 清单")
    bad += check("4 清单条目全空", BAD_MANIFEST,
                 want_paths=["src/main.ts"], want_loose=0)
    bad += check("5 真清单", GOOD_MANIFEST,
                 want_paths=["src/stores/new.ts"], want_loose=0, manifest_src="json-fence")
    bad += check("6 真松散表格", LOOSE_TABLE,
                 want_paths=["src/stores/loose.ts"], want_loose=0, manifest_src="loose-table")
    bad += check("7 tsconfig 的 files:[]", TSCONFIG,
                 want_paths=["tsconfig.json", "src/main.ts"], want_loose=0)

    real = ROOT / ".tmp" / "last_answer.txt"
    if real.exists():
        ans = real.read_text(encoding="utf-8")
        try:
            tree = [ln for ln in urllib.request.urlopen(
                "http://127.0.0.1:8765/workspace/tree", timeout=20).read().decode("utf-8").splitlines()]
        except Exception:  # noqa: BLE001
            tree = TREE
        items, loose = engineer.extract_code_files(ans, tree)
        print(f"[7] 真实回答: {len(items)} 个文件, loose={loose}")
        if len(items) < 25:
            bad.append(f"[7] 真实回答只整理出 {len(items)} 个文件(应为 25+)")
        if any(not it.get("path") or not it.get("content") for it in items):
            bad.append("[7] 真实回答整理出的条目有空路径/空内容")
    else:
        print("[7] 真实回答不在(.tmp/last_answer.txt), 跳过")

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("EXTRACT_NO_HIJACK_OK (代码里的 remove/表格里的删除/package.json 的 files 都不是变更清单; "
          "清单对不上时继续按围栏认文件名)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
