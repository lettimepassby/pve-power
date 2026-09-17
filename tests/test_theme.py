"""配色的对比度断言。

亮色主题最容易坏在这里：改一个色号看着「更好看」，实际把对比度压到了
看不清。WCAG AA 要求正文 4.5:1、非文字图形 3:1，这个文件把那条线钉死，
以后谁调色调过头会直接红。
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pvepower.tui.theme import DARK, LIGHT, contrast, xterm_rgb  # noqa: E402

# 承载文字的角色，按 AA 正文标准
TEXT_ROLES = ("fg", "dim", "primary", "data", "ok", "warn", "crit")
# 只画条形图 / 迷你折线的角色，按非文字图形标准
GRAPHIC_ROLES = ("ok_bar", "warn_bar", "crit_bar", "border")


class TestPaletteContrast(unittest.TestCase):
    def _check(self, name, palette):
        for role in TEXT_ROLES:
            ratio = contrast(palette[role], palette["bg"])
            self.assertGreaterEqual(
                ratio, 4.5,
                f"{name}.{role}（色号 {palette[role]}）对背景只有 {ratio:.2f}:1，"
                "正文需要 4.5:1",
            )
        for role in GRAPHIC_ROLES:
            ratio = contrast(palette[role], palette["bg"])
            self.assertGreaterEqual(
                ratio, 3.0,
                f"{name}.{role}（色号 {palette[role]}）对背景只有 {ratio:.2f}:1，"
                "图形元素需要 3:1",
            )

    def test_light_palette_meets_aa(self):
        self._check("LIGHT", LIGHT)

    def test_dark_palette_meets_aa(self):
        self._check("DARK", DARK)

    def test_inverted_pairs_meet_aa(self):
        """反白的两组（表头、选中行）也要够看。"""
        for name, p in (("LIGHT", LIGHT), ("DARK", DARK)):
            self.assertGreaterEqual(
                contrast(p["primary_fg"], p["primary"]), 4.5,
                f"{name} 表头文字对主色底不够 4.5:1")
            self.assertGreaterEqual(
                contrast(p["sel_fg"], p["sel_bg"]), 4.5,
                f"{name} 选中行文字对选中底不够 4.5:1")

    def test_light_is_actually_light(self):
        """亮色主题的底得真是浅的，否则名不副实。"""
        r, g, b = xterm_rgb(LIGHT["bg"])
        self.assertGreater(min(r, g, b), 200)
        r, g, b = xterm_rgb(DARK["bg"])
        self.assertLess(max(r, g, b), 80)

    def test_palettes_define_the_same_roles(self):
        """两套主题必须角色齐全，否则切换时 KeyError。"""
        self.assertEqual(set(LIGHT), set(DARK))

    def test_all_indices_in_range(self):
        for name, p in (("LIGHT", LIGHT), ("DARK", DARK)):
            for role, idx in p.items():
                self.assertTrue(0 <= idx < 256, f"{name}.{role} 色号越界：{idx}")


class TestThemeSelection(unittest.TestCase):
    def test_default_is_light(self):
        from pvepower.tui.theme import active_palette
        saved = os.environ.pop("PVE_POWER_THEME", None)
        try:
            self.assertIs(active_palette(), LIGHT)
        finally:
            if saved is not None:
                os.environ["PVE_POWER_THEME"] = saved

    def test_env_selects_dark(self):
        from pvepower.tui.theme import active_palette
        saved = os.environ.get("PVE_POWER_THEME")
        os.environ["PVE_POWER_THEME"] = "dark"
        try:
            self.assertIs(active_palette(), DARK)
        finally:
            if saved is None:
                os.environ.pop("PVE_POWER_THEME", None)
            else:
                os.environ["PVE_POWER_THEME"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
