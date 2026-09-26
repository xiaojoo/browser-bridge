"""逐条对话落盘 (append-only JSONL)。

换窗口接力会把站点那份完整对话压成一份摘要, 摘要没留住的细节之后就永远找不回来了
—— 交接笔记连"去哪查"都给不出, 因为原件从来没存在过。这里在**汇总之前**先把逐条
原文写进 `transcripts/<provider>/<conv_id>.jsonl`, 交接笔记只带这个路径指针。

一行一条: `{ts, provider, conv, msg_id, role, text, turn, relay_of, source, bk}`
  source: `site`     = 从浏览器窗口读回来的(含你在站点里手打的那几轮)
          `captured` = 桥自己这一轮捕获到的用户输入 + 回答(写的时候站点往往还没给 id)

文件是**流水账**, 不是数据库: 只追加, 从不改写已有行。同一个 `(站点消息 id, 正文)`
只写一次, 所以反复同步同一份对话一行都不会多; 而某条回答在流式中变长了, 正文哈希就
是新的 -> 追加一条新版本。"那份完整对话"是 `read()` 的投影, 折叠规则见它的 docstring。

conv_id 来自页面 URL, 属于外部输入 -> 一律过 `_seg()` 消毒, 不允许它跳出 transcripts 目录。
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path

from . import config

log = logging.getLogger("bridge")

ROOT: Path = config.BASE_DIR / "transcripts"
NO_ID = "_no-id"                         # 站点还没分配会话 id(新开对话时 URL 里没有 /c/)

_lock = asyncio.Lock()
_seen: dict[Path, set] = {}              # 文件 -> 已写过的 (站点 id, 正文) 组合
_relay: dict[str, str] = {}              # provider -> 刚被接力换掉的会话 id, 下一条带 relay_of


def _seg(v, fallback: str) -> str:
    """把外部来的一段路径裁成安全文件名: 只留 A-Za-z0-9.-_, 且不留纯点(那能往上跳)。"""
    s = re.sub(r"[^A-Za-z0-9._\-]", "", str(v or ""))[:80].strip(".")
    return s or fallback


def path_for(provider: str, conv: str | None) -> Path:
    return ROOT / _seg(provider, "unknown") / (_seg(conv, NO_ID) + ".jsonl")


def _bk(m: dict) -> str:
    body = str(m.get("role") or "") + "\x00" + str(m.get("text") or "")
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()


def _pair(mid: str, bk: str) -> str:
    return mid + "\x00" + bk


def _load_seen(p: Path) -> set:
    cached = _seen.get(p)
    if cached is not None:
        return cached
    pairs: set[str] = set()
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001  半行/被手工改过 -> 跳过, 不拖累其余
                    continue
                if isinstance(r, dict) and r.get("bk"):
                    pairs.add(_pair(str(r.get("msg_id") or ""), str(r["bk"])))
    except OSError:
        pass
    _seen[p] = pairs
    return pairs


async def append(provider: str, conv: str | None, messages: list[dict], *,
                 source: str = "site", turn: str = "") -> dict:
    """把一批消息追加进去, 写过的跳过。返回 `{written, skipped, path, conv}`。

    messages 用 capture 那份形状就行: `{role, text, id}`。空正文的丢掉 —— 站点滚动时
    会挂载一些占位节点。
    """
    p = path_for(provider, conv)
    this_conv = _seg(conv, NO_ID)
    rows = [m for m in (messages or []) if isinstance(m, dict)
            and str(m.get("text") or "").strip()]
    if not rows:
        return {"written": 0, "skipped": 0, "path": str(p),
                "rel": p.relative_to(ROOT).as_posix(), "conv": this_conv}
    async with _lock:
        pairs = _load_seen(p)
        relay_of = _relay.get(provider, "")
        if relay_of == this_conv:
            relay_of = ""                    # 还在同一段对话里, 不算接力
        ts = round(time.time(), 3)
        prov = _seg(provider, "unknown")
        lines: list[str] = []
        skipped = 0
        for m in rows:
            mid = str(m.get("id") or m.get("msg_id") or "")
            bk = _bk(m)
            key = _pair(mid, bk)
            if key in pairs:
                skipped += 1
                continue
            pairs.add(key)
            rec = {"ts": ts, "provider": prov, "conv": this_conv, "msg_id": mid,
                   "role": str(m.get("role") or ""), "text": str(m.get("text") or ""),
                   "turn": turn, "source": source, "bk": bk}
            if relay_of:
                rec["relay_of"] = relay_of
            lines.append(json.dumps(rec, ensure_ascii=False))
        if lines:
            p.parent.mkdir(parents=True, exist_ok=True)
            # newline="\n": Windows 上默认会写成 CRLF, 行尾就不好 grep 了
            with p.open("a", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
            if relay_of:
                _relay.pop(provider, None)   # 链已经落到盘上, 不再重复标后面的行
        return {"written": len(lines), "skipped": skipped, "path": str(p),
                "rel": p.relative_to(ROOT).as_posix(), "conv": this_conv}


def mark_relay(provider: str, conv: str | None) -> None:
    """记一下"这段对话刚被接力换掉": 下一个新会话的首条记录里写 relay_of。"""
    c = _seg(conv, "")
    if c:
        _relay[_seg(provider, "unknown")] = c


def relay_path(provider: str, conv: str | None) -> Path:
    """一次接力的交接笔记存在这里(侧车, 不进 JSONL —— 那是"逐条对话", 这是"两个窗口之间的桥")。"""
    return path_for(provider, conv).with_suffix(".relay.md")


def write_relay(provider: str, conv: str | None, note: str, *, task: str = "",
                meta: dict | None = None) -> dict:
    """把刚生成的交接笔记落盘, 挂在**上一段**对话上(下一段的 id 那时还没分配)。"""
    p = relay_path(provider, conv)
    m = meta or {}
    head = ["<!-- relay_of: %s -->" % _seg(conv, NO_ID),
            "<!-- 生成时间: %s -->" % time.strftime("%Y-%m-%d %H:%M:%S")]
    if task:
        head.append("<!-- 下一棒要办的事: %s -->" % " ".join(task.split())[:200])
    for k in ("carry_chars", "note_chars", "excerpts", "excerpt_chars"):
        if m.get(k) is not None:
            head.append("<!-- %s: %s -->" % (k, m[k]))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(head) + "\n\n" + (note or "").strip() + "\n",
                 encoding="utf-8", newline="\n")
    return {"path": str(p), "chars": len(note or "")}


def read_relay(provider: str, conv: str | None) -> str:
    p = relay_path(provider, conv)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _parents(provider: str) -> dict[str, str]:
    """每个文件只看第一条能解析的行: 里面有 relay_of 就说明这段对话是接力来的。"""
    d = ROOT / _seg(provider, "unknown")
    out: dict[str, str] = {}
    if not d.is_dir():
        return out
    for f in d.glob("*.jsonl"):
        try:
            with f.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(r, dict) and r.get("relay_of"):
                        out[f.stem] = _seg(r["relay_of"], "")
                    break
        except OSError:
            continue
    return out


def chain_of(provider: str, conv: str | None, max_hops: int = 40) -> list[str]:
    """从这段对话出发, 双向走出整条接力链, 返回 最早 -> 最新 的会话 id 列表。

    往前: 这段的 relay_of 是谁; 往后: 谁的 relay_of 是这段(换窗口之后的那一截)。
    带 seen 和 max_hops: relay_of 是外部可读的字段, 万一被手工改成环也不能卡死。
    """
    parents = _parents(provider)
    start = _seg(conv, NO_ID)
    out, seen = [start], {start}
    while len(out) < max_hops:
        p = parents.get(out[0], "")
        if not p or p in seen:
            break
        out.insert(0, p)
        seen.add(p)
    child = {v: k for k, v in parents.items()}
    while len(out) < max_hops:
        c = child.get(out[-1], "")
        if not c or c in seen:
            break
        out.append(c)
        seen.add(c)
    return out


def export_markdown(provider: str, conv: str | None) -> dict:
    """把整条接力链拼成一份 Markdown: 按窗口分节, 窗口之间插那一次接力的交接笔记。

    正文按原样写入、不转义(这份东西是给人和 agent 回去查原文的, 不是网页)。
    """
    prov = _seg(provider, "unknown")
    start = _seg(conv, NO_ID)
    # 先剪掉链上指向不存在文件的环节(被手工删了/写错的 relay_of), 否则"3 个窗口"和实际节数会对不上
    body = {c: read(prov, c) for c in chain_of(provider, conv)}
    chain = [c for c in body if body[c] or c == start]
    blocks: list[str] = []
    n_msgs = n_chars = 0
    for i, cid in enumerate(chain, 1):
        msgs = body[cid]
        chars = sum(len(str(m.get("text") or "")) for m in msgs)
        n_msgs += len(msgs)
        n_chars += chars
        # ts 是"我们什么时候读到它的", 不是站点里那条消息的时间 —— 一批深读下来 56 条会共用
        # 同一个时间戳, 写成 "14:30 ~ 14:30" 会让人以为这段对话只聊了 0 分钟。
        t0 = time.strftime("%H:%M", time.localtime(msgs[0]["ts"])) if msgs else "-"
        t1 = time.strftime("%H:%M", time.localtime(msgs[-1]["ts"])) if msgs else "-"
        when = "捕获 %s" % t0 if t0 == t1 else "捕获 %s ~ %s" % (t0, t1)
        blocks.append("## 窗口 %d/%d · `%s`\n\n%s 条消息 · %s 字 · %s · 记录文件 `%s`\n"
                      % (i, len(chain), cid, len(msgs), f"{chars:,}", when,
                         (ROOT / prov / (cid + ".jsonl")).relative_to(ROOT.parent).as_posix()))
        for j, m in enumerate(msgs, 1):
            who = "用户" if str(m.get("role")) == "user" else "助手"
            head = "### [%d] %s · %s 字\n\n" % (j, who, f"{len(str(m.get('text') or '')):,}")
            blocks.append(head + str(m.get("text") or "").strip() + "\n")
        relay = read_relay(prov, cid)
        if relay and i < len(chain):
            blocks.append("---\n\n## 那一次换窗口带过去的交接笔记\n\n" + relay.strip() + "\n")
    md = ("# 对话记录 · %s\n\n"
          "- 接力链: %s（%d 个窗口）\n"
          "- 合计 %s 条消息 / %s 字\n"
          "- 导出时间: %s\n"
          "- 正文按原样写入未转义; 每条的完整原文以对应 `.jsonl` 为准\n" % (
              prov, " → ".join("`%s`" % c for c in chain), len(chain),
              f"{n_msgs:,}", f"{n_chars:,}", time.strftime("%Y-%m-%d %H:%M:%S"))
          ) + "\n" + "\n".join(blocks)
    # 正文里的图片是相对 transcripts/ 写的(media/<provider>/<conv>/x.png); 导出文件在
    # transcripts/exports/ 下, 所以要往上退一层才点得开。
    md = md.replace("](media/", "](../media/")
    return {"markdown": md, "chain": chain, "windows": len(chain),
            "messages": n_msgs, "chars": n_chars, "provider": prov,
            "conv": _seg(conv, NO_ID)}


def save_export(provider: str, conv: str | None, markdown: str) -> Path:
    """导出目录固定在本仓库 transcripts/exports/ 下: 名字只由 provider/会话 id/时间戳拼,
    不接受任何外部路径 —— 工作区那个根目录是可以被接口改指到任意盘的, 不能借它写出去。"""
    d = ROOT / "exports"
    d.mkdir(parents=True, exist_ok=True)
    name = "%s-%s-%s.md" % (_seg(provider, "unknown"), _seg(conv, NO_ID)[:8],
                            time.strftime("%Y%m%d-%H%M%S"))
    p = d / name
    p.write_text(markdown, encoding="utf-8", newline="\n")
    return p


def read(provider: str, conv: str | None) -> list[dict]:
    """这段对话的完整一份, 文档顺序。流水账 -> 投影的三条折叠规则:

      * 同一站点 id 出现多版(流式中变长) -> 就地换成最后那版, 位置保持在第一次出现处;
      * 没有站点 id 的那条, 正文和已有任何一条重复 -> 丢掉, 让**带站点 id 的那版留下**
        (桥先记了一份没 id 的, 站点之后会补上带 id 的同一条);
      * 被丢掉那条独有的 `turn` / `relay_of` 合并进留下的那条, 别把"这是桥的哪一轮"丢了。
    """
    p = path_for(provider, conv)
    out: list[dict] = []
    by_id: dict[str, int] = {}
    by_text: dict[tuple, int] = {}
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(r, dict) or not str(r.get("text") or "").strip():
                    continue
                mid = str(r.get("msg_id") or "")
                tk = (str(r.get("role") or ""), str(r.get("text") or ""))
                i = by_id.get(mid) if mid else None
                if i is not None:                          # 同一条的更新版本
                    old = out[i]
                    _inherit(r, old)
                    by_text.pop((old.get("role"), old.get("text")), None)
                    out[i] = r
                    by_text[tk] = i
                    continue
                j = by_text.get(tk)
                if j is not None:                          # 正文已经有了
                    old = out[j]
                    if mid and not str(old.get("msg_id") or ""):
                        _inherit(r, old)                   # 带站点 id 的那版上位
                        out[j] = r
                        by_id[mid] = j
                    else:
                        _inherit(old, r)                   # 只把它独有的字段并过来
                    continue
                by_text[tk] = len(out)
                if mid:
                    by_id[mid] = len(out)
                out.append(r)
    except OSError:
        return []
    return out


def _inherit(dst: dict, src: dict) -> None:
    """dst 缺的字段用 src 的补上(合并两条记录时不丢信息)。"""
    for k in ("turn", "relay_of"):
        if not dst.get(k) and src.get(k):
            dst[k] = src[k]


def _terms(q: str) -> set:
    """把一句话拆成可匹配的词条: 拉丁词按词(>=3 字符), 中文按相邻两字。

    不做分词、不引模型 —— 这里只要一个能分辨"这条比那条更相关"的最朴素打分。
    """
    s = str(q or "").lower()
    out = set(re.findall(r"[a-z0-9_./+-]{3,}", s))
    han = "".join(re.findall(r"[一-鿿]", s))
    out |= {han[i:i + 2] for i in range(len(han) - 1)}
    return {t for t in out if len(t) >= 2}


def excerpt_for(provider: str, conv: str | None, query: str, *, budget: int = 3000,
                max_items: int = 3, extra_query: str = "") -> list[dict]:
    """按"下一棒要办的事"从这段对话里捞出最相关的几条**原文**。

    交接笔记只有一千多字, 报错原文/参数/路径这类细节装不下 —— 这些就是从落盘那份里
    按话题捞回来的补充。

    打分是一层最朴素的 IDF: 查询拆成词条, 每个词按"它出现在几条消息里"取倒数加权
    (满篇都有的词不分辨, 只有一两条才有的词才是线索), 消息得分 = 命中词的权重和。
    不按命中条数算是因为: 那样一条 14000 字的表会永远赢过一条 200 字的准确回答。
    extra_query 一般塞笔记开头 —— 只按用户的说法经常捞不到(人说"照着刚才那份继续",
    不会把字段名再敲一遍), 而笔记里已经写着要动哪些名词。

    返回文档顺序的 `[{..., score}]`, 正文累计不超过 budget。
    """
    msgs = read(provider, conv)
    if not msgs:
        return []
    terms = _terms(str(query or "") + " " + str(extra_query or ""))
    if not terms:                              # 没说这一棒要干什么 -> 退化成"带最后几条"
        return [dict(m, score=0.0) for m in msgs[-max_items:]]
    texts = [str(m.get("text") or "") for m in msgs]
    tsets = [_terms(t) & terms for t in texts]
    df: dict[str, int] = {}
    for st in tsets:
        for t in st:
            df[t] = df.get(t, 0) + 1
    ceiling = max(2, int(0.4 * len(msgs)))     # 四成消息都有的词没有分辨力, 丢掉
    keep = {t: 1.0 / (1 + c) for t, c in df.items() if c <= ceiling}
    if not keep:
        return []
    keys = keep.keys()
    scored = sorted(((sum(keep[t] for t in (st & keys)), i) for i, st in enumerate(tsets)),
                    reverse=True)
    picked, used = [], 0
    for sc, i in scored:
        if len(picked) >= max_items or sc <= 0:
            break                              # 0 分的消息带不来任何细节, 别拿它凑数
        if picked and used + len(texts[i]) > budget:
            continue                           # 装不下就跳过这条, 让后面短的有机会
        picked.append((i, sc))
        used += len(texts[i])
    picked.sort()                              # 回到文档顺序, 拼起来才是一段能读的对话
    return [dict(msgs[i], score=round(sc, 3)) for i, sc in picked]


def conversations(provider: str) -> list[str]:
    """这个 provider 已经存了哪几段对话(按文件改动时间排序)。"""
    d = ROOT / _seg(provider, "unknown")
    if not d.is_dir():
        return []
    files = sorted((f for f in d.glob("*.jsonl") if f.stat().st_size),
                   key=lambda f: f.stat().st_mtime)
    return [f.stem for f in files]
