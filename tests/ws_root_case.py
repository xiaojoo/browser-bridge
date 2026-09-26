"""门禁公用: 把 8765 那个服务的"工作区根"临时指到一个自己造的小目录, 用完还回去。

为什么要抽出来: `nested_check` / `ws_title_check` 这两条以前都直接拿**用户真实的工作区根**
(H:\\test 那种)当被测对象 —— 于是要么往他项目里建文件再删, 要么把整棵树(node_modules 在内)
读出来断言, 红的时候看不出是代码坏了还是环境变了。要量"树/标题"这类界面, 就得自己造一个
已知内容的小工作区, 断言完把根还回去。
"""
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

BASE = "http://127.0.0.1:8765"
# 临时工作区造在项目自己的 .tmp 下(不是系统 temp): 他定过规矩——任何验写盘的探针都别把
# 东西写到项目外面去, 也好一次性清理。
TMP_DIR = Path(__file__).resolve().parent.parent / ".tmp"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_root() -> str:
    return req("GET", "/api/workspace").get("root") or ""


def set_root(path) -> str:
    """设工作区根; 返回设完之后服务端实际记的根(它可能会夹取/规范化)。"""
    req("POST", "/api/workspace/root", {"path": str(path)})
    return get_root()


@contextmanager
def workspace_root(files: dict):
    """造一个临时工作区目录(键是相对路径)、把服务指过去, 出来时删干净并把根还回去。"""
    user_root = get_root()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="wlb_ws_", dir=str(TMP_DIR))
    for rel, content in (files or {}).items():
        p = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    try:
        got = set_root(tmp)
        if os.path.normcase(got) != os.path.normcase(tmp):
            raise RuntimeError("服务端没接受这个工作区根: 要 %s, 实际 %s" % (tmp, got))
        yield type("T", (), {"path": tmp, "name": os.path.basename(tmp)})()
    finally:
        if user_root:
            try:
                set_root(user_root)
            except (urllib.error.URLError, OSError):
                pass
        shutil.rmtree(tmp, ignore_errors=True)
