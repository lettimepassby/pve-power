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

# Eighth-block glyphs for bar charts and sparklines.
BLOCKS = " ▁▂▃▄▅▆▇█"


def init_colors() -> bool:
    """Set up colour pairs. Returns False on a monochrome terminal."""
    if not curses.has_colors():
        return False
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(CP_NORMAL, curses.COLOR_WHITE, bg)
    curses.init_pair(CP_TITLE, curses.COLOR_CYAN, bg)
    curses.init_pair(CP_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(CP_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(CP_CRIT, curses.COLOR_RED, bg)
    curses.init_pair(CP_DIM, curses.COLOR_BLUE, bg)
    curses.init_pair(CP_HIGHLIGHT, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(CP_ACCENT, curses.COLOR_MAGENTA, bg)
    curses.init_pair(CP_HEADER, curses.COLOR_BLACK, curses.COLOR_WHITE)
    return True


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


def draw_box(win, y: int, x: int, height: int, width: int, title: str = "",
             attr: int = 0, title_attr: Optional[int] = None) -> None:
    """Draw a single-line box. Clipped to the window; never raises."""
    max_y, max_x = win.getmaxyx()
    height = min(height, max_y - y)
    width = min(width, max_x - x)
    if height < 2 or width < 2:
        return
    safe_addstr(win, y, x, "┌" + "─" * (width - 2) + "┐", attr)
    for row in range(1, height - 1):
        safe_addstr(win, y + row, x, "│", attr)
        safe_addstr(win, y + row, x + width - 1, "│", attr)
    safe_addstr(win, y + height - 1, x, "└" + "─" * (width - 2) + "┘", attr)
    if title:
        label = truncate(f" {title} ", width - 4)
        safe_addstr(win, y, x + 2, label, title_attr if title_attr is not None else attr)


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
        win.erase()
        draw_box(win, 0, 0, box_h, box_w, title,
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        visible = box_h - 4
        start = max(0, min(selected - visible // 2, len(options) - visible))
        for i in range(min(visible, len(options))):
            idx = start + i
            if idx >= len(options):
                break
            attr = color(CP_HIGHLIGHT) if idx == selected else color(CP_NORMAL)
            safe_addstr(win, 2 + i, 2, pad(options[idx], box_w - 4), attr)
        safe_addstr(win, box_h - 1, 2, " ↑↓ 选择  Enter 确定  ESC 取消 ", color(CP_DIM))
        win.refresh()
        key = win.getch()
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
    frame_attr = color(CP_CRIT if is_error else CP_TITLE)
    visible = box_h - 4
    while True:
        win = curses.newwin(box_h, box_w, top, left)
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
        key = win.getch()
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
