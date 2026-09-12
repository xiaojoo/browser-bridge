"""浏览器管理(Provider 化): 持久化 Profile 启动、登录等待、发消息。

每个 Provider 使用独立 Profile(profile/<id>/), 切换 Provider 时自动关闭并重开浏览器。
"""
import asyncio
import logging

from playwright.async_api import async_playwright

from . import capture, config, history, providers
from .events import info_event
from .providers import Provider

log = logging.getLogger("bridge")

# 与真实浏览器一致的基本指纹, 减少被边缘安全策略拦截的概率
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# 站点"已不能输入"的探测: 输入框被禁用, 或者页面上出现额度/次数上限的提示
_BLOCK_JS = r"""(cfg) => {
  const txt = (document.body ? document.body.innerText : "") || "";
  const pats = [
    /聊天已暂停/, /使用额度/, /次数上限/, /已达.{0,4}上限/, /额度将在/, /稍后重试/, /暂时无法/,
    /You've reached[\s\S]{0,40}limit/i, /limit reached/i, /reached the .{0,20}limit/i,
    /too many requests/i, /usage limit/i, /upgrade to continue/i, /try again later/i,
    /please start a new/i
  ];
  const hit = pats.find(p => p.test(txt));
  for (const sel of cfg.sels || []) {
    const els = document.querySelectorAll(sel);
    for (const el of els) {
      const dis = el.disabled === true || el.getAttribute("aria-disabled") === "true" ||
                  el.getAttribute("contenteditable") === "false";
      if (dis && el.getBoundingClientRect().height > 0) {
        const line = (el.closest("form") || el.parentElement || el).innerText || "";
        return ("输入框已被禁用" + (line ? ": " + line.slice(0, 160) : "")).trim();
      }
    }
  }
  if (hit) {
    const m = txt.match(new RegExp(hit.source));
    const at = m ? Math.max(0, txt.indexOf(m[0]) - 40) : 0;
    return txt.slice(at, at + 200).replace(/\s+/g, " ").trim();
  }
  return "";
}"""

# 登录探测: 由调用方传入选择器数组; 取第一个"可见且未禁用"的元素(避开隐藏兜底输入框)
_LOGIN_PROBE_JS = """(sels) => {
  let chosen = null;
  for (const s of sels) {
    for (const el of document.querySelectorAll(s)) {
      const r = el.getBoundingClientRect();
      const disabled = el.disabled === true
        || (el.getAttribute && (el.getAttribute('aria-disabled') === 'true'
            || el.getAttribute('contenteditable') === 'false'));
      if (r.width > 0 && r.height > 0 && !disabled) { chosen = el; break; }
    }
    if (chosen) break;
  }
  let loginVisible = false;
  for (const b of Array.from(document.querySelectorAll('button, a'))) {
    const s = String(b.textContent || '').trim();
    if (/^(log in|sign in|登录|立即登录|sign up)$/i.test(s) && b.getBoundingClientRect().width > 0) {
      loginVisible = true; break;
    }
  }
  return {
    onSite: location.hostname !== 'about:blank',
    path: location.pathname,
    hasComposer: !!chosen && !loginVisible,
    loginVisible: loginVisible,
  };
}"""

# 新建对话: 由调用方传入候选按钮文案
_NEW_CHAT_JS = """(names) => {
  const cands = Array.from(document.querySelectorAll('button, [role="button"], [aria-label], [title]'));
  for (const b of cands) {
    const s = String(b.getAttribute && (b.getAttribute('aria-label') || b.getAttribute('title') || ''))
      + ' ' + String(b.textContent || '').slice(0, 30);
    for (const n of names) {
      if (s.indexOf(n) >= 0 && (b.getBoundingClientRect().width > 0)) {
        b.click();
        return true;
      }
    }
  }
  return false;
}"""

_COOKIE_JS = """() => {
  const btns = Array.from(document.querySelectorAll('button'));
  for (const b of btns) {
    const t = (b.textContent || '').trim();
    if (t === '接受全部' || /^accept all$/i.test(t)) { b.click(); return true; }
  }
  return false;
}"""

