"""流式输出捕获。

主通道: 在导航前向页面注入 window.fetch 包装器。聊天类接口(fetch 流式响应)由页面
自己完成签名/鉴权, 我们只读取响应流, 逐行解析 JSON, 把文本片段推入 window.__dsCap。
兜底通道: 若主通道本轮没有产出任何文本(接口/协议改版), 轮询页面最后一个 markdown
块, 把整段文本作为 snapshot 型 delta 发回(客户端整段替换)。

结束判定: 有内容且(内容与生成指示)静默 QUIET_END_S / 单轮超时; 完全无内容则报错。
"""
import asyncio
import inspect
import logging

from . import config

log = logging.getLogger("bridge")


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
          let code = (n.innerText || n.textContent || '').replace(/^\n+/, '').replace(/\s+$/, '');
          // 站点把语言名(JSON/TypeScript…)放在 <pre> 里的头部, 不在 <code> 里: 这样一来
          // "JSON" 会变成文件的第一行 —— package.json 就不是合法 JSON 了(踩过)。
          // <code> 的文本正好是 <pre> 文本的后半段时, 以 <code> 为准。
          const codeEl = n.querySelector('code');
          if (codeEl) {
            const inner = String(codeEl.innerText || codeEl.textContent || '')
              .replace(/^\n+/, '').replace(/\s+$/, '');
            if (inner && code !== inner && code.endsWith(inner)) code = inner;
          }
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
  let mid = '';
  if (last) {
    try { text = toMarkdown(last); } catch (e) { text = ''; }
    if (!text.trim()) text = (last.innerText || last.textContent || '');   // 兜底: 转不出来就用纯文本
    text = text.slice(0, 300000);
    // 节点身份: 站点自带的消息 id(优先), 用来区分"还是上一轮那个节点"与"新回答节点"
    try {
      const holder = (last.closest && last.closest('[data-message-id]')) || null;
      mid = (holder && holder.getAttribute('data-message-id'))
            || (last.getAttribute && last.getAttribute('data-message-id')) || '';
    } catch (e) { mid = ''; }
  }
  let generating = false;
  for (const b of Array.from(document.querySelectorAll('button, [role="button"], [aria-label], [data-testid]'))) {
    const r = b.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;        // 只看可见元素, 避免隐藏 "Stop" 误判
    const s = ((b.getAttribute && (b.getAttribute('aria-label') || '')) + ' ' +
               (b.getAttribute && (b.getAttribute('data-testid') || '')) + ' ' +
               (b.title || '') + ' ' + String(b.textContent || '').slice(0, 40));
    // 站点语言不确定: 中文可能是「停止生成 / 停止流式传输 / 停止响应」, 英文是 Stop …;
    // 只认「停止生成」会把中文界面漏掉 -> 提前判定生成结束, 回答被截半(踩过)。
    if (/(停止|stop)/i.test(s)) { generating = true; break; }
  }
  return { count: nodes.length, text: text, generating: generating, id: mid };
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


