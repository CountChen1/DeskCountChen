"""单实例唤醒协议（QLocalServer / QLocalSocket，不引入 pywin32 / WM_COPYDATA）。

场景：用户双击桌面上的分类快捷方式 → Windows 起一个新的 ``pythonw main.py
--open <分类目录名>`` 进程。此时第一个实例已经持有互斥量，第二个实例必须
**把"要打开哪个分类 + 鼠标在屏幕上的落点"传给第一个实例然后静默退出**，
绝不能再弹"已在运行"的对话框（否则每次双击桌面图标都弹框）。

协议：一条 UTF-8 JSON（以换行结尾），字段固定为

    {"action": "open", "dir": "<分类目录名>", "x": 123, "y": 456}

服务端名 ``DeskCountChen.single.v1``（带版本号，将来协议升级不会和旧实例串话）。
"""

from __future__ import annotations

import json
import logging

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

SERVER_NAME = "DeskCountChen.single.v1"
ACTION_OPEN = "open"
CONNECT_TIMEOUT_MS = 800       # 连接已有实例的超时：失败才降级为提示框
MAX_REQUEST_BYTES = 64 * 1024  # 协议防御：超长请求直接丢弃
_MAX_RETIRED_SOCKETS = 64      # 已收尾套接字的强引用上限（防无限增长）

__all__ = [
    "SERVER_NAME",
    "ACTION_OPEN",
    "encode_request",
    "decode_request",
    "send_open_request",
    "SingleInstanceServer",
]


def encode_request(dir_name: str, x: int = 0, y: int = 0) -> bytes:
    """编码一条"打开分类"请求（UTF-8 JSON + 换行）。"""
    payload = {
        "action": ACTION_OPEN,
        "dir": str(dir_name or ""),
        "x": int(x),
        "y": int(y),
    }
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def decode_request(raw: bytes | bytearray | None) -> dict | None:
    """解码并校验请求；非法请求返回 None（绝不抛异常）。"""
    if not raw:
        return None
    try:
        text = bytes(raw).decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 - 解码失败视为非法请求
        return None
    if not text or len(text) > MAX_REQUEST_BYTES:
        return None
    # 只取最后一行（容忍客户端多写了内容）
    text = text.splitlines()[-1]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("单实例请求不是合法 JSON: %r", text[:120])
        return None
    if not isinstance(data, dict) or data.get("action") != ACTION_OPEN:
        logger.warning("单实例请求 action 非法: %r", data)
        return None

    def _as_int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    dir_name = str(data.get("dir") or "")
    if not dir_name:
        logger.warning("单实例请求缺少分类目录名，已忽略")
        return None
    return {
        "action": ACTION_OPEN,
        "dir": dir_name,
        "x": _as_int(data.get("x")),
        "y": _as_int(data.get("y")),
    }


def send_open_request(
    dir_name: str,
    x: int = 0,
    y: int = 0,
    server_name: str = SERVER_NAME,
    timeout_ms: int = CONNECT_TIMEOUT_MS,
) -> bool:
    """把"打开分类"请求发给已运行实例；成功写出返回 True。

    关键细节（实测踩坑）：写完立刻让 QLocalSocket 析构会**丢包**——
    套接字此时仍处于 ClosingState 且有待发字节。这里用局部事件循环等到
    对端读完并断开（或超时）才返回，保证请求真的送达。
    """
    budget = max(100, int(timeout_ms))
    sock = QLocalSocket()
    sock.connectToServer(server_name)
    if not sock.waitForConnected(budget):
        logger.info("连接已有实例失败（%s）: %s", server_name, sock.errorString())
        return False

    written = 0
    loop = QEventLoop()
    # 兜底定时器必须是 loop 的成员（不能用 QTimer.singleShot(..., loop.quit)）：
    # singleShot 的回调在 loop 析构后仍可能触发，等于在已释放的 QEventLoop 上调
    # quit()，实测会直接 access violation。用显式 QTimer 并在 finally 里 stop。
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    try:
        written = sock.write(encode_request(dir_name, x, y))
        sock.flush()
        sock.disconnected.connect(loop.quit)
        guard.start(budget)                    # 兜底：对端不主动断开也能收尾
        sock.disconnectFromServer()
        loop.exec()
    except Exception as exc:  # noqa: BLE001
        logger.warning("向已有实例发送请求失败: %s", exc)
        return False
    finally:
        guard.stop()
        try:
            guard.timeout.disconnect(loop.quit)
            sock.disconnected.disconnect(loop.quit)
        except (RuntimeError, TypeError):
            pass  # 已断开 / 套接字已销毁，忽略

    if written <= 0:
        logger.warning("唤醒请求未写出（%s）: %s", server_name, sock.errorString())
        return False
    pending = sock.bytesToWrite()
    if pending:
        logger.warning("唤醒请求仍有 %d 字节未送达（对端可能未处理）", pending)
    logger.info("已请求已有实例打开分类: dir=%s 落点=(%s,%s)", dir_name, x, y)
    return True


