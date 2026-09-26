"""代码围栏配对 + 目标文件归属 + 覆盖前备份 的自检(全程在 .tmp 里的临时工作区)。

起因是一次真实事故: ChatGPT 给了三个文件(index.html / style.css / game.js), 桥只写出一个,
而且把 276 字节的真实 `3d/frontend/index.html` 覆盖成了 27 字节的垃圾。两个根因:

  A) "项目结构"那种块把开闭 ``` 写在**同一行**, 老的 ```...``` 正则要求开围栏后必须换行,
     于是那一行的闭围栏被当成下一块开头 -> 配对整体错一位: 真代码变成"块间正文"一个字提不出来,
     而 `## index.html` + `HTML` 这种标题反而被当成文件内容写进工作区。
  B) 回答里只写裸文件名时, 旧代码用 {basename: 路径} 的字典在工作区树里找 —— 同名多个时
     字典只留最后一个, 覆盖哪一个全看遍历顺序。

盯的七条:
  1) 同行开闭的围栏不再让后面所有配对错位;
  2) 上面那种真实回答能提出三个文件、内容逐字对;
  3) 裸文件名撞上多个同名 -> **每个同名文件都写**(2026-09-26 他定: 都在当前工作区里就直接替换),
     每条各自先备份, 卡片上是一行一个完整路径 —— 中间那一版"拦下来问路径"已经作废;
  4) 只有一个同名文件时仍然能对上(老行为里有用的一半不能丢);
  5) 全新文件名 -> create 到根, 不去树里找一个同名深路径来 update;
  6) update 已有文件前必须先留 .bak, 备份里是原内容;
  7) 内容没变时不写也不备份(别把项目刷一堆 .bak)。
"""
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import engineer, workspace  # noqa: E402

TMP = ROOT / ".tmp" / "fence-ws"

# 真实回答的形状: 项目结构块写成单行围栏, 三个文件块前面各有一行 `## \`文件名\``
ANSWER = """可以，下面给你一个完整可运行的 HTML5 3D 俄罗斯方块。

项目结构：

``` 3d-tetris/ ├── index.html ├── style.css └── game.js ```

## `index.html`

HTML

```
<!DOCTYPE html>
<html lang="zh-CN"><title>3D</title></html>
```

## `style.css`

CSS

```
html,body{margin:0}
canvas{display:block}
```

## `game.js`

JavaScript

```
const COLS=10;
const ROWS=20;
```
"""


def reset_tree(files: dict):
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = TMP / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return workspace.walk_files()