# 把**整段对话**从站点页面读回来(给"消息不同步"用: bridge 只记得从它自己发出去的,
# 你在站点窗口里直接发的、或站点自己产生的内容, 只有页面上有)。
# 默认认 ChatGPT 的 data-message-author-role; 其它站点由 Provider 给选择器。
_CONV_COLLECT = r"""
  const __cache = {};                       // 消息 id -> {len, text}: 大回答别反复转 markdown
  const __collect = (cfg) => {
    const sel = (cfg && cfg.selector) || '[data-message-author-role]';
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { nodes = []; }
    const out = [];
    for (const n of nodes) {
      let role = (n.getAttribute && (n.getAttribute('data-message-author-role') || '')) || '';
      role = role.toLowerCase();
      if (!role) {
        const cls = String((n.className || '') + ' ' + ((n.parentElement || {}).className || '')).toLowerCase();
        role = /user|human|我/.test(cls) ? 'user' : 'assistant';
      }
      if (role !== 'user' && role !== 'assistant') role = 'assistant';
      // 助手那条节点里除了正文还有"思考步骤/耗时"之类的 UI 文本, 只读正文容器(.markdown),
      // 否则同步回来的消息会拖一串"补充 ref 与 reactive 差异"这种标签
      let target = n;
      if (role === 'assistant') {
        const md = n.querySelector('.markdown, [class*="markdown"]');
        if (md) target = md;
      }
      // 消息身份: 站点自带的消息 id —— 页面只挂载视口附近的节点, 分段读回来要靠它去重/拼接
      let mid = '';
      try {
        const holder = (n.closest && n.closest('[data-message-id]')) || null;
        mid = (holder && holder.getAttribute('data-message-id'))
              || (n.getAttribute && n.getAttribute('data-message-id')) || '';
      } catch (e) { mid = ''; }
      const rawLen = String(target.textContent || '').length;
      let text = '';
      if (mid && __cache[mid] && __cache[mid].len === rawLen) {
        text = __cache[mid].text;           // 同一条、长度没变 -> 直接用上次转好的
      } else {
        try { text = toMarkdown(target); } catch (e) { text = ''; }
        if (!text.trim()) text = (target.innerText || target.textContent || '');
        text = String(text || '').trim().slice(0, 200000);
        if (mid) __cache[mid] = { len: rawLen, text: text };
      }
      if (text) out.push({ role: role, text: text, id: mid });
    }
    return out;
  };
"""

_CONV_JS = "(cfg) => {" + _DOM_TO_MD + _CONV_COLLECT + r"""
  const msgs = __collect(cfg);
  return { url: location.href, count: msgs.length, messages: msgs };
}"""


