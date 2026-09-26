"""transcript 落盘自检(离线: 不联网、不开浏览器、不碰真目录)。

盯的是第 1 层那七条不能坏的性质:
  1) 同一份对话反复同步不会写第二遍(幂等) —— 接力/读回/切会话都会重复同步;
  2) 同一条回答在流式中变长 -> 要能追加新版本, 读回时拿到**最全**那版且只有一条;
  3) 桥先记了这条(还没有站点 id), 之后从站点又读回来 -> 正文一样, 不许存两份;
  4) 长回答一个字都不许丢(这层的存在理由就是为了不丢细节);
  5) conv_id 来自页面 URL = 外部输入, 不能让它跳出 transcripts 目录;
  6) 换窗口之后那段新对话要带 relay_of 指回上一段, 而且只带一次;
  7) 并发写同一个文件不能写串行、写坏; 文件里混进坏行也不能拖累其余。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import transcript  # noqa: E402

TMP = ROOT_DIR / ".tmp" / "transcript-store"
LONG = 200_000


def msg(mid, role, text):
    return {"id": mid, "role": role, "text": text}


def reset():
    """换根目录 + 清缓存, 每条用例从干净状态开始。"""
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    transcript.ROOT = TMP
    transcript._seen.clear()
    transcript._relay.clear()


async def main() -> int:
    bad: list[str] = []

    # 1) 幂等: 同一份对话同步三次只有一份
    reset()
    conv = "11111111-aaaa-bbbb-cccc-dddddddddddd"
    batch = [msg("m1", "user", "第一条问题"),
             msg("m2", "assistant", "第一条回答"),
             msg("m3", "user", "追一个问题")]
    w1 = await transcript.append("chatgpt", conv, batch, source="site")
    w2 = await transcript.append("chatgpt", conv, batch, source="site")
    w3 = await transcript.append("chatgpt", conv, batch[::-1], source="site")
    print("1) 首写 %d, 重复写 +%d/跳过 %d, 乱序再写 +%d/跳过 %d"
          % (w1["written"], w2["written"], w2["skipped"], w3["written"], w3["skipped"]))
    if (w1["written"], w2["written"], w2["skipped"], w3["written"], w3["skipped"]) != (3, 0, 3, 0, 3):
        bad.append("幂等坏了: 同一份对话被反复写入")
    if len(transcript.read("chatgpt", conv)) != 3:
        bad.append("读回来不是 3 条")
    if transcript.read("chatgpt", conv)[0]["msg_id"] != "m1":
        bad.append("乱序重播把文档顺序打乱了")
    # 空正文的占位节点要丢掉
    w = await transcript.append("chatgpt", conv, [msg("m9", "assistant", "   "), {}],
                                source="site")
    if w["written"]:
        bad.append("空白/非 dict 的占位消息被写进去了")

    # 2) 同一条变长 -> 追加新版本, 读回最全那版且只有一条
    reset()
    await transcript.append("chatgpt", conv, [msg("a1", "assistant", "从前有个")], source="site")
    await transcript.append("chatgpt", conv,
                            [msg("a1", "assistant", "从前有座山, 山里有个故事")], source="site")
    got = transcript.read("chatgpt", conv)
    lines = transcript.path_for("chatgpt", conv).read_text(encoding="utf-8").strip().splitlines()
    print("2) 变长后: 盘上 %d 行, 读回 %d 条, 文本 %r" % (len(lines), len(got), got[0]["text"]))
    if len(lines) != 2:
        bad.append("同一条回答的更新版本没有追加成第二行(说明旧版本被当成已存在而丢掉了)")
    if len(got) != 1 or got[0]["text"] != "从前有座山, 山里有个故事":
        bad.append("读回的应该是最全那一版、且只有一条: %r" % (got,))

    # 3) 跨来源: captured(无站点 id) 先记, site 之后读回同一句 -> 流水账两行, 投影一条
    reset()
    await transcript.append("chatgpt", conv, [{"role": "user", "text": "桥发出去的话"}],
                            source="captured", turn="turn-1")
    w = await transcript.append("chatgpt", conv, [msg("z1", "user", "桥发出去的话")],
                                source="site")
    got = transcript.read("chatgpt", conv)
    raw = transcript.path_for("chatgpt", conv).read_text(encoding="utf-8").strip().splitlines()
    print("3) captured 后 site 读回同一条: 盘上 %d 行(流水账), 读回 %d 条" % (len(raw), len(got)))
    if len(got) != 1:
        bad.append("同一句话在投影里出现了两次(captured 与 site 没并到一起): %r" % (got,))
    elif got[0].get("msg_id") != "z1":
        bad.append("留下的应该是带站点 id 的那版, 之后深读才认得它: %r" % (got[0],))
    elif got[0].get("turn") != "turn-1":
        bad.append("并版时把桥那一轮的 turn 字段挤掉了, 之后对不上是界面上哪条消息")
    if w["written"] != 1:
        bad.append("流水账就不该改写已有行, 站点那版必须留下痕迹(否则版本信息丢了)")

    # 4) 长文本一字不丢
    reset()
    body = "回答" + "x" * (LONG - 2)
    await transcript.append("chatgpt", conv, [msg("big", "assistant", body)], source="site")
    back = transcript.read("chatgpt", conv)[0]["text"]
    print("4) 写了 %d 字, 读回 %d 字" % (len(body), len(back)))
    if back != body:
        bad.append("长回答被截断/改写了: %d -> %d" % (len(body), len(back)))

    # 5) 路径穿越: 站点 URL 里那段不能决定文件落在哪
    reset()
    for evil in ["../../../../Windows/system32/config", "..%2f..%2fetc/passwd", "...",
                 "a/../../b", ""]:
        p = transcript.path_for("chatgpt", evil)
        inside = p.resolve().is_relative_to(TMP.resolve())
        print("5) conv=%-34r -> %s  在目录内=%s" % (evil[:34], p.name, inside))
        if not inside:
            bad.append("conv=%r 逃出了 transcripts 目录: %s" % (evil, p))
        if evil == "...":
            if p.name != "_no-id.jsonl":
                bad.append("全是点的会话名没有退到占位名, 会变成一个能往上跳的目录名")
    w = await transcript.append("chatgpt", "../../../../Windows/system32/config",
                                [msg("e1", "user", "穿越测试")], source="site")
    if not Path(w["path"]).resolve().is_relative_to(TMP.resolve()):
        bad.append("append 实际写到了目录外: " + w["path"])
    if len(transcript.read("chatgpt", "../../../../Windows/system32/config")) != 1:
        bad.append("消毒后的会话名读不回来(写和读用了两套名字)")

    # 6) 接力链: 换窗口后第一段带 relay_of, 且只带一次
    reset()
    prev = "conv-prev"
    await transcript.append("chatgpt", prev, [msg("p1", "user", "上一段的问题")], source="site")
    transcript.mark_relay("chatgpt", prev)
    await transcript.append("chatgpt", prev, [msg("p2", "user", "还在同一段里追加")], source="site")
    same = transcript.read("chatgpt", prev)
    nxt = "conv-next"
    await transcript.append("chatgpt", nxt, [msg("n1", "user", "新窗口的第一条")], source="site")
    first = transcript.read("chatgpt", nxt)
    await transcript.append("chatgpt", nxt, [msg("n2", "user", "新窗口的第二条")], source="site")
    second = transcript.read("chatgpt", nxt)[-1]
    print("6) 同段追加带 relay_of=%s; 新段第一条 relay_of=%r; 新段第二条 relay_of=%s"
          % (any("relay_of" in r for r in same), first[0].get("relay_of"),
             "relay_of" in second))
    if any("relay_of" in r for r in same):
        bad.append("还在同一段对话里就被标成了接力")
    if first[0].get("relay_of") != transcript._seg(prev, ""):
        bad.append("新窗口第一条没有指回上一段: %r" % (first[0],))
    if "relay_of" in second:
        bad.append("接力链被重复标到每一行(应该只标换窗口后的第一段)")
    convs = transcript.conversations("chatgpt")
    print("   conversations(): %s" % convs)
    if transcript._seg(prev, "") not in convs or transcript._seg(nxt, "") not in convs:
        bad.append("conversations() 没列出已存的两段对话")

    # 7a) 并发写同一个文件
    reset()
    async def writer(k: int):
        rows = [msg("c%d-%d" % (k, i), "assistant", "第 %d 组第 %d 条" % (k, i)) for i in range(12)]
        await transcript.append("chatgpt", conv, rows, source="site")
    await asyncio.gather(*(writer(k) for k in range(8)))
    raw = transcript.path_for("chatgpt", conv).read_text(encoding="utf-8").strip().splitlines()
    torn = [ln for ln in raw if not _parses(ln)]
    print("7a) 8 路并发各 12 条 -> 盘上 %d 行, 坏行 %d" % (len(raw), len(torn)))
    if len(raw) != 96:
        bad.append("并发写丢了行或写了重复: %d 行(应为 96)" % len(raw))
    if torn:
        bad.append("并发写串行/写坏了 %d 行" % len(torn))
    if len(transcript.read("chatgpt", conv)) != 96:
        bad.append("并发写完读回不是 96 条")

    # 7b) 文件里混进坏行(比如写一半断电)不能拖累其余
    reset()
    await transcript.append("chatgpt", conv, batch, source="site")
    p = transcript.path_for("chatgpt", conv)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write('{"ts": 1, "role": "assistant", "te\n')          # 半行
        f.write("not json at all\n")
    got = transcript.read("chatgpt", conv)
    print("7b) 掺 2 行垃圾后读回 %d 条" % len(got))
    if len(got) != 3:
        bad.append("坏行把整份文件毒掉了(读回 %d 条, 应为 3)" % len(got))
    # 重启进程(缓存清空)之后仍然认得已写过的内容 -> 不会重复追加
    transcript._seen.clear()
    w = await transcript.append("chatgpt", conv, batch, source="site")
    print("   清掉内存缓存后再同步: 新写 %d/跳过 %d" % (w["written"], w["skipped"]))
    if w["written"]:
        bad.append("进程重启后把已存的内容又写了一遍(去重只记在内存里)")

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("TRANSCRIPT_STORE_OK "
          "(反复同步幂等 / 流式变长追加新版 / 跨来源不重复 / 20 万字不截断 / "
          "URL 里的会话名跳不出目录 / 接力链只标一次 / 8 路并发 96 行无坏行)")
    return 0


def _parses(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
