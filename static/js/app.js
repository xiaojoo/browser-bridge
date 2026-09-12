"use strict";
const BUILD = "2026-09-11c";        // 前端版本戳: 改了前端就更新它, 方便确认页面是否刷新过
console.log("[bridge] build " + BUILD);
const $ = (id) => document.getElementById(id);
const convEl = $("conv"), convWrapEl = $("convWrap"), inputEl = $("input");
let state = { state: "idle", busy: false, error: null, provider: null };
let lastConnKey = null;        // 上一次的"连接状态+站点", 用来在连上的一刻刷新会话列表
let ws = null;
let providersById = {};
let current = null;
let staged = [];           // [ {file, url?, sendAsText?} ] 待上传文件
const fileInput = $("fileInput"), chipsEl = $("chips"), btnAttach = $("btnAttach");

/* ========== 会话历史: 站点会话(远端 id) × 本地分段 ==========
 * 侧栏"开启新对话"只在本地开新的一段(远端仍是同一个会话, 列表里归到同一个 id 下);
 * 输入框那行的"新对话"才是真的在站点新建会话, 本地跟着换到新 id。
 * 存储: { provider: { "convId|sid": {conv, sid, ts, title, url, messages} } }
 */
const HIST_KEY = "wlb.history.v3";
const HIST_KEY_V2 = "wlb.history.v2";
const HIST_KEY_V1 = "wlb.history.v1";
const PROV_KEY = "wlb.provider.v1";
const NEW_CONV = "~new";                 // 站点还没分配会话 id 时的键
const HIST_MAX_MSGS = 60;                // 每段最多保留条数
const HIST_MAX_CHARS = 60000;            // 单条上限
const HIST_MAX_BYTES = 900000;           // 单段序列化上限(超了先砍详情, 再逐条丢最早的)
let transcript = [];                     // [{role:"user"|"assistant", text, reason}]
let histProvider = null, histConv = null, histSid = null;   // 画面正在看哪一段
let curSid = null;                       // 新消息记到哪一段(当前远端会话里)
let wsApplied = null;                    // 服务端当前生效的工作区根目录
let wsAppliedConv = null;                // 上一次按哪个会话应用过工作区(连接重置里要用, 所以提前声明)
/* 刚连上(或刚开新会话): 画面从空白开始 —— 站点那边由后端打开新对话, 本地这边也不要
   把"上次那个会话"的记录当成当前会话捡回来。按标签页记着, 刷新后同样是干净的。 */
let freshOnConnect = false;
let sawDisconnected = false;             // 本页里见过"未连接"状态 -> 之后的"已连接"才算一次新连接
const FRESH_KEY = "wlb.fresh.v1";
function freshFlag() {
  try { return sessionStorage.getItem(FRESH_KEY) === "1"; } catch (e) { return freshOnConnect; }
}
function setFreshFlag(on) {
  freshOnConnect = !!on;
  try { on ? sessionStorage.setItem(FRESH_KEY, "1") : sessionStorage.removeItem(FRESH_KEY); } catch (e) {}
}
let convItems = [];                      // 站点返回的会话列表
let convTimer = null;
const CONV_PAGE = 12;                    // 首屏渲染多少条(滚到底再加载)
let convRows = [];                       // 当前算出来的会话行(分页渲染用)
let convShown = CONV_PAGE;               // 已渲染多少行
let convLoadingMore = false;             // 是否正在向站点要更多
let convExhausted = false;               // 站点那边也翻不出更多了

function histAll() {
  try { return JSON.parse(localStorage.getItem(HIST_KEY) || "{}") || {}; } catch (e) { return {}; }
}
function histWriteAll(all) {
  try { localStorage.setItem(HIST_KEY, JSON.stringify(all)); } catch (e) {}
}
function providerKey() { return state.provider || $("selProvider").value || ""; }
function liveConvKey() { return state.conversation_id || NEW_CONV; }
function histStore(prov) { return histAll()[prov || providerKey()] || {}; }
function newSid() { return "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5); }
function entryKey(conv, sid) { return (conv || NEW_CONV) + "|" + sid; }

/* 当前在看的(站点会话, 本地分段): 刚开的空分段还没内容, 靠它才能在刷新后复位 */
const CURSOR_KEY = "wlb.cur.v1";
function cursorAll() {
  try { return JSON.parse(localStorage.getItem(CURSOR_KEY) || "{}") || {}; } catch (e) { return {}; }
}
function saveCursor(conv, sid) {
  const prov = providerKey();
  if (!prov || !conv || !sid) return;
  try {
    const all = cursorAll();
    all[prov] = { conv: conv, sid: sid };
    localStorage.setItem(CURSOR_KEY, JSON.stringify(all));
  } catch (e) {}
}
function loadCursor(prov) {
  const c = cursorAll()[prov || providerKey()];
  return (c && c.conv && c.sid) ? c : null;
}

/* 老版本(v1/v2)记录 -> v3: 各当成该会话下的一段 */
function migrateHistory() {
  try {
    if (localStorage.getItem(HIST_KEY)) return;
    const out = {};
    const put = (prov, conv, e, sid) => {
      if (!prov || !e) return;
      if (!out[prov]) out[prov] = {};
      out[prov][entryKey(conv, sid)] = {
        conv: conv, sid: sid, ts: e.ts || Date.now(), title: e.title || "",
        url: e.url || "", messages: Array.isArray(e.messages) ? e.messages : [],
      };
    };
    const v2 = JSON.parse(localStorage.getItem(HIST_KEY_V2) || "{}") || {};
    Object.keys(v2).forEach(prov => Object.keys(v2[prov] || {}).forEach(conv => put(prov, conv, v2[prov][conv], "s0")));
    const v1 = JSON.parse(localStorage.getItem(HIST_KEY_V1) || "{}") || {};
    Object.keys(v1).forEach(prov => { if (!out[prov]) put(prov, NEW_CONV, v1[prov], "s0"); });
    if (Object.keys(out).length) histWriteAll(out);
  } catch (e) { /* 迁移失败就从头记录 */ }
}
/* 站点被限流后我们另开了窗口: 新窗口会是一个新的会话 id。
   这里记一份别名, 让旧 id 和新 id 都指向同一份本地记录(界面里看起来是一次对话)。 */
const ALIAS_KEY = "wlb.alias.v1";
function aliasAll() {
  try { return JSON.parse(localStorage.getItem(ALIAS_KEY) || "{}") || {}; } catch (e) { return {}; }
}
function aliasOf(prov, conv) {
  const m = aliasAll()[prov] || {};
  let cur = conv, guard = 0;
  while (m[cur] && guard++ < 8) cur = m[cur];        // 跟到底(可能连着换过几次窗口)
  return cur;
}
function setAlias(prov, oldConv, newConv) {
  if (!prov || !oldConv || !newConv || oldConv === newConv) return;
  try {
    const all = aliasAll();
    if (!all[prov]) all[prov] = {};
    all[prov][oldConv] = newConv;
    localStorage.setItem(ALIAS_KEY, JSON.stringify(all));
  } catch (e) {}
}
/* 同一段(同一个 sid)可能存成两条: 站点被限流换过窗口时, 旧会话 id 下一条、新 id 下一条,
   别名把两个 id 连起来。这时要以"时间更新的那条"为准 —— 换窗口时留下的那条往往只是最早的快照
   (卡片只有标题 + "正在把 ChatGPT 的回答…"), 真跑完的那份在另一条里。 */
function entryTwins(st, prov, conv, sid) {
  const real = aliasOf(prov, conv);
  const out = [];
  [real, conv].forEach(c => {
    const e = st[entryKey(c, sid)];
    if (e && out.indexOf(e) < 0) out.push(e);
  });
  return out;
}
function bestEntry(st, prov, conv, sid) {
  const list = entryTwins(st, prov, conv, sid);
  return list.length ? list.reduce((a, b) => ((b.ts || 0) > (a.ts || 0) ? b : a)) : null;
}
/* 清掉坏段落: 里面只有本地执行卡片、一条普通消息都没有。
   这种段落是"状态同步在空会话里凭空画卡片"留下的(点完「新对话」再刷新就会产生),
   正常聊过一段的段落一定有 user/assistant 消息。 */
function dropJunkEntries(prov) {
  const all = histAll();
  const st = all[prov];
  if (!st) return;
  let hit = false;
  Object.keys(st).forEach(k => {
    const msgs = (st[k] && Array.isArray(st[k].messages)) ? st[k].messages : [];
    if (msgs.length && msgs.every(m => m && m.role === "world")) { delete st[k]; hit = true; }
  });
  if (hit) histWriteAll(all);
}
/* 这一段该写在哪个会话 id 下: 已经有记录就跟着它走,
   否则整段会被另存一条, 下次刷新又按别名读到旧的那半(卡片就退回残缺版本了) */
function entryConvFor(prov, conv, sid) {
  const e = bestEntry(histStore(prov), prov, conv, sid);
  return (e && e.conv) || conv;
}
/* 某个站点会话下的本地分段(新的在前); 旧 id 会被解析到合并后的 id */
function histEntries(prov, conv) {
  const st = histStore(prov);
  const real = aliasOf(prov, conv);
  const list = Object.keys(st).map(k => st[k])
    .filter(e => e && (e.conv === real || (real !== conv && e.conv === conv)))
    .sort((a, b) => (b.ts || 0) - (a.ts || 0));
  const seen = {}, out = [];                 // 同一段只留真身(换窗口时可能存成两条)
  list.forEach(e => { if (!seen[e.sid || ""]) { seen[e.sid || ""] = 1; out.push(e); } });
  return out;
}
function histMessages(prov, conv, sid) {
  const st = histStore(prov);
  // 指定了 sid 就只认它(刚开的空分段就该是空的, 不能回退到别的段)
  const e = sid ? bestEntry(st, prov, conv, sid)
                : (histEntries(prov, conv)[0] || null);
  const msgs = (e && Array.isArray(e.messages)) ? e.messages : [];
  return msgs.filter(m => m && ((m.role === "user" || m.role === "assistant") && m.text
                                 || m.role === "world" && m.card));   // 本地执行卡片也保留
}
function convTitle() {
  const first = transcript.find(m => m.role === "user");
  return ((first ? first.text : "") || "新会话").replace(/\s+/g, " ").trim().slice(0, 40);
}
function ensureSession(conv) {
  if (curSid && histConv === conv) return curSid;
  const cur = loadCursor(providerKey());
  if (cur && cur.conv === conv) { curSid = cur.sid; return curSid; }
  const list = histEntries(providerKey(), conv);
  curSid = (list[0] && list[0].sid) || newSid();
  return curSid;
}
/* 存历史用的紧凑卡片: 保留结论和行文字, 长详情只留给最近两轮
   (否则几张大卡片就把这一段撑爆, 旧的会被整段丢掉 —— 表现就是"刷新后卡片没了") */
function cardForStore(card, keepDetail) {
  // 存之前先去重: 以前每次刷新同步状态都会把同一批结果追加一遍, 存进历史的行越积越多
  const rows = dedupeWorldRows(card.rows || []).slice(-12).map(r => ({
    mark: String(r.mark || "").slice(0, 4),
    text: String(r.text || "").slice(0, 160),
    cls: String(r.cls || "").slice(0, 12),
    detailsLabel: keepDetail ? String(r.detailsLabel || "").slice(0, 60) : "",
    detailsText: keepDetail ? String(r.detailsText || "").slice(0, 800) : "",
  }));
  return { title: String(card.title || "").slice(0, 80), count: String(card.count || "").slice(0, 40),
           foot: String(card.foot || "").slice(0, 240), busy: false,
           wid: card.wid || "", rows: rows };
}

function histFlush() {
  const prov = providerKey();
  if (!prov) return;
  const live = liveConvKey();
  const sid = (histConv === live && histSid) ? histSid : ensureSession(live);
  const conv = entryConvFor(prov, live, sid);      // 这一段已有的记录在哪个 id 下, 就写回哪个
  const entry = { conv: conv, sid: sid, ts: Date.now(), title: convTitle(),
                  url: state.conversation_url || "",
                  messages: transcript.slice(-HIST_MAX_MSGS).map((m, i, arr) => (m.role === "world"
    ? { role: "world", card: cardForStore(m.card || {}, i >= arr.length - 2) }
    : {
        role: m.role,
        text: String(m.text || "").slice(0, HIST_MAX_CHARS),
        reason: m.reason ? String(m.reason).slice(0, HIST_MAX_CHARS) : undefined,
      })) };
  // 超预算时: 先砍旧卡片的展开详情, 再逐条丢最早的(而不是一次丢掉一半)
  let guard = 0;
  let size = JSON.stringify(entry).length;
  while (size > HIST_MAX_BYTES && guard++ < 60) {
    const heavy = entry.messages.filter(m => m.role === "world" && m.card
      && (m.card.rows || []).some(r => r.detailsText));
    if (heavy.length) {
      heavy.slice(0, Math.ceil(heavy.length / 2)).forEach(m => {
        (m.card.rows || []).forEach(r => { r.detailsText = ""; r.detailsLabel = ""; });
      });
    } else if (entry.messages.length > 6) {
      entry.messages.shift();
    } else {
      break;
    }
    size = JSON.stringify(entry).length;
  }
  const all = histAll();
  if (!all[prov]) all[prov] = {};
  all[prov][entryKey(conv, sid)] = entry;
  histWriteAll(all);
  saveCursor(live, sid);                           // 光标跟着"当前会话 id", 不是记录所在的那个 id
  curSid = sid;
  if (histConv === live) histSid = sid;
}
function histPush(msg) {
  transcript.push(msg);
  histFlush();
}
/* 站点给新会话分配了 id: 把 "~new" 下的这一段挪过去 */
function adoptConversation(newId) {
  if (!newId || newId === liveConvKey()) return;
  setFreshFlag(false);                     // 站点给了 id = 这一轮已经有真会话了, 恢复常规显示
  const prov = providerKey();
  const oldConv = liveConvKey();
  const sid = curSid || histSid;
  state.conversation_id = newId;
  setAlias(prov, oldConv, newId);          // 旧 id 也指向同一份记录
  if (sid) {
    const all = histAll(), p = all[prov] || {};
    const from = entryKey(oldConv, sid), to = entryKey(newId, sid);
    if (p[from]) {
      const oldEntry = p[from];
      const keepMsgs = (p[to] && Array.isArray(p[to].messages)) ? p[to].messages : [];
      // 合并两边的内容(新窗口那半 + 原来那半), 顺序按原样拼接
      const merged = keepMsgs.length ? keepMsgs.concat(oldEntry.messages || [])
                                     : (oldEntry.messages || []);
      p[to] = Object.assign({}, oldEntry, { conv: newId, messages: merged,
                                            ts: Date.now(), title: oldEntry.title || (p[to] || {}).title || "" });
      delete p[from];
      all[prov] = p;
      histWriteAll(all);
    }
    histConv = newId; histSid = sid; curSid = sid;
    saveCursor(newId, sid);
  }
  // 这是"站点被限流后自动另开窗口"引起的会话变更, 不是用户主动切换:
  // 工作区跟着换回默认会让人以为设置丢了, 所以这里直接标记成"已应用过", 保持当前工作区。
  wsAppliedConv = wsRootMapKey();
  renderConversations();
}
function renderStoredMessage(m, mi) {
  if (m.role === "user") { addUserMsg(m.text, mi); return; }
  if (m.role === "world") {                                            // 重画的历史卡片: 永远不是"进行中"
    paintWorldCard(Object.assign({}, m.card || {}, { busy: false }));
    return;
  }
  const keep = current;
  current = openAssistantMsg();
  current.reason = m.reason || "";
  current.raw = m.text || "";
  renderCurrent();
  current = keep;
}
/* 把画面切到某个站点会话的某一段记录(sid 为空 = 该会话最新的一段) */
function renderHistoryFor(prov, conv, sid) {
  dropJunkEntries(prov);                                     // 先清掉"只有卡片"的坏段落
  const list = histEntries(prov, conv);
  const cur = loadCursor(prov);
  let use = null;
  if (sid && list.some(e => e.sid === sid)) use = sid;
  else if (cur && cur.conv === conv) use = cur.sid;          // 刚开的空分段也在这里
  else if (list[0]) use = list[0].sid;
  transcript = use ? histMessages(prov, conv, use) : [];
  convEl.innerHTML = "";
  current = null;
  transcript.forEach((m, i) => renderStoredMessage(m, i));
  histProvider = prov; histConv = conv; histSid = use;
  if (conv === liveConvKey()) { curSid = use; if (use) saveCursor(conv, use); }
  const remote = conv && conv !== NEW_CONV;
  if (!transcript.length && remote) {
    const box = document.createElement("div");
    box.className = "hint-box";
    box.textContent = "这个会话没有本地记录 — 本页只记录从这里发出的对话, 继续发送即可接着聊。";
    convEl.appendChild(box);
  }
  showWelcome(transcript.length === 0 && !remote);
  if (transcript.length) scrollBottom();
  buildConvNav();
  syncScrollbar();
  if (typeof applyConvWorkspace === "function") applyConvWorkspace(false);   // 切会话 -> 切回它的工作区
}
function syncHistoryView(force) {
  const prov = providerKey();
  const conv = liveConvKey();
  if (!prov) return;
  // 刚连上: 站点可能还停在上次那个会话(URL 里带着它的 id), 但用户要的是干净的一屏 ——
  // 不把旧记录画出来(记录一直都在, 侧栏点一下就能回去看)。
  if (freshFlag()) {
    if (!transcript.length) showWelcome(true);
    return;
  }
  if (!force && prov === histProvider && conv === histConv) return;
  if (state.busy || current) return;      // 生成中不切画面
  if (conv !== histConv) curSid = null;
  renderHistoryFor(prov, conv, null);
}
/* 每次连接都是"新开始": 清空画面 + 本地也当成新会话(站点给了 id 再由 adoptConversation 合并回来)。
   旧的会话记录一条都不动, 侧栏里照样能点回去看。 */
function showFreshStart() {
  setFreshFlag(true);
  transcript = [];
  convEl.innerHTML = "";
  clearConvNav();
  current = null;
  const h = $("hintBox"); if (h) h.remove();
  curSid = newSid();
  state.conversation_id = null;              // 本地从现在开始是新会话
  histProvider = providerKey(); histConv = NEW_CONV; histSid = curSid;
  saveCursor(NEW_CONV, curSid);
  wsAppliedConv = wsRootMapKey();            // 别因为"会话变了"就把工作区切回默认
  showWelcome(true);
  renderConversations();
  syncScrollbar();
}

/* 侧栏"开启新对话": 只在本地开一段(远端仍是同一个会话) */
function newLocalSession() {
  const prov = providerKey();
  const conv = liveConvKey();
  curSid = newSid();
  histProvider = prov; histConv = conv; histSid = curSid;
  saveCursor(conv, curSid);
  transcript = [];
  convEl.innerHTML = "";
  clearConvNav();
  current = null;
  const h = $("hintBox"); if (h) h.remove();
  showWelcome(true);
  renderConversations();
  toast("已在本地开一段新对话(远端还是同一个会话, 发送后归到同一 id 下)", "info");
}
/* 已有本地记录的老会话: 用记录里的地址, 或者按当前会话地址的前缀 + 会话 id 还原 */
function convUrlFor(entry, id) {
  if (entry && entry.url) return entry.url;
  const cur = state.conversation_url || "";
  const pat = (providersById[providerKey()] || {}).conversation_pattern || "";
  if (cur && pat) {
    const i = cur.indexOf(pat);
    if (i >= 0) return cur.slice(0, i + pat.length) + id;
  }
  return "";
}

/* ---------- 会话列表: 站点会话(+ 同 id 下的本地分段) ---------- */
async function loadConversations(more) {
  const prov = providerKey();
  if (!prov || state.state !== "logged_in") { convItems = []; renderConversations(); return; }
  try {
    const r = await fetch("/api/conversations" + (more ? "?more=1" : ""));
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    const items = j.items || [];
    if (more) convExhausted = items.length <= convItems.length;   // 站点那边翻不出新的了
    convItems = items;
    if (!more) { convShown = CONV_PAGE; convExhausted = false; }
    if (j.current !== undefined && j.current !== state.conversation_id) state.conversation_id = j.current;
  } catch (e) {
    if (!more) convItems = [];           // 读不到站点列表就只显示本地记录
  }
  renderConversations();
}
/* 会话列表滚到底: 先翻本地分页, 本地翻完再向站点要更多 */
async function loadMoreConversations() {
  if (convLoadingMore) return;
  if (convShown < convRows.length) { convShown += CONV_PAGE; renderConversations(); return; }
  if (convExhausted) return;
  convLoadingMore = true;
  renderConversations();
  try { await loadConversations(true); } finally { convLoadingMore = false; renderConversations(); }
}
function renderConversations() {
  const box = $("convList");
  if (!box) return;
  const prov = providerKey();
  const curConv = liveConvKey();
  const curSession = (histConv === curConv ? histSid : curSid);
  const store = histStore(prov);
  const byConv = {};
  Object.keys(store).forEach(k => {
    const e = store[k];
    if (!e || !e.conv) return;
    (byConv[e.conv] = byConv[e.conv] || []).push(e);
  });
  const groups = [], seen = new Set();
  convItems.forEach(it => {
    const id = it.id || it.key;
    if (!id || seen.has(id)) return;
    seen.add(id);
    groups.push({ id: id, key: it.key || id, title: it.title || "未命名会话",
                  url: it.url || "", remote: true, canOpen: true,
                  sessions: (byConv[id] || []).slice().sort((a, b) => (b.ts || 0) - (a.ts || 0)) });
  });
  Object.keys(byConv).forEach(conv => {                       // 站点没列出来但本地有记录的
    if (seen.has(conv)) return;
    seen.add(conv);
    const sessions = byConv[conv].slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));
    const url = convUrlFor(sessions[0], conv);
    groups.push({ id: conv, key: conv, title: (sessions[0] && sessions[0].title) || "本地记录",
                  url: url, remote: false, canOpen: !!url, sessions: sessions });
  });
  // 本地偏好: 隐藏(删除)与置顶都只作用于本列表, 不动站点
  const prefs = prefsOf(prov);
  let shown = groups.filter(g => !prefs.hidden[groupKey(g.id)]);
  shown.forEach(g => {
    g.sessions = g.sessions.filter(s => !prefs.hidden[sessionKey(g.id, s.sid)]);
  });
  shown.sort((a, b) => (prefs.pinned[groupKey(b.id)] ? 1 : 0) - (prefs.pinned[groupKey(a.id)] ? 1 : 0));
  shown.forEach(g => {
    g.sessions.sort((a, b) => (prefs.pinned[sessionKey(g.id, b.sid)] ? 1 : 0)
                            - (prefs.pinned[sessionKey(g.id, a.sid)] ? 1 : 0));
  });
  convRows = shown;
  box.innerHTML = "";
  if (!shown.length) {
    const empty = document.createElement("div");
    empty.className = "item muted";
    empty.textContent = state.state === "logged_in" ? "暂无历史会话" : "连接后显示历史会话";
    box.appendChild(empty);
    return;
  }
  const mkRow = (opt) => {
    const el = document.createElement("div");
    el.className = opt.cls;
    // 这里**不设 title**: 会话列表里的行不必再弹气泡(文字就在行里, 弹出来是重复的噪音)
    if (opt.pinned) {
      const pm = document.createElement("span");
      pm.className = "pinmark";
      pm.textContent = "📌";
      el.appendChild(pm);
    }
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = opt.text;
    el.appendChild(nm);
    if (opt.tag) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = opt.tag;
      el.appendChild(tag);
    }
    const more = document.createElement("span");
    more.className = "more";
    more.title = "更多(仅本地操作)";
    more.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="2"/>' +
                     '<circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>';
    more.onclick = (ev) => { ev.stopPropagation(); openRowMenu(more, opt.actions); };
    el.appendChild(more);
    el.onclick = opt.onOpen;
    return el;
  };
  const page = shown.slice(0, convShown);
  let sentinel = null;
  if (page.length < shown.length) {                     // 还有没渲染的: 用占位行把滚动顶开
    const hint = document.createElement("div");
    hint.className = "load-more";
    hint.textContent = "继续滚动加载 …";
    page.push({ __hint: hint });
    sentinel = hint;
  } else if (convLoadingMore) {
    const hint = document.createElement("div");
    hint.className = "load-more";
    hint.textContent = "正在加载更多会话 …";
    page.push({ __hint: hint });
  }
  page.forEach(g => {
    if (g.__hint) { box.appendChild(g.__hint); return; }   // 见下方 observeConvMore
    const multi = g.sessions.length > 1;
    const gKey = groupKey(g.id), gPinned = !!prefs.pinned[gKey];
    const tag = g.remote
      ? (g.sessions.length ? (multi ? g.sessions.length + " 段" : "") : "未记录")
      : "本地";
    box.appendChild(mkRow({
      cls: "item" + (g.id === curConv ? " active" : ""),
      text: g.title,
      tag: tag,
      pinned: gPinned,
      onOpen: () => openGroup(g),
      actions: [
        { label: gPinned ? "取消置顶" : "置顶会话", run: () => setPinned(gKey, !gPinned) },
        { label: "删除会话", danger: true, run: () => deleteGroup(g) },
      ],
    }));
    if (multi) {                        // 同一个站点 id 下的多段本地对话
      g.sessions.forEach(s => {
        const sKey = sessionKey(g.id, s.sid), sPinned = !!prefs.pinned[sKey];
        box.appendChild(mkRow({
          cls: "item sub" + (g.id === curConv && s.sid === curSession ? " active" : ""),
          text: s.title || "本地对话",
          tag: (s.messages || []).length + " 条",
          pinned: sPinned,
          onOpen: () => openSession(g, s),
          actions: [
            { label: sPinned ? "取消置顶" : "置顶这一段", run: () => setPinned(sKey, !sPinned) },
            { label: "删除这一段", danger: true, run: () => deleteSession(g, s) },
          ],
        }));
      });
    }
  });
  if (sentinel) observeConvMore(sentinel);      // 列表没撑满时也能自动继续加载
}

