"""流式输出捕获。

主通道: 在导航前向页面注入 window.fetch 包装器。聊天类接口(fetch 流式响应)由页面
自己完成签名/鉴权, 我们只读取响应流, 逐行解析 JSON, 把文本片段推入 window.__dsCap。
兜底通道: 若主通道本轮没有产出任何文本(接口/协议改版), 轮询页面最后一个 markdown
块, 把整段文本作为 snapshot 型 delta 发回(客户端整段替换)。

结束判定: 有内容且(内容与生成指示)静默 QUIET_END_S / 单轮超时; 完全无内容则报错。
"""
import asyncio
import inspect

from . import config


async def _call(fn, *args):
    """调用回调: 兼容同步与异步(协程)回调, 异步需 await 否则增量会丢失。"""
    if fn is None:
        return
    r = fn(*args)
    if inspect.isawaitable(r):
        await r

# 注意: 必须用 raw 字符串, 让 JS 里的 \r?\n 与 \. 等保持字面量。
FETCH_WRAPPER_JS = r"""
(() => {
  if (window.__dsCap) return;
  window.__dsCap = { items: [], enabled: false };

  function walkOut(o, out) {
    if (!o || typeof o !== 'object') return;
    if (typeof o.content === 'string' && o.content) {
      const kind = /think|reason/i.test(String(o.type || '')) ? 'reasoning' : 'text';
      out.push({ kind: kind, text: o.content });
    }
    if (typeof o.reasoning_content === 'string' && o.reasoning_content) {
      out.push({ kind: 'reasoning', text: o.reasoning_content });
    }
    if (Array.isArray(o.choices)) { for (const c of o.choices) walkOut(c, out); }
    if (o.delta && typeof o.delta === 'object') { walkOut(o.delta, out); }
    if (Array.isArray(o.parts)) { for (const p of o.parts) walkOut(p, out); }
  }

  function parseLine(raw) {
    let line = raw.trim();
    if (!line) return [];
    if (line.indexOf('data:') === 0) line = line.slice(5).trim();
    if (line === '[DONE]') return [];
    let obj = null;
    try { obj = JSON.parse(line); } catch (e) { return []; }
    const out = [];
    walkOut(obj, out);
    return out;
  }

  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const req = (typeof input === 'string') ? input
      : (input && input.url) ? input.url : '';
    const resp = await origFetch(input, init);
    const cap = window.__dsCap;
    const hit = cap && cap.enabled && req
      && (/chat\.deepseek\.com/.test(req)
          || /(chat\/completion|chat\/message|\/chat\/|new_chat|create_chat)/.test(req));
    if (!hit) return resp;
    if (resp.status >= 400) {
      cap.items.push({ kind: '__err__', text: 'HTTP ' + resp.status });
      return resp;
    }
    if (resp.body && typeof resp.body.getReader === 'function') {
      cap.inflight = (cap.inflight || 0) + 1;
      (async () => {
        try {
          const reader = resp.clone().body.getReader();
          const dec = new TextDecoder();
          let acc = '';
          while (true) {
            const r = await reader.read();
            if (r.done) break;
            acc += dec.decode(r.value, { stream: true });
            const parts = acc.split(/\r?\n/);
            acc = parts.pop() || '';
            for (const p of parts) {
              for (const it of parseLine(p)) cap.items.push(it);
            }
          }
          acc += dec.decode();
          if (acc.trim()) {
            for (const p of acc.split(/\r?\n/)) {
              for (const it of parseLine(p)) cap.items.push(it);
            }
          }
          cap.items.push({ kind: '__eof__' });
        } catch (e) {
          cap.items.push({ kind: '__err__', text: String((e && e.message) || e) });
        } finally {
          cap.inflight -= 1;
        }
      })();
    }
    return resp;
  };
})();
"""

_ENABLE_JS = "() => { const c = window.__dsCap; if (c) { c.items = []; c.enabled = true; } }"
_DISABLE_JS = "() => { const c = window.__dsCap; if (c) { c.enabled = false; } }"
_DRAIN_JS = ("() => { const c = window.__dsCap; if (!c) return []; "
             "const a = c.items; c.items = []; return a; }")

