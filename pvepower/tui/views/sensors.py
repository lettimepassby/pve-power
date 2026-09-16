"""Sensors view: the full IPMI sensor table with filtering."""

from __future__ import annotations

import curses

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
    draw_box,
    hbar,
    pad,
    safe_addstr,
    status_attr,
    truncate,
)
from .base import View

KINDS = ["all", "temperature", "voltage", "fan", "power", "current", "discrete"]


class SensorsView(View):
    title = "Sensors"
    hotkeys = [("f", "filter"), ("o", "only faults")]

    def __init__(self, app):
        super().__init__(app)
        self.kind_index = 0
        self.faults_only = False

    def visible_sensors(self):
        sensors = self.data.sensors or []
        kind = KINDS[self.kind_index]
        if kind != "all":
            sensors = [s for s in sensors if s.kind == kind]
        if self.faults_only:
            sensors = [s for s in sensors if s.readable and not s.ok]
        return sensors

    def draw(self, win, height: int, width: int) -> None:
        sensors = self.visible_sensors()
        all_sensors = self.data.sensors or []
        faulted = [s for s in all_sensors if s.readable and not s.ok]

        title = f"Sensors — filter: {KINDS[self.kind_index]}"
        if self.faults_only:
            title += " (faults only)"
        draw_box(win, 0, 0, height - 3, width, title,
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        safe_addstr(
            win, 1, 2,
            pad(f"{'Sensor':<20}{'Reading':>12} {'Unit':<11}{'St':<4}"
                f"{'Crit':>9}  Headroom", width - 4),
            color(CP_DIM, bold=True),
        )

        visible = height - 6
        total = len(sensors)
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))
        self.cursor = max(0, min(self.cursor, max(0, total - 1)))

        for i in range(min(visible, total)):
            idx = self.scroll + i
            if idx >= total:
                break
            s = sensors[idx]
            y = 2 + i
            selected = idx == self.cursor
            base = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)

            value = f"{s.value:.2f}" if s.value is not None else "—"
            line = (
                f"{truncate(s.name, 20):<20}"
                f"{value:>12} "
                f"{truncate(s.unit, 10):<11}"
            )
            safe_addstr(win, y, 2, line, base)
            x = 2 + len(line)
            safe_addstr(win, y, x, f"{s.status:<4}",
                        base if selected else status_attr(s.status))
            x += 4
            crit = f"{s.upper_crit:.1f}" if s.upper_crit is not None else "—"
            safe_addstr(win, y, x, f"{crit:>9}  ", base)
            x += 11

            # Headroom to critical, as a bar: the quickest way to spot a
            # sensor that is technically 'ok' but running out of margin.
            headroom = s.headroom()
            if headroom is not None and s.upper_crit:
                bar_width = max(6, width - x - 14)
                used = s.value / s.upper_crit if s.upper_crit else 0
                if used >= 0.95:
                    bar_attr = color(CP_CRIT, bold=True)
                elif used >= 0.85:
                    bar_attr = color(CP_WARN)
                else:
                    bar_attr = color(CP_OK)
                safe_addstr(win, y, x, hbar(s.value, s.upper_crit, bar_width),
                            base if selected else bar_attr)
                safe_addstr(win, y, x + bar_width + 1, f"{headroom:+.0f}",
                            base if selected else color(CP_DIM))

        # ---- summary bar ----
        y = height - 3
        draw_box(win, y, 0, 3, width, "", color(CP_DIM))
        readable = [s for s in all_sensors if s.readable]
        temps = [s for s in readable if s.kind == "temperature"]
        fans = [s for s in readable if s.kind == "fan"]
        parts = [f"{len(all_sensors)} sensors", f"{len(readable)} readable"]
        if temps:
            hottest = max(temps, key=lambda s: s.value)
            parts.append(f"hottest {hottest.name} {hottest.value:.0f}°C")
        if fans:
            parts.append(f"{len(fans)} fans, max {max(f.value for f in fans):.0f} RPM")
        safe_addstr(win, y + 1, 2, truncate("   ".join(parts), width - 20),
                    color(CP_NORMAL))
        if faulted:
            safe_addstr(win, y + 1, width - 18,
                        f"{len(faulted)} FAULTED", color(CP_CRIT, bold=True))
        else:
            safe_addstr(win, y + 1, width - 18, "all nominal", color(CP_OK))

    def handle_key(self, key: int) -> bool:
        height, _ = self.app.content_size()
        visible = max(1, height - 6)
        if self.handle_list_key(key, len(self.visible_sensors()), visible):
            return True
        if key == ord("f"):
            self.kind_index = (self.kind_index + 1) % len(KINDS)
            self.cursor = self.scroll = 0
            return True
        if key == ord("F"):
            self.kind_index = (self.kind_index - 1) % len(KINDS)
            self.cursor = self.scroll = 0
            return True
        if key == ord("o"):
            self.faults_only = not self.faults_only
            self.cursor = self.scroll = 0
            return True
        return False
