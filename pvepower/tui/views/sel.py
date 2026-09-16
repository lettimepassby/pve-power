"""事件日志视图：BMC 的系统事件日志。"""

from __future__ import annotations

import curses

from ...ipmi import IpmiError
from ..widgets import (
    CP_ACCENT,
    CP_CRIT,
    CP_DIM,
    CP_HIGHLIGHT,
    CP_NORMAL,
    CP_OK,
    CP_TITLE,
    CP_WARN,
    color,
    confirm,
    draw_box,
    pad,
    safe_addstr,
    severity_attr,
    show_message,
    truncate,
)
from .base import View


class SelView(View):
    title = "事件日志"
    hotkeys = [("o", "只看问题"), ("X", "清空日志")]

    def __init__(self, app):
        super().__init__(app)
        self.problems_only = False

    def visible_entries(self):
        entries = self.data.sel_entries or []
        if self.problems_only:
            entries = [e for e in entries if e.severity != "info"]
        return entries

    def draw(self, win, height: int, width: int) -> None:
        entries = self.visible_entries()
        info = self.data.sel_info or {}
        title = "BMC 系统事件日志"
        if self.problems_only:
            title += " —— 只看告警和故障"
        draw_box(win, 0, 0, height - 4, width, title,
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        safe_addstr(
            win, 1, 2,
            pad(pad("编号", 6) + pad("时间", 26) + pad("传感器", 26) + "事件",
                width - 4),
            color(CP_DIM, bold=True),
        )

        visible = height - 7
        total = len(entries)
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))
        self.cursor = max(0, min(self.cursor, max(0, total - 1)))

        for i in range(min(visible, total)):
            idx = self.scroll + i
            if idx >= total:
                break
            entry = entries[idx]
            y = 2 + i
            selected = idx == self.cursor
            attr = (
                color(CP_HIGHLIGHT) if selected else severity_attr(entry.severity)
            )
            event = entry.event
            if entry.direction:
                event = f"{event} ({entry.direction})"
            line = (
                pad(truncate(entry.record_id, 5), 6)
                + pad(truncate(entry.when, 25), 26)
                + pad(truncate(entry.sensor, 25), 26)
                + event
            )
            safe_addstr(win, y, 2, truncate(line, width - 4), attr)

        # ---- summary ----
        y = height - 4
        draw_box(win, y, 0, 4, width, "", color(CP_DIM))
        all_entries = self.data.sel_entries or []
        crit = sum(1 for e in all_entries if e.severity == "critical")
        warn = sum(1 for e in all_entries if e.severity == "warning")
        parts = [
            f"BMC 上共 {info.get('Entries', len(all_entries))} 条记录",
            f"日志空间已用 {info.get('Percent Used', '?')}",
        ]
        if info.get("Last Add Time"):
            parts.append(f"最后一条 {info['Last Add Time']}")
        safe_addstr(win, y + 1, 2, truncate("   ".join(parts), width - 28),
                    color(CP_NORMAL))
        if crit or warn:
            safe_addstr(win, y + 1, width - 26,
                        f"严重 {crit}  告警 {warn}",
                        color(CP_CRIT if crit else CP_WARN, bold=True))
        else:
            safe_addstr(win, y + 1, width - 26, "无问题事件",
                        color(CP_OK))
        safe_addstr(win, y + 2, 2,
                    "o：只看问题   X：清空 BMC 事件日志   r：刷新",
                    color(CP_DIM))

    def handle_key(self, key: int) -> bool:
        height, _ = self.app.content_size()
        if self.handle_list_key(key, len(self.visible_entries()), max(1, height - 7)):
            return True
        if key == ord("o"):
            self.problems_only = not self.problems_only
            self.cursor = self.scroll = 0
            return True
        if key == ord("X"):
            self._clear()
            return True
        return False

    def _clear(self):
        stdscr = self.app.stdscr
        if not confirm(
            stdscr,
            "确定清空 BMC 系统事件日志吗？此操作不可撤销，"
            "硬件故障历史将永久丢失。",
            danger=True,
        ):
            return
        try:
            out = self.app.ipmi.sel_clear()
            self.app.storage.log_event("sel_clear", "cleared from TUI")
            show_message(stdscr, "事件日志已清空", out.strip() or "（无输出）")
        except IpmiError as exc:
            show_message(stdscr, "清空事件日志失败", str(exc), is_error=True)
        self.data.invalidate("sel")