# 深度读: 站点是**虚拟列表**(只挂载视口附近几条, 滚过去就把别的卸载掉), 而且长回答能有
# 十几万像素高 —— "滚到顶再读一次"必然丢消息(实测同一会话 10/8/6/6 条; 用户说的"获取的消息
# 也不完整"就是这个), 每步只往上挪一屏(860px)更是永远走不到头。
# 做法: 在页面里一次跑完 —— 按**已挂载的第一条消息**自适应地往上跳(它离视口越远跳得越多),
# 每跳一次读一段并按消息 id 合并, 直到到顶且连续两跳没有新内容。
# (注意: Playwright 的 evaluate 不认 `(async cfg) => {}` 这种写法, 必须写成普通函数里返回 Promise。)
_CONV_DEEP_JS = "(cfg) => (async () => {" + _DOM_TO_MD + _CONV_COLLECT + r"""
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const scrollable = (el) => {
    if (!el || el === document.documentElement) return false;
    let st;
    try { st = window.getComputedStyle(el); } catch (e) { return false; }
    return /(auto|scroll)/.test(String(st.overflowY))
      && el.clientHeight > 120 && el.scrollHeight > el.clientHeight + 40;
  };
  const scrollerOf = (sel) => {
    const anchor = document.querySelector(sel) || document.querySelector('[class*="markdown"]');
    for (let p = anchor; p; p = p.parentElement) if (scrollable(p)) return p;
    const all = Array.from(document.querySelectorAll('main, div, section')).filter(scrollable);
    all.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
    return all[0] || null;
  };
  const sel = (cfg && cfg.selector) || '[data-message-author-role]';
  const el = scrollerOf(sel);
  if (!el) {                                    // 没有可滚动容器: 直接读一遍
    const msgs = __collect(cfg);
    return { url: location.href, count: msgs.length, messages: msgs, reads: 1, scroller: '' };
  }
  const geo = () => ({ top: Math.round(el.scrollTop), h: Math.round(el.scrollHeight),
                       ch: Math.round(el.clientHeight),
                       cls: String(el.className || '').slice(0, 48) });
  const keyOf = (m) => m.id || (m.role + ':' + String(m.text || '').slice(0, 120));
  const acc = [];
  const idxOf = {};
  const insertBlock = (at, block) => {         // 整块插入(保持块内文档顺序)
    if (!block.length) return 0;
    acc.splice(at, 0, ...block);
    for (const k in idxOf) if (idxOf[k] >= at) idxOf[k] += block.length;
    block.forEach((m, i) => { idxOf[keyOf(m)] = at + i; });
    return block.length;
  };
  const midInsert = (m, i, list) => {          // 段内空洞: 靠左右已知道的邻居定位
    const k = keyOf(m);
    let at = null;
    for (let j = i + 1; j < list.length; j++) if (keyOf(list[j]) in idxOf) { at = idxOf[keyOf(list[j])]; break; }
    if (at === null) for (let j = i - 1; j >= 0; j--) if (keyOf(list[j]) in idxOf) { at = idxOf[keyOf(list[j])] + 1; break; }
    return insertBlock(at === null ? 0 : at, [m]);
  };
  const readOnce = () => {
    const list = __collect(cfg);
    if (!list.length) return { added: 0, mounted: 0 };
    // 已经在手上的那条变长了(流式还没完) -> 换成更全的
    for (const m of list) {
      const k = keyOf(m);
      if (k in idxOf && String(m.text || '').length > String(acc[idxOf[k]].text || '').length) acc[idxOf[k]] = m;
    }
    const knownAt = [];
    for (let i = 0; i < list.length; i++) if (keyOf(list[i]) in idxOf) knownAt.push(i);
    if (!knownAt.length) {                     // 整段全新: 我们是**往上**扫 -> 这段比手上的都老, 放最前
      return { added: insertBlock(0, list), mounted: list.length };
    }
    // 锚点之前的部分更老 -> 插到锚点前; 锚点之后的部分更新 -> 插到最后一个锚点后(顺序都不会乱)
    const head = list.slice(0, knownAt[0]);
    const tail = list.slice(knownAt[knownAt.length - 1] + 1);
    const middle = [];
    for (let i = knownAt[0] + 1; i < knownAt[knownAt.length - 1]; i++)
      if (!(keyOf(list[i]) in idxOf)) middle.push(i);
    let added = insertBlock(idxOf[keyOf(list[knownAt[0]])], head);
    added += insertBlock(idxOf[keyOf(list[knownAt[knownAt.length - 1]])] + 1, tail);
    for (const i of middle) added += midInsert(list[i], i, list);
    return { added: added, mounted: list.length };
  };
  const up = () => {                            // 把"当前挂载的第一条"顶到视口下方 -> 它前一条就会挂上
    const nodes = Array.from(document.querySelectorAll(sel));
    const first = nodes.length ? nodes[0] : null;
    if (!first) { el.scrollTop = Math.max(0, el.scrollTop - el.clientHeight * 2); return; }
    const rel = first.getBoundingClientRect().top - el.getBoundingClientRect().top;
    const want = el.scrollTop + rel - Math.max(40, el.clientHeight - 80);
    el.scrollTop = Math.max(0, Math.min(el.scrollTop - 40, want));
  };

  el.scrollTop = el.scrollHeight;               // 先站到最新处读一段(尾部最要紧)
  await sleep(60);
  readOnce();
  const longConv = el.scrollHeight > el.clientHeight * 4;
  const waitTop = longConv ? 8 : 2;             // 长对话在顶部还要等站点把更早的历史取回来
  let idle = 0, guard = 0, reads = 1, topIdle = 0;
  if (el.scrollTop > 0) {                       // 页面本来就一屏放得下 -> 上面那次读就够了
    while (guard++ < 200) {
      up();
      const atTopNow = el.scrollTop <= 0;
      await sleep(atTopNow ? 400 : 150);
      const r = readOnce();
      reads += 1;
      const atTop = el.scrollTop <= 0;
      if (r.added === 0) idle += 1; else idle = 0;
      if (atTop && r.added === 0) topIdle += 1; else topIdle = 0;
      if (atTop && topIdle >= waitTop) break;   // 到顶且连着几次都没有新内容 -> 收工
      if (!r.mounted && idle >= 3) break;       // 页面上什么都没有了, 别空转
      if (!atTop && idle >= 8) break;           // 中间推不动了(站点卡住), 别死等
    }
  }
  el.scrollTop = el.scrollHeight;               // 滚回底部(别把用户的窗口停在半空)
  return { url: location.href, count: acc.length, messages: acc, reads: reads, scroller: geo().cls };
})()"""


