"""本地设置存储(持久化到 .bridge_settings.json, 不入库)。

这里是**全局设置**: 所有会话/窗口共用同一份(不存在按会话或按站点分家的副本)。

planner: 规划/摘要模型(还负责 World 模式的落盘/自测)
  type: "web" (默认, 用当前网页 Provider; 只能做规划/摘要, 不能落盘)
        | "api" (OpenAI 兼容官方 API, 需要 api_key)
        | "local" (本机 llama.cpp / LM Studio / vLLM / Ollama, 不用 Key)
  api_base / api_model / api_key / api_temp
"""
import json
from pathlib import Path

from . import planner as planner_mod

BASE_FILE: Path = Path(__file__).resolve().parent.parent / ".bridge_settings.json"

# 这些是云端服务: 选了"本地模型"却指着它们, 一定是配置串了(落盘会拿默认地址去调, 看起来像"设置没生效")
CLOUD_HINTS = ("api.deepseek.com", "api.openai.com", "api.anthropic.com",
               "generativelanguage.googleapis.com", "openrouter.ai", "siliconflow")

DEFAULTS = {
    "engine": {
        "repo": "",          # 远程仓库地址(可选, 会写进给网页模型的提示词)
        # World 模式默认**自动落盘**: 回答回来后程序把代码块机械地整理成文件改动直接写,
        # 结果画在消息下面那张卡上。勾上设置里那个框就回到"先列清单、逐条勾选再写"。
        "confirm_apply": "0",  # 落盘前弹确认框(可逐条勾选)
        "test_cmd": "",      # 自测命令(在工作区目录下执行), 例如 pytest -q / cmake --build build
    },
    "workspace": {
        "root": "",          # 空 = 用 config.WORKSPACE_DIR
    },
    "planner": {
        "type": "web",
        "api_base": "https://api.deepseek.com/v1",
        "api_model": "deepseek-chat",
        "api_key": "",
        "api_temp": 0.2,
    }
}


def load() -> dict:
    data = json.loads(json.dumps(DEFAULTS))
    try:
        if BASE_FILE.exists():
            stored = json.loads(BASE_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                for k, v in stored.items():
                    if k in data and isinstance(v, dict) and isinstance(data[k], dict):
                        data[k].update({kk: vv for kk, vv in v.items() if vv is not None})
                    else:
                        data[k] = v
    except Exception:
        pass
    return data


def _write(data: dict) -> None:
    BASE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def normalize_planner(patch: dict, probe=None) -> str | None:
    """保存前把 planner 补丁修成一份"真能用"的全局配置。

    选「本地模型」时, 地址/模型名必须落在真正的本机服务上:
      * 没填地址, 或填成了云端地址(从 API 模式切过来时最容易发生) -> 自动扫一遍本机常见端口;
      * 一个都扫不到 -> 返回一句人话, 让界面直接提示, 而不是存下一份调不通的配置
        (那种情况下 World 模式落盘会报"本地模型调用失败", 看起来就像"设置换了窗口就失效")。
    返回 None = 没问题; 返回字符串 = 给界面看的错误。
    """
    if (patch or {}).get("type") != "local":
        return None
    base = str(patch.get("api_base") or "").strip()
    if base and not any(h in base for h in CLOUD_HINTS):
        return None                                  # 界面填的本机地址, 直接用
    found = await (probe or planner_mod.find_local)()
    if not found:
        return ("没找到本机的本地模型服务(试过 1234/8080/8081/8000/11434/5000): "
                "先把 LM Studio / llama.cpp 的 server 跑起来, 或改成「API」模式填 Key")
    pick = found[0]
    models = [m for m in (pick.get("models") or []) if m]
    prefer = [m for m in models if any(k in m.lower() for k in ("coder", "qwen", "deepseek", "llama", "glm"))]
    patch["api_base"] = pick["base"]
    cur_model = str(patch.get("api_model") or "").strip()
    if not cur_model or cur_model not in models:     # 云端留下的模型名在本机不存在 -> 换成本机真有的
        patch["api_model"] = (prefer or models or ["local-model"])[0]
    return None


def save_planner(patch: dict) -> dict:
    """按字段补丁式保存: 只更新 patch 里出现的键; api_key='__CLEAR__' 表示清除。"""
    cur = load()
    for k, v in patch.items():
        if k == "api_key":
            if v == "__CLEAR__":
                cur["planner"]["api_key"] = ""
            elif v:                       # 留空/未提供 = 保持原 key
                cur["planner"]["api_key"] = v
        elif v is not None and str(v) != "":
            cur["planner"][k] = v
    _write(cur)
    return cur


def save_engine(patch: dict) -> dict:
    """工程任务相关设置(目前只有远程仓库地址)。"""
    cur = load()
    for k, v in (patch or {}).items():
        if k in cur["engine"] and v is not None:
            cur["engine"][k] = str(v)
    _write(cur)
    return cur


def save_workspace_root(path: str) -> dict:
    """记住自定义工作区根目录(空字符串 = 恢复默认)。"""
    cur = load()
    cur["workspace"]["root"] = path or ""
    _write(cur)
    return cur


def public() -> dict:
    """给 UI 看: key 打码。"""
    s = load()
    p = dict(s["planner"])
    p["has_key"] = bool(p.get("api_key"))
    p["api_key"] = ""
    return {"planner": p, "engine": dict(s.get("engine") or {}), "file": str(BASE_FILE)}