/* 列表末尾那行"继续滚动加载"进入视野就继续加载(不依赖用户真的滚动) */
let convMoreObs = null;
function observeConvMore(el) {
  if (!window.IntersectionObserver) return;
  if (!convMoreObs) {
    convMoreObs = new IntersectionObserver((entries) => {
      if (entries.some(e => e.isIntersecting)) loadMoreConversations();
    }, { root: $("convList"), rootMargin: "120px" });
  }
  convMoreObs.disconnect();
  convMoreObs.observe(el);
}

/* ---------- 会话行的"更多"菜单: 置顶/删除(都只改本地, 不动站点) ---------- */
const PREFS_KEY = "wlb.convprefs.v1";
function prefsAll() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") || {}; } catch (e) { return {}; }
}
function prefsOf(prov) {
  const p = prefsAll()[prov || providerKey()] || {};
  return { pinned: p.pinned || {}, hidden: p.hidden || {} };
}
function prefsWrite(prov, p) {
  const all = prefsAll();
  all[prov || providerKey()] = { pinned: p.pinned || {}, hidden: p.hidden || {} };
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(all)); } catch (e) {}
}
function groupKey(id) { return "conv:" + id; }
function sessionKey(conv, sid) { return "sess:" + conv + "|" + sid; }
function setPinned(key, on) {
  const prov = providerKey();
  const p = prefsOf(prov);
  if (on) p.pinned[key] = true; else delete p.pinned[key];
  prefsWrite(prov, p);
  renderConversations();
  toast(on ? "已置顶(仅本地, 站点不受影响)" : "已取消置顶", "info");
}
function hideKey(key) {
  const prov = providerKey();
  const p = prefsOf(prov);
  p.hidden[key] = true;
  prefsWrite(prov, p);
  renderConversations();
}
function deleteGroup(g) {
  hideKey(groupKey(g.id));                       // 站点列表下次还会返回它, 靠本地隐藏保持删除状态
  const all = histAll();
  const p = all[providerKey()];
  if (p) {
    Object.keys(p).forEach(k => { if (p[k] && p[k].conv === g.id) delete p[k]; });
    histWriteAll(all);
  }
  if (histConv === g.id) {                       // 正在看的就是它
    transcript = []; convEl.innerHTML = ""; clearConvNav(); current = null; histSid = null;
    if (liveConvKey() === g.id) curSid = null;
    showWelcome(true);
  }
  renderConversations();
  toast("已在本地删除「" + g.title + "」(站点里不受影响)", "info");
}
function deleteSession(g, s) {
  const all = histAll();
  const p = all[providerKey()];
  const k = entryKey(g.id, s.sid);
  if (p && p[k]) { delete p[k]; histWriteAll(all); }
  hideKey(sessionKey(g.id, s.sid));
  if (histConv === g.id && histSid === s.sid) {
    transcript = []; convEl.innerHTML = ""; current = null; histSid = null;
    if (liveConvKey() === g.id) curSid = null;
    showWelcome(true);
  }
  renderConversations();
  toast("已删除这段本地记录(站点里不受影响)", "info");
}
function closeRowMenu() {
  const m = $("rowMenu");
  if (m) m.classList.remove("open");
}
function openRowMenu(anchor, actions) {
  const m = $("rowMenu");
  if (!m) return;
  m.innerHTML = "";
  (actions || []).forEach(a => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "picker-item" + (a.danger ? " danger" : "");
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = a.label;
    b.appendChild(nm);
    b.onclick = (ev) => { ev.stopPropagation(); closeRowMenu(); a.run(); };
    m.appendChild(b);
  });
  m.classList.add("open");
  const r = anchor.getBoundingClientRect();
  const mw = m.offsetWidth, mh = m.offsetHeight;
  let left = Math.max(8, Math.min(r.right - mw, window.innerWidth - mw - 8));
  let top = r.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
  m.style.left = Math.round(left) + "px";
  m.style.top = Math.round(top) + "px";
}
document.addEventListener("click", (e) => {
  const m = $("rowMenu");
  if (m && m.classList.contains("open") && !m.contains(e.target)) closeRowMenu();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeRowMenu(); });
async function openRemote(g, sid) {
  try {
    const r = await post("/api/conversations/open", { key: g.key, url: g.url });
    setFreshFlag(false);                   // 用户主动点开旧会话: 正常显示它的记录
    state.conversation_id = r.current || g.id;
    curSid = null;
    await loadConversations();
    renderHistoryFor(providerKey(), liveConvKey(), sid || null);
    renderConversations();
    toast("已切换到「" + g.title + "」, 可以继续在这个会话里聊", "info");
  } catch (e) { toast(e.message, "err"); }
}
async function openGroup(g) {
  const prov = providerKey();
  setFreshFlag(false);                     // 用户主动点开: 正常显示这段记录(哪怕是刚连上时那个会话)
  if (g.id === liveConvKey()) { renderHistoryFor(prov, g.id, null); renderConversations(); return; }
  if (!g.canOpen) {
    renderHistoryFor(prov, g.id, null);
    toast("这条只有本地记录, 也还原不出远端地址; 发送消息会回到当前远端会话", "warn");
    return;
  }
  await openRemote(g, null);
}
async function openSession(g, s) {
  const prov = providerKey();
  setFreshFlag(false);                     // 同上: 主动点开某一段记录
  if (g.id === liveConvKey()) { renderHistoryFor(prov, g.id, s.sid); renderConversations(); return; }
  if (!g.canOpen) {
    renderHistoryFor(prov, g.id, s.sid);
    toast("这是本地记录, 远端会话回不去; 发送消息会回到当前远端会话", "warn");
    return;
  }
  await openRemote(g, s.sid);
}
/* 记住选择的站点, 刷新后不乱跳 */
function saveProviderChoice(id) {
  // 站点列表还没加载(下拉是空的)时别写: 那会把上次选的站点覆盖成空, 刷新后就跳回默认站点
  if (!id || (Object.keys(providersById).length && !providersById[id])) return;
  try { localStorage.setItem(PROV_KEY, id); } catch (e) {}
}
function savedProviderChoice() {
  try { return localStorage.getItem(PROV_KEY) || ""; } catch (e) { return ""; }
}

const TEXT_EXTS = ["txt","md","py","js","ts","tsx","jsx","json","csv","html","css","scss",
  "java","c","cpp","h","hpp","go","rs","rb","php","sql","yml","yaml","toml","ini","log",
  "sh","bat","ps1","xml","svg","vue","svelte","kt","swift","lua","r"];
function isTextFile(f) {
  if (f.type && f.type.startsWith("text/")) return true;
  const ext = (f.name.split(".").pop() || "").toLowerCase();
  return TEXT_EXTS.indexOf(ext) >= 0;
}
function fmtSize(n) {
  if (n < 1024) return n + "B";
  if (n < 1048576) return (n / 1024).toFixed(0) + "KB";
  return (n / 1048576).toFixed(1) + "MB";
}
function renderChips() {
  chipsEl.innerHTML = "";
  btnAttach.classList.toggle("on", staged.length > 0);
  for (let i = 0; i < staged.length; i++) {
    const s = staged[i];
    const row = document.createElement("div");
    row.className = "chiprow";
    if (s.wsPath) {                                  // 工作区勾选的文件: 就在输入框里显示
      const ic = document.createElement("span");
      ic.className = "ic"; ic.textContent = "📄";          // 文件图标(不是文件夹)
      const nm = document.createElement("span");
      nm.className = "nm"; nm.textContent = s.wsPath.split("/").pop();
      nm.title = s.wsPath;
      const sz = document.createElement("span");
      sz.className = "sz"; sz.textContent = "工作区";
      const rm = document.createElement("span");
      rm.className = "rm"; rm.textContent = "×"; rm.title = "移出(同时取消勾选)";
      rm.onclick = () => {
        staged.splice(i, 1);
        const cb = wsTreeEl.querySelector('input[type="checkbox"][data-path="' + s.wsPath + '"]');
        if (cb) cb.checked = false;
        updateSelCount();
        renderChips();
      };
      row.append(ic, nm, sz, rm);
      chipsEl.appendChild(row);
      continue;
    }
    if (s.url) {
      const img = document.createElement("img");
      img.src = s.url; img.alt = "";
      row.appendChild(img);
    }
    const nm = document.createElement("span");
    nm.className = "nm"; nm.textContent = s.file.name;
    const sz = document.createElement("span");
    sz.className = "sz"; sz.textContent = fmtSize(s.file.size);
    row.appendChild(nm); row.appendChild(sz);
    if (isTextFile(s.file)) {
      const tm = document.createElement("span");
      tm.className = "tmode" + (s.sendAsText ? " txt" : "");
      tm.textContent = s.sendAsText ? "文本" : "文件";
      tm.title = s.sendAsText ? "解析为文本发送(点击改为发送文件)" : "作为文件发送(点击改为解析文本)";
      tm.onclick = () => { s.sendAsText = !s.sendAsText; renderChips(); };
      row.appendChild(tm);
    }
    const rm = document.createElement("span");
    rm.className = "rm"; rm.textContent = "×"; rm.title = "移除";
    rm.onclick = () => { if (s.url) URL.revokeObjectURL(s.url); staged.splice(i, 1); renderChips(); };
    row.appendChild(rm);
    chipsEl.appendChild(row);
  }
  chipsEl.style.display = staged.length ? "" : "none";
}
function addFiles(fileList) {
  for (const f of Array.from(fileList)) {
    const s = { file: f, url: null };
    if (f.type && f.type.startsWith("image/")) s.url = URL.createObjectURL(f);
    staged.push(s);
  }
  renderChips();
}
$("btnAttach").onclick = () => fileInput.click();
fileInput.addEventListener("change", (e) => { addFiles(e.target.files); fileInput.value = ""; });

/* 直接把文件拖进输入框 / 从剪贴板粘贴文件(截图、复制的文件) 都能加进来 */
const composerBox = document.querySelector(".composer");
function fileDropTarget(el) {
  if (!el) return;
  el.addEventListener("dragover", (e) => {
    if (!e.dataTransfer) return;
    const hasFile = Array.from(e.dataTransfer.types || []).indexOf("Files") >= 0;
    if (!hasFile) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    composerBox.classList.add("drop-on");
  });
  el.addEventListener("dragleave", (e) => {
    if (e.target === el) composerBox.classList.remove("drop-on");
  });
  el.addEventListener("drop", (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    e.preventDefault();
    composerBox.classList.remove("drop-on");
    addFiles(files);
    toast("已加入 " + files.length + " 个文件", "info");
  });
}
fileDropTarget(composerBox);
fileDropTarget(inputEl);
// 拖到页面其它地方也不要让浏览器直接打开文件
["dragover", "drop"].forEach(t => document.addEventListener(t, (e) => {
  if (e.target === document || e.target === document.body) e.preventDefault();
}));
function stageClipboardFiles(e) {
  const dt = e.clipboardData;
  if (!dt) return false;
  const files = Array.from(dt.files || []);
  if (!files.length) {                       // 有些浏览器把图片放在 items 里
    Array.from(dt.items || []).forEach(it => {
      if (it.kind === "file") { const f = it.getAsFile(); if (f) files.push(f); }
    });
  }
  if (!files.length) return false;           // 纯文本 -> 交给 textarea 自己处理
  e.preventDefault();
  addFiles(files);
  toast("已加入 " + files.length + " 个文件", "info");
  return true;
}
// 只在 composer 上挂一次(paste 会从 textarea 冒泡上来, 挂两处会重复加入)
composerBox.addEventListener("paste", (e) => {
  stageClipboardFiles(e);                    // 有文件就收下, 纯文本仍然正常粘贴
  setTimeout(autoGrowInput, 0);
});

