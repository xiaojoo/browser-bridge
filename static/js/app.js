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
let lastSiteConvMismatch = null;         // 上一次提示过的"站点停在另一条会话"
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
  // 存之前先去重: 以前每次刷新同步状态都会把同一批结果追加一遍, 存进历史的行越积越多。
  // 行数上限跟卡片一致(文件清单要能整份留在历史里, 以前只留 12 行 -> 刷新后 36 个文件只剩 12 个)。
  const rows = dedupeWorldRows(card.rows || []).slice(-WORLD_MAX_ROWS).map(r => ({
    mark: String(r.mark || "").slice(0, 4),
    text: String(r.text || "").slice(0, 160),
    cls: String(r.cls || "").slice(0, 12),
    detailsLabel: keepDetail ? String(r.detailsLabel || "").slice(0, 60) : "",
    detailsText: keepDetail ? String(r.detailsText || "").slice(0, 800) : "",
  }));
  return { title: String(card.title || "").slice(0, 80), count: String(card.count || "").slice(0, 40),
           foot: String(card.foot || "").slice(0, 240), busy: false, folded: !!card.folded,
           autoPreview: !!card.autoPreview, fileCount: card.fileCount || 0,
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
  // 这里**故意不碰工作区**: 会话变化可能是站点自己换的(限流后自动另开窗口、分配新 id…),
  // 只有用户主动点开某个会话时, 才由 openRemote/openGroup 去切那条会话自己的工作区。
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
    if (j.current !== undefined && j.current !== state.conversation_id) {
      // 站点窗口**自己**停在了另一条会话(你在窗口里点过别的会话 / 上次自动另开窗口)。
      // 以前这里静默改 state.conversation_id; 后来我改成"把画面也切过去" —— 那样更糟:
      // 刚发完消息时画面被切走, 看着就是"消息闪一下没了"。
      // 现在: **不切画面**, 只提示一句; 真正发消息之前会先把站点切回你正在看的那条
      // (见 send() 里的 alignSiteConversation())。
      const siteConv = j.current;
      if (freshFlag()) {
        // 刚连接: 站点窗口还停在上次那条 —— **不跟它走, 也不画它的记录**。
        // 这里以前会 state.conversation_id = siteConv + renderHistoryFor(...), 结果就是
        // 用户要的"每次连接都是干净一屏"被这条刷新打回原样(欢迎页又被上次的首条信息盖掉)。
        // 真发消息时站点会报回它自己的会话 id, 由 message_start 的 adoptConversation() 归位;
        // 旧记录也没丢, 侧栏点一下就能看回去(openGroup 会清掉这个标记)。
        lastSiteConvMismatch = siteConv;
      } else if (lastSiteConvMismatch !== siteConv) {
        lastSiteConvMismatch = siteConv;
        const it = convItems.find(i => i.id === siteConv) || {};
        toast("站点窗口现在停在另一条会话" + (it.title ? "「" + it.title + "」" : "") +
              "；你在这儿发消息时会先把它切回你正在看的这条", "warn");
        console.log("[bridge] 站点会话与界面不一致:", state.conversation_id, "<->", siteConv);
      }
    } else {
      lastSiteConvMismatch = null;
    }
  } catch (e) {
    if (!more) convItems = [];           // 读不到站点列表就只显示本地记录
  }
  renderConversations();
}
/* 把站点那段对话读回来, 和本地记录合并 —— 修"消息不同步"。
   bridge 的消息列表只有**从它自己发出去**的那些; 你在站点窗口里直接发的、或者站点自己
   产生的内容(继续生成/重新生成), 只有页面上有, 所以两边看起来对不上。
   合并规则: 站点那份是骨架, 本地对应的条目复用(保留卡片/光标锚点), 站点多出来的按顺序
   补进原位, 本地独有的(执行卡片等)留在原地。 */
async function syncFromSite(quiet) {
  let r;
  try { r = await (await fetch("/api/conversations/messages")).json(); }
  catch (e) { if (!quiet) toast("读站点对话失败: " + e.message, "err"); return { added: -1, fixed: 0 }; }
  if (!r.ok) { if (!quiet) toast(r.error || "读站点对话失败", "warn"); return { added: -1, fixed: 0 }; }
  const site = (r.messages || []).filter(m => String(m.text || "").trim());
  if (!site.length) {
    if (!quiet) toast("站点当前这条会话里没有消息 —— 先在侧栏点开要同步的那条会话再来", "warn");
    return { added: 0, fixed: 0, empty: true };
  }
  const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
  // 同一条消息的判定: 相等, 或者**一条是另一条的前缀**(本地采集被截断时正好是站点的前缀)。
  // 短文本不猜 —— 「确认」是「确认设计，开始编码」的前缀, 但那不是同一条。
  const PREFIX_MIN = 40;
  const isSame = (a, b) => {
    if (!a || !b || a.role !== b.role) return false;
    const x = norm(a.text), y = norm(b.text);
    if (!x || !y) return false;
    if (x === y) return true;
    const short = x.length <= y.length ? x : y;
    if (short.length < PREFIX_MIN) return false;
    return x.startsWith(short) && y.startsWith(short);
  };
  const out = [];
  // 1) 先给每条本地消息找站点里的位置(只向前找, **不消费**) —— 本地可能带着站点没有的历史
  //    (比如"换窗口之前"那一段), 旧算法一边找一边往后塞, 于是站点那几条全被塞到最前面, 末尾
  //    那条抓错的回答就留下来了。
  const matchOf = new Array(transcript.length).fill(-1);
  let cursor = 0;
  transcript.forEach((m, i) => {
    if (!m || m.role === "world") return;
    for (let j = cursor; j < site.length; j++) {
      if (isSame(m, site[j])) { matchOf[i] = j; cursor = j + 1; return; }
    }
  });
  // 2) 以站点为骨架合并: 本地独有的留在原位; 对上的用站点那份(谁更全用谁);
  //    紧跟在对上的消息之后、同角色却完全对不上、站点更长的本地回答 = "抓错了", 用站点的替换。
  let added = 0, fixed = 0, lastSite = -1, aligned = false;
  const emitSite = (j) => { out.push({ role: site[j].role, text: site[j].text }); added++; lastSite = j; };
  transcript.forEach((m, i) => {
    if (!m || m.role === "world") { out.push(m); return; }       // 执行卡片插在原地
    const j = matchOf[i];
    if (j >= 0) {
      for (let k = lastSite + 1; k < j; k++) emitSite(k);        // 站点上排在它前面、本地没有的
      const sm = site[j];
      if (norm(sm.text).length > norm(m.text).length) {          // 本地被截断 -> 用站点的补全
        out.push(Object.assign({}, m, { text: sm.text }));
        fixed++;
      } else {
        out.push(m);
      }
      lastSite = j;
      aligned = true;
      return;
    }
    const nxt = site[lastSite + 1];
    if (aligned && m.role === "assistant") {
      // (a) 站点下一条同角色、内容对不上、更长 -> 本地这条抓成上一条了, 换成站点那条
      if (nxt && nxt.role === "assistant" && !isSame(m, nxt)
          && norm(nxt.text).length > norm(m.text).length + 20) {
        emitSite(lastSite + 1);
        fixed++;
        return;
      }
      // (b) 站点已经配完(这条是**多出来的尾巴**)、而且它的内容在已经排好的列表里**重复**了
      //     (典型: 采集把上一轮的回答又当成本轮答案, 那条回答站点里也有) -> 丢掉这条残影。
      //     真正的"上一条回答"就在上面, 留着它只会让人以为"这条会话的最后一句是错的"。
      const prev = site[lastSite];
      const dup = out.some(o => o && o.role === m.role && isSame(o, m));
      if (!nxt && i === transcript.length - 1 && dup && prev && prev.role === "assistant"
          && !isSame(m, prev) && norm(prev.text).length > norm(m.text).length + 20) {
        console.log("[bridge] 同步: 丢掉重复的抓错尾巴", norm(m.text).slice(0, 60));
        fixed++;
        return;
      }
    }
    out.push(m);                                                // 本地独有(换窗口前那段等)
    aligned = false;
  });
  for (let k = lastSite + 1; k < site.length; k++) emitSite(k);  // 站点最后多出来的补到最后
  if (!added && !fixed) {
    if (!quiet) toast("已和站点一致(本地 " + (out.length) + " 条 / 站点 " + site.length + " 条)", "info");
    return { added: 0, fixed: 0 };
  }
  transcript = out;
  histFlush();
  renderHistoryFor(providerKey(), liveConvKey(), curSid || null);
  const bits = [];
  if (added) bits.push("补回 " + added + " 条");
  if (fixed) bits.push("补全 " + fixed + " 条被截断/抓错的");
  if (!quiet) toast("从站点同步: " + bits.join("、") + "(本地 " + out.length + " 条)", "info");
  return { added: added, fixed: fixed };
}

