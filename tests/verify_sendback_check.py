r"""验证结论到手后"发不发 ChatGPT"的分流 —— 只有代码层次的问题才发。

规矩(2026-09-26 他改定): **环境/工具链/裁判自己的毛病不发过去, 由本地这一侧解决**;
只有 `real_error`(项目里真的报错 = 代码层次的问题)才回传给 ChatGPT 修。
理由很直白: 把"这台机器上没有 C++ 编译器"发给 ChatGPT 毫无意义 —— 它改不了这台机器。

这条盯的是分流本身, 夹具是当天两份真实留痕:
  A 环境类(rounds-exhausted / 命令在这台机器跑不了) -> 一个字都不发, 卡片上写清没发、为什么
  B 代码类(项目真报错, 且本地模型自己动过手) -> 发, 且带上报错原文和验收点
  C 两轮止损线还在
  D 只看本地模型时不发(没有网页模型可发)
  E 通过时不发
  F 代码报错但本地模型一次都没动手 -> 先停在卡片上, 不默默甩给网页模型
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright  # noqa: E402

from media_render_check import STUB  # noqa: E402

URL = "http://127.0.0.1:8765/"

# 真实留痕 20260926-034418 的两轮(命令不存在 / 路径不存在), 退出码都是 0
ENV_ROUNDS = [
    {"round": 1, "action": "run", "code": 0, "kind": "behavior",
     "command": 'powershell -NoProfile -Command "g++ -std=c++17 -o .verify\\a.exe main.cpp"',
     "output": "g++ : 无法将“g++”项识别为 cmdlet、函数、脚本文件或可运行程序的名称。\n"
               "    + CategoryInfo : ObjectNotFound: (g++:String) [], CommandNotFoundException\n"
               "BUILD_EXIT=\n"},
    {"round": 2, "action": "tool-error", "code": 1,
     "command": 'powershell -NoProfile -Command "cl /EHsc main.cpp"',
     "output": "cl : 无法将“cl”项识别为...\n[这条命令的 shell 退出码是 0, 但它要跑的程序根本没启动]"},
]
CHECKS = "- [1] 构建并运行, 输出严格升序\n- [2] 空数组不崩溃\n"
ENV = {"ok": False, "gave_up": True, "real_error": False, "reason": "rounds-exhausted",
       "rounds": ENV_ROUNDS, "checks": CHECKS, "root": "H:\\test", "local_fixes": 0,
       "tried_local_fix": False, "behavior_ok": True, "changed": [], "applied": [],
       "skipped": [], "last_command": ENV_ROUNDS[0]["command"], "last_output": "",
       "error_log": "", "audit": "H:\\browser-bridge\\.tmp\\verify-audit\\20260926-034418"}
CODE = dict(ENV, ok=False, gave_up=False, real_error=True, reason="real-error",
            local_fixes=1, tried_local_fix=True, behavior_ok=True,
            changed=[{"op": "update", "path": "EditorArea.qml"}],
            error_log="qml/EditorArea.qml:88: error: ScrollBar is not a member of 'ScrollBar'",
            last_output="ScrollBar is not a member", rounds=[])
PASS = dict(ENV, ok=True, gave_up=False, real_error=False, reason="ok", rounds=[])

SETUP = """(v) => {
  window.__sent = [];
  showWelcome(false); switchMode("world");
  post = async (url, body) => { window.__sent.push([url, body]); return { ok: true }; };
}"""


async def run_case(b, verdict, choice="web", twice=False):
    page = await b.new_page(viewport={"width": 1400, "height": 900})
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)[:160]))
    await page.add_init_script(STUB)
    await page.goto(URL, wait_until="networkidle", timeout=30_000)
    await page.wait_for_timeout(700)
    await page.evaluate(SETUP, verdict)
    await page.evaluate("(c) => { replyChoice = c; worldVerifyTask = '把排序做完'; }", choice)
    await page.evaluate("async (v) => { await worldVerdict(v); }", verdict)
    if twice:
        await page.evaluate("async (v) => { await worldVerdict(v); await worldVerdict(v); }", verdict)
    await page.wait_for_timeout(400)
    out = await page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#conv .task-card')];
      const c = cards.pop();
      return {
        chat: window.__sent.filter(s => s[0] === '/api/chat').map(s => s[1].text),
        foot: c ? (c.querySelector('.task-foot') || {}).textContent : '',
        rows: c ? [...c.querySelectorAll('.task-item')].map(e => e.textContent) : [],
        bubbles: [...document.querySelectorAll('#conv .user-bubble')].map(e => e.textContent),
        spinning: !!document.querySelector('#conv .tspin:not([style*="none"])'),
      };
    }""")
    out["errs"] = errs
    await page.close()
    return out


