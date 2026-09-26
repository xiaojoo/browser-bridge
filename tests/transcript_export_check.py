"""接力链 + Markdown 导出的自检(离线: 不联网、不开浏览器、只在临时目录里写)。

链是这层的全部价值所在: 换过一次窗口就留下一个 relay_of, 导出要能顺着它把
"分散在 2~3 个会话里的同一次工作"拼回一份文件。而真实的"上下文到上限自动换窗口"
要 ChatGPT 自己拒绝输入才触发, 线上造不出来 —— 所以这里用**同一套 API**
(`append` + `mark_relay` + `write_relay`)手工接出一条链来验。

盯的几条:
  1) 链能双向走(从中间那段出发, 前面后面都找得到), 且从任一段出发结果一样;
  2) 窗口编号 / "共 N 个窗口" / 实际打出来的节数三者对得上(链上有个不存在的环节时也要对得上);
  3) 交接笔记插在**它所连接的两个窗口之间**, 不是堆在文件末尾;
  4) 每条正文逐字进文件, 不截断;
  5) 导出的文件名不接受任何外部路径(会话 id 里塞 ../ 也跳不出 exports/);
  6) relay_of 被写成环时不能死循环。
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

TMP = ROOT_DIR / ".tmp" / "export-check"


def reset():
    shutil.rmtree(TMP, ignore_errors=True)
    transcript.ROOT = TMP
    transcript._seen.clear()
    transcript._relay.clear()


def rows_of(conv):
    p = transcript.path_for("chatgpt", conv)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


async def main() -> int:
    bad = []

    # ---- 1) 接出一条 A -> B -> C 的链
    reset()
    A, B, C = "conv-aaaa", "conv-bbbb", "conv-cccc"
    await transcript.append("chatgpt", A, [
        {"id": "a1", "role": "user", "text": "第一段: 要做的东西是每周跑一次的显卡检查"},
        {"id": "a2", "role": "assistant", "text": "nvidia-smi 输出: Persistence-M 全 N/A, Bus-Id 00000000:01:00.0"},
        {"id": "a3", "role": "user", "text": "记下来"}], source="site")
    transcript.mark_relay("chatgpt", A)
    await transcript.append("chatgpt", B, [
        {"id": "b1", "role": "user", "text": "第二段: 上下文到上限了, 这是接力过来的"},
        {"id": "b2", "role": "assistant", "text": "好的, 继续"}], source="site")
    transcript.mark_relay("chatgpt", B)
    await transcript.append("chatgpt", C, [
        {"id": "c1", "role": "user", "text": "第三段: 又换了一次窗口"}], source="site")
    transcript.mark_relay("chatgpt", C)          # 指向一个还不存在的下一段
    transcript.write_relay("chatgpt", A, "# 交接: 显卡检查脚本\n\n## 下一步\n写 cron")
    transcript.write_relay("chatgpt", B, "# 交接: 第二段笔记")

    chain_a = transcript.chain_of("chatgpt", A)
    chain_b = transcript.chain_of("chatgpt", B)
    chain_c = transcript.chain_of("chatgpt", C)
    print("1) 链: 从A=%s 从B=%s 从C=%s" % (chain_a, chain_b, chain_c))
    if chain_a != [A, B, C] or chain_b != [A, B, C] or chain_c != [A, B, C]:
        bad.append("链没能双向走: A=%s B=%s C=%s" % (chain_a, chain_b, chain_c))
    if rows_of(B)[0].get("relay_of") != A or rows_of(C)[0].get("relay_of") != B:
        bad.append("relay_of 没写进新段的第一条")

    # ---- 2+3) 导出: 编号/计数/笔记位置
    d = transcript.export_markdown("chatgpt", B)
    md = d["markdown"]
    print("2) 导出 %d 个窗口 %d 条 %d 字" % (d["windows"], d["messages"], d["chars"]))
    n_head = md.count("\n## 窗口 ")
    if not (n_head == d["windows"] == 3):
        bad.append("「%d 个窗口」与实际打出的节数 %d 不符" % (d["windows"], n_head))
    if "共 6 条消息" not in md and "6 条消息" not in md:
        bad.append("合计条数没写对: %r" % md[:220])
    for i in (1, 2, 3):
        if "## 窗口 %d/3" % i not in md:
            bad.append("缺第 %d 个窗口的分节标题" % i)
    # ts 是捕获时刻, 同一批深读下来全都相同 -> 不能写成 "14:30 ~ 14:30" 让人以为只聊了 0 分钟
    line1 = [l for l in md.splitlines() if l.startswith("6 条消息") or "条消息 ·" in l]
    print("   窗口行: %r" % (line1[0][:70] if line1 else "没有"))
    if not line1 or "捕获" not in line1[0]:
        bad.append("窗口那行没写明时间是「捕获」时刻: %r" % (line1[:1],))
    elif "~" in line1[0]:
        bad.append("同一批捕获被写成了时间段(看着像 0 分钟的对话): %r" % line1[0][:80])
    ia, ib, ic = md.index("## 窗口 1/3"), md.index("## 窗口 2/3"), md.index("## 窗口 3/3")
    na = md.index("显卡检查脚本")
    nb = md.index("第二段笔记")
    if not (ia < na < ib):
        bad.append("A->B 的交接笔记没插在两个窗口之间(A 笔记位置 %d, 窗口1 %d, 窗口2 %d)"
                   % (na, ia, ib))
    if not (ib < nb < ic):
        bad.append("B->C 的交接笔记位置不对")
    if md.rindex("第二段笔记") > ic + 40:
        bad.append("笔记堆到了文件末尾而不是窗口之间")

    # ---- 4) 逐字
    for m in [rows_of(A)[1]["text"], rows_of(B)[0]["text"], rows_of(C)[0]["text"]]:
        if m not in md:
            bad.append("有一条正文没逐字进文件: %r" % m[:40])
    long_text = "长" * 50000
    await transcript.append("chatgpt", "conv-long", [{"id": "L1", "role": "assistant",
                                                      "text": long_text}], source="site")
    dl = transcript.export_markdown("chatgpt", "conv-long")
    print("4) 5 万字的单条回答 -> 导出里 %s 字, 完整=%s"
          % (len(long_text), long_text in dl["markdown"]))
    if long_text not in dl["markdown"]:
        bad.append("5 万字那条被截断了")

    # ---- 5) 导出文件名不接受外部路径
    p = transcript.save_export("chatgpt", "../../../Windows/system32/evil", "# x")
    inside = p.resolve().is_relative_to((TMP / "exports").resolve())
    print("5) 恶意会话名 -> %s 在 exports 内=%s" % (p.name, inside))
    if not inside:
        bad.append("导出文件被写到了 exports 外面: " + str(p))
    okp = transcript.save_export("chatgpt", C, md)
    if okp.read_text(encoding="utf-8") != md:
        bad.append("写出去的文件和 export_markdown 的内容不一致")
    print("   正常导出 -> %s (%d 字)" % (okp.relative_to(ROOT_DIR).as_posix(), len(md)))

    # ---- 6) 环 + 悬空指向
    reset()
    await transcript.append("chatgpt", "X", [{"id": "x1", "role": "user", "text": "X 段"}], source="site")
    await transcript.append("chatgpt", "Y", [{"id": "y1", "role": "user", "text": "Y 段"}], source="site")
    # 手工把两段互相指成上游(模拟被改坏/写错的 relay_of)
    for cid, other in (("X", "Y"), ("Y", "X")):
        f = transcript.path_for("chatgpt", cid)
        rs = rows_of(cid)
        rs[0]["relay_of"] = other
        f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rs) + "\n",
                     encoding="utf-8", newline="\n")
    ch = transcript.chain_of("chatgpt", "X")
    print("6) 互相指向时链=%s (必须终止、不重复、且两段都在)" % ch)
    if sorted(ch) != ["X", "Y"] or len(set(ch)) != len(ch):
        # 环本身没有合法的先后顺序, 所以只要求"收得住": 不死循环、不重复、不漏段
        bad.append("环没被 seen 挡住: %s" % ch)
    await transcript.append("chatgpt", "Z", [{"id": "z1", "role": "user", "text": "Z 段"}],
                            source="site")
    fz = transcript.path_for("chatgpt", "Z")
    rs = rows_of("Z")
    rs[0]["relay_of"] = "不存在的段"
    fz.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rs) + "\n",
                  encoding="utf-8", newline="\n")
    dz = transcript.export_markdown("chatgpt", "Z")
    print("   悬空上游: 链=%s 窗口=%d 节数=%d"
          % (dz["chain"], dz["windows"], dz["markdown"].count("\n## 窗口 ")))
    if dz["windows"] != dz["markdown"].count("\n## 窗口 "):
        bad.append("悬空环节让「窗口数」和实际节数对不上: %d vs %d"
                   % (dz["windows"], dz["markdown"].count("\n## 窗口 ")))

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("TRANSCRIPT_EXPORT_OK (链双向可走 / 编号与节数一致 / 笔记插在窗口之间 / "
          "5 万字逐字进文件 / 恶意会话名跳不出 exports / 环与悬空指向都能收)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