# 回读输入框里的内容(用于发送前校验, 防止"键盘敲到一半被打断"导致只发出去半截)
_READ_JS = """(sels) => {
  for (const s of sels) {
    const list = document.querySelectorAll(s);
    for (const c of list) {
      const r = c.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) continue;
      const t = c.isContentEditable ? (c.innerText || "") : (c.value || "");
      return String(t);
    }
  }
  return null;
}"""

# 快速填充输入框(适用于大段文本/代码): 取第一个"可见且可编辑"的候选, 设置内容并派发 input 事件
_FILL_JS = """({sels, text}) => {
  let el = null;
  for (const s of sels) {
    const list = document.querySelectorAll(s);
    for (const c of list) {
      const r = c.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) continue;
      const disabled = c.disabled === true
        || (c.getAttribute && (c.getAttribute('aria-disabled') === 'true'
            || c.getAttribute('contenteditable') === 'false'));
      if (disabled) continue;
      el = c; break;
    }
    if (el) break;
  }
  if (!el) return false;
  if (el.isContentEditable) { el.focus(); el.innerText = text; }
  else if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') { el.focus(); el.value = text; }
  else return false;
  el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
  return true;
}"""

_FILL_THRESHOLD = 120   # 超过则用注入方式(一次写进去, 不会被页面重绘打断), 否则键盘逐字