async def main() -> int:
    bad = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)

        a = await run_case(b, ENV)
        print("A 环境类 -> 发了 %d 条 | 卡片脚: %s" % (len(a["chat"]), a["foot"][:70]))
        if a["chat"]:
            bad.append("环境/工具链的问题被发给了 ChatGPT(它改不了这台机器): " + a["chat"][0][:120])
        if "没发给 ChatGPT" not in a["foot"]:
            bad.append("卡片上没写清「没发给 ChatGPT」: " + a["foot"][:80])
        if not any("不是代码问题" in r for r in a["rows"]):
            bad.append("没有一行说明这不是代码问题: " + json.dumps(a["rows"], ensure_ascii=False)[:160])
        if not any("根本没启动" in r or "无法将" in r for r in a["rows"]):
            bad.append("卡片里看不到过程(哪条命令、什么回显): " + json.dumps(a["rows"], ensure_ascii=False)[:160])
        if a["spinning"]:
            bad.append("环境类结论之后卡片还在转圈(发送按钮会被一直锁住)")

        c = await run_case(b, CODE)
        print("B 代码类 -> 发了 %d 条, 正文 %d 字" % (len(c["chat"]), len(c["chat"][0]) if c["chat"] else 0))
        if len(c["chat"]) != 1:
            bad.append("项目真报错却没发回 ChatGPT: %d 条" % len(c["chat"]))
        else:
            t = c["chat"][0]
            for needle, name in (("ScrollBar is not a member", "报错原文"),
                                 ("EditorArea.qml", "本地改过哪些文件"),
                                 ("验收点", "这次要满足什么")):
                if needle not in t:
                    bad.append("发回去的消息里没有%s: %s" % (name, t[:160]))
        # 判"有没有把代码问题说成环境问题": 不能拿"环境问题"当关键词(正文里
        # "不是这台机器的环境问题"这句就含它)。改判环境结论特有的措辞有没有漏进来。
        if "代码层面" not in t:
            bad.append("发回去的话没点明这是代码层面的问题: " + t[:160])
        for wrong in ("跑不了", "轮数用完", "没拿出证据", "根本没启动"):
            if wrong in t:
                bad.append("环境/裁判类的措辞漏进了代码类回传(%r): %s" % (wrong, t[:160]))
        if not any("自测报错" in x for x in c["bubbles"]):
            bad.append("消息列表里没有一条说明已回传: " + json.dumps(c["bubbles"], ensure_ascii=False)[:160])

        d = await run_case(b, CODE, twice=True)
        print("C 连着三次 -> 发了 %d 条(应 2 就停)" % len(d["chat"]))
        if len(d["chat"]) != 2:
            bad.append("两轮止损线不对: 发了 %d 条" % len(d["chat"]))

        e = await run_case(b, CODE, choice="local")
        print("D 只看本地模型 -> 发了 %d 条(应 0)" % len(e["chat"]))
        if e["chat"]:
            bad.append("选了「只看本地模型」还是往站点发了 %d 条" % len(e["chat"]))

        f = await run_case(b, PASS)
        print("E 通过 -> 发了 %d 条(应 0)" % len(f["chat"]))
        if f["chat"]:
            bad.append("通过了还回传: %d 条" % len(f["chat"]))

        g = await run_case(b, dict(CODE, local_fixes=0, tried_local_fix=False))
        print("F 代码报错但本地没动手 -> 发了 %d 条(应 0)" % len(g["chat"]))
        if g["chat"]:
            bad.append("本地模型一次都没动手就把报错甩给 ChatGPT: %d 条" % len(g["chat"]))
        if "先停在这里" not in g["foot"]:
            bad.append("这种情况卡片没说清停在哪里: " + g["foot"][:80])

        allerr = [x for r in (a, c, d, e, f, g) for x in r["errs"]]
        if allerr:
            bad.append("页面 JS 报错: " + " | ".join(allerr[:3]))
        await b.close()

    if bad:
        print("FAIL:")
        for x in bad:
            print(" -", x)
        return 1
    print("SENDBACK_OK (只有代码层次的报错才发回 ChatGPT; 环境/工具链问题留在本地并写清没发; "
          "两轮止损; 只看本地模型不发; 通过不发; 本地没动手也不发)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
