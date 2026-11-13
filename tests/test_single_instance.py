"""单实例唤醒协议单元测试（协议编解码 + 本机 QLocalServer/QLocalSocket 往返）。

这是"双击桌面图标 → 不弹框、直接打开对应分类面板"的核心链路：
第二实例把 ``{"action":"open","dir":...,"x":...,"y":...}`` 发给第一实例后静默退出。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtNetwork import QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.single_instance import (  # noqa: E402
    ACTION_OPEN,
    SingleInstanceServer,
    decode_request,
    encode_request,
    send_open_request,
)
from tests.qt_support import qt_app  # noqa: E402


def pump_until(app, predicate, timeout: float = 3.0) -> bool:
    """泵事件循环直到 predicate() 为真或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def unique_server_name() -> str:
    return f"DeskCountChen.test.{uuid.uuid4().hex[:8]}"


class ProtocolTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        raw = encode_request("常用", 12, 34)
        self.assertTrue(raw.endswith(b"\n"), "请求必须以换行结尾（行协议）")
        self.assertEqual(decode_request(raw),
                         {"action": ACTION_OPEN, "dir": "常用", "x": 12, "y": 34})

    def test_chinese_dir_name_survives(self) -> None:
        request = decode_request(encode_request("工作 资料"))
        self.assertIsNotNone(request)
        self.assertEqual(request["dir"], "工作 资料")

    def test_decode_rejects_garbage(self) -> None:
        for raw in (b"", b"\n", b"not json\n", b'{"action":"other"}\n',
                    b'{"action":"open"}\n', b"[1,2,3]\n", None):
            self.assertIsNone(decode_request(raw), f"非法请求应被拒绝: {raw!r}")

    def test_decode_coerces_bad_coordinates(self) -> None:
        request = decode_request(b'{"action":"open","dir":"X","x":"abc","y":null}\n')
        self.assertEqual((request["x"], request["y"]), (0, 0))

    def test_decode_takes_last_line(self) -> None:
        raw = b'{"action":"open","dir":"A","x":1,"y":2}\n{"action":"open","dir":"B","x":3,"y":4}\n'
        self.assertEqual(decode_request(raw)["dir"], "B")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qt_app()

    def setUp(self) -> None:
        self.name = unique_server_name()
        self.server = SingleInstanceServer(self.name)
        self.addCleanup(self.server.stop)

    def test_server_delivers_open_request(self) -> None:
        self.assertTrue(self.server.start(), "QLocalServer 监听失败")
        received: list[tuple[str, int, int]] = []
        self.server.open_requested.connect(
            lambda d, x, y: received.append((d, x, y)))

        sock = QLocalSocket()
        sock.connectToServer(self.name)
        self.assertTrue(sock.waitForConnected(1500), "客户端连接失败")
        sock.write(encode_request("常用", 100, 200))
        sock.flush()
        sock.waitForBytesWritten(1000)

        self.assertTrue(pump_until(self.app, lambda: bool(received)), "服务端没收到请求")
        self.assertEqual(received, [("常用", 100, 200)])
        sock.disconnectFromServer()

    def test_send_open_request_helper_reaches_server(self) -> None:
        self.assertTrue(self.server.start())
        received: list[tuple[str, int, int]] = []
        self.server.open_requested.connect(
            lambda d, x, y: received.append((d, x, y)))

        self.assertTrue(send_open_request("娱乐", 7, 9, server_name=self.name))
        self.assertTrue(pump_until(self.app, lambda: bool(received)))
        self.assertEqual(received, [("娱乐", 7, 9)])

    def test_server_start_is_idempotent(self) -> None:
        self.assertTrue(self.server.start())
        self.assertTrue(self.server.start(), "重复 start 应直接返回成功")

    def test_send_to_absent_server_returns_false(self) -> None:
        """没有实例在跑时，唤醒必须安静失败（由调用方决定是否提示）。"""
        self.assertFalse(send_open_request("常用", 0, 0,
                                           server_name=unique_server_name(),
                                           timeout_ms=200))

    def test_malformed_payload_is_ignored(self) -> None:
        self.assertTrue(self.server.start())
        received: list[tuple[str, int, int]] = []
        self.server.open_requested.connect(
            lambda d, x, y: received.append((d, x, y)))

        sock = QLocalSocket()
        sock.connectToServer(self.name)
        self.assertTrue(sock.waitForConnected(1500))
        sock.write(b"garbage-without-newline")
        sock.flush()
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        # 断开时把已收内容当完整请求试一次 → 解码失败 → 不发信号
        self.assertFalse(pump_until(self.app, lambda: bool(received), timeout=0.6))


if __name__ == "__main__":
    unittest.main(verbosity=2)
