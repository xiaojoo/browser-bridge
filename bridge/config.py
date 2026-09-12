"""DeepSeek Browser Bridge 配置。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROFILE_DIR = BASE_DIR / "profile"          # Chrome 持久化用户数据(登录态复用)
STATIC_DIR = BASE_DIR / "static"            # 前端资源(分离的 css/js/html)
STATIC_INDEX = STATIC_DIR / "index.html"

DEEPSEEK_URL = "https://chat.deepseek.com/"

HOST = "127.0.0.1"
PORT = 8765

VIEWPORT = {"width": 1280, "height": 860}

# 登录等待: 首次运行需在弹窗里手动扫码/账号登录一次
LOGIN_TIMEOUT_S = 420

# 流式捕获参数
STREAM_POLL_S = 0.12          # 缓冲轮询间隔(秒)
SNAPSHOT_POLL_S = 0.25        # DOM 兜底快照间隔(秒)
QUIET_END_S = 1.6             # 无新内容/无生成指示多久后判定结束
NO_DATA_ERROR_S = 25          # 一轮内毫无捕获则提示错误
TURN_TIMEOUT_S = 360          # 单轮绝对超时
SNAPSHOT_REVERT_GRACE_S = 20  # 快照退回"本轮之前那条回答"(页面重绘, 新回答节点短暂消失)时最多再等多久

# 文件上传限制
MAX_FILE_MB = 25              # 单文件上限
MAX_STAGED_FILES = 20         # 暂存文件总数上限(超出时淘汰最早的)

# 本地工作区
WORKSPACE_DIR = BASE_DIR / "workspace"          # 工作区根(读写仅限此目录)
WORKSPACE_READ_MAX = 2 * 1024 * 1024            # 单文件读取文本上限(2MB)
WORKSPACE_WRITE_MAX = 2_000_000                 # 单文件写入字符上限
WORKSPACE_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__",
                       "dist", "build", ".idea", ".vscode", "venv"}