class SingleInstanceServer(QObject):
    """第一实例的监听端：收到请求后转发 (分类目录名, x, y)。"""

    open_requested = Signal(str, int, int)

    def __init__(self, server_name: str = SERVER_NAME, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.server_name = server_name
        self._server: QLocalServer | None = None
        # 每个连接一个读缓冲；用套接字对象本身做键（避免 id() 复用带来的错配）
        self._buffers: dict[QLocalSocket, bytearray] = {}
        # 已收尾连接的强引用（见 _retire 的注释：这里绝不能 deleteLater）
        self._retired: set[QLocalSocket] = set()

    def start(self) -> bool:
        """开始监听（幂等）；失败只记日志并返回 False。"""
        if self._server is not None:
            return True
        try:
            server = QLocalServer(self)
            if not server.listen(self.server_name):
                # 上一次异常退出可能留下同名残端点：清理后重试一次。
                # 绝不在首次尝试前就 removeServer —— 那会把**正在运行**的
                # 实例的端点抢掉（第二实例就会变成新的消息接收者）。
                QLocalServer.removeServer(self.server_name)
                if not server.listen(self.server_name):
                    logger.warning("QLocalServer 监听失败（%s）: %s",
                                   self.server_name, server.errorString())
                    return False
            server.newConnection.connect(self._on_new_connection)
            self._server = server
            logger.info("单实例唤醒服务已启动: %s", self.server_name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动单实例唤醒服务异常: %s", exc)
            return False

    def stop(self) -> None:
        """停止监听并断开已连接套接字（幂等）。"""
        server = self._server
        self._server = None
        self._buffers.clear()
        for sock in list(self._retired):
            try:
                sock.close()
            except RuntimeError:
                pass  # 已被底层销毁，忽略
        self._retired.clear()
        if server is None:
            return
        try:
            server.close()
            QLocalServer.removeServer(self.server_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭单实例唤醒服务异常: %s", exc)

    # -- 内部 ---------------------------------------------------------------

    def _retire(self, sock: QLocalSocket) -> None:
        """一个连接处理完毕后的安全收尾。

        实测踩坑（会直接把进程打崩）：对 ``nextPendingConnection()`` 拿到的
        套接字调 ``deleteLater()``，如果在 DeferredDelete 事件被派发之前服务端
        对象先析构（进程退出、或服务端被回收），下一次事件派发就会踩到已释放
        内存 → access violation。所以这里**只关闭、不删除**，并保留强引用，
        由 SingleInstanceServer 析构时统一释放。每次唤醒只产生一个套接字，
        上限也做了裁剪，量级完全可忽略。
        """
        self._buffers.pop(sock, None)
        try:
            sock.close()
        except RuntimeError:
            return  # 底层已销毁，无需再持有
        self._retired.add(sock)
        if len(self._retired) > _MAX_RETIRED_SOCKETS:
            # 只丢 Python 强引用：套接字的 C++ 所有权在 QLocalServer（父对象）手上，
            # 丢弃包装对象不会删掉 C++ 对象，也不会留下悬空事件。
            self._retired.clear()

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                break
            self._buffers[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))
            if sock.bytesAvailable():
                self._on_ready_read(sock)

    def _collect(self, sock: QLocalSocket) -> bytearray | None:
        """把套接字上已到达的字节追加进该连接的读缓冲。"""
        buffer = self._buffers.get(sock)
        if buffer is None:
            return None
        try:
            chunk = bytes(sock.readAll())
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取单实例请求失败: %s", exc)
            return buffer
        if chunk:
            buffer.extend(chunk)
        return buffer

    def _on_ready_read(self, sock: QLocalSocket) -> None:
        buffer = self._collect(sock)
        if buffer is None:
            return
        if len(buffer) > MAX_REQUEST_BYTES:
            logger.warning("单实例请求超长，已丢弃")
            self._buffers.pop(sock, None)
            sock.disconnectFromServer()
            return
        if b"\n" not in buffer:
            return  # 等下一批数据
        request = decode_request(buffer)
        self._buffers.pop(sock, None)
        sock.disconnectFromServer()
        if request is None:
            return
        self.open_requested.emit(request["dir"], request["x"], request["y"])
    def _on_disconnected(self, sock: QLocalSocket) -> None:
        """客户端没发换行就断开：先把残余字节读完，再当完整请求试一次。

        第二实例是"写完就断开"的（它不等回执），所以断开时数据可能还在
        缓冲区里、readyRead 还没派发 —— 必须在这里再 drain 一次，否则
        唤醒请求会静默丢失。
        """
        buffer = self._collect(sock) or bytearray()
        self._retire(sock)
        if not buffer:
            return
        request = decode_request(buffer)
        if request is not None:
            self.open_requested.emit(request["dir"], request["x"], request["y"])
