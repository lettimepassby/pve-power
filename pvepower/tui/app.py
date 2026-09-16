"""The TUI application: chrome, event loop, and view dispatch."""

from __future__ import annotations

import csv
import curses
import datetime as dt
import os
import time
from typing import Optional

from ..config import Config
from ..ipmi import IpmiTool
from ..storage import Storage
from .data import DataCache
from .views.base import View
from .views.bmc import BmcView
from .views.energy import EnergyView
from .views.overview import OverviewView
from .views.sel import SelView
from .views.sensors import SensorsView
from .views.tariff import TariffView
from .views.users import UsersView
from .widgets import (
    CP_ACCENT,
    CP_CRIT,
    CP_DIM,
    CP_HEADER,
    CP_HIGHLIGHT,
    CP_NORMAL,
    CP_OK,
    CP_TITLE,
    CP_WARN,
    Prompt,
    color,
    confirm,
    init_colors,
    pad,
    safe_addstr,
    show_message,
    truncate,
)

HELP_TEXT = """\
Navigation
  Tab / Shift-Tab     next / previous tab
  1 .. 7              jump straight to a tab
  ↑ ↓ / k j           move within a list
  PgUp PgDn g G       page and jump to ends
  r                   refresh everything now
  q                   quit

Overview
  Live power draw, today's and this month's consumption and cost,
  chassis health, and whether the collector is keeping up.

Energy
  d / h               daily or hourly view
  Enter               drill into the selected day
  ← →                 previous / next day (hourly view)
  [ ]                 shrink / grow the date range
  e                   export the visible range to CSV

Sensors
  f / F               cycle the sensor-kind filter
  o                   show only sensors out of spec

BMC
  Enter               edit the selected LAN field
  c                   chassis power action
  p                   power restore policy after AC loss
  i                   blink the identify LED

Users
  p                   set password        n   rename
  v                   privilege level     e/d enable / disable

Event Log
  o                   show only warnings and faults
  X                   erase the BMC event log

Tariff
  Enter               edit the selected setting
  m                   switch between flat and time-of-use
  a / x               add / remove a period or tier
  P                   load the sample Chinese TOU preset
  s                   save the configuration to disk
  R                   recompute stored costs under the current tariff

Notes
  Energy is integrated from power samples with the trapezoidal rule.
  Cost is recorded at the price in force when each sample was taken, so
  changing the tariff later does not rewrite history — use R for that,
  and only when the old prices were wrong rather than merely older.
"""