# DOM -> Markdown: 站点里加粗/标题/列表/代码块的结构要保住, 否则 innerText 会把整段压成纯文本
_DOM_TO_MD = r"""
  const __inlineOf = (el) => {
    let s = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) { s += n.nodeValue.replace(/\s+/g, ' '); continue; }
      if (n.nodeType !== 1) continue;
      const t = n.tagName.toLowerCase();
      if (t === 'br') { s += ' '; }
      else if (t === 'ul' || t === 'ol' || t === 'pre' || t === 'table' || t === 'blockquote') { continue; }
      else if (t === 'strong' || t === 'b') { const x = __inlineOf(n).trim(); if (x) s += '**' + x + '**'; }
      else if (t === 'em' || t === 'i') { const x = __inlineOf(n).trim(); if (x) s += '*' + x + '*'; }
      else if (t === 'code') { const x = n.textContent.trim(); if (x) s += '`' + x + '`'; }
      else if (t === 'a') {
        const href = n.getAttribute('href') || '', x = __inlineOf(n).trim();
        s += (href && href.charAt(0) !== '#' && x) ? '[' + x + '](' + href + ')' : x;
      } else if (t === 'img') { s += '[' + (n.getAttribute('alt') || '图片') + ']'; }
      else { s += __inlineOf(n); }
    }
    return s;
  };
  const __flat = (el) => __inlineOf(el).replace(/\s+/g, ' ').trim();
  const __listInto = (el, indent, lines) => {
    const ordered = el.tagName.toLowerCase() === 'ol';
    let i = 1;
    for (const li of Array.from(el.children)) {
      if (li.tagName.toLowerCase() !== 'li') continue;
      lines.push(indent + (ordered ? (i++) + '. ' : '- ') + __flat(li));
      for (const sub of Array.from(li.children)) {
        const st = sub.tagName.toLowerCase();
        if (st === 'ul' || st === 'ol') __listInto(sub, indent + '  ', lines);
      }
    }
  };
  const toMarkdown = (root) => {
    const lines = [];
    let cur = '';
    const flush = () => { const t = cur.replace(/\s+/g, ' ').trim(); if (t) lines.push(t); cur = ''; };
    const walk = (el) => {
      for (const n of el.childNodes) {
        if (n.nodeType === 3) { cur += n.nodeValue.replace(/\s+/g, ' '); continue; }
        if (n.nodeType !== 1) continue;
        const t = n.tagName.toLowerCase();
        if (/^h[1-6]$/.test(t)) {
          flush();
          lines.push('#'.repeat(Math.min(3, +t[1])) + ' ' + __flat(n));
          lines.push('');
        } else if (t === 'p') {
          flush();
          const x = __flat(n);
          if (x) { lines.push(x); lines.push(''); }
        } else if (t === 'pre') {
          flush();
          const code = (n.innerText || n.textContent || '').replace(/^\n+/, '').replace(/\s+$/, '');
          lines.push('```');
          lines.push(code);
          lines.push('```');
          lines.push('');
        } else if (t === 'ul' || t === 'ol') {
          flush();
          __listInto(n, '', lines);
          lines.push('');
        } else if (t === 'blockquote') {
          flush();
          const x = __flat(n);
          if (x) { lines.push('> ' + x); lines.push(''); }
        } else if (t === 'hr') {
          flush();
          lines.push('---');
          lines.push('');
        } else if (t === 'table') {
          flush();
          for (const tr of Array.from(n.querySelectorAll('tr'))) {
            const cells = Array.from(tr.children).map(__flat).filter(Boolean);
            if (cells.length) lines.push(cells.join(' | '));
          }
          lines.push('');
        } else if (t === 'div' || t === 'section' || t === 'article' || t === 'main' || t === 'details' || t === 'summary') {
          walk(n);
          flush();
        } else {
          cur += __inlineOf(n);
        }
      }
      flush();
    };
    walk(root);
    return lines.join('\n').replace(/\n{3,}/g, '\n\n').replace(/[ \t]+\n/g, '\n').trim();
  };
"""

_SNAPSHOT_JS = "(expr) => {" + _DOM_TO_MD + r"""
  const nodes = Array.from(document.querySelectorAll(expr));
  const last = nodes.length ? nodes[nodes.length - 1] : null;
  let text = '';
  if (last) {
    try { text = toMarkdown(last); } catch (e) { text = ''; }
    if (!text.trim()) text = (last.innerText || last.textContent || '');   // 兜底: 转不出来就用纯文本
    text = text.slice(0, 300000);
  }
  let generating = false;
  for (const b of Array.from(document.querySelectorAll('button, [role="button"], [aria-label]'))) {
    const r = b.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;        // 只看可见元素, 避免隐藏 "Stop" 误判
    const s = ((b.getAttribute && (b.getAttribute('aria-label') || '')) + ' ' +
               (b.title || '') + ' ' + String(b.textContent || '').slice(0, 40));
    if (/(停止生成|Stop generating|Stop streaming)/.test(s)) { generating = true; break; }
  }
  return { count: nodes.length, text: text, generating: generating };
}"""


def default_snapshot_selector() -> str:
    return '.ds-markdown, [class*="ds-markdown"]'


async def enable_capture(page) -> None:
    try:
        await page.evaluate(_ENABLE_JS)
    except Exception:
        pass


async def disable_capture(page) -> None:
    try:
        await page.evaluate(_DISABLE_JS)
    except Exception:
        pass


async def drain_items(page) -> list:
    try:
        return await page.evaluate(_DRAIN_JS)
    except Exception:
        return []


async def read_snapshot(page, selector: str | None = None) -> dict:
    try:
        return await page.evaluate(_SNAPSHOT_JS, selector or default_snapshot_selector())
    except Exception:
        return {"text": "", "generating": False}


class NoDataError(Exception):
    """整轮没有捕获到任何内容。"""