async def read_conversation(page, selector: str = "", deep: bool = True, **_) -> dict:
    """读回站点当前这段对话的所有消息(用户+助手), 顺序与页面一致。

    deep=True: 走页面里的自适应扫描(`_CONV_DEEP_JS`): 站点是虚拟列表 + 长回答极高,
    "滚到顶再读一次"会丢消息, 所以边跳边读边按消息 id 合并; 扫描失败退回单次读取。
    """
    if deep:
        try:
            out = await page.evaluate(_CONV_DEEP_JS, {"selector": selector or ""})
            if isinstance(out, dict) and (out.get("messages") or []):
                log.info("读回整段对话: %d 条(%d 段读, 滚动容器 %s)",
                         len(out["messages"]), out.get("reads") or 0, out.get("scroller") or "?")
                return {"url": out.get("url") or "", "count": len(out["messages"]),
                        "messages": out["messages"]}
        except Exception:  # noqa: BLE001
            log.debug("分段读整段对话失败, 退回单次读取", exc_info=True)
    try:
        out = await page.evaluate(_CONV_JS, {"selector": selector or ""})
        out = out if isinstance(out, dict) else {"url": "", "count": 0, "messages": []}
    except Exception as exc:  # noqa: BLE001
        return {"url": "", "count": 0, "messages": [], "error": str(exc)}
    return {"url": out.get("url") or "", "count": out.get("count") or 0,
            "messages": list(out.get("messages") or [])}


class NoDataError(Exception):
    """整轮没有捕获到任何内容。"""


