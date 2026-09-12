"""设置(planner)保存前的规范化: 选「本地模型」时地址/模型必须落到真正的本机服务上。

场景:
  1) 从 API 模式切到本地模型, 地址还是云端那一份 -> 自动换成扫到的本机服务(否则落盘会去调云端);
  2) 只选了本地模型、地址没填 -> 同样自动补;
  3) 本机一个服务都没有 -> 给一句人话的错误, 不存一份调不通的配置;
  4) 界面自己填的本机地址 -> 一个字都不动;
  5) API 模式(哪怕地址是云端) -> 不碰。

纯逻辑测试: 打桩 find_local, 不联网、不写设置文件。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import settings  # noqa: E402

FOUND = [{"base": "http://127.0.0.1:1234/v1",
          "models": ["llama-3.1-8b", "qwen2.5-coder-7b-instruct"]}]


def probe(*results):
    async def _p():
        return list(results)
    return _p


async def main() -> int:
    bad = []

    # 1) 云端地址 -> 自动换成本机服务; 云端留下的模型名本机没有, 也一并换掉
    p = {"type": "local", "api_base": "https://api.deepseek.com/v1", "api_model": "deepseek-chat"}
    err = await settings.normalize_planner(p, probe(*FOUND))
    print("1) 云端地址 ->", err, p)
    if err or p["api_base"] != FOUND[0]["base"]:
        bad.append("没把云端地址换成本机服务: " + str(p))
    if p["api_model"] not in FOUND[0]["models"]:
        bad.append("模型名还是云端那个(本机没有这个模型): " + str(p))

    # 2) 没填地址 -> 自动补
    p2 = {"type": "local"}
    err2 = await settings.normalize_planner(p2, probe(*FOUND))
    print("2) 空地址  ->", err2, p2)
    if err2 or "1234" not in p2.get("api_base", "") or not p2.get("api_model"):
        bad.append("没给本地模型补上地址/模型: " + str(p2))

    # 3) 本机没有服务 -> 明确报错
    p3 = {"type": "local"}
    err3 = await settings.normalize_planner(p3, probe())
    print("3) 扫不到  ->", err3)
    if not err3:
        bad.append("本机没有本地模型时应该拦下来, 而不是存一份用不了的配置")

    # 4) 用户自己填的本机地址 -> 原样保留
    p4 = {"type": "local", "api_base": "http://127.0.0.1:9999/v1", "api_model": "my-model"}
    err4 = await settings.normalize_planner(p4, probe(*FOUND))
    print("4) 自己填的 ->", err4, p4)
    if err4 or p4["api_base"] != "http://127.0.0.1:9999/v1" or p4["api_model"] != "my-model":
        bad.append("把用户自己填的本地地址改掉了: " + str(p4))

    # 5) API 模式不碰
    p5 = {"type": "api", "api_base": "https://api.deepseek.com/v1", "api_model": "deepseek-chat"}
    err5 = await settings.normalize_planner(p5, probe(*FOUND))
    print("5) API 模式 ->", err5, p5)
    if err5 or p5["api_base"] != "https://api.deepseek.com/v1":
        bad.append("API 模式的地址被改了: " + str(p5))

    if bad:
        print("FAIL:")
        for b in bad:
            print(" -", b)
        return 1
    print("SETTINGS_NORMALIZE_OK (本地模型设置会被补齐/拦住, 自己填的和 API 模式都不动)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