async def main() -> int:
    bad = []
    orig_root = workspace.ROOT
    workspace.use_root(TMP)
    try:
        # ---- 1) 单行围栏不再让配对错位
        blocks = engineer.fence_blocks(engineer.strip_fence_labels(ANSWER))
        print("1) 配对出 %d 个块:" % len(blocks))
        for i, (info, body, prose) in enumerate(blocks, 1):
            print("   #%d info=%-4r %5d 字  开头=%r  前面正文末=%r"
                  % (i, info, len(body), " ".join(body.split())[:26],
                     " ".join(prose.split())[-16:]))
        if len(blocks) != 4:
            bad.append("应该是 4 块(1 个结构图 + 3 个文件), 实际 %d" % len(blocks))
        bodies = [" ".join(b[1].split()) for b in blocks]
        if not any(b.startswith("<!DOCTYPE html>") for b in bodies):
            bad.append("HTML 那段代码没有被当成一个块提出来(配对还是错位的)")
        if any("index.html` HTML" in b for b in bodies):
            bad.append("标题+语言标签又被当成代码内容了")

        # ---- 2) 三个文件都能提出来, 内容逐字对
        tree = reset_tree({"3d/frontend/index.html": "<!-- 真实的旧文件, 276 字节那种 -->\n",
                           "3d - 副本/frontend/index.html": "<!-- 另一个同名文件 -->\n"})
        items, loose = engineer.extract_code_files(ANSWER, tree)
        got = {i["path"]: i for i in items}
        print("2) extract -> %d 条, loose=%d: %s"
              % (len(items), loose, [(i["op"], i["path"]) for i in items]))
        for name, head in [("style.css", "html,body{"), ("game.js", "const COLS=10;")]:
            if name not in got or not got[name]["content"].startswith(head):
                bad.append("%s 没提出来或内容开头不对: %r" % (name, got.get(name, {}).get("content", "")[:30]))
        idx = [i for i in items if i["path"].endswith("frontend/index.html")]
        if any(i["path"] == "index.html" for i in items):
            bad.append("还留着那条「拦住问路径」的占位项(应该展开成两个真实路径): %s"
                       % [i["path"] for i in items])

        # ---- 3) 多个同名 -> 两个都写、各自备份(他 2026-09-26 定: 都在当前工作区就直接替换)
        print("3) index.html 的处置: %s" % [(i["op"], i["path"], i.get("fanout")) for i in idx])
        if len(idx) != 2:
            bad.append("两个同名文件没有都列出来: %s" % [i["path"] for i in idx])
        else:
            if any(i["op"] != "update" for i in idx):
                bad.append("同名文件已存在却不是 update: %s" % [(i["op"], i["path"]) for i in idx])
            if any(i.get("fanout") != 2 for i in idx):
                bad.append("没标 fanout(界面上要说清这是一次写进了两个同名文件): %s" % idx)
            ap, sk, _ = engineer.apply_manifest({"files": items})
            wrote = sorted(a["path"] for a in ap)
            print("   apply -> applied=%s skipped=%s" % (wrote, sk))
            for p in ("3d/frontend/index.html", "3d - 副本/frontend/index.html"):
                if p not in wrote:
                    bad.append("同名的 %s 没被写: %s" % (p, wrote))
                now = (TMP / p).read_text(encoding="utf-8")
                if "<!DOCTYPE html>" not in now:
                    bad.append("%s 内容没换掉: %r" % (p, now[:40]))
                baks = sorted((TMP / p).parent.glob("index.html.bak-*"))
                if not baks:
                    bad.append("%s 写之前没备份" % p)
                elif "真实的旧文件" not in baks[0].read_text(encoding="utf-8") and \
                        "另一个同名文件" not in baks[0].read_text(encoding="utf-8"):
                    bad.append("%s 的备份里不是原内容" % p)

        # ---- 4) 只有一个同名文件时仍然对得上(不能把有用的行为一起砍掉)
        tree1 = reset_tree({"src/ui/EditorArea.qml": "// 旧内容\n"})
        one, _ = engineer.extract_code_files("改一下 EditorArea.qml:\n```qml\n// 新内容\n```", tree1)
        print("4) 唯一同名 -> %s" % [(i["op"], i["path"]) for i in one])
        if not one or one[0]["path"] != "src/ui/EditorArea.qml" or one[0]["op"] != "update":
            bad.append("全工作区只有一个同名文件时也没对上(修过头了): %s" % one)

        # ---- 5) 全新文件名 -> create 到根, 不去树里抓深路径
        tree2 = reset_tree({"README.md": "# demo\n"})
        new, _ = engineer.extract_code_files("## `notes.txt`\n\n```\n第一行\n```", tree2)
        print("5) 新文件 -> %s" % [(i["op"], i["path"]) for i in new])
        if not new or new[0]["path"] != "notes.txt" or new[0]["op"] != "create":
            bad.append("新文件没按 create 落在根上: %s" % new)

        # ---- 6) update 前必须留备份, 且备份里是原内容
        tree3 = reset_tree({"notes.txt": "原内容, 不能丢\n"})
        ap3, sk3, _ = engineer.apply_manifest({"files": [
            {"op": "update", "path": "notes.txt", "content": "新内容\n"}]})
        baks = sorted(p.name for p in TMP.glob("notes.txt.bak-*"))
        print("6) update -> applied=%s 备份=%s" % ([(a["path"], a.get("backup")) for a in ap3], baks))
        if not baks:
            bad.append("覆盖了已有文件却没有备份(以前就是这里裸写, 一次配对错误就把真实文件冲掉)")
        else:
            if (TMP / baks[0]).read_text(encoding="utf-8") != "原内容, 不能丢\n":
                bad.append("备份里不是原内容")
            if ap3 and not ap3[0].get("backup"):
                bad.append("备份没回报到 applied 里, 卡片上看不见")

        # ---- 7) 内容没变 -> 不写也不备份
        tree4 = reset_tree({"same.txt": "一模一样\n"})
        ap4, _, _ = engineer.apply_manifest({"files": [
            {"op": "update", "path": "same.txt", "content": "一模一样\n"}]})
        left = sorted(p.name for p in TMP.glob("same.txt*"))
        print("7) 无变化 -> applied=%d 目录里剩 %s" % (len(ap4), left))
        if ap4 or left != ["same.txt"]:
            bad.append("内容没变也写了/也备份了(会把项目刷一堆 .bak): %s" % left)
    finally:
        workspace.use_root(orig_root)
        shutil.rmtree(TMP, ignore_errors=True)

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("FENCE_TARGET_OK (单行围栏不再错位 / 多个同名全部替换且各自先备份 / 唯一同名仍能对上 / "
          "新文件 create 到根 / 无变化不写不备份)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