async def wait_turn_end(page, on_delta, *, on_warn=None, mode: str = "stream",
                        snapshot_selector: str | None = None,
                        baseline: dict | None = None):
    """在消息已发出后轮询, 直到一轮流式生成结束。

    mode="stream": 主通道为 fetch 流包装(delta 增量), DOM 作兜底;
    mode="dom":    只读 DOM 快照(整段替换型 delta), 用于无稳定流接口的站点。

    baseline: **发送之前**读到的快照(上一轮的答案)。必须由调用方在 send 之前读好传进来:
    发送会让站点重绘整段对话, 发送后再读有可能读到空(节点暂时全没了), 于是一旦页面把
    上一轮那条回答渲染回来, 就被当成"和 baseline 不一样的新回答"回传 —— 页面上就出现
    同一条回答两份(踩过)。传 None 时才退回"现在读一次"。

    on_delta(kind: str, text: str, snapshot: bool) 在 Python 侧被同步回调。
    返回 (truncated: bool, errors: list[str])。
    """
    loop = asyncio.get_running_loop()
    start = loop.time()

    # 本轮开始前页面上的那条回答 = 上一轮的结果。它任何时候都不能当成本轮的答案,
    # 否则新问题会配上旧回答(消息列表看起来"错位/滞后了一条")。
    if baseline is None:
        try:
            baseline = await read_snapshot(page, snapshot_selector)
        except Exception:                                        # noqa: BLE001
            baseline = {}
    base = baseline or {}
    base_text = base.get("text") or ""
    base_count = int(base.get("count") or 0)
    base_id = str(base.get("id") or "")
    sent_text = None             # 本轮已经回传过的整段快照文本(去重用)

    def _norm(s: str) -> str:
        return "".join((s or "").split())

    base_norm = _norm(base_text)

    def is_this_turn(snap: dict) -> bool:
        """快照里是不是"本轮的回答": 有正文, 且不是本轮之前那条。"""
        t = _norm(snap.get("text"))
        if not t:
            return False
        sid = str(snap.get("id") or "")
        if base_id and sid and sid == base_id:
            return False             # 还是上一轮那个节点(站点自带消息 id 相同)
        if not base_norm:
            return True
        # 短文本(例如"好的")逐字重复是可能的, 不能只凭文本否掉:
        # 这时看页面上是不是真多出了一条回答节点(baseline 是发送前读的, 这个计数才可信)。
        if len(t) < 40 or len(base_norm) < 40:
            return t != base_norm or int(snap.get("count") or 0) > base_count
        if t == base_norm:
            return False             # 长文本逐字相同 -> 上一轮那条又渲染回来了(重绘)
        if base_norm.startswith(t):
            return False             # 比上一轮短且是它的开头 -> 上一轮被截断的副本
        if t.startswith(base_norm) and (len(t) - len(base_norm)) < 200:
            return False             # 只比上一轮多几十个字 -> 还是那条
        if t[:60] == base_norm[:60] and abs(len(t) - len(base_norm)) < 200:
            return False             # 开头 60 字一样、长度也差不多 -> 同一条的重绘版本
        return True

    last_content = start         # 最后一次"内容真的变了"的时刻(不看生成指示)
    got_any = False
    stream_mode = False
    errors: list[str] = []
    truncated = False
    prev_gen = False
    revert_deadline = 0.0        # "本轮回答暂时看不见"(页面重绘)时的等待期限
    no_data_at = start + config.NO_DATA_ERROR_S   # 毫无捕获时的报错时刻(站点仍在生成就顺延)

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

        content_changed = False

        if mode == "stream":
            items = await drain_items(page)
            for it in items:
                kind = it.get("kind")
                if kind == "__eof__":
                    content_changed = True
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
                        content_changed = True

        snap = await read_snapshot(page, snapshot_selector)
        snap_text = snap.get("text") or ""
        snap_gen = bool(snap.get("generating"))
        fresh = is_this_turn(snap)
        # 用"本轮已回传过的整段文本"去重(不是和 baseline 比): 新回答和上一轮逐字一样也要回传,
        # 那条已经由 is_this_turn 的节点身份判定放行了。
        if fresh and (sent_text is None or snap_text != sent_text):
            sent_text = snap_text
            if not stream_mode:      # 兜底/DOM 模式: 整段快照替换
                await _call(on_delta, "text", snap_text, True)
                got_any = True
            content_changed = True
        if content_changed:               # 只有"内容真的变了"才重置静默计时(生成指示不算)
            last_content = now

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

        idle = now - last_content
        if got_any and not waiting_revert:
            if idle >= config.QUIET_END_S and not snap_gen:
                break
            # 已有内容, 但"生成中"指示一直亮着(站点偶尔会卡住/残留): 不能一直等到整轮超时
            if snap_gen and idle >= config.SNAPSHOT_STUCK_S:
                truncated = True
                break
        # 一段时间毫无捕获: 处理掉, 绝不挂死。但站点**还在生成**时不能报错 ——
        # 现在回答节点可能先是空的(还在思考/首字未吐), 快照读回来是空文本, 报错就误伤了。
        if not got_any and now >= no_data_at:
            if await maybe_final():
                got_any = True
                break
            if snap_gen:
                no_data_at = now + config.NO_DATA_ERROR_S   # 站点仍在生成: 顺延, 继续等
            else:
                raise NoDataError(
                    f"{int(config.NO_DATA_ERROR_S)} 秒内未捕获到任何内容: "
                    "可能未登录/发送未生效/站点页面改版, 请检查弹出的浏览器窗口。"
                )

        await asyncio.sleep(config.STREAM_POLL_S)

    # 保证最终答案完整可见(整段快照为幂等替换)
    if got_any:
        await maybe_final()
    elif truncated:
        # 整轮超时(比如站点一直显示"生成中"却没吐内容): 页面上真有本轮内容就用它,
        # 否则如实报错 —— 不能收一条空回答, 也不能悄悄留着上一轮的答案。
        if not await maybe_final():
            raise NoDataError(
                f"这一轮等了 {int(config.TURN_TIMEOUT_S)} 秒仍未拿到回答"
                "(页面一直显示生成中或没渲染出新内容), 请检查弹出的浏览器窗口。"
            )

    return truncated, errors
