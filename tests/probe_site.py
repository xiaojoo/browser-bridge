"""探测 chat.deepseek.com 当前真实 DOM 结构(无登录态, headless)。

验证: 输入框选择器假设、fetch 包装器注入、按钮文案样本(供新对话用)。
仅诊断用途, 不发送任何消息。
"""
import asyncio
import json

from playwright.async_api import async_playwright

URL = "https://chat.deepseek.com/"

_PROBE = """() => {
  const ta = document.querySelectorAll('textarea').length;
  const ce = document.querySelectorAll('div[contenteditable="true"]').length;
  const sel = document.querySelector('#chat-input, textarea, div[contenteditable="true"]');
  const btns = Array.from(document.querySelectorAll('button')).slice(0, 15)
    .map(b => (b.textContent || '').trim().slice(0, 24)).filter(Boolean);
  return {
    title: document.title,
    path: location.pathname,
    host: location.hostname,
    textareas: ta,
    contenteditables: ce,
    composerFound: !!sel,
    composerDesc: sel ? sel.tagName + '#' + (sel.id || '') + '.' + (sel.className || '').toString().slice(0, 40) : null,
    sampleButtons: btns,
    hasCap: !!(window.__dsCap)
  };
}"""


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 860})
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            print("goto warn:", exc)
        await page.wait_for_timeout(8000)
        try:
            facts = await page.evaluate(_PROBE)
        except Exception as exc:  # noqa: BLE001
            print("evaluate failed:", exc)
            await browser.close()
            return 1
        print(json.dumps(facts, ensure_ascii=False, indent=1))
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
