"""Shared curses widgets and drawing helpers.

Everything here is defensive about terminal size: curses raises on any
write that touches the last cell of the screen, and a PVE console is
often resized mid-session. `safe_addstr` is the only way this project
writes text.

The interface is in Chinese, so every width calculation counts terminal
*columns*, not characters: a CJK glyph occupies two cells. Using len()
would leave boxes broken and columns misaligned, so `cwidth`, `truncate`
and `pad` below are the only correct way to measure or fit text.
"""

from __future__ import annotations

import curses
import datetime as dt
from typing import Optional, Sequence

from ..textwidth import clip, cwidth
from ..textwidth import lpad as _lpad
from ..textwidth import rpad as _rpad
from .theme import active_palette

# Colour pair ids.
CP_NORMAL = 1
CP_TITLE = 2
CP_OK = 3
CP_WARN = 4
CP_CRIT = 5
CP_DIM = 6
CP_HIGHLIGHT = 7
CP_ACCENT = 8
CP_HEADER = 9
# 亮色主题新增的角色。前九个是既有视图在用的，不动。
CP_BORDER = 10      # 面板边框：比标题淡，不跟数据抢注意力
CP_SURFACE = 11     # 表头色带，对应设计系统里的 Muted
CP_TRACK = 12       # 进度条没填满的那一截
CP_OK_BAR = 13      # 下面四个是「图形档」：条形图、迷你折线这类
CP_WARN_BAR = 14    # 非文字元素，按 3:1 收，颜色比文字档鲜一些
CP_CRIT_BAR = 15
CP_ACCENT_BAR = 16

# Eighth-block glyphs for bar charts and sparklines.
BLOCKS = " ▁▂▃▄▅▆▇█"

# 主题是否真的生效了。8 色终端拿不到亮色底（见 init_colors），
# 视图靠这个决定要不要画色带之类只在亮色下好看的装饰。
THEMED = False


def init_colors() -> bool:
    """建立颜色对。单色终端返回 False。

    只有 256 色终端才上亮色主题。8 色终端（PVE 物理控制台就是）做不到：
    那套调色板里的绿、黄、青在白底上只有 2.3-2.8:1，远低于 4.5:1，硬套
    亮色只会换来一屏看不清的字。所以那边继续沿用终端自己的背景色和经典
    八色 —— 在默认的深色控制台上它本来就是清楚的。
    """
    if not curses.has_colors():
        return False
    curses.start_color()
    try:
        curses.use_default_colors()
        default_bg = -1
    except curses.error:
        default_bg = curses.COLOR_BLACK

    global THEMED
    THEMED = curses.COLORS >= 256
    if not THEMED:
        return _init_basic_colors(default_bg)

    p = active_palette()
    bg, fg = p["bg"], p["fg"]
    pairs = {
        CP_NORMAL: (fg, bg),
        CP_TITLE: (p["primary"], bg),
        CP_OK: (p["ok"], bg),
        CP_WARN: (p["warn"], bg),
        CP_CRIT: (p["crit"], bg),
        CP_DIM: (p["dim"], bg),
        CP_HIGHLIGHT: (p["sel_fg"], p["sel_bg"]),
        CP_ACCENT: (p["data"], bg),
        CP_HEADER: (p["primary_fg"], p["primary"]),
        CP_BORDER: (p["border"], bg),
        CP_SURFACE: (fg, p["surface"]),
        CP_TRACK: (p["track"], bg),
        CP_OK_BAR: (p["ok_bar"], bg),
        CP_WARN_BAR: (p["warn_bar"], bg),
        CP_CRIT_BAR: (p["crit_bar"], bg),
        CP_ACCENT_BAR: (p["warn_bar"], bg),
    }
    for pair, (f, b) in pairs.items():
        try:
            curses.init_pair(pair, f, b)
        except curses.error:
            THEMED = False
            return _init_basic_colors(default_bg)
    return True