/* 右上角那个 ⟳: **刷新并同步**。消息对不上时点它 ——
   重连状态 -> 重拉会话列表(顺带发现"站点那边换了会话") -> 把站点那段对话读回来合并
   -> 补正最近一轮本地执行(落盘/验证)的卡片状态。 */
async function refreshAll() {
  const before = state.conversation_id;
  try {
    const s = await (await fetch("/api/status")).json();
    state = Object.assign({}, state, s);
    refreshUi();
  } catch (e) { /* 拿不到状态就跳过 */ }
  // 用户主动刷新 = 不再处于"刚连接的空一屏": 清掉这个标记, 下面 loadConversations 才会
  // 在"站点那边其实在另一条会话"时**真的把画面切过去**(否则它只静默改 id, 看着就是没同步)。
  setFreshFlag(false);
  try { await loadConversations(); } catch (e) { /* 列表拿不到不影响同步 */ }
  if (state.conversation_id !== before) {
    renderHistoryFor(providerKey(), liveConvKey(), null);      // 确保画面真的换到那条会话
  }
  const res = await syncFromSite(true);
  const added = (res && res.added) || 0, fixed = (res && res.fixed) || 0;
  try { await syncWorldState(); } catch (e) { /* 老后端没有这个接口就算了 */ }
  if (added > 0 || fixed > 0) {
    toast("已刷新并同步: 补回 " + added + " 条" + (fixed ? "、补全 " + fixed + " 条被截断/抓错的" : "") + "消息", "info");
  } else if (res && res.empty) {
    toast("已刷新会话列表; 站点当前那条会话里没有消息(先点开要同步的会话)", "warn");
  } else if (added === 0) {
    toast("已刷新: 本地记录与站点一致", "info");
  } else {
    toast("已刷新会话列表; 站点那段对话没读到(未登录 / 该站点还不支持)", "warn");
  }
  return res;
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
    await applyConvWorkspace(false, true);     // 用户主动点开这条会话 -> 才切它自己的工作区
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
    e.stopPropagation();          // 别再让外层再收一遍(否则同一个文件被加两次)
    composerBox.classList.remove("drop-on");
    addFiles(files);
    toast("已加入 " + files.length + " 个文件", "info");
  });
}
// 只挂一次(挂在 .composer 上): dragover/drop 会从输入框(textarea)冒泡上来 ——
// 以前这里还挂了 inputEl, 两边各加一次, 拖到输入框上就变成"两个同样的文件"。
fileDropTarget(composerBox);
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

/* 回答里的图片。capture 已经把远程图抓到本地并改写成 `media/<provider>/<conv>/x.png`
   (ChatGPT 的 images.openai.com 地址是签名的, 过几天就碎), 所以这里优先放行本地路径。
   属性值必须自己转义引号: esc() 只转 & < >, 不转引号, 把文本直接塞进 src="..." 会被一个
   引号跑出去变成事件属性。 */