class BrowserManager:
    """单例。状态机: idle -> launching -> waiting_login -> logged_in / error。"""

    def __init__(self):
        self._pw = None
        self._ctx = None
        self.page = None
        self.provider: Provider = providers.get(None)
        self.state = "idle"          # idle|launching|waiting_login|logged_in|error
        self.last_error = None
        self.busy = False
        self.on_change = None        # async 回调, 状态变化时触发(用于广播)

    # ---------- 状态 ----------
    def status(self) -> dict:
        return {
            "provider": self.provider.id,
            "state": self.state,
            "error": self.last_error,
            "busy": self.busy,
            "started": self._ctx is not None,
            "conversation_id": self.conversation_id(),
            "conversation_url": self.conversation_url(),
        }

    def _profile_dir(self) -> str:
        d = config.PROFILE_DIR / self.provider.id
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    async def _notify(self, **extra):
        cb = self.on_change
        if cb:
            try:
                await cb(self.status(), **extra)
            except Exception:
                pass

    # ---------- 生命周期 ----------
    async def ensure_started(self, provider_id: str | None = None) -> bool:
        """启动(或切换到)指定 Provider 的浏览器。返回是否已登录。"""
        p = providers.get(provider_id)
        if self._ctx is not None:
            if self.provider.id == p.id:
                return self.state == "logged_in"
            if self.busy:
                self.last_error = "正在生成中, 请稍后再切换 Provider"
                await self._notify()
                return False
            await self._teardown()
        self.provider = p
        return await self._launch()

    async def _launch(self) -> bool:
        self.state = "launching"
        self.last_error = None
        await self._notify()
        try:
            self._pw = await async_playwright().start()
            self._ctx = await self._pw.chromium.launch_persistent_context(
                user_data_dir=self._profile_dir(),
                headless=False,
                viewport=config.VIEWPORT,
                user_agent=_UA,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
                args=["--disable-blink-features=AutomationControlled", "--lang=zh-CN"],
            )
            # 在任何页面加载前注入 fetch 包装器(所有 Provider 共用)
            await self._ctx.add_init_script(capture.FETCH_WRAPPER_JS)

            self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            await self.page.goto(self.provider.url, wait_until="domcontentloaded",
                                 timeout=60_000)
            if self.provider.cookie_accept:
                await self._accept_cookies()
            self.state = "waiting_login"
            await self._notify()
            await self._wait_logged_in()
            if self.state == "logged_in":
                # 每次连接都是一次新开始: 站点(尤其 ChatGPT)会自己回到上次那个会话,
                # 用户不希望一连上就看到上次的界面 —— 这里主动把它切到"新对话"页。
                await self.open_fresh_chat()
                # 站点(尤其 ChatGPT)会把上次没发出去的输入框内容当草稿还原回来, 连接上先清掉
                await self.clear_composer_draft("连接站点后")
            return self.state == "logged_in"
        except Exception as exc:  # noqa: BLE001
            await self._teardown()
            self.state = "error"
            self.last_error = str(exc)
            await self._notify()
            return False

    async def _teardown(self):
        try:
            if self._ctx is not None:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None
        self.page = None
        self.state = "idle"

    async def _wait_logged_in(self):
        """等待用户手动登录或复用已有登录态。"""
        start_time = asyncio.get_running_loop().time()
        deadline = start_time + config.LOGIN_TIMEOUT_S
        notified = False
        warned_mid = False
        sels = list(self.provider.composer_selectors)
        while True:
            await asyncio.sleep(1.5)
            try:
                probe = await self.page.evaluate(_LOGIN_PROBE_JS, sels)
            except Exception as exc:  # noqa: BLE001
                if "closed" in str(exc).lower() or "has been closed" in str(exc).lower():
                    raise RuntimeError("浏览器窗口被关闭, 请重新启动") from exc
                probe = {}
            if probe.get("onSite") and probe.get("hasComposer"):
                self.state = "logged_in"
                if self.provider.cookie_accept:
                    await self._accept_cookies()
                await self._notify()
                return
            # 阶段性提示, 避免用户不知道要做什么
            waited = asyncio.get_running_loop().time() - start_time
            name = self.provider.name
            if not notified and waited > 2:
                await self._notify(info=info_event(
                    f"请在弹出的 Chrome 窗口里登录 {name}(扫码或账号)。"
                    "登录一次后会自动复用本机 Profile, 下次无需再登录。"))
                notified = True
            elif waited > 90 and not warned_mid:
                warned_mid = True
                await self._notify(info=info_event(
                    "仍在等待登录… 若窗口里是安全校验页或空白, 请手动刷新一次页面。"))
            if asyncio.get_running_loop().time() >= deadline:
                self.state = "error"
                self.last_error = "等待登录超时"
                await self._notify()
                return

    # ---------- 动作 ----------
    async def _accept_cookies(self):
        """尽力点掉 Cookie 弹窗的“接受全部”(纯增强, 失败无害)。"""
        try:
            await self.page.evaluate(_COOKIE_JS)
        except Exception:
            pass

    # ---------- 存活检测 ----------
    def page_alive(self) -> bool:
        """桥接浏览器窗口是否还在。"""
        page = self.page
        if page is None:
            return False
        try:
            return not page.is_closed()
        except Exception:  # noqa: BLE001
            return False

    async def _reap_closed_window(self):
        """窗口被关掉: 收尾浏览器、状态回到 idle, 并提示用户重新启动。"""
        try:
            await self._teardown()
        except Exception:  # noqa: BLE001
            pass
        self.state = "idle"
        self.last_error = None
        await self._notify(info=info_event(
            "桥接浏览器窗口已关闭, 请点左下角「启动并登录」重新打开(登录态已保存, 无需重新扫码)"))

    async def ensure_alive(self) -> bool:
        """桥接窗口是否可用; logged_in 状态下被关掉时收敛回 idle, 避免界面一直显示「已连接」。"""
        if self._ctx is None and self.page is None:
            return False                            # 从未启动 / 已收尾
        if self.state in ("launching", "waiting_login"):
            return True                             # 启动/登录流程自己会处理窗口问题
        if self.page_alive():
            return True
        await self._reap_closed_window()
        return False

    async def _composer(self):
        for sel in self.provider.composer_selectors:
            loc = self.page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    async def blocked_reason(self) -> str:
        """站点侧是否已经不让我们输入了(额度/次数上限/输入框禁用)。返回原因文本。"""
        if not self.page_alive():
            return ""
        try:
            return await self.page.evaluate(_BLOCK_JS, {"sels": list(self.provider.composer_selectors)}) or ""
        except Exception:  # noqa: BLE001
            return ""

    async def open_fresh_window(self) -> bool:
        """另开一个窗口(新页面)回到站点首页 = 新会话, 换掉被限流的那个页面。"""
        if self._ctx is None:
            return False
        try:
            old = self.page
            page = await self._ctx.new_page()
            await page.goto(self.provider.url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.5)
            self.page = page
            if (self.provider and self.provider.capture_mode == "stream"):
                try:
                    await capture.enable_capture(page)     # 新页面也要挂捕获(请求发出前)
                except Exception:  # noqa: BLE001
                    log.warning("新窗口挂捕获失败", exc_info=True)
            await self._accept_cookies()
            # 新窗口同样是站点首页: 它会把草稿还原进输入框, 先清掉再用
            await self.clear_composer_draft("另开窗口后")
            try:
                if old is not None and not old.is_closed():
                    await old.close()
            except Exception:  # noqa: BLE001
                pass
            await self._show_window()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("open_fresh_window failed: %s", exc)
            return False

    async def send_text(self, text: str, allow_retry: bool = True):
        """聚焦输入框 -> 键入/注入文本 -> 回车发送。text 可为空(纯文件场景)。

        站点说"不能输入了"(比如达到对话/文件次数上限)时, 自动另开一个窗口重试一次;
        新窗口仍然不行 -> 抛错并把站点的原话带回去(界面会提示"已达上限")。
        """
        log.info("send_text: %d chars | %s", len(text or ""),
                 (text or "")[:500].replace("\n", " \u23ce "))
        if not await self.ensure_alive():
            raise RuntimeError("桥接浏览器窗口已关闭, 请点左下角「启动并登录」重新打开")
        reason = await self.blocked_reason()
        if reason:
            log.warning("站点不可输入: %s", reason)
            if allow_retry:
                await self._notify(info=info_event(
                    "站点提示暂时不能输入(" + reason[:80] + "), 已自动另开一个窗口继续; "
                    "两边的内容会合并记录在同一个会话里。"))
                if await self.open_fresh_window():
                    log.info("已另开窗口重试发送(会话内容会并入当前对话)")
                    return await self.send_text(text, allow_retry=False)
            raise RuntimeError("站点已无法输入(连续两个窗口都不行): " + reason +
                               " —— 请稍后再试或升级账户")
        composer = await self._composer()
        if composer is None:
            if allow_retry:                     # 输入框都找不到, 也可能是被限流页挡住了
                reason2 = await self.blocked_reason()
                if reason2:
                    await self._notify(info=info_event(
                        "站点提示暂时不能输入(" + reason2[:80] + "), 已自动另开一个窗口继续。"))
                    if await self.open_fresh_window():
                        return await self.send_text(text, allow_retry=False)
            raise RuntimeError("找不到输入框: 未登录或站点页面结构已改版")
        if text:
            await self._fill_composer(composer, text)
        else:
            # 纯文件(不带文字)的消息: 站点会把上次没发出去的草稿还原到输入框 —— ChatGPT 尤其明显,
            # 不清掉的话这一下回车会把那段旧文字一起发出去。
            if self._norm(await self._composer_text()):
                if await self.clear_composer():
                    log.info("发送纯文件消息前清掉了输入框里的残留草稿")
                else:
                    log.warning("输入框里有残留草稿但没清掉, 可能连同文件一起发出去")
        await self.page.keyboard.press("Enter")
        if text:
            # 站点(SPA)清空输入框有快有慢: 轮询等一会儿, 清空了就算发出去。
            # 注意: 只记日志, 不报错 —— 曾经因为这里误判, 把已经发出去的消息当成失败,
            # 结果整轮直接中断、网页模型的回答一条都收不到。
            want = self._norm(text)
            for _ in range(10):                      # 最多等 ~5s
                await asyncio.sleep(0.5)
                left = self._norm(await self._composer_text())
                if not left or left != want:
                    return True
            log.warning("发送后输入框里仍有同样内容(站点可能还没提交), 继续等模型回复")
        return True

    async def clear_composer(self) -> bool:
        """清空输入框里残留的内容(站点还原的草稿 / 上次没发出去的半截话)。

        只在该清的时候用: **发纯文件消息之前**, 以及**一轮结束后输入框里还留着刚发出去的那段话**时。
        千万不要在按下回车之后立刻清 —— 那时候站点可能还没提交, 会把消息清没。
        """
        try:
            if not self._norm(await self._composer_text()):
                return True
            composer = await self._composer()
            if composer is None:
                return False
            await composer.click()
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Delete")
            await asyncio.sleep(0.2)
            if not self._norm(await self._composer_text()):
                return True
            # 有的编辑器吞快捷键: 退回直接注入空串
            await self.page.evaluate(_FILL_JS,
                                     {"sels": list(self.provider.composer_selectors), "text": ""})
            await asyncio.sleep(0.2)
            return not self._norm(await self._composer_text())
        except Exception as exc:  # noqa: BLE001
            log.warning("clear_composer failed: %s", exc)
            return False

    async def clear_composer_draft(self, why: str = "") -> int:
        """清掉输入框里被站点还原出来的草稿/残留, 返回清掉的字数。

        连接站点、另开窗口、新建对话、切换会话之后调用: 这些时刻输入框里的内容按定义
        不是"这次要发的东西", 留着只会被下一次回车连同一块发出去(而且站点会把它当草稿
        一直存着, 下次连接又冒出来)。清掉了会广播一条 info, 免得用户不知道为什么没了。
        """
        try:
            left = await self._composer_text()
        except Exception:  # noqa: BLE001
            return 0
        n = len((left or "").strip())
        if not n:
            return 0
        ok = await self.clear_composer()
        log.info("清理输入框残留(%s): %d 字 -> %s", why or "-", n, "已清空" if ok else "没清掉")
        if ok:
            await self._notify(info=info_event(
                "清掉了浏览器输入框里残留的 " + str(n) + " 个字(" + (why or "连接站点后") +
                ") —— 免得下次回车把它一起发出去。"))
        return n if ok else 0

    async def _composer_text(self) -> str:
        try:
            return await self.page.evaluate(
                _READ_JS, list(self.provider.composer_selectors)) or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _norm(s: str) -> str:
        return "".join((s or "").split())

    async def _fill_composer(self, composer, text: str):
        """把文本写进输入框, 并**回读校验**。

        页面重绘/切换会把键盘输入打断, 只敲进去半截就按回车 = 发出去半条消息(曾经出现过)。
        所以: 优先一次性注入; 写完回读比对, 不对就重写一次; 再不行才用键盘兜底。
        """
        want = self._norm(text)

        async def injected() -> bool:
            try:
                return bool(await self.page.evaluate(
                    _FILL_JS, {"sels": list(self.provider.composer_selectors), "text": text}))
            except Exception:  # noqa: BLE001
                return False

        for attempt in (1, 2):
            if len(text) > _FILL_THRESHOLD or attempt == 2:
                await injected()
            else:
                try:
                    await composer.click()
                    await self.page.keyboard.type(text, delay=2)
                except Exception:  # noqa: BLE001
                    await injected()
            got = await self._composer_text()
            if self._norm(got) == want:
                return True
            log.warning("输入框内容不完整(第 %d 次): 期望 %d 字, 实际 %d 字, 重写…",
                        attempt, len(want), len(self._norm(got)))
        got = await self._composer_text()
        if self._norm(got) != want:
            raise RuntimeError("输入框内容没能完整写入(站点页面可能在刷新), 已放弃发送以免只发出半条")
        return True

    async def attach_files(self, files: list[dict]) -> tuple[int, list[str]]:
        """把暂存的文件字节投喂给站点隐藏的 <input type=file>, 触发其上传/预览。

        files: [{"name": str, "mime": str, "data": bytes}]
        返回 (成功数量, 错误列表)。尽力而为, 失败不影响文字发送。
        """
        ok = 0
        errs: list[str] = []
        for f in files:
            try:
                if await self._attach_one(f):
                    ok += 1
                else:
                    errs.append(f.get("name", "文件") + ": 未找到文件输入框")
            except Exception as exc:  # noqa: BLE001
                errs.append(f.get("name", "文件") + ": " + str(exc))
        return ok, errs

    async def _attach_one(self, f: dict) -> bool:
        """定位站点文件输入框(取最后一个 input[type=file])并写入该文件。"""
        locs = await self.page.locator("input[type=file]").count()
        if locs == 0:
            return False
        target = self.page.locator("input[type=file]").last
        payload = {"name": f.get("name", "file"),
                   "mimeType": f.get("mime") or "application/octet-stream",
                   "buffer": f.get("data", b"")}
        await target.set_input_files([payload])
        await asyncio.sleep(1.2)   # 等待站点上传/预览
        return True

    async def _click_new_chat(self) -> bool:
        """点站点内的"新对话": 先按选择器(稳), 再按按钮文案(兜底)。"""
        for sel in self.provider.new_chat_selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                await loc.click(timeout=3000)
                return True
            except Exception:  # noqa: BLE001
                continue
        try:
            return bool(await self.page.evaluate(_NEW_CHAT_JS, list(self.provider.new_chat_names)))
        except Exception:  # noqa: BLE001
            return False

    # ---------- 站点会话历史 ----------
    async def _probe_conversations(self) -> list[dict]:
        try:
            items = await self.page.evaluate(history._CONV_JS, {
                "pattern": self.provider.conversation_pattern or "",
                "extra": list(self.provider.new_chat_names),
            })
            return items or []
        except Exception:  # noqa: BLE001
            return []

    async def conversations(self, more: bool = False) -> list[dict]:
        """读取站点侧栏的会话列表; more=True 时会先把站点列表滚到底再读(触发它的懒加载)。"""
        if not await self.ensure_alive():
            return []
        items = await self._probe_conversations()
        if not more:
            return items
        try:
            await self.page.evaluate(history._SCROLL_MORE_JS)
        except Exception:  # noqa: BLE001
            return items
        await asyncio.sleep(1.5)
        again = await self._probe_conversations()
        return again if len(again) > len(items) else items

    async def open_conversation(self, key: str, url: str = "") -> bool:
        """切到某个历史会话: 有链接就直接跳, 没有就回站点里点它。"""
        if not await self.ensure_alive():
            return False
        try:
            if url:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            elif not await self.page.evaluate(history._OPEN_CONV_JS, key):
                return False
            await asyncio.sleep(1.5)
            # 切过去的那个会话自己也可能存着没发出去的草稿
            await self.clear_composer_draft("切换会话后")
            await self._show_window()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _show_window(self):
        """把桥接浏览器窗口提到最前, 让用户直接看到新对话。"""
        try:
            await self.page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

    async def open_fresh_chat(self) -> bool:
        """连上后主动开一个新对话, 而不是停在站点自己恢复的上次会话上。

        站点(尤其 ChatGPT/DeepSeek)打开首页时会回到上次那个会话, 于是"连上"看起来就像
        "回到了上次的界面"; 这里统一把它切到新会话页, 失败就再试一次(SPA 有时还没渲染出按钮)。
        """
        try:
            if self.conversation_id() is None:
                return True                       # 已经在新会话页, 不用动
            log.info("连接后站点停在上次会话(%s), 主动切到新对话", self.conversation_id())
            if await self.new_chat():
                return True
            await asyncio.sleep(1.5)
            return await self.new_chat()
        except Exception:  # noqa: BLE001
            return False

    async def new_chat(self) -> bool:
        """打开站点的新对话: 先点站内按钮(SPA 内切换), 点不动就直接回站点首页(= 新会话页)。"""
        if self.page is None:
            return False
        try:
            if await self._click_new_chat():
                await asyncio.sleep(1.5)
            if self.conversation_id() is None:      # 已在新会话页(URL 里没有会话 id)
                await self.clear_composer_draft("新建对话后")   # 新会话页也会还原上次的草稿
                await self._show_window()
                return True
            # 站内按钮失效: 直接打开站点首页, 等价于新建对话
            await self.page.goto(self.provider.url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.5)
            await self.clear_composer_draft("新建对话后")
            ok = self.conversation_id() is None
            await self._show_window()
            return ok
        except Exception:  # noqa: BLE001
            return False

    def conversation_url(self):
        """当前地址(含会话 id): 本地记录据此可以回跳到原会话继续聊。"""
        try:
            if self.page is None or self.page.is_closed():
                return None
            return self.page.url
        except Exception:  # noqa: BLE001
            return None

    def conversation_id(self):
        """从 URL 尽力提取会话 id, 失败返回 None。"""
        try:
            url = self.page.url
            pat = self.provider.conversation_pattern
            if pat and pat in url:
                tail = url.split(pat)[-1].split("?")[0].split("#")[0]
                return tail or None
        except Exception:
            pass
        return None

    async def shutdown(self):
        await self._teardown()
        self.state = "idle"
        await self._notify()
