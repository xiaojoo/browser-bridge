"""excerpt_for 的打分自检: 按"下一棒要办的事"捞原文, 必须捞得准、不能只捞最大的那坨。

合成语料盯 5 条:
  1) 命中 needle 的那条要被选上;
  2) 全场最长但与话题无关的那条**不能**被选上(否则"按字数排序"就能冒充这个功能);
  3) 结果保持文档顺序(拼回去才是一段能读的对话);
  4) 中文无空格查询要靠两字滑窗命中; 一个词都不挨着时返回空, 而不是"全都带上";
  5) 没给任务时退化成"最后几条"。
再对**真实落盘的那段对话**(如果存在)量一次: 表格里那些低频字段名能不能被捞回来。
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import transcript  # noqa: E402

TMP = ROOT_DIR / ".tmp" / "excerpt-check"
REAL = ROOT_DIR / "transcripts" / "chatgpt" / "6a97137f-1db8-83ea-a8c8-1eb599c3166f.jsonl"

# 一条特别长的无关消息(下标 6), 专门用来骗"按长度挑"的实现
NOISE = "通用讨论。" * 2600


def corpus():
    return [
        {"id": "m0", "role": "user", "text": "帮我把构建脚本理一遍"},
        {"id": "m1", "role": "assistant", "text": "好的, 先看一下目录结构"},
        {"id": "m2", "role": "assistant", "text": "服务器返回: Persistence-M 列全是 N/A, "
                                                 "Bus-Id 是 00000000:01:00.0, 这说明驱动没常驻"},
        {"id": "m3", "role": "user", "text": "那换个思路"},
        {"id": "m4", "role": "assistant", "text": "可以, 我们聊聊别的模块"},
        {"id": "m5", "role": "user", "text": "先放着"},
        {"id": "m6", "role": "assistant", "text": NOISE},
        {"id": "m7", "role": "assistant", "text": "补充一句: Bus-Id 要配合 pci 重扫才生效"},
    ]


async def main() -> int:
    bad = []
    shutil.rmtree(TMP, ignore_errors=True)
    transcript.ROOT = TMP
    transcript._seen.clear()
    conv = "excerpt-conv"
    await transcript.append("chatgpt", conv, corpus(), source="site")

    def texts(rows):
        return " || ".join(str(r["text"]) for r in rows)

    # 1+2) 中文无空格查询: 要命中 m2/m7, 不能把最长的 m6 带进来
    q = "接着排查显卡常驻问题, 需要 Persistence-M 和 Bus-Id 的原始输出"
    got = transcript.excerpt_for("chatgpt", conv, q, budget=6000, max_items=4)
    ids = [g["msg_id"] for g in got]
    print("1) 查询=%r -> 选到 %s (字数 %d)"
          % (q[:22], ids, sum(len(g["text"]) for g in got)))
    if "m2" not in ids:
        bad.append("带 needle 的那条没被捞回来: %s" % ids)
    if "m7" not in ids:
        bad.append("另一条提到 Bus-Id 的也该捞回来: %s" % ids)
    if "m6" in ids:
        bad.append("最长的无关消息被捞进来了(说明打分其实在按长度挑)")
    if [g for g in got if g["score"] <= 0]:
        bad.append("0 分的消息也被拿来凑数: %s"
                   % [(g["msg_id"], g["score"]) for g in got if g["score"] <= 0])
    if ids != sorted(ids):
        bad.append("结果不是文档顺序: %s" % ids)

    # 3) 预算: 只给很小的预算时不能爆
    got2 = transcript.excerpt_for("chatgpt", conv, q, budget=120, max_items=4)
    tot = sum(len(g["text"]) for g in got2)
    print("2) 预算 120 字 -> 选到 %s, 合计 %d 字" % ([g["msg_id"] for g in got2], tot))
    if not got2:
        bad.append("预算小也不该一条都不给(needle 那条本身就有 60+ 字)")

    # 4) 完全不相干的查询 -> 空, 而不是"全都带上"
    got3 = transcript.excerpt_for("chatgpt", conv, "今天天气不错", budget=6000)
    print("3) 不相干查询 -> %s" % [g["msg_id"] for g in got3])
    if got3:
        bad.append("八竿子打不着的查询也捞到了东西(打分没有分辨力): %s"
                   % [(g["msg_id"], g["score"]) for g in got3])

    # 5) 没给任务 -> 退化成最后几条
    got4 = transcript.excerpt_for("chatgpt", conv, "", budget=6000, max_items=2)
    print("4) 空任务 -> %s" % [g["msg_id"] for g in got4])
    if [g["msg_id"] for g in got4] != ["m6", "m7"]:
        bad.append("空任务时应退回最后两条: %s" % [g["msg_id"] for g in got4])

    # 6) 真实数据(如果这台机器上有): 表格里那些低频字段名捞不捞得回来
    if REAL.exists():
        transcript.ROOT = ROOT_DIR / "transcripts"
        transcript._seen.clear()
        real_conv = REAL.stem
        rows = [json.loads(l) for l in REAL.open(encoding="utf-8") if l.strip()]
        rq = "接着排查显卡状态, 需要 nvidia-smi 那张表里 Persistence-M 和 Bus-Id 的原始输出"
        rgot = transcript.excerpt_for("chatgpt", real_conv, rq, budget=12000, max_items=6)
        rt = texts(rgot)
        print("5) 真实对话 %d 条 -> 捞回 %d 条 %d 字, 含 Persistence-M=%s 含 Bus-Id=%s"
              % (len(rows), len(rgot), len(rt), "Persistence-M" in rt, "Bus-Id" in rt))
        if "Persistence-M" not in rt or "Bus-Id" not in rt:
            bad.append("真实对话里的表格字段名没被捞回来, 那笔记丢了就真丢了")
        longest = max(len(r["text"]) for r in rows)
        print("   (全场最长一条 %d 字; 捞回的占 %d 字)" % (longest, len(rt)))
    else:
        print("5) 跳过: 没找到真实落盘的对话 %s —— 这条是本次最有说服力的证据, 别忽略" % REAL)

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("EXCERPT_OK (中文滑窗命中 / 不被最长消息骗到 / 文档顺序 / 不相干查询返回空 / "
          "空任务退化成最后几条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
