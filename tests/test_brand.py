"""品牌与版本元数据的单测（``app/brand.py`` 的唯一事实来源守卫）。

守的是三件事：
1. 产品名/技术标识/版本号的形状正确（人看的带空格，机器用的不带）；
2. ``app.brand`` 与 ``app.__version__`` 不会各说各话；
3. 产品名在整个 ``app/`` 里**只出现一次**——其他模块必须是它的投影，
   否则改名那天必然漏掉一处。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app as app_pkg  # noqa: E402
from app import brand  # noqa: E402
from app.windows import menus  # noqa: E402

DISPLAY_NAME = "Desk" + "\u0020" + "CountChen"  # 显式空格，避免编辑时被折叠


class BrandIdentityTest(unittest.TestCase):
    def test_display_name_has_space_for_humans(self):
        self.assertEqual(brand.PRODUCT_NAME, DISPLAY_NAME)
        self.assertTrue(brand.PRODUCT_NAME.endswith("CountChen"))
        self.assertIn(" ", brand.PRODUCT_NAME)

    def test_slug_has_no_space_for_paths(self):
        self.assertEqual(brand.PRODUCT_SLUG, "DeskCountChen")
        self.assertNotIn(" ", brand.PRODUCT_SLUG)
        self.assertTrue(brand.PRODUCT_SLUG.isascii())
        # exe 名 / 目录名直接用它拼，必须是合法文件名
        self.assertNotRegex(brand.PRODUCT_SLUG, r'[\\/:*?"<>|]')

    def test_version_single_source(self):
        self.assertEqual(brand.VERSION, app_pkg.__version__)
        self.assertRegex(brand.VERSION, r"^\d+\.\d+\.\d+$")

    def test_author_and_copyright(self):
        self.assertEqual(brand.AUTHOR, "CountChen")
        self.assertIn("CountChen", brand.COPYRIGHT)
        self.assertIn("2026", brand.COPYRIGHT)
        self.assertEqual(brand.LICENSE_NAME, "Apache-2.0")

    def test_app_user_model_id_follows_slug(self):
        self.assertEqual(brand.APP_USER_MODEL_ID, f"CountChen.{brand.PRODUCT_SLUG}")


class BrandProjectionTest(unittest.TestCase):
    def test_version_line_contains_name_version_and_build(self):
        line = brand.version_line()
        self.assertIn(brand.PRODUCT_NAME, line)
        self.assertIn(brand.VERSION, line)
        self.assertIn("build", line)
        self.assertIn(brand.BUILD_COMMIT, line)
        self.assertEqual(len(line.splitlines()), 1)  # 单行，便于 --version 直出

    def test_build_commit_never_empty(self):
        self.assertTrue(brand.BUILD_COMMIT.strip())
        self.assertNotIn(" ", brand.BUILD_COMMIT)

    def test_about_text_covers_legal_fields(self):
        text = brand.about_text()
        for needle in (brand.PRODUCT_NAME, brand.VERSION, brand.COPYRIGHT,
                       brand.LICENSE_NAME, brand.HOMEPAGE, brand.BUILD_COMMIT):
            self.assertIn(needle, text)

    def test_display_name_defined_only_in_brand(self):
        """全 app 包内（除 brand.py）不得再写死产品名。"""
        offenders = []
        for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
            if path.name == "brand.py":
                continue
            if DISPLAY_NAME in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(offenders, [], f"产品名字面量重复出现: {offenders}")

    def test_main_module_has_no_hardcoded_version(self):
        text = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r'APP_VERSION\s*=\s*"\d+\.\d+\.\d+"')
        self.assertIn("brand.VERSION", text)


class TrayAboutEntryTest(unittest.TestCase):
    def test_about_action_declared_and_exported(self):
        self.assertEqual(menus.TRAY_ACTION_ABOUT, "about")
        self.assertIn("TRAY_ACTION_ABOUT", menus.__all__)

    def test_tray_menu_contains_about_entry(self):
        from tests.qt_support import qt_app

        qt_app()
        hits = []

        def dispatch(action, cat_id):
            hits.append(action)

        menu = menus.build_tray_menu(None, dispatch, ())
        labels = [a.text() for a in menu.actions()]
        self.assertTrue(any(brand.PRODUCT_NAME in t for t in labels),
                        f"托盘菜单缺少关于项: {labels}")
        about = next(a for a in menu.actions() if brand.PRODUCT_NAME in a.text())
        about.trigger()
        self.assertEqual(hits, [menus.TRAY_ACTION_ABOUT])


if __name__ == "__main__":
    unittest.main(verbosity=2)
