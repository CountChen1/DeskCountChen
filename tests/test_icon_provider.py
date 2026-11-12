"""图标提取回归（v1.4.1：面板显示全部文件类型后的取图约定）。

覆盖：
1. 任意文件（.txt/.pdf/.docx/无扩展名）都能拿到**非空**图标；
2. ``.url`` 优先使用 INI 里的 ``IconFile=``（与"没有 IconFile"的结果不同）；
3. ``.url`` 内容损坏 / IconFile 指向不存在的文件 → 不抛异常且仍有非空图标；
4. 目录、失效路径仍然非空（v1.3 约定：不灰显、不预检、不提示）。

全部在临时目录内造数据，不碰用户目录。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.qt_support import qt_app

from PySide6.QtGui import QColor, QPixmap  # noqa: E402

from app.icon_provider import IconCache  # noqa: E402

ICON_PX = 48


def _has_visible_pixel(pm: QPixmap) -> bool:
    """图像里是否存在非全透明像素（用来证伪"空白格"）。"""
    img = pm.toImage()
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            if QColor(img.pixel(x, y)).alpha() > 8:
                return True
    return False


class FileTypeIconTests(unittest.TestCase):
    """非 .lnk/.exe 文件的图标。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qt_app()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_icon_")
        self.root = Path(self._tmp.name)
        self.cache = IconCache()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _pixmap(self, path: Path) -> QPixmap:
        pm = self.cache.pixmap(path, ICON_PX)
        self.assertFalse(pm.isNull(), f"取图为空（会出现空白格）：{path.name}")
        self.assertEqual((pm.width(), pm.height()), (ICON_PX, ICON_PX))
        self.assertTrue(_has_visible_pixel(pm), f"图标全透明（等于空白格）：{path.name}")
        return pm

    def test_text_file_has_icon(self) -> None:
        f = self.root / "帝国时代笔记.txt"
        f.write_text("笔记内容", encoding="utf-8")
        self._pixmap(f)

    def test_pdf_docx_have_icon(self) -> None:
        for name in ("manual.pdf", "方案.docx", "data.xlsx"):
            f = self.root / name
            f.write_bytes(b"stub")
            self._pixmap(f)

    def test_file_without_extension_has_icon(self) -> None:
        f = self.root / "LICENSE"
        f.write_text("x", encoding="utf-8")
        self._pixmap(f)

    def test_missing_path_still_returns_icon(self) -> None:
        """失效路径也必须非空（v1.3：不灰显/不预检/不提示）。"""
        self._pixmap(self.root / "不存在的东西.txt")

    def test_directory_has_icon(self) -> None:
        d = self.root / "工具"
        d.mkdir()
        self._pixmap(d)


class UrlIconTests(unittest.TestCase):
    """.url（Internet 快捷方式）的取图策略。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qt_app()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_icon_url_")
        self.root = Path(self._tmp.name)
        self.cache = IconCache()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_url(self, name: str, icon_file: str | None, url: str = "https://example.com"):
        lines = ["[InternetShortcut]", f"URL={url}"]
        if icon_file is not None:
            lines.append(f"IconFile={icon_file}")
            lines.append("IconIndex=0")
        path = self.root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_url_reads_iconfile(self) -> None:
        """有 IconFile 时，取到的图必须与"没有 IconFile"时不同（证明解析生效）。"""
        ico_source = Path(self.root / "custom.ico")
        pm_src = QPixmap(64, 64)
        pm_src.fill(QColor("#FF3B30"))
        self.assertTrue(pm_src.save(str(ico_source), "ICO"), "造 .ico 失败")

        with_icon = self._write_url("with.url", str(ico_source), "https://a.example")
        without_icon = self._write_url("without.url", None, "https://b.example")

        pm_with = self.cache.pixmap(with_icon, ICON_PX)
        pm_without = self.cache.pixmap(without_icon, ICON_PX)
        self.assertFalse(pm_with.isNull())
        self.assertFalse(pm_without.isNull())
        self.assertNotEqual(
            pm_with.toImage().constBits().tobytes(),
            pm_without.toImage().constBits().tobytes(),
            "IconFile 未生效：取到的图与系统关联图完全相同",
        )

    def test_url_ini_parser(self) -> None:
        ico = self.root / "x.ico"
        path = self._write_url("p.url", str(ico))
        ini = IconCache._read_url_ini(path)
        self.assertEqual(ini.get("url"), "https://example.com")
        self.assertEqual(ini.get("iconfile"), str(ico))
        self.assertEqual(ini.get("iconindex"), "0")

    def test_url_without_iconfile_still_has_icon(self) -> None:
        path = self._write_url("plain.url", None)
        pm = self.cache.pixmap(path, ICON_PX)
        self.assertFalse(pm.isNull())
        self.assertTrue(_has_visible_pixel(pm))

    def test_url_with_broken_iconfile_falls_back(self) -> None:
        path = self._write_url("broken.url", str(self.root / "missing.ico"))
        pm = self.cache.pixmap(path, ICON_PX)
        self.assertFalse(pm.isNull(), "IconFile 失效时必须回退系统图标，不能是空白格")
        self.assertTrue(_has_visible_pixel(pm))

    def test_garbage_url_does_not_raise(self) -> None:
        path = self.root / "garbage.url"
        path.write_bytes(b"\xff\xfe\x00garbage not ini at all")
        pm = self.cache.pixmap(path, ICON_PX)
        self.assertFalse(pm.isNull())

    def test_url_with_relative_iconfile(self) -> None:
        """少数工具写相对路径（相对 .url 所在目录）。"""
        ico = self.root / "rel.ico"
        pm_src = QPixmap(64, 64)
        pm_src.fill(QColor("#34C759"))
        self.assertTrue(pm_src.save(str(ico), "ICO"))
        path = self._write_url("rel.url", "rel.ico", "https://c.example")
        pm = self.cache.pixmap(path, ICON_PX)
        self.assertFalse(pm.isNull())
        self.assertTrue(_has_visible_pixel(pm))


if __name__ == "__main__":
    unittest.main()