function nameOf(id) { return (providersById[id] && providersById[id].name) || id || "?"; }
function shortOf(id) { return (providersById[id] && providersById[id].short) || "AI"; }
function modeOf(id) { return (providersById[id] && providersById[id].capture_mode) || ""; }
function esc(s) { return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

function renderInline(t) {
  return t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1(<em>$2</em>)');
}
function renderMarkdown(raw) {
  if (!raw) return "";
  const lines = raw.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let inCode = false, codeBuf = [];
  const flushCode = () => {
    if (!codeBuf.length) return;
    out.push('<pre class="code"><span class="copy">复制</span><span class="save">保存</span>'
             + esc(codeBuf.join("\n")) + "</pre>");
    codeBuf = [];
  };
  for (const line of lines) {
    const m = line.match(/^```(\w*)/);
    if (m) { flushCode(); inCode = !inCode; continue; }
    if (inCode) { codeBuf.push(line); continue; }
    if (/^#{1,3}\s/.test(line)) {
      const lvl = line.match(/^#+/)[0].length;
      out.push("<h" + lvl + ">" + renderInline(esc(line.replace(/^#+\s*/, ""))) + "</h" + lvl + ">");
    } else if (/^\s*>\s?/.test(line)) {
      out.push("<blockquote>" + renderInline(esc(line.replace(/^\s*>\s?/, ""))) + "</blockquote>");
    } else if (/^\s*[-*]\s/.test(line)) {
      out.push("<p>• " + renderInline(esc(line.replace(/^\s*[-*]\s*/, ""))) + "</p>");
    } else if (/^\s*\d+[.)]\s/.test(line)) {
      const num = line.match(/^\s*(\d+)[.)]/)[1];        // 保留序号, 否则整段数字列表会变成无编号行
      out.push("<p>" + num + ". " + renderInline(esc(line.replace(/^\s*\d+[.)]\s*/, ""))) + "</p>");
    } else if (/^\s*$/.test(line)) { out.push(""); }
    else { out.push("<p>" + renderInline(esc(line)) + "</p>"); }
  }
  if (inCode) flushCode();
  return out.join("");
}

function toast(text, kind) {
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = text;
  $("toast").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

/* ===== 底部站点选择: 平时显示为一行文字, 点击后在用户栏上方弹出选择框 ===== */
function closeProviderPicker() {
  const p = $("providerPicker"), b = $("btnProvider");
  if (p) p.classList.remove("open");
  if (b) { b.classList.remove("open"); b.setAttribute("aria-expanded", "false"); }
}
function renderProviderPicker() {
  const p = $("providerPicker"), sel = $("selProvider");
  if (!p || !sel) return;
  p.innerHTML = "";
  Object.keys(providersById).forEach(id => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "picker-item" + (id === sel.value ? " active" : "");
    item.dataset.id = id;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", id === sel.value ? "true" : "false");
    const av = document.createElement("span");
    av.className = "pi-av";
    av.textContent = shortOf(id);
    item.appendChild(av);
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = providersById[id].name;
    item.appendChild(nm);
    if (id === sel.value) {
      const tk = document.createElement("span");
      tk.className = "tick";
      tk.textContent = "✓";
      item.appendChild(tk);
    }
    item.onclick = () => {
      if (sel.value !== id) sel.value = id;
      closeProviderPicker();
      refreshUi();
    };
    p.appendChild(item);
  });
  // 末尾追加"系统设置"入口(原顶栏/用户栏的齿轮)
  p.appendChild(pickerSep());
  p.appendChild(pickerAction("settings", "系统设置", GEAR_SVG, () => {
    closeProviderPicker();
    openSettings();
  }));
}
const GEAR_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
  'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/>' +
  '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 ' +
  '1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83' +
  'l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82' +
  'l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51' +
  '1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4' +
  'h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
function pickerSep() {
  const d = document.createElement("div");
  d.className = "picker-sep";
  return d;
}
function pickerAction(act, label, iconSvg, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "picker-item";
  b.dataset.act = act;
  const ic = document.createElement("span");
  ic.className = "pi-ic";
  ic.innerHTML = iconSvg;
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = label;
  b.appendChild(ic); b.appendChild(nm);
  b.onclick = onClick;
  return b;
}
function toggleProviderPicker() {
  const p = $("providerPicker");
  if (!p || p.classList.contains("open")) { closeProviderPicker(); return; }
  renderProviderPicker();
  p.classList.add("open");
  const b = $("btnProvider");
  if (b) { b.classList.add("open"); b.setAttribute("aria-expanded", "true"); }
}
document.addEventListener("click", (e) => {
  const p = $("providerPicker"), b = $("btnProvider");
  if (!p || !p.classList.contains("open")) return;
  if (p.contains(e.target) || (b && b.contains(e.target))) return;
  closeProviderPicker();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeProviderPicker(); });

const STATE_TEXT = { idle: "未启动", launching: "启动中…", waiting_login: "等待登录", logged_in: "已连接", error: "出错了" };
/* 只管发送按钮能不能点(切"发给谁"时也要立刻跟着变, 但不能顺手把其它状态一起刷新) */
function updateSendState() {
  if (!$("btnSend")) return;
  const usable = state.state === "logged_in";
  const localOnly = replyTarget() === "local";      // 只用本地模型: 不需要桥接浏览器
  const busyAll = state.busy || worldBusy;
  $("btnSend").disabled = (!usable && !localOnly) || busyAll;
  $("btnSend").title = worldBusy ? "本地模型正在执行, 结束后才能发送"
    : (!usable && localOnly ? localReplyLabel + ": 直接由设置里的模型回答, 不需要网页窗口" : "");
}
function refreshUi() {
  const target = $("selProvider").value;
  const active = state.provider || target;
  const st = STATE_TEXT[state.state] || state.state;
  const dotCls = state.state === "logged_in" ? "on" : (state.state === "error" ? "err" :
                  (state.state === "launching" || state.state === "waiting_login") ? "warn" : "");
  ["chipDot", "chipDot2"].forEach(id => { const d = $(id); if (d) d.className = "dot " + dotCls; });
  $("chipState").textContent = st + (state.busy ? " · 生成中" : "");
  // 底部那行显示的是"选中的站点"(和下拉选择一致); 状态/模式挂到它的悬浮提示
  const nameEl = $("chipName"); if (nameEl) nameEl.textContent = nameOf(target);
  const subEl = $("chipSub");
  const subText = state.state === "logged_in"
    ? (modeOf(active) === "dom" ? "DOM 快照模式" : "逐字流式模式")
    : (state.error ? state.error : "选择站点后启动");
  if (subEl) subEl.textContent = subText;
  $("pAvatar").textContent = shortOf(target);
  const pickBtn = $("btnProvider");
  if (pickBtn) {
    pickBtn.disabled = !!state.busy;
    pickBtn.title = "选择要连接的站点 · " + st + (subText ? " · " + subText : "");
    if (state.busy) closeProviderPicker();
  }
  const b = $("btnStart");
  $("selProvider").disabled = !!state.busy;
  if (state.busy) { b.disabled = true; b.textContent = "生成中…"; }
  else if (state.state === "launching" || state.state === "waiting_login") { b.disabled = true; b.textContent = "启动/登录中…"; }
  else if (state.state === "error") { b.disabled = false; b.textContent = "重试启动 " + nameOf(target); }
  else if (state.state === "logged_in") {
    if (target === state.provider) { b.disabled = true; b.textContent = "✓ " + nameOf(target) + " 已连接"; }
    else { b.disabled = false; b.textContent = "② 切换到 " + nameOf(target); }
  } else { b.disabled = false; b.textContent = "① 启动并登录 " + nameOf(target); }
  const usable = state.state === "logged_in";
  const busyAll = state.busy || worldBusy;          // 本地执行过程中也不让发
  updateSendState();
  $("btnNew").disabled = !usable || busyAll;
  $("btnNew2").disabled = !usable || busyAll;
  if ($("btnReply")) paintReplyButton();           // 按钮上的站点缩写/文案跟着连接状态走
  if ($("btnEngineer")) $("btnEngineer").disabled = !usable || state.busy;
  if ($("engSummary")) $("engSummary").disabled = !!state.busy;
  $("chatTitle").textContent = usable ? nameOf(active) + " · 会话" : "默认会话";
  $("chatMode").textContent = usable
    ? "✦ " + (modeOf(active) === "dom" ? "DOM 快照输出" : "流式输出") + " · " + (state.busy ? "生成中" : "就绪")
    : "✦ " + nameOf(active) + " · 网页桥接";
  if ($("modeText")) $("modeText").textContent = modeOf(active) === "dom" ? "DOM 快照" : "逐字流式";
  const hint = $("hintBox");
  if (state.state === "waiting_login") {
    ensureHint().innerHTML = "浏览器已弹出。<b>请在窗口中登录 " + nameOf(active) +
      "</b>（扫码或账号密码）。<br/>登录完成后本页会自动进入可用状态；登录态保存在本机 Profile，下次复用。";
  } else if (state.state === "logged_in") {
    if (hint) hint.remove();
  }
  saveProviderChoice(target);
  syncHistoryView();
  autoRefreshOnConnect();
}

/* 点「启动并登录」连上之后(或切换站点后): 自动刷新会话列表。
   注意: 页面打开时"本来就连着"不算一次新连接(那是刷新恢复现状, 不该把画面清空) ——
   只有本页里先见过"未连接"、之后才连上, 才算这次连接, 这时从空白一屏开始。 */
function autoRefreshOnConnect() {
  const key = state.state + "|" + (state.provider || "") + "|" + (state.started ? "1" : "0");
  if (state.state !== "logged_in") { lastConnKey = key; sawDisconnected = true; return; }
  if (key === lastConnKey) return;              // 已经为这次连接刷过了
  lastConnKey = key;
  if (sawDisconnected) showFreshStart();        // 这次连接: 新窗口 + 空消息列表
  sawDisconnected = false;
  setTimeout(() => { loadConversations(); syncHistoryView(true); }, 400);
  setTimeout(() => { if (!convItems.length) loadConversations(); }, 2600);   // 站点侧栏可能还没渲染出来
}
function ensureHint() {
  let h = $("hintBox");
  if (!h) {
    h = document.createElement("div");
    h.className = "hint-box";
    h.id = "hintBox";
    convEl.prepend(h);
  }
  return h;
}

function scrollBottom() {
  const box = convWrapEl || convEl;          // 滚动容器是最外层
  box.scrollTop = box.scrollHeight;
  if (typeof syncScrollbar === "function") syncScrollbar();
}
function addUserMsg(text, mi) {
  const row = document.createElement("div");
  row.className = "user-row";
  if (mi === undefined || mi === null) mi = transcript.length ? transcript.length : 0;
  row.dataset.mi = String(mi);                    // 给"输入导航"用来定位
  row.innerHTML = '<div class="user-bubble"></div>';
  row.firstChild.textContent = text;
  convEl.appendChild(row);
  scrollBottom();
  buildConvNav();
}
/* ===== 消息列表右侧的"输入导航" =====
   两态是同一个框、同一套行距: 一行 = 一条你发过的输入, 收起时只显示行尾那条小横线(框只有一列宽),
   鼠标落在框上展开 -> 框往左变宽、文字显示出来。点一行跳到那条消息; 滚动时高亮正在看的那条。 */
/* "当前那条"的判定线: 正文可视区顶上往下一点(按视口高度算)。
   跳转和滚动高亮**必须用同一条线** —— 否则点第 N 条会因为落点在视口中间而被算成第 N-1 条。 */
function navActiveLine(viewH) {
  return Math.min(140, Math.max(48, viewH * 0.16));
}
/* 悬停在某一行上 -> 在卡片左边浮出提示气泡, 显示这条输入的完整文字(行里会截断成 …);
   气泡内部不滚, 太长就被 line-clamp 截断。 */
let navTipItem = null, navTipText = "";
function hideConvNavTip() {
  const tip = $("convNavTip");
  if (tip) tip.hidden = true;
  navTipItem = null;
}
function showConvNavTip(item, text) {
  const tip = $("convNavTip"), box = $("convNav");
  if (!tip || !item || !box || !item.isConnected) return;
  navTipItem = item;
  navTipText = text;
  tip.textContent = String(text || "");
  tip.hidden = false;
  // 贴在卡片左边(不压住卡片); 左边放不下才翻到右边; 竖直方向跟着那一行, 并保证不出屏幕
  const rail = box.getBoundingClientRect();
  const row = item.getBoundingClientRect();
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = rail.left - tw - 12;
  if (left < 8) left = rail.right + 12;
  left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
  const top = Math.max(8, Math.min(row.top + row.height / 2 - th / 2, window.innerHeight - th - 8));
  tip.style.left = Math.round(left) + "px";
  tip.style.top = Math.round(top) + "px";
}
/* 跳到某一条: 把那条输入的顶部放到判定线稍上一点的地方(和"当前那条"的判定对齐);
   下面内容不够滚就贴底, 这时"当前那条"按贴底规则算成最后一条。 */
function jumpToMessage(mi) {
  const el = convEl.querySelector('.user-row[data-mi="' + mi + '"]');
  if (!el) return;
  if (convWrapEl) {
    const wrapTop = convWrapEl.getBoundingClientRect().top;
    const line = navActiveLine(convWrapEl.clientHeight);
    const max = Math.max(0, convWrapEl.scrollHeight - convWrapEl.clientHeight);
    const to = convWrapEl.scrollTop + (el.getBoundingClientRect().top - wrapTop) - line + 2;
    const clamped = Math.max(0, Math.min(to, max));
    try { convWrapEl.scrollTo({ top: clamped, behavior: "smooth" }); }
    catch (e) { convWrapEl.scrollTop = clamped; }
    if (typeof syncScrollbar === "function") syncScrollbar();
  } else {
    try { el.scrollIntoView({ block: "start" }); } catch (e) {}
  }
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1000);
  hideConvNavTip();
}
/* 当前看到的是哪一条: 正文里最后一个越过判定线的输入(还没滚过任何一条时按第一条)。
   选中的那一行 = 蓝色文字 + 蓝色横线, 正文里对应的那条输入也亮一圈 —— 两边一起跟着正文滚动走。 */
function clearNavActiveMark() {
  for (const el of convEl.querySelectorAll(".user-row.nav-active")) el.classList.remove("nav-active");
}
function convNavItems() {
  const list = $("convNavList");
  return list ? Array.from(list.children).filter(el => el.classList.contains("cnav-item")) : [];
}
function updateConvNavActive() {
  const box = $("convNav"), list = $("convNavList");
  if (!box || box.hidden || !list) { clearNavActiveMark(); return; }
  const rows = convNavItems();
  if (!rows.length) { clearNavActiveMark(); return; }
  // 判定线 = 正文可视区顶上往下一点(和 jumpToMessage 用同一条线, 点完立刻就是那条是"当前")
  const wrapTop = convWrapEl.getBoundingClientRect().top;
  const line = wrapTop + navActiveLine(convWrapEl.clientHeight);
  const max = Math.max(0, convWrapEl.scrollHeight - convWrapEl.clientHeight);
  const atBottom = max > 8 && convWrapEl.scrollTop >= max - 2;      // 贴底: 最后一条就是"当前"
  let active = null;
  for (const el of convEl.querySelectorAll(".user-row[data-mi]")) {
    if (el.getBoundingClientRect().top <= line) active = el.dataset.mi;   // 最后一个越过判定线的
    else break;
  }
  if (active === null) active = rows[0].dataset.mi;
  if (atBottom) {                                                   // 滚到底了: 当前 = 最后一条输入
    const all = convEl.querySelectorAll(".user-row[data-mi]");
    if (all.length) active = all[all.length - 1].dataset.mi;
  }
  for (const it of rows) it.classList.toggle("active", it.dataset.mi === active);
  clearNavActiveMark();
  const row = convEl.querySelector('.user-row[data-mi="' + active + '"]');
  if (row) row.classList.add("nav-active");
}
/* 放不下时不要滚动条, 也不留半行: 围着"当前那条"取一段显示, 剩下的地方给一格「……」。 */
function navMoreEl() {
  const list = $("convNavList");
  if (!list) return null;
  let more = $("convNavMore");
  if (!more) {
    more = document.createElement("div");
    more.className = "cnav-more";
    more.id = "convNavMore";
    more.textContent = "……";
    more.setAttribute("aria-hidden", "true");
  }
  if (more.parentElement !== list) list.appendChild(more);
  return more;
}
/* 收起态是一列等间距短线、展开态是一行一条的卡片, 位置都由 CSS 的文档流决定; 这里只管:
   哪一条是"当前"(蓝色), 以及"这一屏放得下几条 / 放不下的用「……」提示"。 */
function layoutConvNav() {
  const box = $("convNav"), list = $("convNavList");
  if (!box || box.hidden || !list) return;
  updateConvNavActive();                       // 先定"当前那条", 窗口要围着它取
  const items = convNavItems();
  if (!items.length) return;
  // 两态同一套行距/内边距(跟 CSS 里一致): 收起只是框变窄、文字藏起来, 什么都不动
  const rowH = 30;
  const gap = 2;
  const padY = 15;
  const railH = Math.max(1, box.clientHeight);
  const avail = Math.max(rowH, railH - 2 - padY * 2);
  const fit = Math.max(1, Math.floor((avail + gap) / (rowH + gap)));   // 这一屏最多几格
  const n = items.length;
  const more = navMoreEl();
  let from = 0, to = n;
  if (n > fit) {                               // 放不下: 留一格给「……」, 窗口围着当前那条
    const slots = Math.max(1, fit - 1);
    const ai = Math.max(0, items.findIndex(it => it.classList.contains("active")));
    const start = Math.max(0, Math.min(Math.round(ai - (slots - 1) / 2), n - slots));
    from = start;
    to = start + slots;
  }
  items.forEach((it, i) => {                   // 注意: 行是 display:flex, 得用 style 才盖得住
    const show = i >= from && i < to;
    if ((it.style.display === "none") === show) it.style.display = show ? "" : "none";
  });
  const truncated = n > fit;
  more.style.display = truncated ? "" : "none";
  if (truncated) {                             // 下面还有就放末尾, 上面还有就放开头
    const anchor = to < n ? items[to - 1].nextSibling : items[from];
    if (more.nextSibling !== anchor && more !== anchor) list.insertBefore(more, anchor);
  }
}
let navActiveRaf = 0;
function scheduleConvNavLayout() {
  if (navActiveRaf) return;
  navActiveRaf = requestAnimationFrame(() => { navActiveRaf = 0; layoutConvNav(); });
}
let navCollapseTimer = 0;
function openConvNav() {
  const box = $("convNav");
  if (!box) return;
  clearTimeout(navCollapseTimer);
  navCollapseTimer = 0;
  box.classList.add("open");
  requestAnimationFrame(() => { if (!$("convNav").hidden) layoutConvNav(); });   // 展开后行高变了, 重算窗口
}
function closeConvNav() {
  const card = $("convNavCard");
  if (!card) return;
  clearTimeout(navCollapseTimer);
  navCollapseTimer = setTimeout(() => {          // 稍等一下再收, 免得鼠标划过时来回闪
    const c = $("convNavCard");
    if (c && !c.matches(":hover") && !c.contains(document.activeElement)) {
      const b = $("convNav");
      if (b) b.classList.remove("open");
      requestAnimationFrame(() => { if (!$("convNav").hidden) layoutConvNav(); });
      hideConvNavTip();
    }
  }, 200);
}
/* 只有鼠标落在**卡片本身**上才展开(收起时卡片就是那一列短线), 离开卡片就收回。
   .convnav 只是定位用的空壳(pointer-events:none), 不会挡住正文的鼠标操作。 */
function bindConvNavHover() {
  const card = $("convNavCard");
  if (!card || card.dataset.bound) return;
  card.dataset.bound = "1";
  card.addEventListener("mouseenter", openConvNav);
  card.addEventListener("mouseleave", closeConvNav);
  card.addEventListener("focusin", openConvNav);      // 键盘也能展开
  card.addEventListener("focusout", closeConvNav);
}
function buildConvNav() {
  const box = $("convNav"), list = $("convNavList");
  if (!box || !list) return;
  bindConvNavHover();
  list.innerHTML = "";
  for (const el of convEl.querySelectorAll(".user-row[data-mi]")) {
    const mi = Number(el.dataset.mi);
    const msg = transcript[mi];
    if (Number.isNaN(mi) || !msg) continue;
    const item = document.createElement("div");
    item.className = "cnav-item";
    item.dataset.mi = String(mi);
    item.setAttribute("role", "button");
    const label = document.createElement("span");
    label.className = "t";
    label.textContent = String(msg.text || "").replace(/\s+/g, " ").trim() || "(空输入)";
    // 行尾那一格: 固定宽度的格子 + 里面那条横线(参考里的 tail > line 两层)
    const tail = document.createElement("span");
    tail.className = "cnav-tail";
    const dash = document.createElement("span");
    dash.className = "cnav-d";
    tail.appendChild(dash);
    item.appendChild(label);
    item.appendChild(tail);
    item.onclick = () => jumpToMessage(mi);
    item.onmouseenter = () => showConvNavTip(item, msg.text);
    item.onmouseleave = hideConvNavTip;
    list.appendChild(item);
  }
  box.hidden = convNavItems().length < 2;     // 只有一条输入时不占地方
  if (!box.hidden) layoutConvNav();
  else { box.classList.remove("open"); clearNavActiveMark(); hideConvNavTip(); }
}
function clearConvNav() {
  const box = $("convNav"), list = $("convNavList");
  if (list) list.innerHTML = "";
  if (box) { box.hidden = true; box.classList.remove("open"); }
  clearNavActiveMark();
  hideConvNavTip();
}
function openAssistantMsg() {
  const row = document.createElement("div");
  row.className = "message";
  const body = document.createElement("div");
  body.className = "assistant-body";
  body.dataset.rendered = "";
  row.appendChild(body);
  convEl.appendChild(row);
  scrollBottom();
  return { row, body, raw: "", reason: "" };
}
function renderCurrent() {
  if (!current) return;
  let html = "";
  if (current.reason) {
    html += '<details class="reasoning-details"><summary>思考过程</summary><div class="rtext">' +
            esc(current.reason) + '</div></details>';
  }
  html += current.raw ? renderMarkdown(current.raw) : "";
  const key = html + (current.busy ? "|cursor" : "");        // 光标状态一起参与比对
  if (current.body.dataset.rendered !== key) {
    current.body.innerHTML = html;
    current.body.dataset.rendered = key;
    if (current.busy) placeCursor(current.body);
    scrollBottom();
  }
}
/* 流式光标: 塞进最后一个有文字的内容块里, 紧贴文字右侧, 不另起一行 */
function placeCursor(root) {
  const cur = document.createElement("span");
  cur.className = "cursor";
  const blocks = root.querySelectorAll("p, h1, h2, h3, h4, h5, h6, blockquote, pre, li, td");
  let host = null;
  for (let i = blocks.length - 1; i >= 0; i--) {
    if (blocks[i].textContent.trim()) { host = blocks[i]; break; }
  }
  (host || root).appendChild(cur);
}
function appendReason(s) { if (current) { current.reason += s; renderCurrent(); } }
function appendText(s) { if (current) { current.raw += s; renderCurrent(); } }
function replaceText(s) { if (current) { current.raw = s; renderCurrent(); } }

function handle(ev) {
  switch (ev.type) {
    case "status":
      state = Object.assign({}, state, ev);
      refreshUi();
      if (ev.info) handle(ev.info);
      break;
    case "info":
      toast(ev.text, "info");
      break;
    case "message_start": {
      const h = $("hintBox"); if (h) h.remove();
      if (ev.conversation_id) adoptConversation(ev.conversation_id);   // 站点给了新会话 id
      showWelcome(false);   // 一旦有消息, 切回底部固定输入框
      current = openAssistantMsg();
      current.busy = true;
      renderCurrent();
      break;
    }
    case "delta": {
      if (!current) { current = openAssistantMsg(); current.busy = true; }
      if (ev.snapshot) { replaceText(ev.text); }
      else if (ev.kind === "reasoning") { appendReason(ev.text); }
      else { appendText(ev.text); }
      break;
    }
    case "message_end": {
      let answerText = "";
      if (current) {
        answerText = current.raw || "";
        current.busy = false;
        if (!current.raw && !current.reason) {
          current.body.innerHTML = '<p style="color:#BFC2C9">(空回复)</p>';
        }
        renderCurrent();
        histPush({ role: "assistant", text: current.raw, reason: current.reason });
        current = null;
      }
      if (ev.conversation_id) adoptConversation(ev.conversation_id);
      if (currentMode === "world" && worldTask && answerText) {
        const task = worldTask;
        worldTask = null;                            // 防止重复触发
        worldAfterAnswer(task, answerText);
      }
      clearTimeout(convTimer);                       // 一轮结束后刷新会话列表(标题/新会话)
      convTimer = setTimeout(loadConversations, 800);
      break;
    }
    case "world": {
      handleWorldEvent(ev);
      break;
    }
    case "engineer": {
      handleEngineerEvent(ev);          // 计划/进度/结果都画到消息列表里
      break;
    }
    case "error": {
      addErrorRow(ev.text);
      break;
    }
  }
}

function connect() {
  try { ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws"); }
  catch (e) { setTimeout(connect, 2000); return; }
  ws.onmessage = (m) => { try { handle(JSON.parse(m.data)); } catch (e) {} };
  ws.onclose = () => setTimeout(connect, 2000);
}

/* ========== World 模式新流程 ==========
 * 1) 你的原始消息原样发给网页模型(ChatGPT)
 * 2) 回答直接显示在消息列表里(和普通聊天一样)
 * 3) 本地模型把回答落盘到工作区对应文件
 * 4) 跑一次自测命令; 有报错就把报错回给 ChatGPT 继续修(最多 2 轮)
 */
const FENCE = String.fromCharCode(96).repeat(3);   // 三个反引号(避免在源码里出现)
let worldTask = null;         // 本轮 World 任务(等回答回来后落盘)
let worldFixRounds = 0;       // 自动修复轮数
let worldVerifyCard = null;   // 本地模型验证的卡片(实时事件往里写)
let worldBusy = false;        // 本地执行链路进行中: 发送按钮置灰
let worldContinue = false;    // 已把报错回传, 等下一轮回答继续跑
let worldCards = [];          // 本轮用过的卡片(结束时统一停掉"进行中"动画)

/* 卡片既可以实时长出来, 也能从历史记录里重画(刷新后还在) */
function paintWorldCard(spec) {
  const el = document.createElement("div");
  el.className = "task-card";
  if (spec.wid) el.dataset.wid = spec.wid;
  el.innerHTML = '<div class="task-head"><span>🛠️</span><b></b>' +
                 '<span class="tspin" title="进行中"></span><span class="tcount"></span>' +
                 '<span class="caret">▾</span></div><div class="task-list"></div><div class="task-foot"></div>';
  el.querySelector("b").textContent = spec.title || "";
  el.querySelector(".tcount").textContent = spec.count || "";
  el.querySelector(".tspin").style.display = spec.busy ? "" : "none";
  el.querySelector(".task-foot").textContent = spec.foot || "";
  const list = el.querySelector(".task-list");
  // 历史里可能积了重复行(每次刷新同步状态都会追加一遍): 重画时顺手去重, 并把干净的写回历史
  const before = (spec.rows || []).length;
  spec.rows = dedupeWorldRows(spec.rows);
  if (spec.rows.length !== before) histFlush();
  spec.rows.forEach(r => {
    const row = document.createElement("div");
    row.className = "task-item" + (r.cls ? " " + r.cls : "");
    row.innerHTML = '<span class="ti"></span><span class="ttext"><span class="tt"></span></span>';
    row.querySelector(".ti").textContent = r.mark || "•";
    row.querySelector(".tt").textContent = r.text || "";
    if (r.detailsText) {
      const d = document.createElement("details");
      d.innerHTML = "<summary></summary><pre></pre>";
      d.querySelector("summary").textContent = r.detailsLabel || "查看详情";
      d.querySelector("pre").textContent = r.detailsText;
      row.querySelector(".ttext").appendChild(d);
    }
    list.appendChild(row);
  });
  el.querySelector(".task-head").onclick = () => el.classList.toggle("folded");
  convEl.appendChild(el);
  scrollBottom();
  return el;
}
/* 去掉完全相同的行(标记+文字一样就算重复), 保留第一次出现的那条 */
function dedupeWorldRows(rows) {
  const seen = Object.create(null);
  return (rows || []).filter(r => {
    const k = String((r && r.mark) || "") + "\u0000" + String((r && r.text) || "");
    if (seen[k]) return false;
    seen[k] = 1;
    return true;
  });
}
/* 这行是不是已经画过了 —— 同一批结果会从"事件/接口返回值/刷新的状态同步"进来好几遍 */
function worldRowExists(card, text) {
  if (!card || !card.spec) return false;
  const t = String(text || "").slice(0, 300);        // worldRow 存的是截断后的文字
  return (card.spec.rows || []).some(r => r.text === t);
}
let worldWid = 0;
function handleFor(spec, el) {
  return { el: el, spec: spec, list: el ? el.querySelector(".task-list") : null,
           foot: el ? el.querySelector(".task-foot") : null,
           count: el ? el.querySelector(".tcount") : null };
}
/* 最近一条同阶段的卡片记录(可能在历史里) */
function lastWorldSpec(prefix) {
  for (let i = transcript.length - 1; i >= 0; i--) {
    const m = transcript[i];
    if (m && m.role === "world" && m.card && String(m.card.title || "").indexOf(prefix) === 0) return m.card;
  }
  return null;
}
/* 找本阶段要更新的卡片: 先用本页正在跑的, 再退回历史里的那张(刷新过也能继续更新, 不会新开一张) */
function cardFor(prefix) {
  for (let i = worldCards.length - 1; i >= 0; i--) {
    if (worldCards[i] && worldCards[i].spec && worldCards[i].spec.title.indexOf(prefix) === 0) return worldCards[i];
  }
  const spec = lastWorldSpec(prefix);
  if (!spec) return null;
  const el = spec.wid ? document.querySelector('#conv .task-card[data-wid="' + spec.wid + '"]') : null;
  const c = handleFor(spec, el);
  worldCards.push(c);
  return c;
}
function worldCard(title) {
  const spec = { title: title, count: "", foot: "", busy: true, rows: [],
                 wid: "w" + (++worldWid) + "-" + Date.now().toString(36) };
  const el = paintWorldCard(spec);
  const entry = { role: "world", card: spec };      // 进历史: 刷新后还在
  histPush(entry);
  const card = Object.assign(handleFor(spec, el), { entry: entry });
  worldCards.push(card);
  return card;
}
/* 本地这一轮结束了: 所有卡片停止"进行中"动画 */
function stopWorldSpinners() {
  worldCards.forEach(c => {
    if (!c || !c.spec) return;
    c.spec.busy = false;
    const sp = c.el && c.el.querySelector(".tspin");
    if (sp) sp.style.display = "none";
  });
  worldCards = [];
  histFlush();
}
function setCardFoot(card, text, busy) {
  if (!card) return;
  card.spec.foot = text || "";
  if (busy !== undefined) card.spec.busy = busy;
  if (card.foot) card.foot.textContent = card.spec.foot;
  const sp = card.el && card.el.querySelector(".tspin");
  if (sp) sp.style.display = card.spec.busy ? "" : "none";
  histFlush();
}
function setCardCount(card, text) {
  if (!card) return;
  card.spec.count = text || "";
  if (card.count) card.count.textContent = card.spec.count;
  histFlush();
}
function worldRow(card, mark, text, cls, detailsText, detailsLabel) {
  if (!card) return null;
  const spec = { mark: mark, text: String(text || "").slice(0, 300), cls: cls || "",
                 detailsText: String(detailsText || "").slice(0, 4000), detailsLabel: detailsLabel || "" };
  if (card.spec.rows.length < 24) card.spec.rows.push(spec);   // 卡片别无限长
  if (!card.list) { histFlush(); return null; }                // 只更新记录(画面下次重画)
  const row = document.createElement("div");
  row.className = "task-item" + (cls ? " " + cls : "");
  row.innerHTML = '<span class="ti"></span><span class="ttext"><span class="tt"></span></span>';
  row.querySelector(".ti").textContent = mark;
  row.querySelector(".tt").textContent = text;
  if (detailsText) {
    const d = document.createElement("details");
    d.innerHTML = "<summary></summary><pre></pre>";
    d.querySelector("summary").textContent = detailsLabel || "查看详情";
    d.querySelector("pre").textContent = detailsText;
    row.querySelector(".ttext").appendChild(d);
  }
  card.list.appendChild(row);
  histFlush();
  scrollBottom();
  return row;
}

/* 落盘卡片: ChatGPT 没给代码时, 把"代码是谁写的"说清楚(否则用户会以为凭空写入) */
function noteSelfAuthored(card, ev) {
  if (!card || !ev || !ev.selfAuthored) return;
  const t = "注意: " + (ev.note || "ChatGPT 的回答里没有代码, 这些改动由本地模型自己编写");
  if (!worldRowExists(card, t)) worldRow(card, "!", t, "err");
  histFlush();
}

/* 确认框: 列出本地模型建议的改动, 逐条勾选 */
function confirmApply(files, hasVerify) {
  return new Promise((resolve) => {
    const ov = $("applyOverlay"), list = $("applyList");
    const risky = files.filter((f) => f.op === "warn").length;
    $("applyNote").textContent = "本地模型准备好了下面这些步骤, 勾选后才会执行; 点「取消」就直接结束(什么都不做)。" +
      (risky ? " 注意: 有 " + risky + " 项被标成「疑似改坏项目」(大幅截断/路径写错), 默认不勾选 —— 看清原因再决定。" : "");
    list.innerHTML = "";
    if (!files.length) {
      const empty = document.createElement("div");
      empty.className = "hint2";
      empty.textContent = "(这次回答里没有具体文件改动, 只有自测)";
      list.appendChild(empty);
    }
    files.forEach((f) => {
      const it = document.createElement("div");
      it.className = "apply-item";
      it.innerHTML = '<input type="checkbox"' + (f.op === "warn" ? "" : " checked") +
        ' /><div class="ai-main">' +
        '<div class="ai-head"><span class="ai-op"></span><span class="ai-path"></span>' +
        '<span class="ai-meta"></span></div></div>';
      it.querySelector(".ai-op").textContent = f.op;
      it.querySelector(".ai-op").classList.add(f.op);
      it.querySelector(".ai-path").textContent = f.path;
      it.querySelector(".ai-meta").textContent = (f.op === "warn" || f.op === "invalid")
        ? ("⚠ " + (f.error || "这条被拦下了") +
           (f.op === "warn" ? " · +" + (f.add || 0) + " / -" + (f.del || 0) + " 行 · " + (f.size || 0) + "B" : ""))
        : (f.op === "delete")
          ? ("删除 " + (f.oldLines || 0) + " 行")
          : ("+" + (f.add || 0) + " / -" + (f.del || 0) + " 行 · " + (f.size || 0) + "B" +
             (f.unchanged ? " · 内容没变化" : ""));
      if (f.preview) {
        const d = document.createElement("details");
        d.innerHTML = "<summary>查看新内容</summary><pre></pre>";
        d.querySelector("pre").textContent = f.preview;
        it.querySelector(".ai-main").appendChild(d);
      }
      list.appendChild(it);
    });
    if (hasVerify) {                                  // 第二类步骤: 代码自测
      const v = document.createElement("div");
      v.className = "apply-item";
      v.innerHTML = '<input type="checkbox" id="applyVerify" checked />' +
        '<div class="ai-main"><div class="ai-head">' +
        '<span class="ai-op update">自测</span>' +
        '<span class="ai-path">本地模型自己构建/测试</span>' +
        '<span class="ai-meta">挑命令 → 看输出 → 有报错自己改 → 直到通过</span>' +
        '</div></div>';
      list.appendChild(v);
    }
    ov.classList.add("show");
    const boxes = () => Array.from(list.querySelectorAll("input"));
    $("applyOk").onclick = () => {
      const fileBoxes = boxes().filter(b => b.id !== "applyVerify");
      const picked = files.filter((f, i) => fileBoxes[i] && fileBoxes[i].checked);
      const vb = $("applyVerify");
      ov.classList.remove("show");
      resolve({ files: picked, verify: vb ? vb.checked : false });
    };
    $("applySkip").onclick = () => { ov.classList.remove("show"); resolve(null); };
  });
}

/* 落盘阶段的事件: 让"一直在转"的卡片能自己更新(刷新过的页面也一样) */
function handleApplyEvent(ev) {
  let card = cardFor("本地落盘");
  if (!card) {
    card = worldCard("本地落盘(本地模型写入工作区)");
    if (!worldBusy) setCardFoot(card, "", false);             // 不是本页发起的: 不转圈
  }
  if (ev.action === "start") {
    setCardFoot(card, ev.text || "正在把 ChatGPT 的回答落实成文件改动…", worldBusy);
    return;
  }
  if (ev.action === "preview") {
    const files = ev.files || [];
    noteSelfAuthored(card, ev);
    if (!files.length) {
      setCardFoot(card, "本地模型认为这次回答没有具体文件改动" +
        (ev.text ? ": " + ev.text : "") + " —— 你可以选择只让它去项目里自测", false);
      return;
    }
    files.forEach(f => {
      const risky = (f.op === "warn" || f.op === "invalid");
      const text = (risky ? "⚠ " + String(f.error || "这条被拦下了") + " — " : "") +
        f.op + " " + f.path +
        (f.op === "delete" ? " (删除)" :
          " +" + (f.add || 0) + "/-" + (f.del || 0) + " 行 · " + (f.size || 0) + "B") +
        (f.unchanged ? " · 内容没变化" : "");
      if (!worldRowExists(card, text))
        worldRow(card, risky ? "!" : "•", text, risky ? "err" : "");
    });
    setCardCount(card, files.length + " 个文件");
    const riskCount = files.filter(f => f.op === "warn" || f.op === "invalid").length;
    setCardFoot(card, "本地模型建议 " + files.length + " 个文件改动" +
      (riskCount ? " (其中 " + riskCount + " 项疑似改坏项目, 默认不勾选)" : "") + ", 等你确认步骤…", false);
    return;
  }
  if (ev.action === "applied") {
    const applied = ev.applied || [], skipped = ev.skipped || [];
    noteSelfAuthored(card, ev);
    // 同一批结果会从"事件 + 接口返回值 + 刷新后的状态同步"进来好几遍, 已经画过的不再画
    applied.forEach(a => {
      const t = a.op + " " + a.path + (a.size ? " (" + a.size + "B)" : "");
      if (!worldRowExists(card, t)) worldRow(card, "✓", t, "done");
    });
    skipped.forEach(s => { if (!worldRowExists(card, s)) worldRow(card, "!", s, "err"); });
    if (applied.length || skipped.length) setCardCount(card, applied.length + " 个文件");
    setCardFoot(card, (ev.text ? ev.text + " · " : "") + "应用 " + applied.length + " 项" +
      (skipped.length ? ", 跳过 " + skipped.length + " 项" : ""), false);
  }
}

/* 页面加载/刷新后: 取最近一轮本地执行的状态, 把卡片补成正确的样子
   (否则会停在"正在把 ChatGPT 的回答落实成文件改动…"这种过期文案上)
   注意: 只补正页面上**已经存在**的卡片, 绝不凭空造卡片 —— 服务端只留了"最近一轮"的状态,
   点到别的会话(或刚点完「新对话」)时那条状态跟眼前的会话没关系, 照着它画卡片就会
   在空会话里冒出上一轮的落盘/验证卡片。 */
async function syncWorldState() {
  try {
    const r = await (await fetch("/api/world/state")).json();
    if (!r) return;
    if (r.apply && lastWorldSpec("本地落盘"))
      handle({ type: "world", stage: "apply", ...r.apply });
    if (r.verify && lastWorldSpec("本地验证"))
      handle({ type: "world", stage: "verify", ...r.verify });
  } catch (e) { /* 老后端没有这个接口就算了 */ }
}

/* 本地模型验证项目的过程(服务端边跑边推事件) */
function handleWorldEvent(ev) {
  if (ev.stage === "apply") { handleApplyEvent(ev); return; }
  if (ev.stage !== "verify") return;
  let existing = cardFor("本地验证");
  if (!existing || (existing.el && !existing.el.isConnected)) {
    worldVerifyCard = existing || worldCard("本地验证(按需求验收点找证据)");
    // 刷新过页面/不是本页发起的任务: 只显示过程, 不转圈
    setCardFoot(worldVerifyCard, worldVerifyCard.spec.foot || "", worldBusy);
  } else {
    worldVerifyCard = existing;
  }
  const c = worldVerifyCard;
  if (ev.action === "run") {
    worldRow(c, "▸", "执行: " + (ev.command || ""), "run");
    setCardFoot(c, "本地模型正在跑: " + (ev.command || ""), true);
  } else if (ev.action === "run-done") {
    worldRow(c, ev.code === 0 ? "✓" : "✗", (ev.command || "") + " → 退出码 " + ev.code,
             ev.code === 0 ? "done" : "err", ev.text || "", "查看输出");
    setCardFoot(c, ev.code === 0 ? "这一步通过" : "这一步报错, 本地模型会自己修", true);
  } else if (ev.action === "fix-done") {
    const files = (ev.applied || []).map(a => a.op + " " + a.path).join(", ");
    worldRow(c, "✓", "本地模型自己改: " + (files || "(没有文件变化)"), "done");
    (ev.skipped || []).forEach(s => worldRow(c, "!", s, "err"));
    setCardFoot(c, "本地模型改了代码, 接着验证…", true);
  } else if (ev.action === "repair") {
    worldRow(c, "↻", ev.text || "本地模型还没动手改过, 再让它自己修几轮", "run");
    setCardFoot(c, "本地模型还没动手改, 先让它自己修(本地能修的不甩给网页模型)…", true);
  } else if (ev.action === "blocked") {
    worldRow(c, "!", "命令被安全策略拦截: " + (ev.text || ""), "err");
    setCardFoot(c, "有命令被拦截, 已停下", false);
  } else if (ev.action === "checks") {
    const checks = ev.checks || [];
    checks.forEach((c, i) => {
      const exp = (c && (c.expect || c.text)) || String(c || "");
      const how = (c && c.how) || "";
      worldRow(c, "•", "验收点 " + ((c && c.id) || (i + 1)) + ": " + exp, "run",
               how ? "验法: " + how : "", how ? "打算怎么验" : "");
    });
    setCardFoot(c, "已按需求定下 " + checks.length + " 条验收点, 开始找证据…", true);
  } else if (ev.action === "no-evidence") {
    worldRow(c, "!", "不接受「完成」: " + (ev.text || "还没跑出任何可当证据的命令(只读文件不算验证)"), "err");
    setCardFoot(c, "它想宣布通过但拿不出证据, 已让它重来…", true);
  } else if (ev.action === "no-checks") {
    worldRow(c, "!", "不接受「完成」: " + (ev.text || "它还没定出可判定的验收点"), "err");
    setCardFoot(c, "没有验收点就没有判定标准, 已让它重来…", true);
  } else if (ev.action === "weak-evidence") {
    worldRow(c, "!", "不接受「完成」: 证据对不上这次真跑过的命令/输出(或只有编译证据)", "err");
    setCardFoot(c, "证据不成立, 已让它重来…", true);
  } else if (ev.action === "parse-fail") {
    worldRow(c, "!", "本地模型没给出可解析的验证决策", "err");
    setCardFoot(c, "本地模型没给出可解析的决策", false);
  } else if (ev.action === "done") {
    worldRow(c, "✓", "本地模型确认: " + (ev.text || "项目已通过验证"), "done");
    setCardFoot(c, "本地模型确认项目跑通了(验收点 + 真实证据)", false);
  } else if (ev.action === "finished") {
    setCardFoot(c, ev.text || "本地验证结束", false);
    stopWorldSpinners();
  }
}

/* 外层: 结束后一定解锁发送按钮(除非已经安排好下一轮继续跑) */
async function worldAfterAnswer(task, answer) {
  worldContinue = false;
  try {
    await worldPipeline(task, answer);
  } finally {
    stopWorldSpinners();                         // 动画一定要停, 不能一直转
    if (worldContinue) { worldTask = task; }     // 报错已回传, 保持忙碌等下一轮
    else { worldBusy = false; }
    refreshUi();
  }
}

async function worldPipeline(task, answer) {
  const card = worldCard("本地落盘(本地模型写入工作区)");
  setCardFoot(card, "正在把 ChatGPT 的回答落实成文件改动…", true);
  let confirm = true, settings = {};
  try {
    settings = await (await fetch("/api/settings")).json();
    confirm = !((settings.engine && settings.engine.confirm_apply) === "0");
  } catch (e) {}
  let r = { applied: [], skipped: [] };
  try {
    if (confirm) {                                  // 先只拿方案 -> 弹确认框 -> 按勾选执行
      const pv = await post("/api/world/apply", { task: task, text: answer, dry_run: true });
      const files = pv.files || [];
      noteSelfAuthored(card, pv);
      setCardFoot(card, files.length
        ? ("本地模型建议 " + files.length + " 个文件改动, 等你确认步骤…")
        : ("本地模型认为这次回答没有具体文件改动" + (pv.message ? ": " + pv.message : "") +
           " —— 你可以选择只让它去项目里自测"), false);
      const choice = await confirmApply(files, true);
      if (!choice) {                                 // 取消 = 直接结束
        setCardFoot(card, "你取消了, 本次不做任何改动", false);
        return;
      }
      if (!choice.files.length) {
        setCardFoot(card, files.length ? "你没有勾选任何文件, 跳过写入" : "没有需要写入的文件", false);
      } else {
        r = await post("/api/world/commit", {
          files: choice.files.map(f => ({ op: f.realOp || f.op, path: f.path,
                                          content: f.content, force: !!f.force })),
          message: pv.message || "",
        });
      }
      if (!choice.verify) {                          // 没选自测 -> 到此结束
        setCardFoot(card, (card.spec.foot ? card.spec.foot + " · " : "") + "你选择跳过自测, 到此结束", false);
        return;
      }
    } else {
      r = await post("/api/world/apply", { task: task, text: answer });
    }
  } catch (e) {
    worldRow(card, "✗", "落盘失败: " + e.message, "err");
    setCardFoot(card, "✗ 本地模型没能落盘(检查设置里的本地模型是否在跑)", false);
    return;
  }
  const applied = r.applied || [], skipped = r.skipped || [];
  noteSelfAuthored(card, r);
  if (applied.length || skipped.length) {
    // 服务端已经把同一批结果作为事件广播过一遍(刷新过的页面靠它), 这里别再画第二行
    applied.forEach(a => {
      const t = a.op + " " + a.path + (a.size ? " (" + a.size + "B)" : "");
      if (!worldRowExists(card, t)) worldRow(card, "✓", t, "done");
    });
    skipped.forEach(s => { if (!worldRowExists(card, s)) worldRow(card, "!", s, "err"); });
    setCardCount(card, applied.length + " 个文件");
  } else if (!confirm) {                 // 全自动模式且没有任何改动: 不留一张空卡片
    card.el.remove();
    return;
  }
  if (applied.length || skipped.length) {
    setCardFoot(card, (r.message || "") + " · 应用 " + applied.length + " 项" +
      (skipped.length ? ", 跳过 " + skipped.length + " 项" : ""), false);
  }

  await worldVerify(task, answer, applied, settings, card);
}

/* 本地模型自己去验证这个项目: 挑构建/测试命令 -> 看输出 -> 自己改 -> 直到通过 */
const VERIFY_WHY = {
  blocked: "命令被安全策略拦住了",
  "parse-fail": "它没能给出可解析的验证决策",
  "no-checks": "它没先定出可判定的验收点(没有判定标准就不算通过)",
  "weak-evidence": "它给的证据对不上真跑过的命令/输出, 或只有编译证据",
  "no-evidence": "它没能跑出任何可当证据的命令(只读文件不算验证)",
  "tool-error": "它的命令在这台机器上跑不了",
  "rounds-exhausted": "轮数用完也没拿出证据宣布通过",
  "real-error": "项目里有真实报错",
};

function verifyWhy(v) {
  const r = (v && v.reason) || "";
  if (VERIFY_WHY[r]) return VERIFY_WHY[r];
  // 旧版服务端不给 reason: 按轮次动作猜一个(至少别把什么都赖成"命令跑不了")
  const acts = ((v && v.rounds) || []).map(x => x && x.action);
  for (const k of ["tool-error", "no-evidence", "parse-fail", "blocked"]) {
    if (acts.includes(k)) return VERIFY_WHY[k];
  }
  return "没能自己完成验证";
}

async function worldVerify(task, answer, applied, settings, card) {
  worldVerifyCard = null;
  let v;
  try {
    v = await post("/api/world/verify", {
      task: task, answer: answer, applied: applied,
      command: (settings.engine && settings.engine.test_cmd) || "",
      max_rounds: 4,
    });
  } catch (e) {
    const c0 = worldVerifyCard || worldCard("本地验证(按需求验收点找证据)");
    worldRow(c0, "!", "验证没能执行: " + e.message, "err");
    setCardFoot(c0, "验证没能执行", false);
    return;
  }
  const vcard = worldVerifyCard || worldCard("本地验证(按需求验收点找证据)");
  // 把"它到底按什么标准判通过"摊开给用户看: 验收点 + 验证的是哪个工作区 + 留痕在哪
  if (v.checks) {
    String(v.checks).split("\n").filter(Boolean).slice(0, 6).forEach((line) => {
      const t = "验收点 " + line.replace(/^-\s*/, "");
      if (!worldRowExists(vcard, t)) worldRow(vcard, "•", t, "");
    });
  }
  if (v.root) {
    const t = "验证的工作区: " + v.root;
    if (!worldRowExists(vcard, t)) worldRow(vcard, "•", t, "");
  }
  if (v.audit) {
    const t = "证据留痕: " + v.audit;
    if (!worldRowExists(vcard, t)) worldRow(vcard, "•", t, "");
  }
  setCardCount(vcard, v.ok ? "通过" : (v.gave_up ? "没搞定" : "未通过"));
  if (v.ok) {
    setCardFoot(vcard, "本地模型确认项目跑通了(验收点 + 真实证据" +
      (v.behavior_ok ? "" : " · 仅编译/静态检查") + ")", false);
    worldBusy = false; refreshUi(); return;
  }
  if (!v.real_error) {          // 不是项目报错 —— 具体是哪种情况如实说, 别一律赖"命令跑不了"
    setCardFoot(vcard, "本地模型没能自己完成验证(不是项目报错: " + verifyWhy(v) + "), 先停在这里", false);
    return;
  }
  // 有真实报错: 本地模型自己修过没有? 没动手就先把话说明白, 别默默甩给网页模型
  const fixes = v.local_fixes || 0;
  if (!fixes && !v.tried_local_fix) {
    worldRow(vcard, "!", "本地模型一次都没动手改就想交出去 —— 报错留在这里", "err",
             (v.error_log || v.last_output || "").slice(-4000), "查看报错");
    setCardFoot(vcard, "本地模型没动手改, 先停在这里(报错见卡片)", false);
    return;
  }
  if (replyTarget() === "local") {      // 只用本地模型: 没有网页模型可回传, 报错就留在卡片里
    worldRow(vcard, "!", "本地模型自己改了 " + fixes + " 次都没修好", "err",
             (v.error_log || v.last_output || "").slice(-4000), "查看报错");
    setCardFoot(vcard, "只看本地模型, 不回传 ChatGPT: 报错留在卡片里, 你可以直接说下一步", false);
    return;
  }
  if (worldFixRounds >= 2) {
    worldRow(vcard, "!", "已经交给 ChatGPT 修过 2 轮, 先停在这里", "err");
    setCardFoot(vcard, "先停在这里(需要你自己看看了)", false);
    return;
  }
  worldFixRounds++;
  // 回给 ChatGPT 的消息: 原始需求 + 本地模型改了哪些文件 + 真正的报错日志
  const logText = (v.error_log || v.last_output || "").trim();
  const changedTxt = (v.changed || []).map(a => a.op + " " + a.path).join(", ");
  let fixMsg = "按你上面的方案, 本地已经改好了" + (changedTxt ? "(" + changedTxt + ")" : "") +
    ", 但项目验证没通过。请根据下面的报错给出修复方案(需要改的文件请给完整内容)。\n";
  if (v.checks) fixMsg += "\n这次要满足的验收点:\n" + String(v.checks).slice(0, 1500) + "\n";
  if (v.last_command) fixMsg += "最后一次命令: " + v.last_command + "\n";
  if (logText) {
    fixMsg += "\n" + FENCE + "\n" + logText.slice(-6000) + "\n" + FENCE + "\n";
  } else {
    fixMsg += "\n(本地模型这次没能跑出可用日志: " +
      ((v.rounds && v.rounds.length) ? JSON.stringify(v.rounds[v.rounds.length - 1]).slice(0, 800) : "无验证记录") + ")\n";
  }
  setCardFoot(vcard, "本地模型自己改了 " + fixes + " 次没修好, 才把报错发回 ChatGPT(第 " + worldFixRounds + " 轮)", false);
  addUserMsg("【自测报错 · 第 " + worldFixRounds + " 轮】本地模型没修好, 已把报错发回 ChatGPT");
  histPush({ role: "user", text: fixMsg });
  worldTask = task;                       // 下一轮回答继续落盘 + 自测
  worldContinue = true;                   // 保持忙碌, 别解锁发送
  try { await post("/api/chat", { text: fixMsg }); }
  catch (e) { toast("回传报错失败: " + e.message, "err"); }
}

/* ================= 编码执行器: 任务卡片画在消息列表里 ================= */
let taskCard = null;        // { el, items: [el], done: n, total: n }

function ensureTaskCard(total) {
  if (taskCard && taskCard.el.isConnected) return taskCard;
  const el = document.createElement("div");
  el.className = "task-card";
  el.innerHTML =
    '<div class="task-head"><span>🗒️</span><b>任务</b>' +
    '<span class="tcount"></span><span class="caret">▾</span></div>' +
    '<div class="task-list"></div><div class="task-foot"></div>';
  el.querySelector(".task-head").onclick = () => el.classList.toggle("folded");
  convEl.appendChild(el);
  taskCard = { el: el, items: [], done: 0, total: total || 0, foot: el.querySelector(".task-foot") };
  scrollBottom();
  return taskCard;
}
function cardCount() {
  if (!taskCard) return;
  const c = taskCard.el.querySelector(".tcount");
  if (c) c.textContent = taskCard.done + "/" + taskCard.total + " 已完成";
}
/* 规划好了: 按计划画一个清单(和参考图一样, 每步一个 ✓) */
function renderTaskPlan(steps) {
  const card = ensureTaskCard(steps.length);
  card.total = steps.length;
  card.done = 0;
  const list = card.el.querySelector(".task-list");
  list.innerHTML = "";
  card.items = [];
  steps.forEach((s, i) => {
    const item = document.createElement("div");
    item.className = "task-item";
    item.innerHTML = '<span class="ti">▸</span><span class="ttext"><span class="tt"></span>' +
                     '<span class="tmeta"></span></span>';
    item.querySelector(".tt").textContent = s.text || ("步骤 " + (i + 1));
    const meta = item.querySelector(".tmeta");
    if ((s.files || []).length) meta.textContent = "需要文件: " + s.files.join(", ");
    list.appendChild(item);
    card.items.push(item);
  });
  cardCount();
  scrollBottom();
}
function setStepState(idx, state, metaText) {
  if (!taskCard) return;
  const item = taskCard.items[idx - 1];
  if (!item) return;
  item.classList.remove("run", "done", "err");
  if (state) item.classList.add(state);
  const ti = item.querySelector(".ti");
  if (ti) ti.textContent = state === "done" ? "✓" : (state === "err" ? "✗" : "▸");
  if (metaText !== undefined) {
    const m = item.querySelector(".tmeta");
    if (m) m.textContent = metaText;
  }
  if (state === "done") {
    taskCard.done = Math.min(taskCard.total, taskCard.done + 1);
    cardCount();
  }
}
function stepDetails(idx, label, text) {
  if (!taskCard) return;
  const item = taskCard.items[idx - 1];
  if (!item) return;
  const d = document.createElement("details");
  d.innerHTML = "<summary></summary><pre></pre>";
  d.querySelector("summary").textContent = label;
  d.querySelector("pre").textContent = text;
  item.querySelector(".ttext").appendChild(d);
  scrollBottom();
}
function handleEngineerEvent(ev) {
  const stage = ev.stage;
  if (stage === "plan") { renderTaskPlan(ev.steps || []); return; }
  if (stage === "step") {
    ensureTaskCard(ev.total);
    setStepState(ev.index, "run", (ev.files && ev.files.length)
      ? ("已发送给网页模型: " + ev.files.join(", ") + " (" + (ev.chars || 0) + " 字符)")
      : "已发送给网页模型(无附带文件)");
    if (ev.prompt) stepDetails(ev.index, "查看发给网页模型的请求", ev.prompt);
    return;
  }
  if (stage === "answer") {
    stepDetails(ev.index, "查看网页模型的回复 (" + (ev.chars || 0) + " 字符)", ev.text || "");
    return;
  }
  if (stage === "apply") {
    setStepState(ev.index, "done",
      (ev.applied || []).length ? "已落盘: " + ev.applied.map(a => a.op + " " + a.path).join(", ")
                                : "没有文件变化");
    if (ev.skipped && ev.skipped.length) {
      const card = ensureTaskCard();
      const line = document.createElement("div");
      line.className = "task-item err";
      line.innerHTML = '<span class="ti">!</span><span class="ttext"><span class="tt"></span></span>';
      line.querySelector(".tt").textContent = "跳过: " + ev.skipped.join("; ");
      card.el.querySelector(".task-list").appendChild(line);
    }
    return;
  }
  if (stage === "summary") {
    const keep = current;
    current = openAssistantMsg();
    current.raw = "**工程任务摘要**\n\n" + (ev.text || "");
    renderCurrent();
    current = keep;
    return;
  }
  if (stage === "done" || stage === "error") {
    const card = taskCard || ensureTaskCard();
    if (card && card.foot) card.foot.textContent = (ev.text || "") +
      (ev.applied && ev.applied.length ? " · 应用 " + ev.applied.length + " 项" : "");
    if (stage === "done") toast("工程任务完成", "info");
    else toast("工程任务出错", "err");
    taskCard = null;          // 下一次任务重新开卡片
    scrollBottom();
    return;
  }
  if (stage === "progress" && taskCard && taskCard.foot) {
    taskCard.foot.textContent = ev.text || "";
  }
}

/* ============ 空状态欢迎区 + Chat/World 模式 ============ */
const MODE_KEY = "wlb.mode.v1";
function savedMode() {
  try { return localStorage.getItem(MODE_KEY) === "world" ? "world" : "chat"; } catch (e) { return "chat"; }
}
function saveMode(m) {
  try { localStorage.setItem(MODE_KEY, m); } catch (e) {}
}
let currentMode = savedMode();
const composerEl = document.querySelector(".composer");
const composerWrapEl = document.querySelector(".composer-wrap");
const tipEl = composerWrapEl ? composerWrapEl.querySelector(".tip") : null;

function showWelcome(show) {
  const w = $("welcome");
  if (!w || !composerEl || !composerWrapEl) return;
  if (show) {
    composerWrapEl.style.display = "none";
    (convWrapEl || convEl).style.display = "none";   // 空态隐藏消息区, 欢迎区独占并居中
    w.style.display = "flex";
    $("composerSlot").appendChild(composerEl);
  } else {
    w.style.display = "none";
    (convWrapEl || convEl).style.display = "";
    composerWrapEl.style.display = "";
    if (tipEl) composerWrapEl.insertBefore(composerEl, tipEl);
    else composerWrapEl.appendChild(composerEl);
  }
  syncComposerSpace();
}
/* 输入区浮在底部: 把它的高度告诉 CSS, 让最后一条消息不会被挡住 */
/* ---------- 自绘滚动条: 轨道从页头下面到底, 滑块可拖/点轨道 ---------- */
const cScrollEl = $("cScroll"), cScrollThumb = $("cScrollThumb");
let sbDrag = null;

function syncScrollbar() {
  if (!cScrollEl || !cScrollThumb || !convWrapEl) return;
  const wrap = convWrapEl;
  const visible = getComputedStyle(wrap).display !== "none";
  const scrollable = visible && wrap.scrollHeight > wrap.clientHeight + 2;
  cScrollEl.classList.toggle("on", scrollable);
  if (!scrollable) return;
  const trackH = cScrollEl.clientHeight;
  const thumbH = Math.max(28, Math.round(trackH * (wrap.clientHeight / wrap.scrollHeight)));
  const maxTop = Math.max(1, trackH - thumbH);
  const maxScroll = Math.max(1, wrap.scrollHeight - wrap.clientHeight);
  const top = Math.round((wrap.scrollTop / maxScroll) * maxTop);
  cScrollThumb.style.height = thumbH + "px";
  cScrollThumb.style.top = Math.min(top, maxTop) + "px";
}
function scrollFromThumbTop(top) {
  if (!convWrapEl || !cScrollEl || !cScrollThumb) return;
  const trackH = cScrollEl.clientHeight, thumbH = cScrollThumb.offsetHeight;
  const maxTop = Math.max(1, trackH - thumbH);
  const maxScroll = Math.max(0, convWrapEl.scrollHeight - convWrapEl.clientHeight);
  const t = Math.min(Math.max(0, top), maxTop);
  convWrapEl.scrollTop = (t / maxTop) * maxScroll;
  syncScrollbar();
}
if (cScrollThumb) {
  cScrollThumb.addEventListener("mousedown", (e) => {
    e.preventDefault();
    sbDrag = { y: e.clientY, top: cScrollThumb.offsetTop };
    document.addEventListener("mousemove", onSbMove);
    document.addEventListener("mouseup", onSbUp);
  });
}
function onSbMove(e) {
  if (!sbDrag) return;
  scrollFromThumbTop(sbDrag.top + (e.clientY - sbDrag.y));
}
function onSbUp() {
  sbDrag = null;
  document.removeEventListener("mousemove", onSbMove);
  document.removeEventListener("mouseup", onSbUp);
}
if (cScrollEl) {
  cScrollEl.addEventListener("mousedown", (e) => {          // 点轨道: 跳到该位置(滑块居中对齐)
    if (e.target === cScrollThumb) return;
    const r = cScrollEl.getBoundingClientRect();
    scrollFromThumbTop(e.clientY - r.top - cScrollThumb.offsetHeight / 2);
  });
}

/* 把本页真实几何上报到服务端日志(排查"滚动条没到底"这类问题用) */
function reportLayout() {
  try {
    const wrap = convWrapEl || $("conv");
    const m = document.querySelector(".main");
    const head = document.querySelector(".main .top");
    const data = {
      build: BUILD,
      innerH: window.innerHeight,
      innerW: window.innerWidth,
      dpr: window.devicePixelRatio,
      wrapTop: Math.round(wrap.getBoundingClientRect().top),
      wrapBottom: Math.round(wrap.getBoundingClientRect().bottom),
      wrapClientH: wrap.clientHeight,
      wrapPos: getComputedStyle(wrap).position,
      mainBottom: Math.round(m.getBoundingClientRect().bottom),
      headH: head ? Math.round(head.getBoundingClientRect().height) : null,
      composerPos: composerWrapEl ? getComputedStyle(composerWrapEl).position : null,
      composerH: getComputedStyle(document.documentElement).getPropertyValue("--composer-h").trim(),
      padBottom: getComputedStyle($("conv")).paddingBottom,
      inputStyle: (() => {
        const el = $("input");
        if (!el) return null;
        const cs = getComputedStyle(el);
        return { whiteSpace: cs.whiteSpace, height: cs.height, minHeight: cs.minHeight,
                 overflowX: cs.overflowX, clientW: el.clientWidth, scrollW: el.scrollWidth,
                 clientH: el.clientHeight, scrollH: el.scrollHeight };
      })(),
      sbOn: cScrollEl ? cScrollEl.classList.contains("on") : null,
      sbTop: cScrollEl ? Math.round(cScrollEl.getBoundingClientRect().top) : null,
      sbBottom: cScrollEl ? Math.round(cScrollEl.getBoundingClientRect().bottom) : null,
      thumbTop: cScrollThumb ? Math.round(cScrollThumb.getBoundingClientRect().top) : null,
      thumbBottom: cScrollThumb ? Math.round(cScrollThumb.getBoundingClientRect().bottom) : null,
    };
    fetch("/api/client-log", { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify(data) }).catch(() => {});
  } catch (e) {}
}

function syncComposerSpace() {
  if (!composerWrapEl) return;
  const hidden = getComputedStyle(composerWrapEl).display === "none";
  const h = hidden ? 0 : composerWrapEl.getBoundingClientRect().height;
  document.documentElement.style.setProperty("--composer-h", Math.round(h) + "px");
  const head = document.querySelector(".main .top");        // 滚动区顶到页头下面
  if (head) {
    document.documentElement.style.setProperty(
      "--top-h", Math.round(head.getBoundingClientRect().height) + "px");
  }
  if (typeof scheduleConvNavLayout === "function") scheduleConvNavLayout();  // 右轨高度跟着变, 要重排
}
function setModeLabel() {
  $("welcomeModeLabel").textContent = currentMode === "world" ? "World 模式" : "Chat 模式";
  $("welcomeSub").textContent = currentMode === "world"
    ? "工程编码: 描述任务, 自动 规划 → 实现 → 落盘 → 摘要"
    : "输入消息, 与网页模型对话(逐字流式)";
  inputEl.placeholder = currentMode === "world"
    ? "描述工程任务, 例如: 实现带缓存的 fib(n)…"
    : "给 AI 发送消息…（Enter 发送 / Shift+Enter 换行）";
  $("modeChat").classList.toggle("active", currentMode === "chat");
  $("modeWorld").classList.toggle("active", currentMode === "world");
  // 侧栏工作区(workspace 文件树 + 发送选中)只在 World 模式下展示
  const wsBlock = $("wsBlock");
  if (wsBlock) wsBlock.hidden = currentMode !== "world";
  document.documentElement.classList.toggle("has-workspace", currentMode === "world");
  if (typeof syncScrollbar === "function") syncScrollbar();   // 会话列表/内容区高度可能变了
}
function switchMode(m) { currentMode = m; saveMode(m); setModeLabel(); }
$("modeChat").onclick = () => switchMode("chat");
$("modeWorld").onclick = () => switchMode("world");

async function runEngineerTask(task) {
  if (!task) { toast("请填写工程任务", "warn"); return; }
  if (state.state !== "logged_in") { toast("请先连接并登录", "warn"); return; }
  const include = Array.from(wsTreeEl.querySelectorAll('input[type="checkbox"]:checked'))
    .map(i => i.dataset.path);
  showWelcome(false);                      // 任务过程直接画在消息列表里
  taskCard = null;
  const card = ensureTaskCard(0);
  card.foot.textContent = "开始…" + (include.length ? "(限定文件: " + include.join(", ") + ")" : "");
  try {
    await post("/api/engineer/run", { task: task, include: include, summary: true });
  } catch (e) {
    if (taskCard && taskCard.foot) taskCard.foot.textContent = "✗ " + e.message;
    toast(e.message, "err");
  }
}

/* 出错要留在消息列表里(和 WS 的 error 事件同一个样子):
   只弹一个 6 秒就消失的 toast, 用户回头看就是"什么都没有" */
function addErrorRow(text) {
  if (current) { current.busy = false; current = null; }
  const row = document.createElement("div");
  row.className = "message";
  const box = document.createElement("div");
  box.className = "errbox";
  box.textContent = "⚠ " + text;
  row.appendChild(box);
  convEl.appendChild(row);
  scrollBottom();
  syncScrollbar();
  return row;
}
async function post(url, body) {
  const r = await fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : { method: "POST" });
  if (!r.ok) {
    // 错误信息尽量带上后端原话; 后端返回非 JSON(比如 405 的纯文本)时也别只说个状态码
    let msg = (r.status + " " + r.statusText).trim();
    try {
      const t = await r.text();
      try { const j = JSON.parse(t); msg = j.error || j.detail || msg; }
      catch (e) { if (t.trim()) msg = msg + " · " + t.trim().slice(0, 140); }
    } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}
$("btnStart").onclick = async () => {
  const target = $("selProvider").value;
  const switching = state.state === "logged_in" && target !== state.provider;
  try {
    await post("/api/start", { provider: target });
    toast(switching ? "正在切换并重启浏览器…" : "正在启动浏览器…", "info");
  } catch (e) { toast(e.message, "err"); }
};
/* 输入框那行: 真的在站点新建会话(ChatGPT/DeepSeek 页面也开新会话), 本地跟着换到新 id */
async function newChat() {
  const site = nameOf(state.provider || $("selProvider").value);
  try {
    const r = await post("/api/new_chat");
    if (!r.ok) {
      toast("在 " + site + " 新建对话失败, 请到浏览器窗口里手动点「新对话」", "warn");
      return;
    }
    showFreshStart();                             // 远端已是新会话 -> 本地也从空白开始
    loadConversations();
    toast("已在 " + site + " 新建会话, 本地也跟着换新了", "info");
  } catch (e) { toast(e.message, "err"); }
}
$("btnNew").onclick = newLocalSession;    // 侧栏: 只在本地开一段
$("btnNew2").onclick = newChat;           // 输入框那行: 站点新会话
function getInputText() {
  return String(inputEl.value || "").trim();
}
/* 输入框随内容长高(到 180px 后自己纵向滚动), 原生 textarea 粘贴/换行都由浏览器处理 */
function autoGrowInput() {
  if (!inputEl) return;
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(Math.max(inputEl.scrollHeight, 60), 180) + "px";
}
function readFileText(f) {
  return new Promise((resolve) => {
    const r = new FileReader();
    r.onload = () => {
      let t = String(r.result || "");
      if (t.length > MAX_TEXT_CHAR) t = t.slice(0, MAX_TEXT_CHAR) + "\n…(已截断)";
      resolve("【文件 " + f.name + " 内容】\n" + t);
    };
    r.onerror = () => resolve("");
    r.readAsText(f);
  });
}
let sendingLock = 0;                  // 正在发送(防连点/连按 Enter 发两条)
/* 输入框那行的"发给谁": web = 走桥接浏览器(网页模型); local = 只用设置里的模型(不进 ChatGPT)。
   样式与左下角"连接模型"一致: 一个按钮 + 向上弹出的菜单。全局偏好, 刷新后保持。 */
const REPLY_KEY = "wlb.reply.v1";
let replyChoice = "web";
let localReplyLabel = "只用本地模型";      // 设置里配的是 API 模式时变成"只用 API 模型"
let localReplyBadge = "本地";
function replyTarget() { return replyChoice; }
function savedReplyTarget() {
  try { return localStorage.getItem(REPLY_KEY) === "local" ? "local" : "web"; } catch (e) { return "web"; }
}
/* 这项到底用哪个模型回答, 取决于设置里的 planner: 文案跟着变, 免得说"本地模型"其实打的是云端 API */
async function syncReplyLabel() {
  try {
    const s = await (await fetch("/api/settings")).json();
    const p = (s && s.planner) || {};
    if (p.type === "api") { localReplyLabel = "只用 API 模型"; localReplyBadge = "API"; }
    else { localReplyLabel = "只用本地模型"; localReplyBadge = "本地"; }
  } catch (e) { /* 读不到就用默认文案 */ }
  if (replyChoice === "local") paintReplyButton();
  const p = $("replyPicker");
  if (p && p.classList.contains("open")) renderReplyPicker();
}
function paintReplyButton() {
  const b = $("btnReply");
  if (!b) return;
  const local = replyChoice === "local";
  b.classList.toggle("local", local);
  const av = $("rbAv"), nm = $("rbNm");
  if (av) av.textContent = local ? localReplyBadge : (shortOf(state.provider || $("selProvider").value) || "AI");
  if (nm) nm.textContent = local ? localReplyLabel : "网页模型";
  b.title = local
    ? localReplyLabel + ": 这条消息不进网页模型, 由设置里的模型直接回答(World 模式也会照常落盘+自测)"
    : "这条消息发给当前站点的网页模型(走桥接浏览器)";
}
function closeReplyPicker() {
  const p = $("replyPicker"), b = $("btnReply");
  if (p) p.classList.remove("open");
  if (b) { b.classList.remove("open"); b.setAttribute("aria-expanded", "false"); }
}
function replyItem(value, badge, label, active) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "picker-item" + (active ? " active" : "");
  item.dataset.v = value;
  item.setAttribute("role", "option");
  item.setAttribute("aria-selected", active ? "true" : "false");
  const av = document.createElement("span");
  av.className = "pi-av";
  av.textContent = badge;
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = label;
  item.appendChild(av); item.appendChild(nm);
  if (active) {
    const tk = document.createElement("span");
    tk.className = "tick";
    tk.textContent = "✓";
    item.appendChild(tk);
  }
  item.onclick = () => { closeReplyPicker(); applyReplyTarget(value, true); };
  return item;
}
function renderReplyPicker() {
  const p = $("replyPicker");
  if (!p) return;
  p.innerHTML = "";
  p.appendChild(replyItem("web", shortOf(state.provider || $("selProvider").value) || "AI",
                          "网页模型", replyChoice === "web"));
  p.appendChild(replyItem("local", localReplyBadge, localReplyLabel, replyChoice === "local"));
  p.appendChild(pickerSep());
  p.appendChild(pickerAction("settings", "模型设置…", GEAR_SVG, () => {
    closeReplyPicker();
    openSettings();
  }));
}
function toggleReplyPicker() {
  const p = $("replyPicker");
  if (!p || p.classList.contains("open")) { closeReplyPicker(); return; }
  renderReplyPicker();
  p.classList.add("open");
  const b = $("btnReply");
  if (b) { b.classList.add("open"); b.setAttribute("aria-expanded", "true"); }
}
function applyReplyTarget(v, announce) {
  replyChoice = (v === "local") ? "local" : "web";
  try { localStorage.setItem(REPLY_KEY, replyChoice); } catch (e) {}
  paintReplyButton();
  updateSendState();                 // 本地模型模式下不需要网页窗口, 发送按钮跟着放开
  if (announce) {
    toast(replyChoice === "local"
      ? localReplyLabel + ": 消息不会发给网页模型, 由设置里的模型直接回答(World 模式也会照常落盘+自测)"
      : "回到网页模型: 消息照旧发给当前站点的网页模型", "info");
  }
}
/* 本地模型这条链路失败的提示: 405/404 = 后端还没重启(接口不存在); 400 = 设置里没配好 */
function localFailHint(e) {
  const m = String((e && e.message) || e).trim();
  if (/\b40[45]\b/.test(m) || /Method Not Allowed|Not Found/i.test(m)) {
    return "后端还没有 /api/local_chat 这个接口(" + m + "): 桥接服务跑的还是旧代码。" +
      "把它重启一次就生效 —— 关掉跑 main.py 的那个窗口, 重新运行 run.bat(或 python main.py), 再刷新本页。";
  }
  return "本地模型没能回答: " + m;
}
/* 给本地模型的上下文: 最近几轮普通消息(卡片/超长内容不进) */
function localHistory() {
  return transcript
    .filter(m => (m.role === "user" || m.role === "assistant") && m.text)
    .slice(-20)
    .map(m => ({ role: m.role, text: String(m.text).slice(0, 8000) }));
}
async function send() {
  if (sendingLock) return;            // 上一次发送还没结束
  const userText = getInputText();
  if (!userText && staged.length === 0) return;
  const localOnly = replyTarget() === "local";
  sendingLock = Date.now();
  $("btnSend").disabled = true;       // 立刻置灰, 别等状态推送
  // 只用本地模型时根本不经过站点, 所以不要求桥接浏览器已登录
  if (!localOnly && state.state !== "logged_in") {
    sendingLock = 0;
    toast(state.state === "idle"
      ? "桥接浏览器未启动, 请先点左下角「启动并登录」"
      : "浏览器未就绪, 请先点左下角「启动并登录」", "warn");
    return;
  }
  syncHistoryView(true);        // 正在回看旧记录时, 发送前先回到远端当前会话
  // World 模式: 直接把输入作为工程任务交给执行器
  if (currentMode === "world") {
    // World 模式: 原始消息原样发给网页模型; 回答回来后由本地模型落盘 + 验证
    worldTask = userText;
    worldFixRounds = 0;
    worldBusy = true;                 // 发送按钮置灰, 直到本地执行链路结束
    refreshUi();
  }
  const textFiles = staged.filter(s => s.sendAsText && isTextFile(s.file));
  const asFiles = staged.filter(s => !s.wsPath && !(s.sendAsText && isTextFile(s.file)));
  if (localOnly && asFiles.length) {          // 本地模型没有站点那边的文件上传, 直接说清楚
    toast("只用本地模型时不能发图片/PDF 这类附件, 已忽略: " +
      asFiles.map(s => s.file.name).join(", ") + "(可以勾选工作区里的文本文件, 内容会一起发给本地模型)", "warn");
  }
  // 先解析"文本模式"文件内容 + 勾选的工作区文件
  const blocks = [];
  if (userText) blocks.push(userText);
  for (const s of textFiles) {
    const c = await readFileText(s.file);
    if (c) blocks.push(c);
  }
  const ws = await workspaceBlocks();
  blocks.push(...ws.blocks);
  const finalText = blocks.join("\n\n").slice(0, MAX_FINAL_TEXT);
  // 上传"文件模式"的文件
  const fileIds = [];
  if (asFiles.length) {
    try {
      for (const s of asFiles) {
        const fd = new FormData();
        fd.append("file", s.file, s.file.name);
        const r = await fetch("/api/chat_files", { method: "POST", body: fd });
        if (!r.ok) { let m = r.status; try { m = (await r.json()).error || m; } catch (e) {} toast("上传失败 " + m, "err"); sendingLock = 0; refreshUi(); return; }
        const j = await r.json();
        fileIds.push(j.id);
      }
    } catch (e) { toast("上传出错 " + e.message, "err"); sendingLock = 0; refreshUi(); return; }
  }
  // 用户气泡
  let label = userText;
  if (ws.paths.length) label += (label ? "\n" : "") + "📄 工作区文件: " + ws.paths.join(", ");
  if (textFiles.length) label += (label ? "\n" : "") + "📄 解析为文本: " + textFiles.map(s => s.file.name).join(", ");
  if (asFiles.length && !localOnly) label += (label ? "\n" : "") + "📎 文件: " + asFiles.map(s => s.file.name).join(", ");
  if (localOnly) label += (label ? "\n" : "") + "🧠 " + localReplyLabel;
  addUserMsg(label);
  histPush({ role: "user", text: label });
  inputEl.value = "";
  autoGrowInput();
  staged.forEach(s => { if (s.url) URL.revokeObjectURL(s.url); });
  staged = [];
  wsTreeEl.querySelectorAll('input[type="checkbox"]:checked')
    .forEach(cb => { cb.checked = false; });      // 发完把勾选也清掉
  updateSelCount();
  renderChips();
  try {
    // 只用本地模型: 直接交给后端调本地模型(不碰站点), 回答照旧走 message_start/delta/message_end 事件
    if (localOnly) await post("/api/local_chat", { text: finalText, history: localHistory() });
    else await post("/api/chat", { text: finalText, file_ids: fileIds });
  } catch (e) {
    const msg = localOnly ? localFailHint(e) : String((e && e.message) || e);
    addErrorRow(msg);                            // 留在消息列表里, 不是弹一下就没了
    toast(msg, "err");
    if (localOnly) {
      // 本地这条链路失败 = 一个字都没发出去: 把输入还原回去, 用户修好设置就能直接重发
      if (!inputEl.value.trim()) { inputEl.value = userText; autoGrowInput(); }
      sendingLock = 0;
      refreshUi();
    }
  } finally {
    // 留一点点冷却时间: 输入法回车 + 手快再按一次也不会发两条
    setTimeout(() => { sendingLock = 0; refreshUi(); }, 600);
  }
}
const MAX_TEXT_CHAR = 40000;
const MAX_FINAL_TEXT = 200000;
$("btnSend").onclick = send;
if ($("btnReply")) {
  $("btnReply").onclick = toggleReplyPicker;
  applyReplyTarget(savedReplyTarget(), false);     // 刷新/重开也保持上次选的"发给谁"
  syncReplyLabel();                                // 设置里是 API 模式的话, 文案改成"只用 API 模型"
  document.addEventListener("click", (e) => {
    const p = $("replyPicker"), b = $("btnReply");
    if (!p || !p.classList.contains("open")) return;
    if (p.contains(e.target) || (b && b.contains(e.target))) return;
    closeReplyPicker();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeReplyPicker(); });
}
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
inputEl.addEventListener("input", autoGrowInput);
inputEl.addEventListener("paste", () => setTimeout(autoGrowInput, 0));
autoGrowInput();

/* ================= 工作区 ================= */
const wsTreeEl = $("wsTree");

async function wsFetch(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { let m = r.status; try { m = (await r.json()).error || m; } catch (e) {} throw new Error(m); }
  return r.json();
}
function wsFileUrl(p) { return "/workspace/file?path=" + encodeURIComponent(p); }

/*
 * 将后端返回的扁平 path 列表转换为真正的树：
 * folder/
 *   file.txt
 *   child/
 *     app.py
 *
 * 即使后端只返回文件、不显式返回 dir，也会根据 path 自动补齐文件夹节点。
 */
function buildWorkspaceTree(items) {
  const root = { name: "", path: "", kind: "dir", children: new Map(), item: null };

  const ensureDir = (path) => {
    if (!path) return root;
    let node = root;
    let current = "";
    for (const part of path.split("/").filter(Boolean)) {
      current = current ? current + "/" + part : part;
      if (!node.children.has(part)) {
        node.children.set(part, { name: part, path: current, kind: "dir", children: new Map(), item: null });
      }
      node = node.children.get(part);
    }
    return node;
  };

  for (const raw of (items || [])) {
    if (!raw || !raw.path) continue;
    const path = String(raw.path).replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    if (!path) continue;
    const parts = path.split("/").filter(Boolean);
    const name = parts.pop();
    const parentPath = parts.join("/");
    const parent = ensureDir(parentPath);

    if (raw.kind === "dir") {
      const dir = ensureDir(path);
      dir.item = raw;
      dir.kind = "dir";
    } else {
      parent.children.set(name, { name, path, kind: "file", children: new Map(), item: raw });
    }
  }
  return root;
}

function sortedWorkspaceChildren(node) {
  return Array.from(node.children.values()).sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name, "zh-CN", { numeric: true, sensitivity: "base" });
  });
}

function createWorkspaceFile(node) {
  const item = node.item || {};
  const row = document.createElement("div");
  row.className = "ws-file";
  // 不设 title: 行里已经能看到文件名, 再弹一个气泡是重复的噪音(复选框的 aria-label 里仍带完整路径)

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.dataset.path = node.path;
  cb.setAttribute("aria-label", "选择 " + node.path);

  const ic = document.createElement("span");
  ic.className = "ic";
  ic.textContent = "📄";

  const fn = document.createElement("span");
  fn.className = "fn";
  fn.textContent = node.name;
  fn.onclick = () => openEditor(node.path);

  const sz = document.createElement("span");
  sz.className = "sz";
  sz.textContent = fmtSize(item.size || 0);

  row.append(cb, ic, fn, sz);
  return row;
}

function createWorkspaceFolder(node, depth = 0) {
  const wrap = document.createElement("div");
  wrap.className = "ws-node ws-folder";
  wrap.dataset.path = node.path;
  wrap.dataset.depth = depth;

  const row = document.createElement("div");
  row.className = "ws-folder-row";
  // 不设 title: 同上, 文件夹名行里就有
  row.setAttribute("role", "button");
  row.setAttribute("aria-expanded", "true");

  const toggle = document.createElement("span");
  toggle.className = "ws-folder-toggle";
  toggle.textContent = "▾";

  const icon = document.createElement("span");
  icon.className = "ws-folder-icon";
  icon.textContent = "📂";

  const name = document.createElement("span");
  name.className = "ws-folder-name";
  name.textContent = node.name;

  row.append(toggle, icon, name);

  const children = document.createElement("div");
  children.className = "ws-folder-children";

  for (const child of sortedWorkspaceChildren(node)) {
    children.appendChild(child.kind === "dir"
      ? createWorkspaceFolder(child, depth + 1)
      : createWorkspaceFile(child));
  }

  const setOpen = (open) => {
    children.hidden = !open;
    row.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "▾" : "▸";
    icon.textContent = open ? "📂" : "📁";
  };

  row.addEventListener("click", () => {
    setOpen(children.hidden);
  });

  setOpen(false);                 // 默认折叠
  wrap._setOpen = setOpen;        // 供"全部折叠/展开"用

  wrap.append(row, children);
  return wrap;
}

let wsRootName = "workspace";

/* ---------- 每个会话各自的工作区目录(记在本地, 刷新/重启都还在) ---------- */
const WSROOT_KEY = "wlb.wsroot.v1";
function wsRootMap() {
  try { return JSON.parse(localStorage.getItem(WSROOT_KEY) || "{}") || {}; } catch (e) { return {}; }
}
function wsRootMapKey() { return providerKey() + "|" + (state.conversation_id || NEW_CONV); }
function savedWsRoot() { return wsRootMap()[wsRootMapKey()] || ""; }
function saveWsRoot(path) {
  const m = wsRootMap();
  if (path) m[wsRootMapKey()] = path; else delete m[wsRootMapKey()];
  try { localStorage.setItem(WSROOT_KEY, JSON.stringify(m)); } catch (e) {}
}
async function wsPostRoot(path) {
  return wsFetch("/api/workspace/root", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: path || "" }),
  });
}
/* 切会话/刷新后: 把工作区切回这条会话记住的那个。
   关键: 这条会话**没有**单独设置过工作区时, 一根汗毛都不动 —— 以前这里会 POST 空路径
   (= 恢复内置默认), 于是"新会话 / 换会话 / 站点换了个会话 id"就会把用户选的项目目录冲掉
   (用户实际遇到过: 配置里的 H:\steward 被 silently 换成内置的 workspace/)。 */
async function applyConvWorkspace(announce) {
  const conv = wsRootMapKey();
  if (wsAppliedConv === conv && !announce) return;
  wsAppliedConv = conv;
  let info;
  try { info = await wsFetch("/api/workspace"); } catch (e) { return; }
  wsApplied = info.root;
  const want = savedWsRoot();
  if (!want) {                                   // 没设置过 -> 沿用当前目录, 不去动服务端
    loadWorkspace();
    if (announce) toast("这条会话没有单独设置工作区, 继续用当前目录 " + info.root, "info");
    return;
  }
  if (info.root === want) { loadWorkspace(); return; }
  try {
    const r = await wsPostRoot(want);
    wsApplied = r.root;
    loadWorkspace();
    if (announce) toast("已切到该会话的工作区: " + r.root, "info");
  } catch (e) { toast("切换工作区失败: " + e.message, "err"); }
}

/* ---------- 工作区目录(可切换成任意本地文件夹) ---------- */
async function openWsDialog() {
  const ov = $("wsOverlay");
  let info = null;
  try { info = await wsFetch("/api/workspace"); } catch (e) { toast("读取工作区失败: " + e.message, "err"); }
  $("wsRootInput").value = savedWsRoot() || (info && info.root) || "";
  const convName = (convItems.find(i => i.id === state.conversation_id) || {}).title
    || (state.conversation_id ? "会话 " + state.conversation_id.slice(0, 8) : "新会话");
  $("wsConvHint").textContent = "只对当前会话生效 — " + convName +
    "（每条会话可以各用各的工作区; 没有单独设置过的会话沿用当前目录, 不会被切走）";
  ov.classList.add("show");
  wsBrowse("");                       // 打开时先显示"这台电脑"(只列盘符)
  $("wsApply").onclick = async () => {
    const path = $("wsRootInput").value.trim();
    try {
      const r = await wsPostRoot(path);
      saveWsRoot(r.custom ? r.root : "");        // 记住: 只对当前会话生效
      wsApplied = r.root;
      ov.classList.remove("show");
      toast("该会话的工作区已切到 " + r.root, "info");
      loadWorkspace();
    } catch (e) { toast("切换失败: " + e.message, "err"); }
  };
  $("wsDefault").onclick = async () => {
    try {
      const r = await wsPostRoot("");
      saveWsRoot("");
      wsApplied = r.root;
      ov.classList.remove("show");
      toast("该会话已恢复默认工作区 " + r.root, "info");
      loadWorkspace();
    } catch (e) { toast("恢复失败: " + e.message, "err"); }
  };
  $("wsCancel").onclick = () => ov.classList.remove("show");
  $("wsUp").onclick = () => wsBrowse(wsBrowsePath.parent || "");   // 盘根再往上 = 这台电脑
}
let wsBrowsePath = {};

async function wsBrowse(path) {
  let r;
  try { r = await wsFetch("/api/workspace/browse?path=" + encodeURIComponent(path || "")); }
  catch (e) { toast("读取目录失败: " + e.message, "err"); return; }
  wsBrowsePath = r;
  $("wsBrowsePath").textContent = r.path || "这台电脑";
  wsBrowsePath.parentPath = r.parent;
  if (r.path) $("wsRootInput").value = r.path;      // "这台电脑"视图不改输入框
  const box = $("wsBrowseList");
  box.innerHTML = "";
  if (!r.dirs.length && !(r.drives || []).length) {
    const e = document.createElement("div");
    e.className = "ws-browse-empty";
    e.textContent = r.atDrives ? "没有找到可用的磁盘" : "这个目录下没有子文件夹";
    box.appendChild(e);
  }
  r.dirs.forEach(d => {
    const el = document.createElement("div");
    el.className = "ws-dir";
    el.textContent = "📁 " + d.name;
    el.title = d.path;
    el.onclick = () => wsBrowse(d.path);
    box.appendChild(el);
  });
  if (r.atDrives) {
    (r.drives || []).forEach(dr => {
      const el = document.createElement("div");
      el.className = "ws-dir";
      el.textContent = "💽 " + dr;
      el.onclick = () => wsBrowse(dr);
      box.appendChild(el);
    });
  }
}

async function loadWorkspace() {
  try {
    const r = await wsFetch("/workspace/tree");
    const wsTitle = $("wsTitle");
    wsRootName = r.name || "workspace";
    wsTitle.textContent = "📁 " + wsRootName;
    wsTitle.title = (r.root || "") + "  (点击切换工作区目录)";
    wsTreeEl.innerHTML = "";

    const root = buildWorkspaceTree(r.items || []);
    const children = sortedWorkspaceChildren(root);

    for (const node of children) {
      wsTreeEl.appendChild(node.kind === "dir"
        ? createWorkspaceFolder(node)
        : createWorkspaceFile(node));
    }

    updateSelCount();
    if (!children.length) {
      const e = document.createElement("div");
      e.className = "ws-empty";
      e.textContent = "工作区为空 — 点 AI 代码块的「保存」即可在此建文件";
      wsTreeEl.appendChild(e);
    }
  } catch (e) { toast("工作区加载失败: " + e.message, "err"); }
}

function askPath(title, def) {
  return new Promise((res) => {
    $("dlgTitle").textContent = title;
    $("dlgInput").value = def || "";
    const ov = $("dlgOverlay");
    ov.classList.add("show");
    $("dlgInput").focus();
    const ok = () => { ov.classList.remove("show"); res(($("dlgInput").value || "").trim()); };
    const cc = () => { ov.classList.remove("show"); res(null); };
    $("dlgOk").onclick = ok;
    $("dlgCancel").onclick = cc;
    $("dlgInput").onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); ok(); }
      else if (e.key === "Escape") cc();
    };
  });
}

async function writeWsFile(path, content) {
  await wsFetch("/workspace/file", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: path, content: content }),
  });
}

async function saveCodeBlock(pre, text) {
  const def = "ai/" + new Date().toISOString().slice(0, 10) + "/code_" +
              String(Date.now()).slice(-6) + ".txt";
  const path = await askPath("保存代码块到工作区(相对 " + wsRootName + "/)", def);
  if (!path) return;
  try {
    await writeWsFile(path, text);
    toast("已写入 " + path, "info");
    loadWorkspace();
  } catch (e) { toast("保存失败: " + e.message, "err"); }
}

async function openEditor(path) {
  try {
    const r = await wsFetch(wsFileUrl(path));
    if (r.binary) { toast("二进制文件不支持编辑: " + path, "warn"); return; }
    $("editPath").textContent = path;
    $("editText").value = r.text || "";
    $("editText").dataset.path = path;
    $("editOverlay").classList.add("show");
  } catch (e) { toast("读取失败: " + e.message, "err"); }
}
$("editClose").onclick = () => $("editOverlay").classList.remove("show");
$("editSave").onclick = async () => {
  const p = $("editText").dataset.path;
  try { await writeWsFile(p, $("editText").value); toast("已保存 " + p, "info"); loadWorkspace(); }
  catch (e) { toast("保存失败: " + e.message, "err"); }
};
$("editSend").onclick = async () => {
  const p = $("editText").dataset.path;
  const t = $("editText").value;
  if (state.state !== "logged_in") { toast("请先连接并登录", "warn"); return; }
  $("editOverlay").classList.remove("show");
  await sendBlocks(["【工作区文件 " + p + "】\n" + t.slice(0, 200000)], "📁 发送工作区文件: " + p);
};

async function sendBlocks(blocks, label) {
  if (!blocks.length) return;
  addUserMsg(label);
  histPush({ role: "user", text: label });
  try {
    await post("/api/chat", { text: blocks.join("\n\n").slice(0, 250000), file_ids: [] });
  } catch (e) { toast(e.message, "err"); }
}

/* 勾选的工作区文件内容: 发送时随消息一起带给模型 */
async function workspaceBlocks() {
  const paths = staged.filter(s => s.wsPath).map(s => s.wsPath);
  const blocks = [];
  for (const p of paths) {
    try {
      const r = await wsFetch(wsFileUrl(p));
      if (r.binary) { toast("跳过二进制文件: " + p, "warn"); continue; }
      let t = r.text || "";
      const MAX = 60000;
      if (t.length > MAX) t = t.slice(0, MAX) + "\n…(已截断 " + p + ")";
      blocks.push("【工作区文件 " + p + "】\n" + t);
    } catch (e) { toast("读取 " + p + " 失败: " + e.message, "warn"); }
  }
  return { paths: paths, blocks: blocks };
}
function setAllWorkspaceFolders(open) {
  wsTreeEl.querySelectorAll(".ws-folder").forEach(el => { if (el._setOpen) el._setOpen(open); });
}
$("wsRefresh").onclick = loadWorkspace;
$("wsCollapse").onclick = () => setAllWorkspaceFolders(false);
$("wsExpand").onclick = () => setAllWorkspaceFolders(true);
$("wsTitle").onclick = openWsDialog;
$("convList").addEventListener("scroll", () => {
  const el = $("convList");
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40) loadMoreConversations();
});

function updateSelCount() {
  const n = wsTreeEl.querySelectorAll('input[type="checkbox"]:checked').length;
  $("wsSelCount").textContent = n ? n + " 个" : "";
}
/* 勾选工作区文件 = 放进输入框(不直接发送); 取消勾选 = 从输入框移出 */
function syncStagedFromTree(path, on) {
  const idx = staged.findIndex(s => s.wsPath === path);
  if (on && idx < 0) staged.push({ wsPath: path });
  else if (!on && idx >= 0) staged.splice(idx, 1);
  renderChips();
}
wsTreeEl.addEventListener("change", (e) => {
  if (e.target.type !== "checkbox") return;
  syncStagedFromTree(e.target.dataset.path, e.target.checked);
  updateSelCount();
});
$("wsNew").onclick = async () => {
  const path = await askPath("新建文件(相对 " + wsRootName + "/)", "");
  if (!path) return;
  try { await writeWsFile(path, ""); toast("已新建 " + path, "info"); loadWorkspace(); }
  catch (e) { toast("新建失败: " + e.message, "err"); }
};
convEl.addEventListener("click", (e) => {
  function codeText(pre) {
    let t = Array.from(pre.childNodes)
      .filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join("");
    if (!t) t = pre.innerText.replace(/复制|保存/g, "").trim();
    return t.trim();
  }
  const c = e.target.closest(".copy");
  if (c) {
    const pre = c.closest("pre.code");
    if (pre) {
      const text = codeText(pre);
      if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
      const old = c.textContent; c.textContent = "✓ 已复制";
      setTimeout(() => { c.textContent = old; }, 1000);
    }
    return;
  }
  const sv = e.target.closest(".save");
  if (sv) {
    const pre = sv.closest("pre.code");
    if (pre) saveCodeBlock(pre, codeText(pre));
  }
});

/* ================= 设置(planner) ================= */
const setOverlay = $("setOverlay");
function syncSetType() {
  const t = $("setType").value;
  const api = t === "api" || t === "local";        // 两者都走 OpenAI 兼容接口
  document.querySelectorAll("#setOverlay .api-only").forEach(r => r.style.display = api ? "" : "none");
  document.querySelectorAll("#setOverlay .key-only").forEach(r => r.style.display = t === "api" ? "" : "none");
  if (t === "local" && !$("setBase").value.trim()) $("setBase").value = "http://127.0.0.1:8080/v1";
  if (t === "local" && !$("setModel").value.trim()) $("setModel").value = "local-model";
  $("setClearKey").style.display = t === "api" && window.__hasKey ? "" : "none";
}
async function openSettings() {
  try {
    const s = await (await fetch("/api/settings")).json();
    $("setType").value = s.planner.type;
    $("setBase").value = s.planner.api_base || "";
    $("setModel").value = s.planner.api_model || "";
    $("setKey").value = "";
    $("setTemp").value = s.planner.api_temp ?? 0.2;
    if ($("setRepo")) $("setRepo").value = (s.engine && s.engine.repo) || "";
    if ($("setTestCmd")) $("setTestCmd").value = (s.engine && s.engine.test_cmd) || "";
    if ($("setConfirm")) $("setConfirm").checked = !((s.engine && s.engine.confirm_apply) === "0");
    $("setKeyHint").textContent = s.planner.has_key ? "已保存(留空保持不变)" : "未设置";
    window.__hasKey = s.planner.has_key;
    $("setClearKey").style.display = s.planner.has_key ? "" : "none";
    $("setResult").className = "hint2";
    $("setResult").textContent = "当前生效: " + plannerSummary(s) +
      " · 全局设置(所有会话/窗口共用) · 文件: " + s.file;
    syncSetType();
    setOverlay.classList.add("show");
  } catch (e) { toast("读取设置失败: " + e.message, "err"); }
}

/* 一键找本机的本地模型服务(LM Studio / llama.cpp / vLLM / Ollama)并填好 */
$("setDetect").onclick = async () => {
  const box = $("setResult");
  box.className = "hint2";
  box.textContent = "正在检测本地模型服务…";
  try {
    const r = await (await fetch("/api/local-models")).json();
    const found = r.found || [];
    if (!found.length) {
      box.className = "hint2 bad";
      box.textContent = "没找到本地模型服务(试过 1234/8080/8000/11434/5000 这些常用端口)。" +
        "LM Studio 的话先开 Local Server(或命令行 lms server start)。";
      return;
    }
    const pick = found[0];
    $("setType").value = "local";
    syncSetType();
    $("setBase").value = pick.base;
    const model = pick.models.find(m => /coder|qwen|deepseek|llama|glm/i.test(m)) || pick.models[0];
    $("setModel").value = model;
    box.className = "hint2 ok";
    box.textContent = "✓ 找到 " + pick.base + " · " + pick.models.length + " 个模型, 已填「" + model +
      "」; 规划就会走本地模型(记得点「保存」)。";
  } catch (e) {
    box.className = "hint2 bad";
    box.textContent = "✗ 检测失败: " + e.message;
  }
};
$("setClose").onclick = () => setOverlay.classList.remove("show");
$("setType").onchange = syncSetType;
/* 设置面板里的一句话说明: 现在真正生效的是哪个模型(全局设置, 所有会话/窗口共用) */
function plannerSummary(s) {
  const p = (s && s.planner) || {};
  if (p.type === "local") return "本地模型 · " + (p.api_model || "(没填模型名)") + " @ " +
    (p.api_base || "(没填地址)");
  if (p.type === "api") return "API · " + (p.api_model || "(没填模型名)") + " @ " +
    (p.api_base || "(没填地址)") + (p.has_key ? " · 已填 Key" : " · 缺 Key");
  return "网页当前 Provider(只做规划/摘要, 不能落盘/自测)";
}
function apiPlannerBody(withKey) {
  const type = $("setType").value;
  const body = { type: type };
  // 本地模型和 API 都是 OpenAI 兼容接口: 地址/模型/温度**都要带上**。
  // (之前只在 type === "api" 时才带, 选「本地模型」等于只存了个 type,
  //  落盘时用的是默认的云端地址, 看起来就是"设置没生效/换了窗口就失效")
  if (type === "api" || type === "local") {
    body.api_base = $("setBase").value.trim();
    body.api_model = $("setModel").value.trim();
    const t = parseFloat($("setTemp").value);
    if (!isNaN(t)) body.api_temp = t;
    const key = $("setKey").value.trim();
    if (withKey && type === "api" && key) body.api_key = key;
  }
  return { planner: body };
}
$("setTest").onclick = async () => {
  const box = $("setResult");
  if ($("setType").value === "web") {
    box.className = "hint2";
    box.textContent = "网页模式无需测试";
    return;
  }
  box.className = "hint2";
  box.textContent = "测试中…";
  const typedKey = $("setKey").value.trim();
  try {
    // 直接把界面里刚填的 key 一起发去测试: 不用先保存
    const r = await post("/api/settings/test", apiPlannerBody(true));
    const models = (r.models || []);
    box.className = "hint2 ok";
    box.textContent = "✓ 连接成功 · 可用模型: " + models.slice(0, 8).join(", ") +
      (models.length > 8 ? " …" : "") +
      (r.key_source === "request" && typedKey ? "（用的是刚填的 Key，点「保存」才会记住）" : "");
    if (r.key_source === "request" && typedKey) {
      $("setKeyHint").textContent = "刚填的 Key 测试通过，尚未保存";
    }
  } catch (e) {
    box.className = "hint2 bad";
    box.textContent = "✗ " + e.message;
  }
};
$("setSave").onclick = async () => {
  try {
    const body = apiPlannerBody(true);
    body.engine = { repo: $("setRepo") ? $("setRepo").value.trim() : "",
                    test_cmd: $("setTestCmd") ? $("setTestCmd").value.trim() : "",
                    confirm_apply: ($("setConfirm") && $("setConfirm").checked) ? "1" : "0" };
    const saved = await post("/api/settings", body);
    toast("设置已保存(全局): " + plannerSummary(saved), "info");     // 存完立刻回显"现在用的是哪个模型"
    openSettings();
  } catch (e) { toast("保存失败: " + e.message, "err"); }
};
$("setClearKey").onclick = async () => {
  try {
    await post("/api/settings", { planner: { api_key: "__CLEAR__" } });
    toast("Key 已清除", "info");
    openSettings();
  } catch (e) { toast(e.message, "err"); }
};

(async () => {
  try {
    const pl = await (await fetch("/api/providers")).json();
    const sel = $("selProvider");
    pl.providers.forEach(p => { providersById[p.id] = p; });
    sel.innerHTML = pl.providers.map(p => `<option value="${p.id}">${p.name}</option>`).join("");
    const saved = savedProviderChoice();                                     // 刷新后保持上次选的站点
    sel.value = pl.providers.some(p => p.id === saved) ? saved : pl.default;
    sel.onchange = refreshUi;
  } catch (e) {}
  const pickBtn = $("btnProvider");
  if (pickBtn) pickBtn.onclick = toggleProviderPicker;
  try {
    const s = await (await fetch("/api/status")).json();
    state = Object.assign({}, state, s);
    const sel = $("selProvider");
    if (sel && !savedProviderChoice() && state.provider) sel.value = state.provider;   // 没记录时跟随已连接站点
    refreshUi();
  } catch (e) {}
  connect();
  loadWorkspace();
  syncWorldState();                      // 把最近一轮落盘/验证结果补画到卡片上
  migrateHistory();                      // v1/v2 老记录 -> v3
  applyConvWorkspace(false);             // 恢复这条会话记住的工作区
  syncHistoryView(true);                 // 恢复当前会话的记录(有记录就直接进消息区)
  showWelcome(transcript.length === 0 && !(histConv && histConv !== NEW_CONV));
  renderConversations();
  loadConversations();
  syncComposerSpace();
  reportLayout();
  console.log("[bridge] layout", JSON.stringify({
    build: BUILD,
    innerH: window.innerHeight,
    wrapBottom: Math.round((convWrapEl || convEl).getBoundingClientRect().bottom),
    mainBottom: Math.round(document.querySelector(".main").getBoundingClientRect().bottom),
    composerPos: getComputedStyle(composerWrapEl).position,
    composerH: getComputedStyle(document.documentElement).getPropertyValue("--composer-h").trim(),
    padBottom: getComputedStyle(convEl).paddingBottom,
  }));
  if (window.ResizeObserver && composerWrapEl) new ResizeObserver(syncComposerSpace).observe(composerWrapEl);
  if (convWrapEl) {
    convWrapEl.addEventListener("scroll", () => { syncScrollbar(); scheduleConvNavLayout(); hideConvNavTip(); });
    if (window.ResizeObserver) new ResizeObserver(() => { syncScrollbar(); scheduleConvNavLayout(); }).observe(convWrapEl);
  }
  if (window.ResizeObserver && convEl) new ResizeObserver(syncScrollbar).observe(convEl);
  syncScrollbar();
  window.addEventListener("resize", syncComposerSpace);
  const cBtn = $("convRefresh");
  if (cBtn) cBtn.onclick = () => { loadConversations(); toast("已刷新会话列表", "info"); };
  setModeLabel();
  // 侧栏收起/展开(按钮 + 视口变窄自动隐藏)
  let sidebarVisible = window.innerWidth > 900;
  function applySidebar() {
    const app = document.querySelector(".app");
    app.classList.toggle("sidebar-hidden", !sidebarVisible);
    app.classList.toggle("sidebar-open", sidebarVisible);
  }
  $("btnSidebar").onclick = () => { sidebarVisible = !sidebarVisible; applySidebar(); };
  window.addEventListener("resize", () => {
    const wide = window.innerWidth > 900;
    if (wide !== sidebarVisible) { sidebarVisible = wide; }   // 缩到窄视口自动隐藏, 拉宽自动恢复
    applySidebar();
  });
  applySidebar();
})();

/* ===== 全局悬停提示 =====
 * 任何带 title 的元素(HTML 里静态写的, 或 JS 后来设置的)都不再弹系统默认气泡,
 * 统一换成 #gtip 这个样式的深色气泡。做法: 指针进入/键盘聚焦时先把 title 摘下来
 * 存着(元素没有 title, 浏览器就不会画系统气泡), 离开时再原样放回去, 所以
 * 别处读 title 的代码不受影响。 */
(function () {
  const DELAY = 140;                 // 悬停多久才浮出来(太灵敏会晃眼)
  let tip = null, timer = null, host = null, saved = "", keeps = false;

  function box() {
    if (!tip || !tip.isConnected) {
      tip = document.createElement("div");
      tip.id = "gtip";
      tip.setAttribute("role", "tooltip");
      document.body.appendChild(tip);
    }
    return tip;
  }
  function hide() {
    if (timer) { clearTimeout(timer); timer = null; }
    // 期间别处可能已经写了新的 title(比如状态刷新), 那就别用旧的盖掉它
    if (host && host.isConnected && saved && !keeps && !host.hasAttribute("title")) host.title = saved;
    host = null; saved = ""; keeps = false;
    if (tip) tip.classList.remove("on");
  }
  function place() {
    const t = box(), r = host.getBoundingClientRect(), tr = t.getBoundingClientRect();
    const left = Math.max(8, Math.min(r.left + r.width / 2 - tr.width / 2, window.innerWidth - tr.width - 8));
    let top = r.bottom + 8;                                          // 默认浮在下方
    if (top + tr.height > window.innerHeight - 8) top = r.top - tr.height - 8;   // 下方放不下就翻到上方
    t.style.left = Math.round(left) + "px";
    t.style.top = Math.round(Math.max(8, top)) + "px";
  }
  function show() {
    if (!host || !host.isConnected) return hide();
    const t = box();
    t.textContent = saved;
    t.classList.add("on");
    place();                                                         // 内容变了要重新量宽高再定位
  }
  function arm(node, keepTitle) {
    const text = (node.getAttribute("title") || "").trim();
    if (!text || node === host) {
      // 键盘聚焦时先留着 title(可能是无障碍名字); 这之后又用鼠标悬停, 就得补摘一次
      if (node === host && keeps) { keeps = false; node.removeAttribute("title"); }
      return;
    }
    hide();
    host = node; saved = text; keeps = !!keepTitle;
    if (!keepTitle) node.removeAttribute("title");                   // 关键一步: 没 title 就没有系统气泡
    timer = setTimeout(() => { timer = null; show(); }, DELAY);
  }
  document.addEventListener("mouseover", (e) => {
    const node = e.target instanceof Element ? e.target.closest("[title]") : null;
    if (node) arm(node);
    else if (host && !(e.target instanceof Element && host.contains(e.target))) hide();
  }, true);
  document.addEventListener("mouseout", (e) => {
    if (host && (!e.relatedTarget || !host.contains(e.relatedTarget))) hide();
  }, true);
  document.addEventListener("focusin", (e) => {                      // 键盘 Tab 过来也照样提示
    const node = e.target instanceof Element ? e.target.closest("[title]") : null;
    if (node) arm(node, true); else hide();
  }, true);
  document.addEventListener("focusout", hide, true);
  document.addEventListener("mousedown", hide, true);
  window.addEventListener("scroll", hide, true);                     // 滚动时锚点会跑偏, 直接收起来
  window.addEventListener("resize", hide);
  window.addEventListener("blur", hide);
})();
