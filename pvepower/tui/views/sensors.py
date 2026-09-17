"""传感器视图：完整的 IPMI 传感器表，可按类别筛选。"""

from __future__ import annotations

import curses

from ..widgets import (
    CP_ACCENT,
    CP_BORDER,
    CP_CRIT,
    CP_DIM,
    CP_HIGHLIGHT,
    CP_NORMAL,
    CP_OK,
    CP_TITLE,
    CP_WARN,
    CP_CRIT_BAR,
    CP_WARN_BAR,
    CP_OK_BAR,
    color,
    draw_bar,
    cwidth,
    draw_box,
    hbar,
    highlight_row,
    pad,
    panel,
    rpad,
    safe_addstr,
    table_header,
    status_attr,
    truncate,
)
from .base import View

KINDS = ["all", "temperature", "voltage", "fan", "power", "current", "discrete"]

KIND_LABELS = {
    "all": "全部",
    "temperature": "温度",
    "voltage": "电压",
    "fan": "风扇",
    "power": "功率",
    "current": "电流",
    "discrete": "离散量",
}


class SensorsView(View):
    title = "传感器"
    hotkeys = [("f", "筛选"), ("o", "只看故障")]

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

        title = f"传感器（筛选：{KIND_LABELS[KINDS[self.kind_index]]}）"
        if self.faults_only:
            title = title[:-1] + "，只看故障）"
        panel(win, 0, 0, height - 3, width, title)
        table_header(
            win, 1, 2,
            pad("传感器名称", 20) + rpad("读数", 12) + " "
            + pad("单位", 11) + pad("状态", 6) + rpad("阈值", 9) + "  余量",
            width - 4,
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
            if selected:
                highlight_row(win, y, 1, width - 2)
            base = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)

            value = f"{s.value:.2f}" if s.value is not None else "—"
            # pad/rpad measure terminal columns, which `:<20` and `:>12`
            # do not: those count characters and would misplace every
            # column after a CJK sensor name.
            line = (
                pad(truncate(s.name, 20), 20)
                + rpad(value, 12) + " "
                + pad(truncate(s.unit, 10), 11)
            )
            safe_addstr(win, y, 2, line, base)
            x = 2 + cwidth(line)
            safe_addstr(win, y, x, pad(s.status, 6),
                        base if selected else status_attr(s.status))
            x += 6
            crit = f"{s.upper_crit:.1f}" if s.upper_crit is not None else "—"
            safe_addstr(win, y, x, rpad(crit, 9) + "  ", base)
            x += 11

            # Headroom to critical, as a bar: the quickest way to spot a
            # sensor that is technically 'ok' but running out of margin.
            headroom = s.headroom()
            fraction = s.bar_fraction()
            if headroom is not None and fraction is not None:
                bar_width = max(6, width - x - 14)
                # 颜色跟着 BMC 给的阈值走，不按「占上限多少比例」推算——
                # 后者会把标称 12V 的电压轨（上限临界 14.3V，读数正好在
                # 量程 83% 处）画成告警，而它其实是最健康的值。
                # 条形用「图形档」的色（3:1），比文字档鲜一点；选中行整行
                # 反白，这时候不画轨道，否则轨道的灰会在反白底上开个洞。
                bar_attr = {
                    "crit": color(CP_CRIT_BAR),
                    "warn": color(CP_WARN_BAR),
                }.get(s.severity(), color(CP_OK_BAR))
                if selected:
                    safe_addstr(win, y, x,
                                pad(hbar(fraction, 1.0, bar_width),
                                    bar_width), base)
                else:
                    draw_bar(win, y, x, fraction, 1.0, bar_width, bar_attr)
                safe_addstr(win, y, x + bar_width + 1, rpad(f"{headroom:+.0f}", 4),
                            base if selected else color(CP_DIM))

        # ---- summary bar ----
        y = height - 3
        draw_box(win, y, 0, 3, width, "", color(CP_BORDER))
        readable = [s for s in all_sensors if s.readable]
        temps = [s for s in readable if s.kind == "temperature"]
        fans = [s for s in readable if s.kind == "fan"]
        parts = [f"共 {len(all_sensors)} 个传感器", f"{len(readable)} 个有读数"]
        if temps:
            hottest = max(temps, key=lambda s: s.value)
            parts.append(f"最高温 {hottest.name} {hottest.value:.0f}°C")
        if fans:
            parts.append(f"{len(fans)} 个风扇，最高 {max(f.value for f in fans):.0f} RPM")
        safe_addstr(win, y + 1, 2, truncate("   ".join(parts), width - 24),
                    color(CP_NORMAL))
        if faulted:
            safe_addstr(win, y + 1, width - 22,
                        f"{len(faulted)} 项故障", color(CP_CRIT, bold=True))
        else:
            safe_addstr(win, y + 1, width - 22, "全部正常", color(CP_OK))

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
