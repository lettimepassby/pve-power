"""总览：实时功率、今日电费和机器健康状况一览。"""

from __future__ import annotations

import datetime as dt

from ..widgets import (
    CP_ACCENT,
    CP_CRIT,
    CP_DIM,
    CP_NORMAL,
    CP_OK,
    CP_TITLE,
    CP_WARN,
    color,
    draw_bar,
    cwidth,
    humanize_ago,
    pad,
    panel,
    rpad,
    safe_addstr,
    sparkline,
    status_attr,
    truncate,
)
from .base import View


class OverviewView(View):
    title = "总览"
    hotkeys = [("r", "刷新")]

    def draw(self, win, height: int, width: int) -> None:
        cfg = self.app.config
        cur = cfg.tariff.currency

        # ---- live power panel ----
        reading = self.data.power_reading
        watts = reading.instantaneous if reading and reading.valid else None
        box_h = 8
        panel(win, 0, 0, box_h, width, "实时功率")

        if watts is None:
            safe_addstr(win, 2, 2, "无法从 BMC 读取功率",
                        color(CP_CRIT, bold=True))
            if self.data.last_error:
                safe_addstr(win, 3, 2, truncate(self.data.last_error, width - 4),
                            color(CP_DIM))
        else:
            big = f"{watts} W"
            safe_addstr(win, 2, 2, big, color(CP_OK, bold=True))
            detail = (
                f"最低 {reading.minimum}W   最高 {reading.maximum}W   "
                f"平均 {reading.average}W   状态 {reading.state}"
            )
            safe_addstr(win, 2, 2 + cwidth(big) + 3,
                        truncate(detail, width - cwidth(big) - 8),
                        color(CP_DIM))

            # Draw the instantaneous value against the observed max so the
            # bar means something even on a machine that never idles low.
            ceiling = max(reading.maximum or 0, watts, 1)
            bar_width = max(10, width - 24)
            safe_addstr(win, 3, 2, "负载 ", color(CP_DIM))
            draw_bar(win, 3, 7, watts, ceiling, bar_width, color(CP_ACCENT))
            safe_addstr(win, 3, 7 + bar_width + 1, f"{watts / ceiling * 100:3.0f}%",
                        color(CP_DIM))

            history = self.data.watt_history
            if len(history) > 1:
                values = [w for _, w in history]
                spark_width = max(10, width - 20)
                safe_addstr(win, 5, 2, "趋势", color(CP_DIM))
                safe_addstr(win, 5, 8, sparkline(values, spark_width),
                            color(CP_ACCENT))
                caption = f"最近 {len(values)} 次采样："
                safe_addstr(win, 6, 8, caption, color(CP_DIM))
                safe_addstr(
                    win, 6, 8 + cwidth(caption) + 1,
                    f"{min(values):.0f}W .. {max(values):.0f}W",
                    color(CP_DIM),
                )

        # ---- cost panels ----
        row = box_h
        panel_h = 9
        half = width // 2
        self._draw_consumption(win, row, 0, panel_h, half, cur)
        self._draw_status(win, row, half, panel_h, width - half)

        # ---- collector health ----
        row += panel_h
        remaining = height - row
        if remaining >= 5:
            self._draw_health(win, row, 0, remaining, width)

    def _draw_consumption(self, win, y, x, h, w, cur) -> None:
        panel(win, y, x, h, w, "用电量")
        today = self.data.today
        month = self.data.month
        rows = [
            ("今日", today.kwh, today.cost, today.avg_watts),
            ("本月", month.kwh, month.cost, month.avg_watts),
        ]
        line = y + 2
        for label, kwh, cost, avg in rows:
            safe_addstr(win, line, x + 2, pad(label, 12), color(CP_NORMAL, bold=True))
            safe_addstr(win, line, x + 14, rpad(f"{kwh:.3f} kWh", 13), color(CP_ACCENT))
            safe_addstr(win, line, x + 28, rpad(f"{cost:.2f} {cur}", 14),
                        color(CP_OK, bold=True))
            line += 1
            if avg:
                safe_addstr(win, line, x + 4, f"平均 {avg:.0f}W", color(CP_DIM))
                line += 1

        # Projection is the honest kind: month-to-date rate extended to the
        # month's end, labelled as an estimate rather than presented as fact.
        projected = self.data.projected_month_cost()
        if projected is not None:
            safe_addstr(win, line, x + 2, "预计", color(CP_NORMAL, bold=True))
            safe_addstr(win, line, x + 14,
                        rpad(f"{self.data.projected_month_kwh():.1f} kWh", 13),
                        color(CP_DIM))
            safe_addstr(win, line, x + 28, rpad(f"{projected:.2f} {cur}", 14),
                        color(CP_WARN))
            line += 1
            safe_addstr(win, line, x + 4, "（按当前速率推算到月底）",
                        color(CP_DIM))

    def _draw_status(self, win, y, x, h, w, ) -> None:
        panel(win, y, x, h, w, "机器信息")
        chassis = self.data.chassis_status or {}
        fru = self.data.fru or {}
        line = y + 2
        items = [
            ("型号", fru.get("Product Name", "?")),
            ("序列号", fru.get("Product Serial", "?")),
            ("电源", chassis.get("System Power", "?")),
            ("恢复策略", chassis.get("Power Restore Policy", "?")),
            ("上次电源事件", chassis.get("Last Power Event", "-") or "-"),
        ]
        for label, value in items:
            if line >= y + h - 1:
                break
            safe_addstr(win, line, x + 2, pad(label, 15), color(CP_DIM))
            attr = status_attr(value) if label == "电源" else color(CP_NORMAL)
            safe_addstr(win, line, x + 18, truncate(value, w - 20), attr)
            line += 1

        faults = [
            ("电源过载", chassis.get("Power Overload")),
            ("主电源故障", chassis.get("Main Power Fault")),
            ("散热风扇故障", chassis.get("Cooling/Fan Fault")),
            ("硬盘故障", chassis.get("Drive Fault")),
        ]
        active = [name for name, value in faults if (value or "").lower() == "true"]
        if line < y + h - 1:
            if active:
                safe_addstr(win, line, x + 2,
                            truncate("故障：" + "、".join(active), w - 4),
                            color(CP_CRIT, bold=True))
            else:
                safe_addstr(win, line, x + 2, "机箱未报告故障",
                            color(CP_OK))

    def _draw_health(self, win, y, x, h, w) -> None:
        panel(win, y, x, h, w, "采集器")
        stats = self.data.storage_stats
        line = y + 2
        last = self.data.last_sample
        if last is None:
            safe_addstr(win, line, x + 2, "还没有任何采样记录。",
                        color(CP_WARN, bold=True))
            safe_addstr(win, line + 1, x + 2,
                        "启动采集器：systemctl start pve-power-collector",
                        color(CP_DIM))
            return

        age = dt.datetime.now().timestamp() - last.ts
        interval = self.app.config.collector.interval_seconds
        # Two missed intervals is the point at which a human should look.
        if age > interval * 3:
            attr, verdict = color(CP_CRIT, bold=True), "已停滞"
        elif age > interval * 2:
            attr, verdict = color(CP_WARN), "有延迟"
        else:
            attr, verdict = color(CP_OK), "正常"

        safe_addstr(win, line, x + 2, pad("上次采样", 15), color(CP_DIM))
        safe_addstr(win, line, x + 18,
                    f"{humanize_ago(last.ts)}  ({verdict})", attr)
        line += 1
        safe_addstr(win, line, x + 2, pad("今日覆盖率", 15), color(CP_DIM))
        cov = self.data.today_coverage * 100
        cov_attr = color(CP_OK) if cov > 95 else (
            color(CP_WARN) if cov > 70 else color(CP_CRIT))
        safe_addstr(win, line, x + 18, f"{cov:.1f}%", cov_attr)
        line += 1
        if line < y + h - 1:
            safe_addstr(win, line, x + 2, pad("已存储", 15), color(CP_DIM))
            safe_addstr(
                win, line, x + 18,
                f"{stats.get('samples', '0')} 条采样，"
                f"{stats.get('gaps', '0')} 处缺口，{stats.get('db_size', '-')}",
                color(CP_NORMAL),
            )
            line += 1
        if line < y + h - 1:
            tariff = self.app.config.tariff
            now = dt.datetime.now()
            desc = tariff.describe_at(now, self.data.month.kwh)
            safe_addstr(win, line, x + 2, pad("当前电价", 15), color(CP_DIM))
            safe_addstr(win, line, x + 18,
                        truncate(f"{desc} {tariff.currency}/kWh", w - 20),
                        color(CP_ACCENT))
