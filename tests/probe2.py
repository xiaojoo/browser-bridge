"""第二轮探针: 换更接近真人的浏览器指纹重试 chat.deepseek.com。"""
import asyncio
import json

from playwright.async_api import async_playwright

URL = "https://chat.deepseek.com/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

_PROBE = """() => {
  const ta = document.querySelectorAll('textarea').length;
  const ce = document.querySelectorAll('div[contenteditable="true"]').length;
  const sel = document.querySelector('#chat-input, textarea, div[contenteditable="true"]');
  const bodyTxt = (document.body.innerText || '').slice(0, 300);
  return {
    title: document.title,
    path: location.pathname,
    host: location.hostname,
    textareas: ta,
    contenteditables: ce,
    composerFound: !!sel,
    composerDesc: sel ? sel.tagName + '#' + (sel.id || '') : null,
    hasCap: !!(window.__dsCap),
    bodySample: bodyTxt,
    readyState: document.readyState
  };
}"""


async def one(pw, label: str, headless: bool):
    try:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception as exc:  # noqa: BLE001
        print(label, "launch ERR", exc)
        return
    page = await browser.new_page(
        viewport={"width": 1280, "height": 860},
        user_agent=UA,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:  # noqa: BLE001
        print(label, "goto warn:", exc)
    for wait in (5, 8, 10):
        await page.wait_for_timeout(wait * 1000)
        try:
            facts = await page.evaluate(_PROBE)
        except Exception as exc:  # noqa: BLE001
            print(label, "eval ERR", exc)
            break
        if facts.get("composerFound") or facts.get("bodySample"):
            print(label, json.dumps(facts, ensure_ascii=False)[:600])
            break
    else:
        try:
            print(label, json.dumps(facts, ensure_ascii=False)[:600])
        except Exception:
            pass
    await browser.close()


async def main():
    import sys
    async with async_playwright() as pw:
        await one(pw, "headless:", True)
        if "--headed" in sys.argv:
            await one(pw, "headed:  ", False)


asyncio.run(main())