class App:
    def __init__(self, config: Config, config_path: str):
        self.config = config
        self.config_path = config_path
        self.ipmi = IpmiTool(
            binary=config.ipmi.binary,
            host=config.ipmi.host,
            user=config.ipmi.user,
            password=config.ipmi.password,
            interface=config.ipmi.interface,
            timeout=config.ipmi.timeout,
        )
        self.storage = Storage(config.db_path)
        self.data = DataCache(config, self.ipmi, self.storage)
        self.views: list[View] = []
        self.active = 0
        self.stdscr = None
        self._flash = ""
        self._flash_until = 0.0
        self._flash_ok = True
        self._running = True

    # ---------------- lifecycle ----------------

    def run(self) -> int:
        return curses.wrapper(self._main)

    def _main(self, stdscr) -> int:
        self.stdscr = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        init_colors()

        self.views = [
            OverviewView(self),
            EnergyView(self),
            SensorsView(self),
            BmcView(self),
            UsersView(self),
            SelView(self),
            TariffView(self),
        ]

        last_draw = 0.0
        while self._running:
            self.data.tick()
            now = time.time()
            # Redraw at ~2 Hz when idle; a keypress forces one immediately.
            if now - last_draw >= 0.5:
                self._draw()
                last_draw = now
            key = stdscr.getch()
            if key == -1:
                time.sleep(0.05)
                continue
            self._handle_key(key)
            self._draw()
            last_draw = time.time()
        self.storage.close()
        return 0

    # ---------------- drawing ----------------

    def content_size(self) -> tuple[int, int]:
        if self.stdscr is None:
            return 24, 80
        height, width = self.stdscr.getmaxyx()
        # Header takes 2 rows, footer 2.
        return max(1, height - 4), width

    def _draw(self) -> None:
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        stdscr.erase()

        if height < 12 or width < 60:
            safe_addstr(stdscr, 0, 0,
                        f"Terminal too small: {width}x{height}, need 60x12",
                        color(CP_CRIT, bold=True))
            stdscr.refresh()
            return

        self._draw_header(stdscr, width)

        content_h = height - 4
        content = stdscr.derwin(content_h, width, 2, 0)
        try:
            self.views[self.active].draw(content, content_h, width)
        except curses.error:
            # A resize mid-draw can invalidate geometry; next frame recovers.
            pass

        self._draw_footer(stdscr, height, width)
        stdscr.refresh()

    def _draw_header(self, stdscr, width: int) -> None:
        host = self.config.ipmi.host or "local"
        fru = self.data.fru or {}
        product = fru.get("Product Name", "")
        title = f" pve-power  {product} @ {host} "
        safe_addstr(stdscr, 0, 0, pad(title, width), color(CP_HEADER, bold=True))

        watts = self.data.current_watts()
        if watts is not None:
            badge = f" {watts:.0f} W  {self.data.today.cost:.2f} " \
                    f"{self.config.tariff.currency} today "
            safe_addstr(stdscr, 0, max(0, width - len(badge) - 1), badge,
                        color(CP_HEADER, bold=True))

        x = 0
        for idx, view in enumerate(self.views):
            label = f" {idx + 1}:{view.title} "
            attr = color(CP_HIGHLIGHT, bold=True) if idx == self.active else color(CP_DIM)
            safe_addstr(stdscr, 1, x, label, attr)
            x += len(label)
            if x >= width:
                break

    def _draw_footer(self, stdscr, height: int, width: int) -> None:
        y = height - 2
        safe_addstr(stdscr, y, 0, "─" * width, color(CP_DIM))
        if self._flash and time.time() < self._flash_until:
            attr = color(CP_OK, bold=True) if self._flash_ok else color(CP_CRIT, bold=True)
            safe_addstr(stdscr, y + 1, 1, truncate(self._flash, width - 2), attr)
            return

        keys = [("Tab", "switch"), ("r", "refresh"), ("?", "help"), ("q", "quit")]
        keys = list(self.views[self.active].hotkeys) + keys
        x = 1
        for key, label in keys:
            if x + len(key) + len(label) + 3 >= width:
                break
            safe_addstr(stdscr, y + 1, x, key, color(CP_ACCENT, bold=True))
            x += len(key)
            safe_addstr(stdscr, y + 1, x, f":{label}  ", color(CP_DIM))
            x += len(label) + 3

        clock = dt.datetime.now().strftime("%H:%M:%S")
        safe_addstr(stdscr, y + 1, max(0, width - len(clock) - 1), clock,
                    color(CP_DIM))

    # ---------------- input ----------------

    def _handle_key(self, key: int) -> None:
        if self.views[self.active].handle_key(key):
            return

        if key in (ord("q"), ord("Q")):
            self._running = False
        elif key == ord("\t") or key == curses.KEY_RIGHT and False:
            self.active = (self.active + 1) % len(self.views)
        elif key == curses.KEY_BTAB:
            self.active = (self.active - 1) % len(self.views)
        elif ord("1") <= key <= ord("9"):
            idx = key - ord("1")
            if idx < len(self.views):
                self.active = idx
        elif key in (ord("r"), ord("R")) and key == ord("r"):
            for kind in list(self.data._fetched):
                self.data.invalidate(kind)
            self.data.refresh_energy()
            self.flash("Refreshed")
        elif key == ord("?"):
            show_message(self.stdscr, "pve-power — keys and behaviour", HELP_TEXT)
        elif key == curses.KEY_RESIZE:
            curses.update_lines_cols()

    # ---------------- helpers used by views ----------------

    def flash(self, message: str, ok: bool = True, seconds: float = 4.0) -> None:
        self._flash = message
        self._flash_ok = ok
        self._flash_until = time.time() + seconds

    def save_config(self) -> None:
        problems = self.config.validate()
        if problems:
            show_message(
                self.stdscr,
                "Configuration not saved",
                "Fix these first:\n\n" + "\n".join(f"  • {p}" for p in problems),
                is_error=True,
            )
            return
        try:
            self.config.save(self.config_path)
            self.storage.log_event("config_saved", self.config_path)
            self.flash(f"Saved to {self.config_path}", ok=True)
        except OSError as exc:
            show_message(self.stdscr, "Could not write configuration", str(exc),
                         is_error=True)

    def export_csv(self) -> None:
        prompt = Prompt(self.stdscr)
        default = os.path.join(
            os.path.expanduser("~"),
            f"pve-power-{dt.date.today().isoformat()}.csv",
        )
        path = prompt.ask("Export daily totals to:", default)
        if not path:
            return
        path = os.path.expanduser(path.strip())
        view = self.views[self.active]
        days = getattr(view, "days", 30)
        series = self.data.daily_series(days)
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    ["date", "kwh", f"cost_{self.config.tariff.currency}",
                     "avg_watts", "min_watts", "max_watts", "samples",
                     "covered_seconds"]
                )
                for agg in series:
                    writer.writerow([
                        agg.label,
                        f"{agg.kwh:.4f}",
                        f"{agg.cost:.4f}",
                        f"{agg.avg_watts:.1f}",
                        f"{agg.min_watts:.0f}" if agg.min_watts is not None else "",
                        f"{agg.max_watts:.0f}" if agg.max_watts is not None else "",
                        agg.samples,
                        agg.duration_s,
                    ])
            self.flash(f"Exported {len(series)} days to {path}", ok=True)
        except OSError as exc:
            show_message(self.stdscr, "Export failed", str(exc), is_error=True)


def run(config: Config, config_path: str) -> int:
    return App(config, config_path).run()