def _init_basic_colors(bg: int) -> bool:
    """8/16 色回退：沿用终端自己的背景，经典配色。"""
    curses.init_pair(CP_NORMAL, curses.COLOR_WHITE, bg)
    curses.init_pair(CP_TITLE, curses.COLOR_CYAN, bg)
    curses.init_pair(CP_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(CP_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(CP_CRIT, curses.COLOR_RED, bg)
    curses.init_pair(CP_DIM, curses.COLOR_BLUE, bg)
    curses.init_pair(CP_HIGHLIGHT, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(CP_ACCENT, curses.COLOR_MAGENTA, bg)
    curses.init_pair(CP_HEADER, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(CP_BORDER, curses.COLOR_BLUE, bg)
    curses.init_pair(CP_SURFACE, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(CP_TRACK, curses.COLOR_BLUE, bg)
    curses.init_pair(CP_OK_BAR, curses.COLOR_GREEN, bg)
    curses.init_pair(CP_WARN_BAR, curses.COLOR_YELLOW, bg)
    curses.init_pair(CP_CRIT_BAR, curses.COLOR_RED, bg)
    curses.init_pair(CP_ACCENT_BAR, curses.COLOR_MAGENTA, bg)
    return True


def paint_background(win) -> None:
    """把窗口底色刷成主题背景。

    curses 的 erase() 用的是窗口的 bkgd 字符，默认底色是终端自己的。
    亮色主题下不设这个，面板之间的空白会露出终端的深色底，整屏变成
    补丁。弹窗每次是新建的 window，所以也得各自刷一次。
    """
    if not THEMED:
        return
    try:
        win.bkgd(" ", curses.color_pair(CP_NORMAL))
    except curses.error:
        pass


def color(pair: int, bold: bool = False) -> int:
    attr = curses.color_pair(pair)
    if bold:
        attr |= curses.A_BOLD
    return attr


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Write text, clipped to the window, never raising on overflow."""
    if y < 0 or x < 0:
        return
    try:
        height, width = win.getmaxyx()
    except curses.error:
        return
    if y >= height or x >= width:
        return
    space = width - x
    # Writing the bottom-right cell scrolls and raises; leave it alone.
    if y == height - 1:
        space -= 1
    if space <= 0:
        return
    try:
        win.addstr(y, x, clip(text, space), attr)
    except curses.error:
        pass


def safe_hline(win, y: int, x: int, char: str, length: int, attr: int = 0) -> None:
    safe_addstr(win, y, x, char * max(0, length), attr)


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if cwidth(text) <= width:
        return text
    ell = cwidth(ellipsis)
    if width <= ell:
        return clip(text, width)
    return clip(text, width - ell) + ellipsis


def pad(text: str, width: int) -> str:
    """Left-align to `width` terminal columns, ellipsising if too long."""
    return _lpad(truncate(text, width), width)


def rpad(text: str, width: int) -> str:
    """Right-align to `width` terminal columns, ellipsising if too long."""
    return _rpad(truncate(text, width), width)


def sparkline(values: Sequence[float], width: int) -> str:
    """Render a series as block glyphs, right-aligned to `width`."""
    if not values or width <= 0:
        return ""
    series = list(values)[-width:]
    lo = min(series)
    hi = max(series)
    if hi - lo < 1e-9:
        # A flat series should read as flat, mid-height, not as noise.
        return BLOCKS[4] * len(series)
    out = []
    for value in series:
        idx = int((value - lo) / (hi - lo) * (len(BLOCKS) - 1))
        out.append(BLOCKS[max(0, min(len(BLOCKS) - 1, idx))])
    return "".join(out)


def hbar(value: float, maximum: float, width: int, fill: str = "█") -> str:
    """A horizontal bar with sub-cell resolution from the block glyphs."""
    if width <= 0 or maximum <= 0:
        return ""
    fraction = max(0.0, min(1.0, value / maximum))
    exact = fraction * width
    whole = int(exact)
    bar = fill * whole
    remainder = exact - whole
    if whole < width and remainder > 0.05:
        idx = int(remainder * (len(BLOCKS) - 1))
        bar += BLOCKS[max(1, idx)]
    return bar.ljust(width)


def draw_bar(win, y: int, x: int, value: float, maximum: float, width: int,
             attr: int) -> None:
    """画一条带轨道的进度条。

    只画填充部分的话，条形短的时候读者看不出量程有多长 —— 「20%」和
    「一小截」之间要靠猜。轨道用淡灰把剩余部分补出来，形状就成了量表。
    """
    if width <= 0:
        return
    bar = hbar(value, maximum, width)
    filled = len(bar.rstrip(" "))
    safe_addstr(win, y, x, bar[:filled], attr)
    if filled < width:
        safe_addstr(win, y, x + filled, "─" * (width - filled), color(CP_TRACK))


def draw_box(win, y: int, x: int, height: int, width: int, title: str = "",
             attr: int = 0, title_attr: Optional[int] = None) -> None:
    """Draw a single-line box. Clipped to the window; never raises."""
    max_y, max_x = win.getmaxyx()
    height = min(height, max_y - y)
    width = min(width, max_x - x)
    if height < 2 or width < 2:
        return
    safe_addstr(win, y, x, "╭" + "─" * (width - 2) + "╮", attr)
    for row in range(1, height - 1):
        safe_addstr(win, y + row, x, "│", attr)
        safe_addstr(win, y + row, x + width - 1, "│", attr)
    safe_addstr(win, y + height - 1, x, "╰" + "─" * (width - 2) + "╯", attr)
    if title:
        label = truncate(f" {title} ", width - 4)
        safe_addstr(win, y, x + 2, label, title_attr if title_attr is not None else attr)


def panel(win, y: int, x: int, height: int, width: int, title: str = "") -> None:
    """标准面板：淡边框 + 主色标题。

    视图原来每处都手写 `draw_box(..., color(CP_TITLE), color(CP_TITLE, True))`，
    边框和标题同色同亮度，一屏八个框全在抢眼。这里把边框降到 CP_BORDER，
    只让标题留在主色上，层次就出来了。
    """
    draw_box(win, y, x, height, width, title,
             color(CP_BORDER), color(CP_TITLE, bold=True))


def table_header(win, y: int, x: int, text: str, width: int) -> None:
    """表头：亮色下铺一条 Muted 色带，暗色 / 8 色下退回加粗。"""
    attr = color(CP_SURFACE, bold=True) if THEMED else color(CP_DIM, bold=True)
    safe_addstr(win, y, x, pad(text, width), attr)


def highlight_row(win, y: int, x: int, width: int) -> None:
    """把选中行整行铺成高亮底。

    行内各列是分段写的，段与段之间、以及行尾剩下的空白都保持背景色 ——
    只给文字上高亮的话，选中行看起来是一串断续的色块而不是一整条。
    先铺底再写字，光标落在哪一行就一目了然。
    """
    safe_addstr(win, y, x, " " * max(0, width), color(CP_HIGHLIGHT))


def row_attr(selected: bool, base: Optional[int] = None) -> int:
    """列表行的属性：选中行整行反白，否则用给定的语义色。"""
    if selected:
        return color(CP_HIGHLIGHT, bold=True)
    return base if base is not None else color(CP_NORMAL)


def status_attr(status: str) -> int:
    s = status.lower()
    if s in ("ok", "on", "enabled", "true", "active", "present"):
        return color(CP_OK)
    if s in ("nc", "warning", "degraded"):
        return color(CP_WARN)
    if s in ("cr", "nr", "critical", "fault", "failed", "off"):
        return color(CP_CRIT, bold=True)
    if s in ("ns", "na", "n/a", "no reading", "disabled"):
        return color(CP_DIM)
    return color(CP_NORMAL)


def severity_attr(severity: str) -> int:
    return {
        "critical": color(CP_CRIT, bold=True),
        "warning": color(CP_WARN),
    }.get(severity, color(CP_NORMAL))


def humanize_seconds(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60:02d}秒"
    if seconds < 86400:
        return f"{seconds // 3600}小时{(seconds % 3600) // 60:02d}分"
    return f"{seconds // 86400}天{(seconds % 86400) // 3600:02d}小时"


def humanize_ago(ts: int, now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now()
    delta = now.timestamp() - ts
    if delta < 0:
        return "时间在未来"
    return f"{humanize_seconds(delta)}前"


class Prompt:
    """A modal single-line input drawn over the current view."""

    def __init__(self, stdscr):
        self.stdscr = stdscr

    def ask(
        self,
        label: str,
        initial: str = "",
        secret: bool = False,
        validator=None,
    ) -> Optional[str]:
        """Read a line. Returns None if the user cancels with ESC.

        `validator` may return an error string to reject the value and keep
        the prompt open.
        """
        height, width = self.stdscr.getmaxyx()
        buffer = list(initial)
        cursor = len(buffer)
        error = ""
        curses.curs_set(1)
        try:
            while True:
                row = height - 2
                self.stdscr.move(row, 0)
                self.stdscr.clrtoeol()
                self.stdscr.move(row + 1, 0)
                self.stdscr.clrtoeol()
                shown = "*" * len(buffer) if secret else "".join(buffer)
                safe_addstr(self.stdscr, row, 0, label, color(CP_TITLE, bold=True))
                field_x = cwidth(label) + 1
                safe_addstr(self.stdscr, row, field_x, shown, color(CP_NORMAL))
                hint = "Enter=确定  ESC=取消"
                if error:
                    safe_addstr(self.stdscr, row + 1, 0, error, color(CP_CRIT, bold=True))
                else:
                    safe_addstr(self.stdscr, row + 1, 0, hint, color(CP_DIM))
                try:
                    before = cwidth("".join(buffer[:cursor])) if not secret else cursor
                    self.stdscr.move(row, min(field_x + before, width - 1))
                except curses.error:
                    pass
                self.stdscr.refresh()

                # get_wch returns a str for printable input, so a Chinese
                # name can be typed; getch would hand back raw UTF-8 bytes
                # one at a time and mangle it.
                try:
                    raw = self.stdscr.get_wch()
                except curses.error:
                    continue
                if isinstance(raw, str):
                    key = ord(raw) if len(raw) == 1 else -1
                else:
                    key = raw
                if key == 27:  # ESC
                    return None
                if key in (curses.KEY_ENTER, 10, 13):
                    value = "".join(buffer)
                    if validator:
                        error = validator(value) or ""
                        if error:
                            continue
                    return value
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    if cursor > 0:
                        del buffer[cursor - 1]
                        cursor -= 1
                    error = ""
                elif key == curses.KEY_DC:
                    if cursor < len(buffer):
                        del buffer[cursor]
                    error = ""
                elif key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(buffer), cursor + 1)
                elif key == curses.KEY_HOME:
                    cursor = 0
                elif key == curses.KEY_END:
                    cursor = len(buffer)
                elif key == 21:  # Ctrl-U
                    buffer, cursor = [], 0
                elif isinstance(raw, str) and raw.isprintable():
                    buffer.insert(cursor, raw)
                    cursor += 1
                    error = ""
        finally:
            curses.curs_set(0)


def confirm(stdscr, question: str, danger: bool = False) -> bool:
    """Modal yes/no. Destructive actions require typing `yes` in full."""
    height, _ = stdscr.getmaxyx()
    row = height - 2
    if danger:
        prompt = Prompt(stdscr)
        answer = prompt.ask(f"{question} 输入 yes 确认：")
        return answer is not None and answer.strip().lower() == "yes"
    while True:
        stdscr.move(row, 0)
        stdscr.clrtoeol()
        safe_addstr(stdscr, row, 0, f"{question} [y/N] ", color(CP_WARN, bold=True))
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27, curses.KEY_ENTER, 10, 13):
            return False


# 看到 ESC 之后再等多久，用来区分「单独按了 ESC」和「方向键序列的开头」。
# ncurses 自己的 ESCDELAY 默认是 1 秒，对人手来说太长了；50ms 足够让同一次
# 按键的后续字节到齐，又不会让按 ESC 取消有可感的迟滞。
ESC_PEEK_MS = 50

# 普通模式（未收到 smkx 的终端）下方向键序列的末字节。
_ESC_FINAL = {
    ord("A"): curses.KEY_UP,
    ord("B"): curses.KEY_DOWN,
    ord("C"): curses.KEY_RIGHT,
    ord("D"): curses.KEY_LEFT,
    ord("H"): curses.KEY_HOME,
    ord("F"): curses.KEY_END,
}


def _enable_keys(win) -> None:
    """让这个窗口认识方向键。

    keypad 在 ncurses 里是**按窗口**的属性，不是全局的。app 给 stdscr 开了
    keypad(True)，但弹窗是 curses.newwin() 新建的，新窗口默认是关的 ——
    于是方向键不会被翻译成 KEY_UP/KEY_DOWN，而是以原始转义序列逐字节
    到达。第一个字节是 27，正好被 choose() 当成「取消」，结果就是按一下
    方向键菜单直接关掉。
    """
    try:
        win.keypad(True)
    except curses.error:
        # 极少数终端类型上会失败。方向键用不了，但 j/k 和 Enter 还在，
        # 不该因此让弹窗开不出来。
        pass


def _read_key(win) -> int:
    """读一个按键，并把终端没帮忙翻译的方向键序列补上。

    keypad 开着时，ncurses 认得**应用模式**的方向键（ESC O A/B）——
    终端收到 smkx 之后发的就是这一种，绝大多数情况走的是这条路。

    但有的终端不理会 smkx，照发**普通模式**的 ESC [ A/B。那串在 ncurses
    的 terminfo 里没有对应项，于是原样吐出三个字节，而头一个 27 会被
    choose() 读成「ESC＝取消」——一按方向键菜单就关，和完全没修一样。
    所以这里在看到 ESC 之后再探一下：后面紧跟着字节就是序列，什么都没有
    才是真的按了 ESC。
    """
    key = win.getch()
    if key != 27:
        return key
    win.timeout(ESC_PEEK_MS)
    try:
        nxt = win.getch()
        if nxt == -1:
            return 27                       # 真的只按了 ESC
        if nxt in (ord("["), ord("O")):
            return _ESC_FINAL.get(win.getch(), -1)
        # ESC 后面跟的不是序列引导符，说明那是一个独立的 ESC（取消），
        # 后面那个键是另一次独立按键 —— 退回去，下一轮再读。
        #
        # 不退回去、直接返回 -1 会死循环：连按两下 ESC 时，第一个 ESC 探到
        # 第二个 ESC，返回 -1，菜单不关也不动；下一轮又读到那个 ESC……
        # 菜单就再也关不掉了。冒烟测试每 50ms 灌一个 ESC，当场就挂住了。
        try:
            curses.ungetch(nxt)
        except curses.error:
            pass
        return 27
    finally:
        win.timeout(-1)                     # 恢复阻塞读


def choose(stdscr, title: str, options: Sequence[str]) -> Optional[int]:
    """Modal single-choice list. Returns the chosen index, or None."""
    if not options:
        return None
    height, width = stdscr.getmaxyx()
    box_h = min(len(options) + 4, height - 4)
    box_w = min(max(cwidth(title), max(cwidth(o) for o in options)) + 8, width - 4)
    top = max(0, (height - box_h) // 2)
    left = max(0, (width - box_w) // 2)
    selected = 0
    while True:
        win = curses.newwin(box_h, box_w, top, left)
        _enable_keys(win)
        paint_background(win)
        win.erase()
        panel(win, 0, 0, box_h, box_w, title)
        visible = box_h - 4
        start = max(0, min(selected - visible // 2, len(options) - visible))
        for i in range(min(visible, len(options))):
            idx = start + i
            if idx >= len(options):
                break
            attr = row_attr(idx == selected)
            safe_addstr(win, 2 + i, 2, pad(options[idx], box_w - 4), attr)
        safe_addstr(win, box_h - 1, 2, " ↑↓ 选择  Enter 确定  ESC 取消 ", color(CP_DIM))
        win.refresh()
        key = _read_key(win)
        if key in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(options)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(options)
        elif key in (curses.KEY_ENTER, 10, 13):
            return selected
        elif key == 27:
            return None


def show_message(stdscr, title: str, body: str, is_error: bool = False) -> None:
    """Modal scrollable message. Used for command output and errors."""
    lines: list[str] = []
    for raw in body.splitlines() or [""]:
        lines.append(raw)
    height, width = stdscr.getmaxyx()
    box_h = min(max(len(lines) + 4, 6), height - 2)
    box_w = min(
        max(cwidth(title) + 6, max((cwidth(l) for l in lines), default=20) + 6),
        width - 2,
    )
    top = max(0, (height - box_h) // 2)
    left = max(0, (width - box_w) // 2)
    offset = 0
    frame_attr = color(CP_CRIT if is_error else CP_BORDER)
    visible = box_h - 4
    while True:
        win = curses.newwin(box_h, box_w, top, left)
        _enable_keys(win)
        paint_background(win)
        win.erase()
        draw_box(win, 0, 0, box_h, box_w, title, frame_attr,
                 color(CP_CRIT if is_error else CP_TITLE, bold=True))
        for i in range(visible):
            idx = offset + i
            if idx >= len(lines):
                break
            safe_addstr(win, 2 + i, 2, truncate(lines[idx], box_w - 4),
                        color(CP_NORMAL))
        footer = " 按任意键关闭 "
        if len(lines) > visible:
            footer = (f" {offset + 1}-{min(offset + visible, len(lines))}/{len(lines)}"
                      "  ↑↓ 滚动  q 关闭 ")
        safe_addstr(win, box_h - 1, 2, footer, color(CP_DIM))
        win.refresh()
        key = _read_key(win)
        if len(lines) > visible:
            if key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                offset = min(max(0, len(lines) - visible), offset + 1)
                continue
            if key in (curses.KEY_NPAGE, ord(" ")):
                offset = min(max(0, len(lines) - visible), offset + visible)
                continue
            if key == curses.KEY_PPAGE:
                offset = max(0, offset - visible)
                continue
        return
