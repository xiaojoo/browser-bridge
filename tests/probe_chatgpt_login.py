"""诊断: 用登录 Profile 打开站点, 跑登录探测并 dump 输入框候选, 定位 waiting_login 卡点。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from bridge import browser, config, providers  # noqa: E402

_UA = browser._UA

DUMP_JS = """(sels) => {
  const out = [];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (!el) continue;
    const r = el.getBoundingClientRect();
    out.push({
      sel: s, tag: el.tagName, id: el.id || null,
      cls: String(el.className || '').slice(0, 60),
      editable: el.isContentEditable === true,
      disabled: el.disabled === true ||
        (el.getAttribute && (el.getAttribute('aria-disabled') === 'true' ||
         el.getAttribute('contenteditable') === 'false')),
      w: Math.round(r.width), h: Math.round(r.height)
    });
  }
  // 所有 input[type=file] 与可见的登录按钮
  const fileInputs = Array.from(document.querySelectorAll('input[type=file]')).map(e => e.outerHTML.slice(0, 80));
  const loginBtns = Array.from(document.querySelectorAll('button,a')).filter(b =>
    /^(log in|sign in|登录|登录|sign up)$/i.test(String(b.textContent||'').trim()) && b.getBoundingClientRect().width>0)
    .map(b => String(b.textContent||'').trim());
  return { cands: out, fileInputs, loginBtns, textareas: document.querySelectorAll('textarea').length };
}"""


async def main() -> int:
    p = providers.get("chatgpt")
    profile = config.PROFILE_DIR / "chatgpt"
    profile.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=True,
            viewport={"width": 1280, "height": 860},
            user_agent=_UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            args=["--disable-blink-features=AutomationControlled", "--lang=zh-CN"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(p.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            print("goto warn:", exc)
        await page.wait_for_timeout(6000)
        probe = await page.evaluate(browser._LOGIN_PROBE_JS, list(p.composer_selectors))
        dump = await page.evaluate(DUMP_JS, list(p.composer_selectors))
        print("probe:", json.dumps(probe, ensure_ascii=False))
        print("dump:", json.dumps(dump, ensure_ascii=False, indent=1))
        await ctx.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
