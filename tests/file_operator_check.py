"""file_operator 单元测试(离线: 不联网、不开浏览器、不重启服务)。

覆盖:
  - read_file: 文本 / 二进制 / 不存在 / 越界(../、绝对路径、D:\\) 全部按预期
  - prepare_send_file: 名称/MIME/大小 + MAX_FILE_MB 上限
  - send_file: 用桩 manager 验证"读字节 -> attach_files", 未登录/附加失败要报错
  - /api/file/operate: 直接调用路由函数, 断言返回结构; 并断言全程没有调用本地模型

工作区一律切到 .tmp 下的临时目录, 绝不碰用户配置的工作区。
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bridge import config, file_operator, server, workspace  # noqa: E402

TMP_WS = ROOT / ".tmp" / "file-operator-ws"


class StubManager:
    """只实现 file_operator / 路由用到的那几个接口。"""

    def __init__(self, state="logged_in", attach_ok=True, alive=True):
        self.state = state
        self.busy = False
        self.alive = alive
        self.attach_ok = attach_ok
        self.attached: list[dict] = []

    async def ensure_alive(self) -> bool:
        return self.alive

    async def attach_files(self, files):
        self.attached.extend(files)
        if self.attach_ok:
            return 1, []
        name = files[0].get("name", "?") if files else "?"
        return 0, [name + ": 未找到文件输入框"]

    def status(self) -> dict:
        return {"provider": "stub", "state": self.state, "busy": self.busy}


def body(resp) -> dict:
    return json.loads(bytes(resp.body).decode("utf-8"))


def expect_error(fn, what: str):
    try:
        fn()
    except file_operator.FileOperatorError as exc:
        print(f"  拒绝 {what}: {exc}")
        return
    raise AssertionError(f"{what} 应该被拒绝, 但通过了")


async def expect_async_error(coro, what: str):
    try:
        await coro
    except file_operator.FileOperatorError as exc:
        print(f"  拒绝 {what}: {exc}")
        return
    raise AssertionError(f"{what} 应该被拒绝, 但通过了")


async def main() -> int:
    shutil.rmtree(TMP_WS, ignore_errors=True)
    TMP_WS.mkdir(parents=True)
    # 一律 write_bytes: write_text 在 Windows 会把 \n 变成 \r\n, 断言会跟着漂
    (TMP_WS / "a.txt").write_bytes(b"HELLO=1\n")
    (TMP_WS / "raw.bin").write_bytes(b"\x00\x01\xff\xfe")
    (TMP_WS / "sub").mkdir()
    (TMP_WS / "sub" / "b.qml").write_bytes(b"// B\n")
    (TMP_WS / "big.dat").write_bytes(b"x" * (2 * 1024 * 1024))   # 2MB, 下面把上限压到 1MB

    orig_root = workspace.ROOT
    orig_max = config.MAX_FILE_MB
    workspace.use_root(TMP_WS)
    try:
        print("[1] read_file")
        r = file_operator.read_file("a.txt")
        assert r["binary"] is False and r["text"] == "HELLO=1\n" and r["path"] == "a.txt", r
        r = file_operator.read_file("sub\\b.qml")            # Windows 分隔符也要认
        assert r["text"] == "// B\n", r
        r = file_operator.read_file("raw.bin")
        assert r["binary"] is True and "text" not in r and r["size"] == 4, r
        print(f"  文本/二进制 OK: {r}")
        expect_error(lambda: file_operator.read_file("nope.txt"), "不存在的文件")
        expect_error(lambda: file_operator.read_file("sub"), "目录")
        expect_error(lambda: file_operator.read_file("../../secret.txt"), "../ 越界")
        expect_error(lambda: file_operator.read_file("C:/Windows/win.ini"), "绝对路径")
        expect_error(lambda: file_operator.read_file("D:\\docs\\test.pdf"), "D:\\ 盘符路径")
        expect_error(lambda: file_operator.read_file(""), "空路径")

        print("[2] prepare_send_file")
        p = file_operator.prepare_send_file("a.txt")
        assert p["name"] == "a.txt" and p["mime"] == "text/plain", p
        assert p["data"] == b"HELLO=1\n" and p["size"] == 8 and p["path"] == "a.txt", p
        print(f"  {p['name']} {p['mime']} {p['size']}B")
        config.MAX_FILE_MB = 1
        expect_error(lambda: file_operator.prepare_send_file("big.dat"), "超过 MAX_FILE_MB")

        print("[3] send_file (桩 manager)")
        stub = StubManager()
        res = await file_operator.send_file(stub, "a.txt")
        assert res["ok"] and res["attached"] and res["name"] == "a.txt", res
        assert len(stub.attached) == 1 and stub.attached[0]["data"] == b"HELLO=1\n", stub.attached
        assert "path" not in stub.attached[0], "attach_files 只需要 name/mime/data"
        print(f"  附加成功: {res}")
        await expect_async_error(file_operator.send_file(StubManager(state="idle"), "a.txt"),
                                 "未登录")
        await expect_async_error(file_operator.send_file(StubManager(alive=False), "a.txt"),
                                 "窗口已关闭")
        await expect_async_error(file_operator.send_file(StubManager(attach_ok=False), "a.txt"),
                                 "站点没有文件输入框")
        await expect_async_error(file_operator.send_file(StubManager(), "nope.txt"), "文件不存在")

        print("[4] /api/file/operate 路由")
        assert any(getattr(rt, "path", "") == "/api/file/operate" for rt in server.app.routes), \
            "路由没注册"
        stub = StubManager()
        server.manager = stub
        resp = await server.api_file_operate(
            server.FileOperateRequest(action="read_file", path="a.txt"))
        assert resp["ok"] and resp["action"] == "read_file", resp
        assert resp["result"]["text"] == "HELLO=1\n", resp
        print(f"  read_file -> {resp['result']}")

        resp = await server.api_file_operate(
            server.FileOperateRequest(action="send_file", path="a.txt"))
        assert resp["ok"] and resp["result"]["attached"], resp
        assert stub.busy is False, "send_file 结束后 busy 必须复位"
        print(f"  send_file -> {resp['result']}")

        resp = await server.api_file_operate(
            server.FileOperateRequest(action="delete_everything", path="a.txt"))
        assert resp.status_code == 400 and body(resp)["allowed_actions"] == \
            ["read_file", "send_file"], body(resp)
        resp = await server.api_file_operate(
            server.FileOperateRequest(action="read_file", path="../../secret.txt"))
        assert resp.status_code == 400 and body(resp)["error"], body(resp)
        print(f"  越界/非法 action -> 400: {body(resp)['error']}")

        print("[5] 本地模型必须完全没被调用")
        called = []

        async def boom(*a, **kw):
            called.append(a)
            raise AssertionError("本地模型被调用了!")

        server.planner.ask, server.planner.ask_stream = boom, boom
        await server.api_file_operate(server.FileOperateRequest(action="read_file", path="a.txt"))
        await server.api_file_operate(server.FileOperateRequest(action="send_file", path="a.txt"))
        assert not called, called
        print("  planner.ask / ask_stream 一次都没被调用 OK")

    finally:
        config.MAX_FILE_MB = orig_max
        workspace.use_root(orig_root)
        shutil.rmtree(TMP_WS, ignore_errors=True)

    print("FILE_OPERATOR_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
