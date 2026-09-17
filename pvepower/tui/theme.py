"""配色主题。

默认是亮色：取自 ui-ux-pro-max 的 "Data-Dense Dashboard / Light" 设计系统
（Primary #1E40AF、Accent #D97706、Background #F8FAFC、Muted #E9EEF6、
Destructive #DC2626，蓝色承载数据、琥珀色承载强调）。

终端没法直接吃十六进制，所以每个设计色都折算成 xterm-256 调色板里最近的
一格。**故意不调用 curses.init_color()**：那会改写终端自己的调色板，很多
终端在退出后不会还原，等于把用户其它窗口的颜色一起改了。挑现成的格子虽然
有一点色差，但不会留下副作用。

亮色底最容易踩的坑是对比度 —— 浅色上的黄、亮绿、亮红都不够看。所以每个
角色分成「文字色」和「图形色」两档：文字档一律做到 WCAG AA 的 4.5:1，
图形档（进度条、迷你折线这些非文字元素）按 3:1 收。
`tests/test_theme.py` 会把这条规则跑成断言，改色时改坏了会直接红。

8 色终端（PVE 物理控制台就是）没有这些格子，另走一套基础色映射。
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------- 色号

# 亮色主题。注释里的对比度是相对 bg(#eeeeee) 算的。
LIGHT = {
    "bg":         255,  # #eeeeee 纸白，比纯白柔和，长时间看不刺眼
    "surface":    254,  # #e4e4e4 表头 / 分组带，对应设计里的 Muted
    "fg":          18,  # #000087 深藏青正文        13.4:1
    "dim":        240,  # #585858 次要文字 / 标签     6.1:1
    "primary":     25,  # #005faf 主色，标题和边框    5.6:1
    "primary_fg": 231,  # #ffffff 主色带上的白字      6.5:1
    "data":        26,  # #005fd7 数据值蓝            5.0:1
    "ok":          22,  # #005f00 正常（文字）        6.9:1
    "ok_bar":      28,  # #008700 正常（图形）        4.1:1
    "warn":        94,  # #875f00 告警（文字）        4.9:1
    "warn_bar":   166,  # #d75f00 告警（图形）≈Accent 3.3:1
    "crit":       124,  # #af0000 故障（文字）        6.4:1
    "crit_bar":   160,  # #d70000 故障（图形）        4.7:1
    "border":      67,  # #5f87af 边框蓝灰            3.2:1
    "track":      253,  # #dadada 进度条未填充部分
    "sel_bg":     153,  # #afd7ff 选中行底色
    "sel_fg":      18,  # 选中行文字                 10.4:1 on sel_bg
}

# 暗色主题，给 PVE_POWER_THEME=dark 用。同样的角色、同样的对比度规矩。
DARK = {
    "bg":         234,  # #1c1c1c
    "surface":    236,  # #303030
    "fg":         252,  # #d0d0d0
    "dim":        245,  # #8a8a8a
    "primary":     75,  # #5fafff
    "primary_fg": 234,
    "data":        81,  # #5fd7ff
    "ok":          77,  # #5fd75f
    "ok_bar":      77,
    "warn":       214,  # #ffaf00
    "warn_bar":   214,
    "crit":       203,  # #ff5f5f
    "crit_bar":   203,
    "border":      67,  # #5f87af
    "track":      238,  # #444444
    "sel_bg":      24,  # #005f87
    "sel_fg":     231,
}


def active_palette() -> dict:
    """亮色为默认；PVE_POWER_THEME=dark 可以切暗色。"""
    if os.environ.get("PVE_POWER_THEME", "").strip().lower() == "dark":
        return DARK
    return LIGHT


# ---------------------------------------------- xterm-256 → RGB，用于算对比度

def xterm_rgb(index: int) -> tuple[int, int, int]:
    """xterm-256 色号对应的 RGB。测试用它验算对比度。"""
    basic = [
        (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
        (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
        (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
        (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ]
    if index < 16:
        return basic[index]
    if index < 232:
        steps = (0, 95, 135, 175, 215, 255)
        i = index - 16
        return steps[i // 36], steps[(i // 6) % 6], steps[i % 6]
    grey = 8 + 10 * (index - 232)
    return grey, grey, grey


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        v = value / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: int, bg: int) -> float:
    """两个 xterm 色号之间的 WCAG 对比度。"""
    a, b = _luminance(xterm_rgb(fg)), _luminance(xterm_rgb(bg))
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)