function imgTag(alt, src) {
  const s = String(src || "").trim();
  if (!/^(https?:\/\/|media\/|\.\/media\/)/i.test(s) || /javascript:/i.test(s))
    return "[" + esc(String(alt || "图片")) + "]";
  const local = s.indexOf("media/") === 0 || s.indexOf("./media/") === 0;
  const url = (local ? "/transcripts/" + s.replace(/^\.\//, "") : s)
    .replace(/"/g, "%22").replace(/&/g, "&amp;");
  const a = esc(String(alt || "")).replace(/"/g, "&quot;");
  return '<img class="md-img" src="' + url + '" alt="' + a + '" loading="lazy">';
}

/* 点缩略图看大图: 在本页开一个弹框。原来是在原处加 .big 撑开, 那样整列正文会跟着跳,
   一张竖图能把后面的内容顶出屏幕 —— 看图这件事不该改动正文的布局。 */
const imgOverlay = $("imgOverlay"), imgView = $("imgView"), imgCap = $("imgCap"), imgSize = $("imgSize");
function openImageView(im) {
  imgView.src = im.currentSrc || im.src;
  imgView.alt = im.alt || "";
  imgCap.textContent = im.alt || "图片";
  imgSize.textContent = "";
  imgOverlay.classList.add("show");
}
function closeImageView() { imgOverlay.classList.remove("show"); }   // 不清 src: 清掉会再去请求一次页面地址
imgView.addEventListener("load", () => {
  if (imgOverlay.classList.contains("show"))
    imgSize.textContent = imgView.naturalWidth + " × " + imgView.naturalHeight;
});
$("imgClose").onclick = closeImageView;
imgOverlay.addEventListener("mousedown", (ev) => { if (ev.target === imgOverlay) closeImageView(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeImageView(); });
document.addEventListener("click", (e) => {
  const im = e.target.closest("img.md-img");
  if (im) { openImageView(im); e.preventDefault(); }   // 正文里和接力笔记里的图都走这一条
});

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
  let imgBuf = [];
  // capture 把每张图单独放一行, 连续的图片行并成一个容器 —— 否则每张各占一段,
  // 三张图就会竖着排三行(实测就是他截图里那个样子)。
  const flushImgs = () => {
    if (!imgBuf.length) return;
    out.push('<div class="md-imgs">' + imgBuf.join("") + "</div>");
    imgBuf = [];
  };
  const flushCode = () => {
    if (!codeBuf.length) return;
    out.push('<pre class="code"><span class="copy">复制</span><span class="save">保存</span>'
             + esc(codeBuf.join("\n")) + "</pre>");
    codeBuf = [];
  };
  for (const line of lines) {
    const m = line.match(/^```(\w*)/);
    if (m) { flushImgs(); flushCode(); inCode = !inCode; continue; }
    if (inCode) { codeBuf.push(line); continue; }
    const im = line.match(/^\s*!\[([^\]]*)\]\((\S+)\)\s*$/);
    if (im) { imgBuf.push(imgTag(im[1], im[2])); continue; }
    // 混在段落里的 ![..](..) 仍会被下面的链接规则拍平 —— 那是故意的: 不生成 <a href>,
    // 也就没有 javascript: 的口子
    if (/^\s*$/.test(line)) continue;      // 空行原来就是 push(""), 对 join("") 没影响;
                                           // 关键是别拿它冲掉图片行(图与图之间常夹空行)
    flushImgs();
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
    } else { out.push("<p>" + renderInline(esc(line)) + "</p>"); }
  }
  flushImgs();
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
  hoSyncEnable();                             // 接力预览跟着连接/生成状态置灰, 但按钮不消失
  const target = $("selProvider").value;
  const active = state.provider || target;
  const st = STATE_TEXT[state.state] || state.state;
  const dotCls = state.state === "logged_in" ? "on" : (state.state === "error" ? "err" :
                  (state.state === "launching" || state.state === "waiting_login") ? "warn" : "");
  ["chipDot", "chipDot2"].forEach(id => { const d = $(id); if (d) d.className = "dot " + dotCls; });
  $("chipState").textContent = st + (state.busy ? " · " + (state.busy_reason || "生成中") : "");
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
  // 启动/切换站点现在也占互斥, 所以先看 state 再看 busy —— 否则登录等待会被显示成"生成中…"
  if (state.state === "launching" || state.state === "waiting_login") { b.disabled = true; b.textContent = "启动/登录中…"; }
  else if (state.busy) { b.disabled = true; b.textContent = "生成中…"; }
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
/* 定位单独拆出来: 卡片宽度过渡要 120ms, 指到"刚露出来的文字"那一刻量到的是动画中间值
   (实测气泡停在 981, 而卡片左沿最终是 1146 —— 气泡压在卡片上)。所以过渡结束还要再贴一次。 */
function placeConvNavTip() {
  const tip = $("convNavTip"), box = $("convNav");
  if (!tip || tip.hidden || !box || !navTipItem) return;
  const rail = box.getBoundingClientRect();
  const row = navTipItem.getBoundingClientRect();
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = rail.left - tw - 12;
  if (left < 8) left = rail.right + 12;
  left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
  const top = Math.max(8, Math.min(row.top + row.height / 2 - th / 2, window.innerHeight - th - 8));
  tip.style.left = Math.round(left) + "px";
  tip.style.top = Math.round(top) + "px";
}
function showConvNavTip(item, text) {
  const tip = $("convNavTip"), box = $("convNav");
  if (!tip || !item || !box || !item.isConnected) return;
  navTipItem = item;
  navTipText = text;
  // 写进内层那一格: 外层带内边距, 文字直接放外层会被裁出半行(见 app.css 的 .cnav-tip 注释)
  const inner = tip.firstElementChild || tip;
  inner.textContent = String(text || "");
  tip.hidden = false;
  // 贴在卡片左边(不压住卡片); 左边放不下才翻到右边; 竖直方向跟着那一行, 并保证不出屏幕
  placeConvNavTip();
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
  const box = $("convNav");                           // 宽度过渡结束 -> 气泡重新贴一次
  // 只认框自己的 width: 里面那些横线(.cnav-d)的过渡会冒泡上来, 别跟着白算
  if (box) box.addEventListener("transitionend", (e) => {
    if (e.target === box && e.propertyName === "width") placeConvNavTip();
  });
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
    // 气泡只跟着**文字**走: 收起态整行就是那一格横线, 挂在行上等于"指横线也弹一大块",
    // 会把横线左边那些正文盖住。文字在收起态是 display:none, 所以挂它上面天然就不弹。
    label.onmouseenter = () => showConvNavTip(item, msg.text);
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
  if (spec.folded) el.classList.add("folded");     // 默认折叠(点标题展开) —— 36 个文件的清单太占地方
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
  if (spec.autoPreview && spec.fileCount) {      // 刷新后重画也要有"写入工作区"按钮
    addWriteAllButton({ el: el, spec: spec, foot: el.querySelector(".task-foot") }, spec.fileCount);
  }
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
function appliedRow(a) {
  // 两处画同一批结果(WS 事件 + 接口返回值), 文字必须一字不差 —— worldRowExists 是按
  // 整段文本去重的, 两边写法不一致就会各画一行。
  return String(a.op || "") + " " + String(a.path || "") +
    (a.size ? " (" + a.size + "B)" : "") + (a.backup ? " · 已备份" : "");
}

function worldRowExists(card, text) {
  if (!card || !card.spec) return false;
  const t = String(text || "").slice(0, 300);        // worldRow 存的是截断后的文字
  return (card.spec.rows || []).some(r => r.text === t);
}
let worldWid = 0;
const WORLD_MAX_ROWS = 400;      // 卡片最多记多少行(文件清单要能列全, 以前 24 行会把 36 个文件砍掉一半)
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
/* 「写入工作区(N 个)」一键按钮: 加在卡片的脚注上。
   预览卡(事件推来的那张)和它刷新后的重画都走这里 —— 否则刷新一次按钮就没了。 */
function addWriteAllButton(card, n) {
  if (!card || !card.el || !n) return;
  if (card.el.querySelector(".task-btns")) return;
  const btns = document.createElement("div");
  btns.className = "task-btns";
  const all = document.createElement("button");
  all.className = "pri";
  all.textContent = "写入工作区(" + n + " 个)";
  all.title = "一键把上面这些文件全部写进工作区(内容就是回答里的原文, 被拦下的会跳过)";
  all.onclick = async () => {
    all.disabled = true;
    setCardFoot(card, "正在写入工作区…", true);
    try {
      const r = await post("/api/world/apply_all", {});
      const ap = (r.applied || []).length, sk = (r.skipped || []).length;
      setCardFoot(card, "已写入 " + ap + " 个文件" + (sk ? ", 跳过 " + sk + " 个" : ""), false);
      all.remove();
    } catch (e) {
      setCardFoot(card, "写入失败: " + e.message, false);
      all.disabled = false;
    }
  };
  btns.appendChild(all);
  card.el.querySelector(".task-foot").appendChild(btns);
}

function worldCard(title, folded) {
  const spec = { title: title, count: "", foot: "", busy: true, rows: [], folded: !!folded,
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
  if (card.spec.rows.length < WORLD_MAX_ROWS) card.spec.rows.push(spec);   // 卡片别无限长(但文件清单要够长)
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

/* 事件推来的那张"只读预览卡"和 worldPipeline 马上要画的"勾选卡"是同一件事 ->
   留勾选卡, 把只读那张(连同历史记录)去掉, 免得同一批文件出现两遍。 */
function dropAutoPreviewCard() {
  const spec = lastWorldSpec("本地落盘");
  if (!spec || !spec.autoPreview) return;
  const el = spec.wid ? document.querySelector('#conv .task-card[data-wid="' + spec.wid + '"]') : null;
  if (el) el.remove();
  transcript = transcript.filter(m => !(m && m.role === "world" && m.card === spec));
  histFlush();
}

/* 这一条回答常常是"接着上一条"给的(ChatGPT 会从 `## 8. src/api/index.ts` 接着上一条的 1~7 写),
   只整理当前这一条就会少掉前面那批文件 —— 整条消息看起来就"文件不全"。
   这里把这条回答**之前**那几条带代码块的回答原文取回来(最多 limit 条), 交给服务端一起整理:
   顺序是"早的在前", 同路径后面的版本覆盖前面的(服务端 _dedupe_items 就是这么做的)。 */
function worldCarryTexts(answer, limit) {
  const want = String(answer || "");
  let idx = -1;
  for (let i = transcript.length - 1; i >= 0; i--) {
    const m = transcript[i];
    if (m && m.role === "assistant" && String(m.text || "") === want) { idx = i; break; }
  }
  if (idx < 0) idx = transcript.length;         // 还没进历史(刚收到) -> 从末尾往前找
  const out = [];
  for (let i = idx - 1; i >= 0 && out.length < (limit || 2); i--) {
    const m = transcript[i];
    if (!m || m.role !== "assistant") continue;
    if (!/```/.test(String(m.text || ""))) continue;    // 没代码块的回答不值得带
    out.unshift(String(m.text));
  }
  return out;
}

/* 识别出的文件改动: 列成卡片放在**消息下面**, 让用户自己决定写哪几个。
   内容全部来自 ChatGPT 的回答(程序只负责把代码块和文件名对上, 一个字不改);
   本地模型不参与这一步, 也不写代码。 */
function worldWriteCard(files, loose, opts) {
  const o = opts || {};
  const answerText = String(o.answer || "");
  const carried = [];                       // 来自前面几条回答的文件(服务端标 fromPrev)
  const rows = files.map((f) => {
    // 服务端按"这条回答里有没有这段内容"给 fromPrev; 万一没有(老数据)就退回到文本里找路径
    const fromPrev = (f.fromPrev !== undefined && f.fromPrev !== null)
      ? !!f.fromPrev
      : (!!answerText && answerText.indexOf(f.path) < 0);
    if (fromPrev) carried.push(f.path);
    const body = (f.op === "warn" || f.op === "invalid")
      ? ("⚠ " + (f.error || "这条被拦下了"))
      : (f.op === "delete" ? ("删除 " + (f.oldLines || 0) + " 行")
         : ("+" + (f.add || 0) + " / -" + (f.del || 0) + " 行 · " + (f.size || 0) + "B" +
            (f.unchanged ? " · 内容没变化" : "")));
    return {
      mark: f.op === "create" ? "＋" : (f.op === "delete" ? "−" : "✎"),
      text: (fromPrev ? "上一条 · " : "") + f.path + "  " + body,
      cls: (f.op === "warn" || f.op === "invalid") ? "err" : "done",
      detailsText: f.content || f.preview || "",
      detailsLabel: "查看文件内容",
    };
  });
  const spec = { title: "本地落盘 · 识别到的文件改动(写不写由你决定)",
                 count: files.length + " 个文件", foot: "", busy: false, rows: rows,
                 folded: files.length > 8,     // 文件多时默认折叠(点标题展开逐条勾选), 按钮一直在脚注上
                 wid: "w" + (++worldWid) + "-" + Date.now().toString(36) };
  const el = paintWorldCard(spec);
  const card = Object.assign(handleFor(spec, el), { entry: { role: "world", card: spec } });
  histPush(card.entry);
  worldCards.push(card);

  const list = el.querySelector(".task-list");
  const boxes = [];
  Array.from(list.querySelectorAll(".task-item")).forEach((row, i) => {
    const f = files[i];
    const box = document.createElement("input");
    box.type = "checkbox";
    // 被拦下的默认不勾; 内容和工作区里一模一样(unchanged)的也不用勾(勾了也是空写一次)
    box.checked = (f.op !== "warn" && f.op !== "invalid" && !f.unchanged);
    box.title = "勾上 = 写入这个文件";
    row.insertBefore(box, row.firstChild);
    boxes.push({ f, box });
  });
  if (loose) {
    const hint = document.createElement("div");
    hint.className = "hint2";
    hint.textContent = "还有 " + loose + " 段代码块没对上文件(命令/目录树/网址之类会落在这里, 不猜)"
      + (carried.length ? "(这里面也算上了前面那条回答)" : "") + " —— "
      + "需要的话让 ChatGPT 在代码块上写明文件名。";
    list.appendChild(hint);
  }

  const foot = el.querySelector(".task-foot");
  if (carried.length || files.length > 8) {
    setCardFoot(card, (carried.length
        ? ("本条回答 " + (files.length - carried.length) + " 个 + 前面回答 " + carried.length
           + " 个(标了「上一条」; 内容没变化的默认不勾), ")
        : ("共 " + files.length + " 个文件(点标题展开可逐条勾选), "))
      + "内容都是回答里的原文", false);
  }
  const vlab = document.createElement("label");
  vlab.className = "task-check";
  vlab.innerHTML = '<input type="checkbox" checked /> 写入后自测(本地模型只跑命令看输出, 不改代码)';
  const btns = document.createElement("div");
  btns.className = "task-btns";
  const ok = document.createElement("button");
  ok.className = "pri";
  ok.textContent = "写入选中的";
  const skip = document.createElement("button");
  skip.textContent = "全部跳过";
  btns.append(ok, skip);
  foot.append(vlab, btns);

  return new Promise((resolve) => {
    ok.onclick = () => {
      const picked = boxes.filter(b => b.box.checked).map(b => b.f);
      const verify = vlab.querySelector("input").checked;
      btns.remove(); vlab.remove();
      if (!picked.length) {
        setCardFoot(card, "你没有勾选任何文件, 本次不写入", false);
        resolve(null);
        return;
      }
      setCardFoot(card, "按你的勾选写入…", true);
      resolve({ files: picked, verify: verify, card: card });
    };
    skip.onclick = () => {
      boxes.forEach(b => { b.box.checked = false; });
      btns.remove(); vlab.remove();
      setCardFoot(card, "你跳过了, 本次不写入任何文件", false);
      resolve(null);
    };
  });
}

/* 落盘阶段的事件: 让"一直在转"的卡片能自己更新(刷新过的页面也一样) */
function handleApplyEvent(ev) {
  let card = cardFor("本地落盘");
  if (!card) {
    card = worldCard("本地落盘(从 ChatGPT 的回答里整理文件改动)", true);   // 默认折叠, 点标题展开全部
    if (!worldBusy) setCardFoot(card, "", false);             // 不是本页发起的: 不转圈
  }
  if (ev.action === "start") {
    setCardFoot(card, ev.text || "正在把 ChatGPT 的回答落实成文件改动…", worldBusy);
    return;
  }
  if (ev.action === "preview") {
    // 用户正在那张勾选卡片上决定写不写 -> 别重复画一遍文件行, 只更新脚注
    if (card.el && card.el.querySelector(".task-btns")) {
      setCardFoot(card, ev.text || "上面的文件改动请你决定写哪几个…", false);
      return;
    }
    const files = ev.files || [];
    if (!files.length) {
      setCardFoot(card, "这条回答里没有可写入的文件" +
        (ev.text ? ": " + ev.text : "") + " —— 没有动任何文件", false);
      return;
    }
    const prevCount = files.filter(f => f.fromPrev).length;
    files.forEach(f => {
      const risky = (f.op === "warn" || f.op === "invalid");
      const text = (risky ? "⚠ " + String(f.error || "这条被拦下了") + " — " : "") +
        (f.fromPrev ? "上一条 · " : "") + f.op + " " + f.path +
        (f.op === "delete" ? " (删除)" :
          " +" + (f.add || 0) + "/-" + (f.del || 0) + " 行 · " + (f.size || 0) + "B") +
        (f.unchanged ? " · 内容没变化" : "");
      if (!worldRowExists(card, text))
        worldRow(card, risky ? "!" : "•", text, risky ? "err" : "");
    });
    setCardCount(card, files.length + " 个文件");
    const riskCount = files.filter(f => f.op === "warn" || f.op === "invalid").length;
    setCardFoot(card, "从回答里整理出 " + files.length + " 个文件改动" +
      (prevCount ? "(其中 " + prevCount + " 个来自前面那条回答)" : "") +
      (riskCount ? " (其中 " + riskCount + " 项疑似改坏项目, 会被跳过)" : "") + ", 写不写由你决定", false);
    card.spec.autoPreview = true;            // 只读预览卡: 外层要画"勾选卡"时可以把它去掉, 别两张一样
    card.spec.fileCount = files.length;      // 让"写入工作区"按钮在刷新后重画时也出现
    addWriteAllButton(card, files.length);
    return;
  }
  if (ev.action === "skipped") {
    // 自动模式下这条通知是唯一的一份: 必须把"为什么什么都没写"写在卡片上, 不能静默过去
    setCardFoot(card, ev.text || ("这条回答里没有可写入的文件(" + (ev.reason || "没认出文件改动")
                                  + ") —— 没有动任何文件"), false);
    return;
  }
  if (ev.action === "applied") {
    const applied = ev.applied || [], skipped = ev.skipped || [];
    // 同一批结果会从"事件 + 接口返回值 + 刷新后的状态同步"进来好几遍, 已经画过的不再画
    applied.forEach(a => {
      const t = appliedRow(a);
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
  if (ev.stage === "verdict") { worldVerdict(ev); return; }   // 后台跑完的结论(以前是 HTTP 返回值)
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

/* World 模式下附在你那段话后面的一行格式要求。
   为什么要有: ChatGPT 会把整份代码装进 Canvas 卡片, 而卡片在消息 DOM 里只留一行标题 +
   一个运行预览 iframe —— 实测那条"完整代码(一个HTML即可)"的回答只采到 803 字、0 个代码围栏,
   源码一个字都没进来, 于是本地永远拿不到文件内容。要它直接用围栏写在消息里才采得到。 */
const WORLD_CODE_ASK =
  "【输出格式】请把每个文件的完整内容直接写在消息正文里, 用带文件路径的代码围栏" +
  "(例如 ```index.html 换行后放整份内容, 结尾 ```); 不要用 Canvas / 代码卡片 / 只给运行预览 —— " +
  "那种形式内容取不到, 我这边没法落盘。";

/* 认不出文件名时给个"重来"的口子: 整理规则可能刚修过, 或者让 ChatGPT 补上文件名后
   想重试这一条(不用把整段回答再发一遍)。自动模式和确认模式都要有, 所以抽出来。 */
function addRetryButton(c, task, answer) {
  const foot = c && c.el ? c.el.querySelector(".task-foot") : null;
  if (!foot || foot.querySelector(".task-btns")) return;      // 别叠两张一样的按钮
  const btns = document.createElement("div");
  btns.className = "task-btns";
  const again = document.createElement("button");
  again.textContent = "重新整理这条回答";
  again.onclick = () => { btns.remove(); worldPipeline(task, answer); };
  btns.appendChild(again);
  foot.appendChild(btns);
}

/* 输入框下面那行的落盘模式指示。读的是设置里同一个 confirm_apply, 不另存一份判断;
   只在 World 模式出现 —— 只有这个模式会把回答里的代码写进你工作区的文件。
   没有它的话, "会不会不问我就改文件" 这件事只在设置弹层里看得见, 外面一点征兆都没有。 */
let applyConfirm = true;
function paintApplyMode(confirm) {
  applyConfirm = !!confirm;
  const el = $("applyMode");
  if (!el) return;
  if (currentMode !== "world") { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = applyConfirm ? "· 落盘: 先给你勾选" : "· 落盘: 自动写入工作区";
  el.className = applyConfirm ? "" : "auto";
  el.title = "World 模式: 回答里整理出来的文件改动会不会不问你就写进工作区。\n" +
             "改这里: 设置 → 「本地模型落盘前弹确认框」";
}

/* 网页模型的回答回来后: 把回答里的文件改动**机械地**整理出来 -> 列成卡片 -> 用户决定写不写。
   本地模型不参与这一步(不写代码、也不判断该改什么); 只有"自测"那一步才会用到它(跑命令看输出)。 */
async function worldPipeline(task, answer) {
  let confirm = true, settings = {};
  try {
    settings = await (await fetch("/api/settings")).json();
    confirm = !((settings.engine && settings.engine.confirm_apply) === "0");
  } catch (e) {}
  paintApplyMode(confirm);
  let r = { applied: [], skipped: [] };
  let verify = false;
  let card = null;                                  // 确认模式下=那张勾选卡片, 结果就写在它上面
  // 这一条回答常常是"接着上一条"给的(ChatGPT 从 `## 8.` 接着上一条的 1~7 写):
  // 把前面几条带代码的回答原文一起交给服务端整理(它按"早的在前"合并, 同路径本条的版本覆盖前面的),
  // 卡片才列得全。extra_texts 里的文件由服务端标 fromPrev -> 行首标「上一条 ·」。
  // 取 3 条: 这条会话里 vite.config.ts / tsconfig*.json 的内容在**再往前**那条回答里(实测),
  // 只带 2 条就会"项目树里有、卡片里没有"。
  const carry = worldCarryTexts(answer, 3);
  try {
    if (confirm) {
      // 先只让服务端"整理"出文件清单(不写盘) -> 卡片列在消息下面 -> 用户勾选
      const pv = await post("/api/world/apply",
                            { task: task, text: answer, extra_texts: carry, dry_run: true });
      if (pv.no_changes) {
        // 复用服务端事件可能已经建好的那张卡, 别画第二张
        const c = cardFor("本地落盘") || worldCard("本地落盘(从 ChatGPT 的回答里整理文件改动)");
        setCardFoot(c, pv.text || "这条回答里没有可写入的文件, 已跳过", false);
        addRetryButton(c, task, answer);
        return;
      }
      dropAutoPreviewCard();                        // 同一次整理别画两张卡
      const pick = await worldWriteCard(pv.files || [], pv.loose || 0, { answer: answer });
      if (!pick) return;                            // 用户跳过了(卡片上已经写明)
      verify = pick.verify;
      card = pick.card;
      r = await post("/api/world/commit", {
        files: pick.files.map(f => ({ op: f.realOp || f.op, path: f.path,
                                      content: f.content, force: !!f.force })),
        message: "",
      });
    } else {
      // 自动模式: 先把卡片建出来, 服务端广播的 apply 事件才会落在同一张卡上
      // (否则 handleApplyEvent 自己建一张、这里再建一张, 就是"一张空白 + 一张结果")
      card = worldCard("本地落盘(从 ChatGPT 的回答里整理文件改动)");
      r = await post("/api/world/apply", { task: task, text: answer });
      if (r.no_changes) {
        // 原因就在返回值里, 直接写上 —— 等 WS 事件回传会漏(别的窗口/事件顺序/这一发被拦)
        setCardFoot(card, r.text || "这条回答里没有可写入的文件, 已跳过", false);
        addRetryButton(card, task, answer);
        return;
      }
      verify = true;
    }
  } catch (e) {
    const c = card || worldCard("本地落盘(从 ChatGPT 的回答里整理文件改动)");
    worldRow(c, "✗", "写入失败: " + e.message, "err");
    setCardFoot(c, "✗ 没能写入(工作区不可写? 或插件被拦下)", false);
    return;
  }

  const applied = r.applied || [], skipped = r.skipped || [];
  if (!applied.length && !skipped.length) return;
  if (!card) card = worldCard("本地落盘(从 ChatGPT 的回答里整理文件改动)");
  // 文件清单两边都画: 接口返回值 + 服务端广播的 applied 事件。
  // 只靠一条通道的话, 事件没到(重连/别的窗口/顺序)卡片就只剩一句"应用 N 项"而列不出改了哪几个文件。
  // worldRowExists 会去重, 所以两边都到也不会重复。
  applied.forEach(a => {
    const t = appliedRow(a);
    if (!worldRowExists(card, t)) worldRow(card, "✓", t, "done");
  });
  skipped.forEach(s => { if (!worldRowExists(card, s)) worldRow(card, "!", s, "err"); });
  setCardCount(card, applied.length + " 个文件");
  setCardFoot(card, "应用 " + applied.length + " 项" +
    (skipped.length ? ", 跳过 " + skipped.length + " 项" : ""), false);

  if (!applied.length) return;                      // 一个都没写进去 -> 没什么可自测的
  if (!verify) {                                    // 用户没勾"写入后自测"
    setCardFoot(card, card.spec.foot + " · 你选择跳过自测, 到此结束", false);
    return;
  }
  await worldVerify(task, answer, applied, settings, card, false);
}

/* 本地模型自己去验证这个项目: 挑构建/测试命令 -> 看输出 -> 自己改 -> 直到通过 */
const VERIFY_WHY = {
  blocked: "命令被安全策略拦住了",
  "parse-fail": "它没能给出可解析的验证决策",
  "no-checks": "它没先定出可判定的验收点(没有判定标准就不算通过)",
  "weak-evidence": "它给的证据对不上真跑过的命令/输出, 或只有编译证据",
  "no-evidence": "它没能跑出任何可当证据的命令(只读文件不算验证)",
  "planner-error": "本地/规划模型调用失败, 验证没做完",
  crash: "验证中途异常退出(不是项目报错)",
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

let worldVerifyTask = "";         // 结论现在是异步回来的, 回传 ChatGPT 那一步还要用 task

async function worldVerify(task, answer, applied, settings, card, force) {
  worldVerifyCard = null;
  worldVerifyTask = task || "";
  let v;
  try {
    v = await post("/api/world/verify", {
      task: task, answer: answer, applied: applied,
      command: (settings.engine && settings.engine.test_cmd) || "",
      max_rounds: 4, force: !!force,
    });
  } catch (e) {
    const c0 = worldVerifyCard || worldCard("本地验证(按需求验收点找证据)");
    worldRow(c0, "!", "验证没能执行: " + e.message, "err");
    setCardFoot(c0, "验证没能执行", false);
    worldBusy = false; refreshUi();     // 一次没跑起来的验证不该把发送永久锁住
    return;
  }
  if (v && v.started) {                 // 后端转到后台了: 卡片停在"验证中", 结论走 WS verdict
    const c0 = worldVerifyCard || worldCard("本地验证(按需求验收点找证据)");
    setCardFoot(c0, "本地模型正在验证(最多 4 轮命令, 每条最长 300 秒)…", true);
    return;
  }
  worldVerdict(v);
}

/* 把验证跑过的每一轮压成一段文字: 命令 + 退出码 + 输出尾巴。
   "轮数用完"、"命令在这台机器跑不了" 这类结论没有报错日志可发, 但**过程本身就是最有用的信息**
   —— 比如"cl / gcc / clang 全部 MISSING"就写在某一轮的输出里, 不发回去它下一轮照旧给 g++ 方案。 */
function verifyDigest(v) {
  const rs = (v && v.rounds) || [];
  if (!rs.length) return "(本地模型这次一条命令都没跑过, 没有过程可看)";
  const parts = ["本地验证跑过 " + rs.length + " 轮:"];
  for (const r of rs.slice(-6)) {
    const out = String(r.output || r.text || "").trim();
    parts.push("· 第" + (r.round || "?") + "轮 [" + (r.action || "run") + "] 退出码 "
               + (r.code === undefined ? "?" : r.code) + " —— "
               + String(r.command || "(没有命令)").slice(0, 220));
    if (out) parts.push("  输出尾: " + out.slice(-500).replace(/\n/g, " ⏎ "));
  }
  return parts.join("\n").slice(0, 6000);
}

/* 验证结论到手后的分支: 通过就收尾; 不通过(不管是"项目真报错"还是"它自己没搞定")
   都把结果 + 过程发回 ChatGPT, 最多两轮; 只看本地模型时不回传。
   原来是 `await post()` 之后的代码, 拆出来只是因为结论不再随请求返回。 */
async function worldVerdict(v) {
  const task = worldVerifyTask;
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
  const fixes = v.local_fixes || 0;
  const why = verifyWhy(v);
  // **只有代码层次的问题才发回 ChatGPT**(v.real_error)。环境/工具链/裁判自己的毛病不发:
  // 它改不了这台机器 —— 2026-09-26 那次把"这台机器没有 C++ 编译器"发过去毫无意义。
  // 这类问题由本地这一侧解决: 提示词里已经告诉它"换一条本机确实存在的命令",
  // 还不行就把缺什么写清楚交给人, 并且**照实说这不是项目报错**。
  if (!v.real_error) {
    worldRow(vcard, "!", "验证没做完: " + why + "(不是代码问题, 也不是项目报错)", "err",
             verifyDigest(v), "查看验证过程");
    setCardFoot(vcard, "环境/流程问题, 没发给 ChatGPT(它改不了这台机器): " + why, false);
    worldBusy = false; refreshUi();
    return;
  }
  if (!fixes && !v.tried_local_fix) {
    worldRow(vcard, "!", "本地模型一次都没动手改就想交出去 —— 报错留在这里", "err",
             (v.error_log || v.last_output || "").slice(-4000), "查看报错");
    setCardFoot(vcard, "本地模型没动手改, 先停在这里(报错见卡片)", false);
    worldBusy = false; refreshUi();
    return;
  }
  if (replyTarget() === "local") {      // 只用本地模型: 没有网页模型可回传, 报错就留在卡片里
    worldRow(vcard, "!", "本地模型自己改了 " + fixes + " 次都没修好", "err",
             (v.error_log || v.last_output || "").slice(-4000), "查看报错");
    setCardFoot(vcard, "只看本地模型, 不回传 ChatGPT: 报错留在卡片里, 你可以直接说下一步", false);
    worldBusy = false; refreshUi();
    return;
  }
  if (worldFixRounds >= 2) {
    worldRow(vcard, "!", "已经交给 ChatGPT 修过 2 轮, 先停在这里", "err");
    setCardFoot(vcard, "先停在这里(需要你自己看看了)", false);
    worldBusy = false; refreshUi();
    return;
  }
  worldFixRounds++;
  // 回给 ChatGPT 的消息: 原始需求 + 本地模型改了哪些文件 + 真正的报错日志
  const logText = (v.error_log || v.last_output || "").trim();
  const changedTxt = (v.changed || []).map(a => a.op + " " + a.path).join(", ");
  let fixMsg = "按你上面的方案, 本地已经改好了" + (changedTxt ? "(" + changedTxt + ")" : "") +
    ", 但项目验证没通过(代码层面的报错, 不是这台机器的环境问题)。请根据下面的报错给出修复方案" +
    "(需要改的文件请给完整内容)。\n";
  if (v.checks) fixMsg += "\n这次要满足的验收点:\n" + String(v.checks).slice(0, 1500) + "\n";
  if (v.last_command) fixMsg += "最后一次命令: " + v.last_command + "\n";
  if (logText) {
    fixMsg += "\n" + FENCE + "\n" + logText.slice(-6000) + "\n" + FENCE + "\n";
  } else {
    fixMsg += "\n(这次没有可用的报错日志, 下面是验证过程: )\n" + verifyDigest(v) + "\n";
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
  paintApplyMode(applyConfirm);          // 切模式时那行指示要跟着走(用缓存值, 不再发请求)
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
    await applyConvWorkspace(false, true);        // 用户主动新建会话 -> 才切它自己的工作区
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
/* 发消息之前先把"站点窗口停在的会话"对齐到"你正在看的这条"。
   两边不一致时(你在站点窗口里点过别的会话 / 上次自动另开窗口 / 我这边实测留下的对话),
   消息会发到**站点那条**去, 而记录记在你这条下 —— 随后刷新会话列表画面又被切走,
   看着就是"消息闪一下没了"。所以先切回来再发。 */
async function alignSiteConversation() {
  if (!state.conversation_id || state.conversation_id === "~new") return true;
  try {
    const s = await (await fetch("/api/status")).json();
    if (!s || s.state !== "logged_in") return true;
    if (!s.conversation_id || s.conversation_id === state.conversation_id) return true;
    const r = await post("/api/conversations/open",
                         { key: state.conversation_id, url: state.conversation_url || "" });
    if (r && r.current) {
      state.conversation_id = r.current;
      lastSiteConvMismatch = null;
      console.log("[bridge] 发消息前把站点切回:", r.current);
    } else {
      toast("站点窗口停在另一条会话, 没能切回来 —— 这条消息可能会发到那边去", "warn");
    }
  } catch (e) { /* 切不动就算了, 别挡住发送 */ }
  return true;
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
  if (!localOnly) await alignSiteConversation();   // 站点窗口别停在别的会话上(否则消息会发错地方)
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
  if (currentMode === "world" && userText && !localOnly) blocks.push(WORLD_CODE_ASK);
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
/* 换工作区目录**只有一条合法来源: 用户主动的动作** ——
   主动点侧栏某个会话 / 主动新建会话 / 在工作区对话框里选目录。
   连接站点、刷新页面、站点被限流后自动另开窗口、站点自己分配/切换会话 id…
   这些"自己发生"的事一律不碰工作区目录(连 POST 都不发)。
   以前这里是"会话一变就把工作区切回该会话记过的目录", 于是启动/连接时会把用户
   正在用的项目目录悄悄换成**另一个会话**记过的目录(用户实际遇到过)。 */
async function applyConvWorkspace(announce, userAction) {
  const conv = wsRootMapKey();
  wsAppliedConv = conv;
  if (!userAction) return;                  // 不是用户主动开的 -> 只记标记, 绝不动服务端
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
    paintApplyMode(body.engine.confirm_apply !== "0");   // 存完外面那行立刻跟着变, 不用等下一次回答
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

/* ================= 上下文接力预览 ================= */
/* 换窗口本身是自动的(站点拒绝输入时桥会自己开一个新窗口), 这一页只负责"带过去什么"看得见:
   代价写成一句话, 笔记是模型生成的所以走 renderMarkdown(它逐行 esc), 片段只列清单不内嵌全文。 */
const hoOverlay = $("hoOverlay");
let hoLast = null;

const hoFmt = n => (n || 0).toLocaleString("zh-CN");

function hoSyncEnable() {
  const b = $("btnHandoff");
  if (!b) return;
  const ok = state.state === "logged_in" && !state.busy;
  b.disabled = !ok;
  b.title = ok
    ? "换到新窗口时会把什么带过去: 先看一眼交接笔记(要调一次规划模型)"
    : (state.busy ? "正在生成中, 等这一轮结束再生成接力预览"
                  : "要先「启动并登录」才能读上一段对话");
  if (!ok && hoOverlay.classList.contains("show")) closeHandoff("现在不能生成: " + b.title);
  exSyncEnable();                             // 导出按钮跟同一套状态, 但它还多要一个会话 id
}

function hoCostSentence(d) {
  const conv = d.conv_chars || 0, carry = d.carry_chars || 0;
  const pct = conv ? Math.round(carry * 100 / conv) : 0;
  return "上一段对话 " + hoFmt(conv) + " 字 / " + d.records + " 条 → 换窗口时带过去 " +
    hoFmt(carry) + " 字(相当于搬过去 " + pct + "%): 交接笔记 " + hoFmt(d.note_chars) +
    " 字 + 原文片段 " + d.excerpts + " 条 " + hoFmt(d.excerpt_chars) + " 字。" +
    "整体硬上限 " + hoFmt(d.carry_max || 20000) + " 字, 超了会截断并在开头写明截断了。";
}

function hoRow(cls, a, b, tip) {
  const row = document.createElement("div");
  row.className = "ho-kv" + (cls ? " " + cls : "");
  const k = document.createElement("span");
  k.className = "k";
  k.textContent = a;
  const v = document.createElement("b");
  v.textContent = b;
  if (tip) v.title = tip;
  row.append(k, v);
  return row;
}

function closeHandoff(msg) {
  hoOverlay.classList.remove("show");
  if (msg) toast(msg, "err");
}

function openHandoff() {
  hoSyncEnable();
  const t = $("hoTask");
  const typing = $("input").value.trim();
  if (typing && !t.value.trim()) t.value = typing.slice(0, 400);   // 你正在打的这段话就是要办的事
  hoOverlay.classList.add("show");
  if (!hoLast) $("hoRun").focus();
}

async function runHandoff() {
  const btn = $("hoRun"), note = $("hoNote"), stat = $("hoStat"), pth = $("hoPath");
  btn.disabled = true;
  note.hidden = stat.hidden = pth.hidden = false;
  note.className = "ho-note wait";
  note.textContent = "正在整理上一段对话… 先把整段滚到顶读回来, 再调一次规划模型 —— "
                   + "这两段都要时间, 条数越多越久。";
  stat.textContent = "";
  try {
    const d = await post("/api/handoff/preview", { task: $("hoTask").value.trim() });
    if (!d.ok) {
      hoLast = null;
      $("hoCopy").disabled = true;
      stat.hidden = pth.hidden = true;
      note.className = "ho-note bad";
      note.textContent = "没生成出来: " + (d.error || "未知原因");
      return;
    }
    hoLast = d;
    $("hoCost").textContent = hoCostSentence(d);
    stat.className = "ho-stat";
    stat.replaceChildren(
      hoRow("", "带过去", hoFmt(d.carry_chars) + " 字"),
      hoRow("", "其中笔记", hoFmt(d.note_chars) + " 字"),
      hoRow("", "原文片段", d.excerpts + " 条 " + hoFmt(d.excerpt_chars) + " 字"),
      hoRow(d.rel ? "ok wide" : "bad wide", "逐条记录", d.rel || "这段对话还没落盘",
            (d.path || "") + "  (相对仓库根目录)"),
    );
    const list = document.createElement("div");
    list.className = "ho-ex";
    (d.excerpt_list || []).forEach(e => list.append(
      hoRow("", (e.role === "user" ? "用户" : "助手") + " · " + hoFmt(e.chars) + " 字",
            e.head || "(空)")));
    note.className = "ho-note";
    note.replaceChildren();
    const md = document.createElement("div");
    md.className = "assistant-body";          // 笔记走助手气泡那套排版, 不再造一套标题/列表样式
    md.innerHTML = renderMarkdown(d.note || "");
    note.append(md);
    if ((d.excerpt_list || []).length) note.append(list);
    pth.hidden = false;
    pth.textContent = "这份笔记只带原文片段里那些; 换窗口之后如果对方问的细节不在里面, " +
      "完整逐条记录在本机 " + (d.rel || "(未落盘)") + ", 向你要而不是让它猜。";
    $("hoCopy").disabled = false;
  } catch (e) {
    hoLast = null;
    $("hoCopy").disabled = true;
    stat.hidden = pth.hidden = true;
    note.className = "ho-note bad";
    note.textContent = "请求失败: " + e.message;
  } finally {
    btn.disabled = false;
    hoSyncEnable();
    btn.disabled = $("btnHandoff").disabled;     // 未登录/生成中时按钮保持置灰
  }
}

$("btnHandoff").onclick = openHandoff;
$("hoClose").onclick = () => closeHandoff("");
$("hoRun").onclick = runHandoff;
$("hoCopy").onclick = async () => {
  if (!hoLast) return;
  try {
    await navigator.clipboard.writeText(hoLast.context || "");
    toast("整份接力文本已复制(" + hoFmt((hoLast.context || "").length) + " 字)", "ok");
  } catch (e) { toast("复制失败: " + e.message, "err"); }
};
$("hoTask").addEventListener("keydown", ev => {
  if (ev.key === "Enter") { ev.preventDefault(); if (!$("hoRun").disabled) runHandoff(); }
});


/* ================= 导出对话记录 ================= */
/* 导出走的是"当前这段对话的站点 id", 所以没会话 id 时按钮就是灰的(而不是点了报错)。 */
const exOverlay = $("exOverlay");
let exLast = null;

function exSyncEnable() {
  const b = $("convExport");
  if (!b) return;
  const ok = state.state === "logged_in" && !state.busy && !!state.conversation_id;
  b.disabled = !ok;
  b.title = ok
    ? "把这段对话导出成一份 Markdown 文件（连着它前后接力出来的窗口一起）"
    : (state.busy ? "正在生成中, 等这一轮结束再导出"
       : (state.state !== "logged_in" ? "要先「启动并登录」才能导出"
                                      : "这段对话还没有站点会话 id: 先打开一段有内容的会话"));
}

async function copyText(s, what) {
  try {
    await navigator.clipboard.writeText(s || "");
    toast(what + "已复制", "ok");
  } catch (e) { toast("复制失败: " + e.message, "err"); }
}

async function runExport() {
  const b = $("convExport");
  if (b.disabled) return;
  b.disabled = true;
  try {
    const d = await post("/api/transcript/export", {});
    if (!d.ok) { toast("导出失败: " + (d.error || "未知原因"), "err"); return; }
    exLast = d;
    $("exStat").textContent = "写到 " + d.rel + " —— 共 " + hoFmt(d.chars) + " 字 / " +
      hoFmt(d.messages) + " 条消息 / " + d.windows + " 个窗口。";
    $("exChain").replaceChildren(
      hoRow("wide", "接力链", d.chain.map(c => c.slice(0, 8)).join(" → ") || d.conv.slice(0, 8),
            d.chain.join("  →  ")));
    const md = d.markdown || "";
    $("exPre").textContent = md.length > 6000
      ? md.slice(0, 6000) + "\n\n……(预览到 6,000 字, 全文在文件里)" : md;
    exOverlay.classList.add("show");
  } catch (e) {
    toast("导出失败: " + e.message, "err");
  } finally {
    hoSyncEnable();
  }
}

$("convExport").onclick = runExport;
$("exClose").onclick = () => exOverlay.classList.remove("show");
$("exCopyPath").onclick = () => exLast && copyText(exLast.path, "文件路径");
$("exCopyAll").onclick = () => exLast && copyText(exLast.markdown, "全文 Markdown");
[hoOverlay, exOverlay].forEach(ov => ov.addEventListener("mousedown", ev => {
  if (ev.target === ov) ov.classList.remove("show");     // 外点只关遮罩这一层, 不吞掉里面的点击
}));


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
  // 这次连接还是"干净一屏"(刚连上就刷新): 重新立一段本地新会话, 别沿用站点那条的旧分段 ——
  // 只把画面清空是不够的: 光标还指在旧分段上, 这时发消息会把旧记录整段覆盖掉。
  if (freshFlag()) showFreshStart();
  // 刷新页面不换工作区目录: 保持服务端当前那个(要换只能由用户主动点会话/选目录)
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
  const sBtn = $("convSync");
  if (sBtn) sBtn.onclick = () => syncFromSite(false);
  const rBtn = $("topRefresh");
  if (rBtn) rBtn.onclick = () => refreshAll();
  setModeLabel();
  // 落盘模式指示要在第一次切到 World 模式之前就拿到真值(否则只会显示默认值)
  try {
    const st0 = await (await fetch("/api/settings")).json();
    paintApplyMode(!((st0.engine && st0.engine.confirm_apply) === "0"));
  } catch (e) { paintApplyMode(true); }
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
