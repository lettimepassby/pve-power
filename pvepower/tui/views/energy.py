"""电量视图：按天／按小时的历史用电量与电费明细。"""

from __future__ import annotations

import curses
import datetime as dt

from ..widgets import (
    CP_ACCENT,
    CP_DIM,
    CP_HIGHLIGHT,
    CP_NORMAL,
    CP_OK,
    CP_TITLE,
    CP_WARN,
    color,
    draw_bar,
    cwidth,
    hbar,
    highlight_row,
    pad,
    panel,
    rpad,
    safe_addstr,
    table_header,
    truncate,
)
from .base import View


class EnergyView(View):
    title = "电量"
    hotkeys = [
        ("d/h", "按天/按小时"),
        ("[ ]", "范围"),
        ("e", "导出"),
    ]

    RANGES = [7, 14, 30, 60, 90]

    def __init__(self, app):
        super().__init__(app)
        self.mode = "daily"
        self.range_index = 2  # 30 days
        self.day = dt.date.today()

    @property
    def days(self) -> int:
        return self.RANGES[self.range_index]

    def draw(self, win, height: int, width: int) -> None:
        if self.mode == "daily":
            self._draw_daily(win, height, width)
        else:
            self._draw_hourly(win, height, width)

    # ---------------- daily ----------------

    def _draw_daily(self, win, height, width):
        cur = self.app.config.tariff.currency
        series = self.data.daily_series(self.days)
        header = f"按天用电量：最近 {self.days} 天"
        panel(win, 0, 0, height - 6, width, header)

        cols = (
            pad("日期", 12)
            + rpad("电量/kWh", 9) + "  "
            + rpad("电费", 10) + "  "
            + rpad("平均功率", 9) + "  "
        )
        table_header(win, 1, 2, cols + "趋势", width - 4)

        visible = height - 9
        total = len(series)
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))
        peak = max((a.kwh for a in series), default=0.0) or 1.0
        bar_width = max(8, width - 50)

        for i in range(min(visible, total)):
            idx = self.scroll + i
            if idx >= total:
                break
            agg = series[idx]
            y = 2 + i
            selected = idx == self.cursor
            if selected:
                highlight_row(win, y, 1, width - 2)
            attr = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)
            line = (
                f"{pad(agg.label, 12)}"
                f"{rpad(f'{agg.kwh:.3f}', 9)}  "
                f"{rpad(f'{agg.cost:.2f}', 10)}  "
                f"{rpad(f'{agg.avg_watts:.0f}', 9)}  "
            )
            safe_addstr(win, y, 2, pad(line, min(cwidth(line), width - 4)), attr)
            if not selected and agg.kwh:
                draw_bar(win, y, 2 + cwidth(line), agg.kwh, peak, bar_width,
                         color(CP_ACCENT))
            elif selected:
                safe_addstr(win, y, 2 + cwidth(line),
                            pad(hbar(agg.kwh, peak, bar_width), bar_width), attr)

        self._draw_footer_totals(win, height, width, series, cur)

    def _draw_footer_totals(self, win, height, width, series, cur):
        total_kwh = sum(a.kwh for a in series)
        total_cost = sum(a.cost for a in series)
        active = [a for a in series if a.kwh > 0]
        y = height - 5
        panel(win, y, 0, 5, width, "合计")
        safe_addstr(win, y + 1, 2,
                    f"{len(series)} 天：{total_kwh:.2f} kWh   "
                    f"{total_cost:.2f} {cur}",
                    color(CP_OK, bold=True))
        if active:
            mean_day = total_kwh / len(active)
            mean_cost = total_cost / len(active)
            safe_addstr(
                win, y + 2, 2,
                f"平均每天：{mean_day:.2f} kWh   {mean_cost:.2f} {cur}"
                f"（有数据的 {len(active)} 天）",
                color(CP_DIM),
            )
            monthly = mean_cost * 30
            annual = mean_cost * 365
            safe_addstr(
                win, y + 3, 2,
                f"按此推算：每月 {monthly:.0f} {cur}   "
                f"每年 {annual:.0f} {cur}",
                color(CP_WARN),
            )

    # ---------------- hourly ----------------

    def _draw_hourly(self, win, height, width):
        cur = self.app.config.tariff.currency
        series = self.data.hourly_series(self.day)
        header = f"按小时用电量：{self.day.isoformat()}"
        panel(win, 0, 0, height - 6, width, header)
        table_header(
            win, 1, 2,
            pad("时段", 8) + rpad("电量/kWh", 8) + "  "
            + rpad("电费", 9) + "  " + rpad("平均功率", 9) + "  趋势",
            width - 4,
        )

        peak = max((a.kwh for a in series), default=0.0) or 1.0
        bar_width = max(8, width - 46)
        visible = min(24, height - 9)
        for i in range(visible):
            if i >= len(series):
                break
            agg = series[i]
            y = 2 + i
            selected = i == self.cursor
            if selected:
                highlight_row(win, y, 1, width - 2)
            attr = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)
            line = (
                f"{pad(agg.label, 8)}"
                f"{rpad(f'{agg.kwh:.3f}', 8)}  "
                f"{rpad(f'{agg.cost:.3f}', 9)}  "
                f"{rpad(f'{agg.avg_watts:.0f}', 9)}  "
            )
            safe_addstr(win, y, 2, line, attr)
            if agg.kwh:
                if selected:
                    safe_addstr(win, y, 2 + cwidth(line),
                                pad(hbar(agg.kwh, peak, bar_width), bar_width), attr)
                else:
                    draw_bar(win, y, 2 + cwidth(line), agg.kwh, peak, bar_width,
                             color(CP_ACCENT))

        y = height - 5
        panel(win, y, 0, 5, width, "分时电价明细")
        start = dt.datetime.combine(self.day, dt.time.min)
        end = start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
        rows = self.data.period_breakdown(start, end)
        if not rows:
            safe_addstr(win, y + 1, 2, "当天没有数据。", color(CP_DIM))
        else:
            x = 2
            for name, kwh, cost in rows[:4]:
                block = f"{name}：{kwh:.2f} kWh / {cost:.2f} {cur}"
                safe_addstr(win, y + 1, x, block, color(CP_ACCENT))
                x += cwidth(block) + 4
            day_total = sum(c for _, _, c in rows)
            day_kwh = sum(k for _, k, _ in rows)
            safe_addstr(win, y + 2, 2,
                        f"当天合计：{day_kwh:.3f} kWh   {day_total:.2f} {cur}",
                        color(CP_OK, bold=True))
            safe_addstr(win, y + 3, 2,
                        "← → 切换日期    d：返回按天视图",
                        color(CP_DIM))

    # ---------------- keys ----------------

    def handle_key(self, key: int) -> bool:
        height, _ = self.app.content_size()
        if self.mode == "daily":
            visible = max(1, height - 9)
            if self.handle_list_key(key, self.days, visible):
                return True
            if key in (ord("h"), curses.KEY_ENTER, 10, 13):
                # Drill into the selected day.
                series = self.data.daily_series(self.days)
                if 0 <= self.cursor < len(series):
                    self.day = dt.date.fromisoformat(series[self.cursor].label)
                self.mode = "hourly"
                self.cursor = 0
                return True
        else:
            if self.handle_list_key(key, 24, 24):
                return True
            if key == ord("d"):
                self.mode = "daily"
                self.cursor = 0
                return True
            if key == curses.KEY_LEFT:
                self.day -= dt.timedelta(days=1)
                return True
            if key == curses.KEY_RIGHT:
                if self.day < dt.date.today():
                    self.day += dt.timedelta(days=1)
                return True

        if key == ord("d") and self.mode != "daily":
            self.mode = "daily"
            return True
        if key == ord("["):
            self.range_index = max(0, self.range_index - 1)
            self.cursor = self.scroll = 0
            return True
        if key == ord("]"):
            self.range_index = min(len(self.RANGES) - 1, self.range_index + 1)
            self.cursor = self.scroll = 0
            return True
        if key == ord("e"):
            self.app.export_csv()
            return True
        return False
