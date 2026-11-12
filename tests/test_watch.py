"""目录实时刷新测试（红线 9）：QFileSystemWatcher + 轮询 → changed 信号。

用 offscreen QApplication 跑真实事件循环（不弹窗，可超时退出）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.folder_model import FolderModel  # noqa: E402
from tests.qt_support import qt_app  # noqa: E402

WAIT_SECONDS = 5.0


def pump(app, seconds: float) -> None:
    """跑事件循环若干秒（用于等待 watcher/定时器）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


class WatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qt_app()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_watch_")
        self.dir = Path(self._tmp.name)
        self.fm = FolderModel(self.dir, poll_ms=500)
        self.signals = 0
        self.fm.changed.connect(self._on_changed)
        self.fm.reload()
        self.fm.start_watch()

    def tearDown(self) -> None:
        self.fm.stop_watch()
        self._tmp.cleanup()

    def _on_changed(self) -> None:
        self.signals += 1

    def _wait_for_change(self) -> bool:
        end = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < end:
            QCoreApplication.processEvents()
            time.sleep(0.02)
            if self.signals:
                return True
        return False

    def test_adding_file_emits_changed(self) -> None:
        (self.dir / "new.lnk").write_bytes(b"")
        self.assertTrue(self._wait_for_change(), "向目录新增文件后应在数秒内触发 changed")
        self.assertIn("new.lnk", [e.name for e in self.fm.reload()])

    def test_removing_file_emits_changed(self) -> None:
        target = self.dir / "gone.lnk"
        target.write_bytes(b"")
        self.fm.reload()
        pump(self.app, 0.3)
        self.signals = 0
        target.unlink()
        self.assertTrue(self._wait_for_change(), "删除文件后应触发 changed")

    def test_watch_is_idempotent_and_stoppable(self) -> None:
        self.fm.start_watch()
        self.fm.start_watch()
        self.fm.stop_watch()
        self.fm.stop_watch()
        # 停止后新增文件不应再触发
        self.signals = 0
        (self.dir / "after_stop.lnk").write_bytes(b"")
        pump(self.app, 1.5)
        self.assertEqual(self.signals, 0, "stop_watch 之后不应再有 changed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
