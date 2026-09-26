"""把回答里引用的远程图片抓到本地。

为什么必须抓: ChatGPT 的图片是 `https://images.openai.com/static-rsc-4/<一长串签名>` ——
签名会过期, 过几天回看就是一堆碎图, 而"换会话之后历史还在不在"要的是**几年后还能看**。
抓到 `transcripts/media/<provider>/<conv>/` 之后, JSONL / 导出 / 界面都引本地相对路径。

抓取走 Playwright 的 `page.request` —— 它带着这个浏览器上下文的 cookie, 所以签名过期或
需要登录态的图也能拿下来; 没有页面时退回 urllib(公开图仍然可用)。
"""
import hashlib
import json
import logging
import re
import shutil
import time
import urllib.request
from pathlib import Path

from . import transcript

log = logging.getLogger("bridge")

MD_IMG = re.compile(r"!\[([^\]\n]{0,200})\]\((https?://[^\s)]+)\)")
MAX_BYTES = 20 * 1024 * 1024          # 单图上限(站点截图再大也不该到这里)
MAX_PER_CALL = 20                     # 一次同步最多抓几张, 免得整段历史被拉爆
PAGE_TIMEOUT_MS = 10_000              # 单张图的抓取上限
BUDGET_S = 40.0                       # 一次 harvest 的总时间预算: 站点爱在正文里塞失效的外链
#                                       (实测一条 google 图标代理链就要等满超时), 没有预算的话
#                                       20 张坏链能把这次同步拖到十分钟, 而它常占着互斥。
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/webp": ".webp", "image/svg+xml": ".svg", "image/avif": ".avif"}

media_root = lambda: transcript.ROOT / "media"
_cache: dict[str, str] = {}           # url -> 相对路径(本次进程内不再重复判断)
_bad: dict[str, float] = {}           # url -> 上次抓取失败的时刻
BAD_TTL_S = 600.0                     # 坏链在进程里记十分钟: 同一段会话会被反复同步很多遍
#                                       (每轮结束、切会话、点 ⤓), 每次都重新等一遍超时,
#                                       实测 3 条失效图标链就能把那次后台落盘占着互斥 30s。
_index: dict[str, str] = {}           # url -> 相对路径(跨进程: 存成 media/index.json)
_INDEX_LOADED = False


def _index_path() -> Path:
    return media_root() / "index.json"


def _load_index() -> dict:
    global _INDEX_LOADED
    if not _INDEX_LOADED:
        _INDEX_LOADED = True
        try:
            data = json.loads(_index_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _index.update({str(k): str(v) for k, v in data.items()})
        except Exception:  # noqa: BLE001  没有索引/坏了都当空的
            pass
    return _index


def _save_index() -> None:
    try:
        _index_path().parent.mkdir(parents=True, exist_ok=True)
        _index_path().write_text(json.dumps(_index, ensure_ascii=False, indent=0),
                                 encoding="utf-8", newline="\n")
    except Exception as exc:  # noqa: BLE001
        log.warning("图片索引写不下去(下次会重抓): %s", exc)


def _known(url: str) -> str:
    """这张图是不是已经抓过了 —— 必须查索引, 不能靠"猜文件名再看存在"。

    扩展名是**抓到之后**按响应的 content-type 才定下来的(.png/.jpg/...), 所以发请求之前
    根本算不出最终文件名; 之前用固定 `.img` 去 exists() 判断, 和真实名字永远对不上,
    结果每次服务重启都把同批图重下一遍(实测)。
    """
    rel = _cache.get(url) or _load_index().get(url)
    if rel and (transcript.ROOT / rel).exists():
        return rel
    return ""


def _name(url: str, ctype: str) -> str:
    return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:16] + _EXT.get(
        (ctype or "").split(";")[0].strip().lower(), ".img")


def _save(data: bytes, rel: str):
    p = transcript.ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(p)                    # 别留下写了一半的图(界面会画一个碎图)


def _fetch_urllib(url: str) -> tuple[bytes, str] | None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "image/avif,image/webp,image/*,*/*"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = r.read(MAX_BYTES + 1)
        return data, (r.headers.get("content-type") or "")


def _recently_bad(url: str) -> bool:
    t = _bad.get(url)
    if t is None:
        return False
    if time.time() - t > BAD_TTL_S:
        _bad.pop(url, None)             # 过期就再试一次(签名类地址有可能自己恢复)
        return False
    return True


async def harvest(page, provider: str, conv: str, text: str) -> tuple[str, dict]:
    """把 text 里的远程图片抓到本地, 返回 (改写后的 text, {抓到几张/失败几张/超预算没试几张})。

    抓不到的**保留原 URL** 并在统计里记一笔 —— 不静默丢图, 也不假装抓下来了。
    超过 BUDGET_S 就不再试剩下的(也保留原 URL), 下次同步会再来一遍。
    """
    urls = list(dict.fromkeys(m.group(2) for m in MD_IMG.finditer(text or "")))
    if not urls:
        return text or "", {"found": 0, "saved": 0, "failed": 0, "skipped": 0}
    got: dict[str, str] = {}
    failed = skipped = 0
    t0 = time.time()
    for url in urls[:MAX_PER_CALL]:
        rel = _known(url)
        if rel:
            got[url] = rel
            continue
        if _recently_bad(url):
            failed += 1                 # 原地址照样保留, 只是这一遍不再等它
            continue
        if time.time() - t0 > BUDGET_S:
            skipped += 1              # 保留原地址, 下次同步还会再试
            continue
        try:
            if page is not None:
                resp = await page.request.get(url, timeout=PAGE_TIMEOUT_MS)
                if not resp.ok:
                    raise RuntimeError("HTTP %s" % resp.status)
                body = await resp.body()
                ctype = resp.headers.get("content-type", "")
            else:
                body, ctype = _fetch_urllib(url) or (None, "")
                if body is None:
                    raise RuntimeError("没有可用的浏览器上下文")
            if not body or len(body) > MAX_BYTES:
                raise RuntimeError("图片过大或为空(%d 字节)" % len(body or b""))
            rel = "media/%s/%s/%s" % (transcript._seg(provider, "unknown"),
                                      transcript._seg(conv, transcript.NO_ID), _name(url, ctype))
            _save(body, rel)
            _cache[url] = rel
            _load_index()[url] = rel
            _save_index()
            got[url] = rel
        except Exception as exc:  # noqa: BLE001  一张图抓不到不该拖垮整次同步
            failed += 1
            _bad[url] = time.time()
            log.warning("图片抓取失败(保留原地址): %s | %s", url[:70], str(exc)[:120])
    out = text
    for url, rel in got.items():
        out = out.replace("](%s)" % url, "](%s)" % rel)
    return out, {"found": len(urls), "saved": len(got), "failed": failed, "skipped": skipped}


def purge(provider: str, conv: str) -> int:
    """删掉某段对话的媒体目录(会话被删时用)。"""
    d = media_root() / transcript._seg(provider, "unknown") / transcript._seg(conv, transcript.NO_ID)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        return 1
    return 0
