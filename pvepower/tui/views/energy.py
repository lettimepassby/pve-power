"""Energy view: daily/hourly consumption history with cost breakdown."""

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
    draw_box,
    hbar,
    pad,
    rpad,
    safe_addstr,
    truncate,
)
from .base import View


class EnergyView(View):
    title = "Energy"
    hotkeys = [
        ("d/h", "daily/hourly"),
        ("[ ]", "range"),
        ("e", "export"),
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
        header = f"Daily consumption — last {self.days} days"
        draw_box(win, 0, 0, height - 6, width, header,
                 color(CP_TITLE), color(CP_TITLE, bold=True))

        cols = f"{'Date':<12}{'kWh':>9}  {'Cost':>10}  {'Avg W':>7}  "
        safe_addstr(win, 1, 2, pad(cols + "Profile", width - 4),
                    color(CP_DIM, bold=True))

        visible = height - 9
        total = len(series)
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))
        peak = max((a.kwh for a in series), default=0.0) or 1.0
        bar_width = max(8, width - 48)

        for i in range(min(visible, total)):
            idx = self.scroll + i
            if idx >= total:
                break
            agg = series[idx]
            selected = idx == self.cursor
            attr = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)
            y = 2 + i
            line = (
                f"{agg.label:<12}"
                f"{agg.kwh:>9.3f}  "
                f"{agg.cost:>10.2f}  "
                f"{agg.avg_watts:>7.0f}  "
            )
            safe_addstr(win, y, 2, pad(line, min(len(line), width - 4)), attr)
            if not selected and agg.kwh:
                safe_addstr(win, y, 2 + len(line),
                            hbar(agg.kwh, peak, bar_width),
                            color(CP_ACCENT))
            elif selected:
                safe_addstr(win, y, 2 + len(line),
                            pad(hbar(agg.kwh, peak, bar_width), bar_width), attr)

        self._draw_footer_totals(win, height, width, series, cur)

    def _draw_footer_totals(self, win, height, width, series, cur):
        total_kwh = sum(a.kwh for a in series)
        total_cost = sum(a.cost for a in series)
        active = [a for a in series if a.kwh > 0]
        y = height - 5
        draw_box(win, y, 0, 5, width, "Totals",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        safe_addstr(win, y + 1, 2,
                    f"{len(series)} days:  {total_kwh:.2f} kWh   "
                    f"{total_cost:.2f} {cur}",
                    color(CP_OK, bold=True))
        if active:
            mean_day = total_kwh / len(active)
            mean_cost = total_cost / len(active)
            safe_addstr(
                win, y + 2, 2,
                f"per active day:  {mean_day:.2f} kWh   {mean_cost:.2f} {cur}"
                f"   ({len(active)} days with data)",
                color(CP_DIM),
            )
            monthly = mean_cost * 30
            annual = mean_cost * 365
            safe_addstr(
                win, y + 3, 2,
                f"at this rate:  {monthly:.0f} {cur}/month   "
                f"{annual:.0f} {cur}/year",
                color(CP_WARN),
            )

    # ---------------- hourly ----------------

    def _draw_hourly(self, win, height, width):
        cur = self.app.config.tariff.currency
        series = self.data.hourly_series(self.day)
        header = f"Hourly consumption — {self.day.isoformat()}"
        draw_box(win, 0, 0, height - 6, width, header,
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        safe_addstr(win, 1, 2,
                    pad(f"{'Hour':<8}{'kWh':>8}  {'Cost':>9}  {'Avg W':>7}  Profile",
                        width - 4),
                    color(CP_DIM, bold=True))

        peak = max((a.kwh for a in series), default=0.0) or 1.0
        bar_width = max(8, width - 44)
        visible = min(24, height - 9)
        for i in range(visible):
            if i >= len(series):
                break
            agg = series[i]
            y = 2 + i
            selected = i == self.cursor
            attr = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)
            line = (
                f"{agg.label:<8}"
                f"{agg.kwh:>8.3f}  "
                f"{agg.cost:>9.3f}  "
                f"{agg.avg_watts:>7.0f}  "
            )
            safe_addstr(win, y, 2, line, attr)
            if agg.kwh:
                safe_addstr(win, y, 2 + len(line), hbar(agg.kwh, peak, bar_width),
                            color(CP_ACCENT) if not selected else attr)

        y = height - 5
        draw_box(win, y, 0, 5, width, "Tariff breakdown",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        start = dt.datetime.combine(self.day, dt.time.min)
        end = start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
        rows = self.data.period_breakdown(start, end)
        if not rows:
            safe_addstr(win, y + 1, 2, "No data for this day.", color(CP_DIM))
        else:
            x = 2
            for name, kwh, cost in rows[:4]:
                block = f"{name}: {kwh:.2f} kWh / {cost:.2f} {cur}"
                safe_addstr(win, y + 1, x, block, color(CP_ACCENT))
                x += len(block) + 4
            day_total = sum(c for _, _, c in rows)
            day_kwh = sum(k for _, k, _ in rows)
            safe_addstr(win, y + 2, 2,
                        f"day total: {day_kwh:.3f} kWh   {day_total:.2f} {cur}",
                        color(CP_OK, bold=True))
            safe_addstr(win, y + 3, 2,
                        "← → change day    d: back to daily view",
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
