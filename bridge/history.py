"""读取/打开站点自己的会话历史(尽力而为: 站点改版时返回空列表, 不影响其它功能)。

返回条目: {id, key, title, url, active}
  id    - 与 BrowserManager.conversation_id() 同规则的会话 id(本地记录按它归档)
  key   - 唯一键(优先用 pathname)
  url   - 绝对地址; 为空表示站点没给出链接, 需要"点它"来切换
  active- 站点标记的当前会话
"""
from __future__ import annotations

# 列表探测: 先找会话链接, 再兜底找"历史容器"里的可点条目
_CONV_JS = r"""({pattern, extra}) => {
  const out = [], seen = new Set();
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 4 && r.height > 4; };
  const idOf = (path) => {
    if (pattern && path.indexOf(pattern) >= 0) return path.split(pattern).pop().split('?')[0].split('#')[0];
    return path.replace(/^\/+/, '');
  };
  const push = (key, title, url, active, el) => {
    if (!key || seen.has(key)) return;
    seen.add(key);
    out.push({ id: idOf(key), key: key, title: (title || '').replace(/\s+/g, ' ').trim().slice(0, 120),
               url: url || '', active: !!active });
    if (el && !url) el.setAttribute('data-bridge-conv', key);   // 没链接的条目: 记住它以便点击
  };
  const tagBox = (el) => {                       // 记住可滚动的会话列表容器
    let cur = el, box = null;
    while (cur && cur !== document.body) {
      const cs = getComputedStyle(cur);
      if (/(auto|scroll|overlay)/.test(cs.overflowY) && cur.scrollHeight > cur.clientHeight + 4) { box = cur; }
      cur = cur.parentElement;
    }
    if (box) box.setAttribute('data-bridge-convbox', '1');
    return !!box;
  };
  // 1) 会话链接
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    const href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#') continue;
    let u; try { u = new URL(a.href, location.href); } catch (e) { continue; }
    if (u.host !== location.host) continue;
    if (pattern && u.pathname.indexOf(pattern) < 0) continue;
    if (!pattern && !/^\/(c|chat|a)\/|chat/i.test(u.pathname)) continue;
    if (!visible(a)) continue;
    const cls = String(a.className || '');
    push(u.pathname, a.textContent, u.href,
         a.getAttribute('aria-current') === 'page' || /active|selected|current/i.test(cls), null);
    if (!document.querySelector('[data-bridge-convbox]')) tagBox(a);
  }
  // 2) 兜底: 历史容器里的可点条目(没有 a[href] 的站点)
  if (!out.length) {
    const boxes = Array.from(document.querySelectorAll(
      '[aria-label*="history" i],[aria-label*="会话"],[aria-label*="历史"],nav,aside,[class*="history"],[class*="sidebar"]'));
    for (const box of boxes) {
      const items = Array.from(box.querySelectorAll('li,[role="listitem"],[role="option"],a,button,[class*="item"]'));
      for (const el of items) {
        if (!visible(el)) continue;
        if (el.querySelector('li,a,button')) continue;          // 只看最内层条目
        const title = (el.textContent || '').replace(/\s+/g, ' ').trim();
        if (title.length < 2 || title.length > 120) continue;
        const cls = String(el.className || '');
        push('t:' + title, title, '', /active|selected|current/i.test(cls), el);
      }
      if (out.length) break;
    }
  }
  return out.slice(0, 80);
}"""

# 加载更多: 把站点会话列表滚到底, 触发它自己的懒加载
_SCROLL_MORE_JS = r"""() => {
  const box = document.querySelector('[data-bridge-convbox]');
  if (!box) return { scrolled: false };
  const before = box.scrollTop;
  box.scrollTop = box.scrollHeight;
  return { scrolled: box.scrollTop !== before || box.scrollHeight > box.clientHeight,
           scrollHeight: box.scrollHeight };
}"""

# 点击切换(没有链接的站点)
_OPEN_CONV_JS = r"""(key) => {
  for (const el of Array.from(document.querySelectorAll('[data-bridge-conv]'))) {
    if (el.getAttribute('data-bridge-conv') === key) { el.click(); return true; }
  }
  return false;
}"""