async def wait_turn_end(page, on_delta, *, on_warn=None, mode: str = "stream",
                        snapshot_selector: str | None = None):
    """在消息已发出后轮询, 直到一轮流式生成结束。

    mode="stream": 主通道为 fetch 流包装(delta 增量), DOM 作兜底;
    mode="dom":    只读 DOM 快照(整段替换型 delta), 用于无稳定流接口的站点。

    on_delta(kind: str, text: str, snapshot: bool) 在 Python 侧被同步回调。
    返回 (truncated: bool, errors: list[str])。
    """
    loop = asyncio.get_running_loop()
    start = loop.time()

    # 本轮开始前页面上的那条回答 = 上一轮的结果。它任何时候都不能当成本轮的答案,
    # 否则新问题会配上旧回答(消息列表看起来"错位/滞后了一条")。
    try:
        base = await read_snapshot(page, snapshot_selector)
    except Exception:                                            # noqa: BLE001
        base = {}
    base_text = base.get("text") or ""
    base_count = int(base.get("count") or 0)
    prev_text = base_text

    def is_this_turn(snap: dict) -> bool:
        """快照里是不是"本轮的回答": 有正文, 且不是本轮之前那条。"""
        t = snap.get("text") or ""
        if not t:
            return False
        if t != base_text:
            return True
        # 正文刚好和上一轮一样: 只有页面上真的多出了一条回答节点才算本轮
        return int(snap.get("count") or 0) > base_count

    last_change = start
    got_any = False
    stream_mode = False
    errors: list[str] = []
    truncated = False
    prev_gen = False
    revert_deadline = 0.0        # "本轮回答暂时看不见"(页面重绘)时的等待期限

    async def maybe_final():
        """若页面上是本轮的回答, 以整段快照回传(幂等替换), 保证最终答案可见。"""
        try:
            snap = await read_snapshot(page, snapshot_selector)
        except Exception:                                        # noqa: BLE001
            return False
        if not is_this_turn(snap):
            return False             # 页面上还是上一轮那条 -> 不能拿它顶替本轮答案
        await _call(on_delta, "text", snap.get("text") or "", True)
        return True

    while True:
        now = loop.time()
        if now - start > config.TURN_TIMEOUT_S:
            truncated = True
            break

        changed = False

        if mode == "stream":
            items = await drain_items(page)
            for it in items:
                kind = it.get("kind")
                if kind == "__eof__":
                    changed = True
                elif kind == "__err__":
                    msg = it.get("text", "stream error")
                    errors.append(msg)
                    if on_warn:
                        await _call(on_warn, msg)
                else:
                    text = it.get("text", "")
                    if text:
                        await _call(on_delta, kind or "text", text, False)
                        got_any = True
                        stream_mode = True
                        changed = True

        snap = await read_snapshot(page, snapshot_selector)
        snap_text = snap.get("text") or ""
        snap_gen = bool(snap.get("generating"))
        fresh = is_this_turn(snap)
        if fresh and snap_text != prev_text:
            prev_text = snap_text
            if not stream_mode:      # 兜底/DOM 模式: 整段快照替换
                await _call(on_delta, "text", snap_text, True)
                got_any = True
            changed = True
        elif snap_text != prev_text:
            prev_text = snap_text    # 只是记下来, 免得拿同一条反复比较
        if snap_gen:
            changed = True           # 页面仍在生成 -> 保持活跃, 不因静默误判

        # 页面重绘时新一轮的节点会短暂消失, 快照退回"本轮之前那条回答"(或空):
        # 这既不是本轮答案, 也不能就此收尾 —— 真正的回答还在路上, 给它一点时间等回来。
        waiting_revert = False
        if mode != "stream" and got_any and not snap_gen and not fresh:
            if not revert_deadline:
                revert_deadline = now + config.SNAPSHOT_REVERT_GRACE_S
            waiting_revert = now < revert_deadline
        else:
            revert_deadline = 0.0

        # 生成刚结束但整轮未捕到增量: 回传最终答案并结束(避免 25s 白等)
        if prev_gen and not snap_gen and not got_any:
            if await maybe_final():
                got_any = True
                break
            # 页面上没有本轮的内容: 不当作"这一轮答完了", 继续等(超时后如实报"没捕获到内容")
        prev_gen = snap_gen

        if changed:
            last_change = now

        idle = now - last_change
        if got_any and idle >= config.QUIET_END_S and not snap_gen and not waiting_revert:
            break
        # 一段时间毫无捕获: 无论站点是否仍在"生成"都处理, 绝不挂死
        if (not got_any and (now - start) >= config.NO_DATA_ERROR_S):
            if await maybe_final():
                got_any = True
                break
            raise NoDataError(
                f"{int(config.NO_DATA_ERROR_S)} 秒内未捕获到任何内容: "
                "可能未登录/发送未生效/站点页面改版, 请检查弹出的浏览器窗口。"
            )

        await asyncio.sleep(config.STREAM_POLL_S)

    # 保证最终答案完整可见(整段快照为幂等替换)
    if got_any:
        await maybe_final()

    return truncated, errors
