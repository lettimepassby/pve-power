"""电价视图：编辑电价与采集器设置。

只改电价并不会改变历史：已存储的采样保留它们当初计价时的费用。
`R` 动作按当前电价对整个数据库重新计价 —— 当发现电价此前一直
填错时这么做是对的；而当电价只是在中途发生了真实变化时，这么做
是错的。因此它是一个单独的、需要确认的动作，而不是保存配置的
副作用。

电价模式（tariff.mode）里存的值 "flat" / "tou" 会写入 JSON 并被
代码比较，只做中文显示，不改动其存储值。
"""

from __future__ import annotations

import curses
import datetime as dt

from ...config import default_china_tou, Tier, TouPeriod
from ..widgets import (
    CP_ACCENT,
    CP_CRIT,
    CP_DIM,
    CP_HIGHLIGHT,
    CP_NORMAL,
    CP_OK,
    CP_TITLE,
    CP_WARN,
    Prompt,
    choose,
    color,
    confirm,
    cwidth,
    highlight_row,
    pad,
    panel,
    rpad,
    safe_addstr,
    table_header,
    show_message,
    truncate,
)
from .base import View

# 计费模式的显示名。存储值仍是 "flat" / "tou"，只有显示用中文。
MODE_LABELS = {"flat": "单一电价", "tou": "分时电价"}


def _validate_price(value: str) -> str:
    try:
        v = float(value.strip())
    except ValueError:
        return "请输入一个数字"
    if v < 0:
        return "电价不能为负数"
    if v > 100:
        return "这个数值不合常理 —— 请输入每 kWh 的电价，而不是每月电费"
    return ""


def _validate_hours(value: str) -> str:
    """Accepts '0-6,22,23' style hour specifications."""
    try:
        parse_hours(value)
    except ValueError as exc:
        return str(exc)
    return ""


def parse_hours(value: str) -> list[int]:
    hours: set[int] = set()
    for chunk in value.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            if not lo.isdigit() or not hi.isdigit():
                raise ValueError(f"范围 '{chunk}' 格式不对")
            lo_i, hi_i = int(lo), int(hi)
            if not (0 <= lo_i <= 23 and 0 <= hi_i <= 23) or lo_i > hi_i:
                raise ValueError(f"范围 '{chunk}' 必须在 0-23 之间且从小到大")
            hours.update(range(lo_i, hi_i + 1))
        else:
            if not chunk.isdigit() or not 0 <= int(chunk) <= 23:
                raise ValueError(f"'{chunk}' 不是 0-23 之间的小时")
            hours.add(int(chunk))
    if not hours:
        raise ValueError("请至少指定一个小时")
    return sorted(hours)


def format_hours(hours: list[int]) -> str:
    """Collapse a sorted hour list back into range notation."""
    if not hours:
        return "-"
    parts = []
    start = previous = hours[0]
    for hour in hours[1:]:
        if hour == previous + 1:
            previous = hour
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = hour
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


class TariffView(View):
    title = "电价"
    hotkeys = [("Enter", "编辑"), ("s", "保存"), ("R", "重算")]

    def _rows(self):
        t = self.app.config.tariff
        c = self.app.config.collector
        rows = [
            ("mode", "计费模式", MODE_LABELS.get(t.mode, t.mode)),
            ("currency", "币种", t.currency),
            ("flat_price", "基础电价（每 kWh）", f"{t.flat_price:.4f}"),
            ("service", "月固定费用", f"{t.monthly_service_fee:.2f}"),
            ("tiered", "阶梯电价", "开" if t.tiered_enabled else "关"),
            ("interval", "采样间隔", f"{c.interval_seconds} 秒"),
            ("gap", "缺口阈值", f"{c.max_gap_seconds} 秒"),
            ("retention", "原始数据保留",
             f"{c.retention_days} 天" if c.retention_days else "不限制"),
        ]
        return rows

    def draw(self, win, height: int, width: int) -> None:
        cfg = self.app.config
        t = cfg.tariff
        half = width // 2

        rows = self._rows()
        panel(win, 0, 0, 11, half, "设置")
        for i, (_, label, value) in enumerate(rows):
            y = 1 + i
            if y >= 10:
                break
            selected = i == self.cursor
            if selected:
                highlight_row(win, y, 1, half - 2)
            attr = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)
            safe_addstr(win, y, 2, pad(label, 22),
                        attr if selected else color(CP_DIM))
            safe_addstr(win, y, 25, pad(truncate(str(value), half - 27), half - 27),
                        attr)

        # ---- current price ----
        panel(win, 0, half, 11, width - half, "当前电价")
        now = dt.datetime.now()
        mtd = self.data.month.kwh
        price = t.price_at(now, mtd)
        base, label = t.base_price(now)
        surcharge, tier_label = t.tier_surcharge(mtd)
        lines = [
            ("时刻", now.strftime("%H:%M  %A")),
            ("当前时段", label),
            ("基础电价", f"{base:.4f} {t.currency}/kWh"),
        ]
        if t.tiered_enabled:
            lines.append(("本月累计", f"{mtd:.1f} kWh"))
            lines.append(("阶梯", f"{tier_label}（加价 {surcharge:.4f}）"))
        lines.append(("生效电价", f"{price:.4f} {t.currency}/kWh"))
        watts = self.data.current_watts()
        if watts:
            hourly = watts / 1000.0 * price
            lines.append(("按当前负载", f"{hourly:.3f} {t.currency}/小时"))
            lines.append(("", f"{hourly * 24:.2f} {t.currency}/天  "
                              f"{hourly * 24 * 30:.0f} {t.currency}/月"))
        for i, (k, v) in enumerate(lines):
            y = 1 + i
            if y >= 10:
                break
            safe_addstr(win, y, half + 2, pad(k, 16), color(CP_DIM))
            attr = color(CP_OK, bold=True) if k == "生效电价" else color(CP_NORMAL)
            safe_addstr(win, y, half + 19, truncate(v, width - half - 21), attr)

        # ---- schedule ----
        row = 11
        remaining = height - row
        if t.mode == "tou":
            self._draw_tou(win, row, 0, remaining, width)
        else:
            self._draw_tiers(win, row, 0, remaining, width)

    def _draw_tou(self, win, y, x, h, w):
        t = self.app.config.tariff
        panel(win, y, x, h, w, "分时电价时段表")
        if not t.tou_periods:
            safe_addstr(win, y + 2, x + 2,
                        "尚未定义任何时段。按 P 载入示例的中国分时电价方案，"
                        "或按 a 添加一个时段。",
                        color(CP_WARN))
            return

        # 列宽（终端列，不是字符数）：时段名 14 + 电价 12 + 间隔 2 +
        # 小时 30 + 星期 14 = 58，与下方的阶梯电价表共用同一套列宽。
        table_header(win, y + 1, x + 2,
                     pad("时段", 14) + rpad("电价", 12) + "  "
                     + pad("覆盖小时", 30) + "星期", w - 4)
        line = y + 2
        for period in t.tou_periods:
            if line >= y + h - 4:
                break
            days = "每天" if not period.days else "、".join(
                ["一", "二", "三", "四", "五", "六", "日"][d] for d in period.days
            )
            safe_addstr(
                win, line, x + 2,
                pad(truncate(period.name or "-", 13), 14)
                + rpad(f"{period.price:.4f}", 12) + "  "
                + pad(truncate(format_hours(period.hours), 29), 30)
                + days,
                color(CP_NORMAL),
            )
            line += 1

        # A 24-cell strip showing which window each hour falls in makes an
        # accidental gap in coverage obvious at a glance.
        if line < y + h - 2:
            line += 1
            cov_label = "覆盖"
            # The strip and its hour axis must start at the same column,
            # and the label is Chinese, so measure it in cells.
            strip_x = x + 2 + cwidth(cov_label) + 2
            safe_addstr(win, line, x + 2, cov_label, color(CP_DIM))
            names = {}
            for idx, period in enumerate(t.tou_periods):
                for hour in period.hours:
                    names.setdefault(hour, idx)
            strip = ""
            for hour in range(24):
                strip += "▒" if hour not in names else "█"
            safe_addstr(win, line, strip_x, strip, color(CP_ACCENT))
            safe_addstr(win, line + 1, strip_x, "0   4   8   12  16  20  ",
                        color(CP_DIM))
            missing = [h for h in range(24) if h not in names]
            if missing:
                safe_addstr(win, line + 1, strip_x + 26,
                            f"⚠ 小时 {format_hours(missing)} 未被任何时段覆盖，"
                            "将按基础电价计费",
                            color(CP_WARN, bold=True))

        safe_addstr(win, y + h - 2, x + 2,
                    "a：添加时段   x：删除时段   P：载入示例   "
                    "m：切换到单一电价",
                    color(CP_DIM))

    def _draw_tiers(self, win, y, x, h, w):
        t = self.app.config.tariff
        panel(win, y, x, h, w, "阶梯电价（按本月累计电量分档）")
        if not t.tiered_enabled:
            safe_addstr(win, y + 2, x + 2,
                        "阶梯电价当前是关闭的。选中上方的“阶梯电价”并按 Enter，"
                        "即可打开。",
                        color(CP_DIM))
        elif not t.tiers:
            safe_addstr(win, y + 2, x + 2,
                        "阶梯电价已启用，但尚未定义任何阶梯 —— 按 a 添加一个。",
                        color(CP_WARN))
        else:
            table_header(win, y + 1, x + 2,
                         pad("阶梯", 14) + rpad("上限（kWh）", 16)
                         + rpad("加价", 14) + rpad("生效电价", 14), w - 4)
            line = y + 2
            mtd = self.data.month.kwh
            for tier in t.tiers:
                if line >= y + h - 3:
                    break
                limit = "无上限" if tier.limit_kwh is None else f"{tier.limit_kwh:.0f}"
                active = (tier.limit_kwh is None or mtd < tier.limit_kwh)
                # Only the first matching tier is the live one.
                attr = color(CP_OK, bold=True) if active else color(CP_NORMAL)
                safe_addstr(
                    win, line, x + 2,
                    pad(truncate(tier.name or "-", 13), 14)
                    + rpad(limit, 16)
                    + rpad(f"{tier.surcharge:+.4f}", 14)
                    + rpad(f"{t.flat_price + tier.surcharge:.4f}", 14),
                    attr,
                )
                if active:
                    safe_addstr(win, line, x + 62, "← 当前阶梯",
                                color(CP_OK, bold=True))
                    break
                line += 1
        safe_addstr(win, y + h - 2, x + 2,
                    "a：添加阶梯   x：删除阶梯   m：切换到分时电价",
                    color(CP_DIM))

    # ---------------- keys ----------------

    def handle_key(self, key: int) -> bool:
        rows = self._rows()
        if self.handle_list_key(key, len(rows), len(rows)):
            return True
        if key in (curses.KEY_ENTER, 10, 13):
            self._edit(rows[self.cursor][0])
            return True
        if key == ord("s"):
            self.app.save_config()
            return True
        if key == ord("R"):
            self._reprice()
            return True
        if key == ord("m"):
            t = self.app.config.tariff
            t.mode = "tou" if t.mode == "flat" else "flat"
            self.app.flash(
                f"计费模式：{MODE_LABELS.get(t.mode, t.mode)}（按 s 保存）"
            )
            return True
        if key == ord("P"):
            self._load_preset()
            return True
        if key == ord("a"):
            if self.app.config.tariff.mode == "tou":
                self._add_period()
            else:
                self._add_tier()
            return True
        if key == ord("x"):
            if self.app.config.tariff.mode == "tou":
                self._remove_period()
            else:
                self._remove_tier()
            return True
        return False

    def _edit(self, field: str):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)
        cfg = self.app.config
        t = cfg.tariff

        if field == "mode":
            idx = choose(stdscr, "计费模式",
                         ["单一电价 —— 全天同一价格",
                          "分时电价 —— 按时段区分价格"])
            if idx is not None:
                t.mode = ["flat", "tou"][idx]
        elif field == "currency":
            value = prompt.ask("币种代码：", t.currency)
            if value:
                t.currency = value.strip().upper()
        elif field == "flat_price":
            value = prompt.ask(
                f"基础电价，每 kWh（{t.currency}）：",
                f"{t.flat_price:.4f}", validator=_validate_price,
            )
            if value is not None:
                t.flat_price = float(value.strip())
        elif field == "service":
            value = prompt.ask(
                f"月固定费用（{t.currency}）：",
                f"{t.monthly_service_fee:.2f}",
                validator=lambda v: "" if _is_number(v) else "请输入一个数字",
            )
            if value is not None:
                t.monthly_service_fee = float(value.strip())
        elif field == "tiered":
            t.tiered_enabled = not t.tiered_enabled
        elif field == "interval":
            value = prompt.ask(
                "采样间隔（秒）：",
                str(cfg.collector.interval_seconds),
                validator=lambda v: "" if v.strip().isdigit() and int(v) >= 5
                else "请输入整数秒数，且不小于 5",
            )
            if value is not None:
                cfg.collector.interval_seconds = int(value.strip())
                self.app.flash(
                    "新的采样间隔要重启采集器后才会生效"
                )
        elif field == "gap":
            value = prompt.ask(
                "采样间隔超过多少秒就视为停机（缺口）？",
                str(cfg.collector.max_gap_seconds),
                validator=lambda v: "" if v.strip().isdigit() and int(v) > 0
                else "请输入整数秒数",
            )
            if value is not None:
                cfg.collector.max_gap_seconds = int(value.strip())
        elif field == "retention":
            # 轮换只删原始采样；天级汇总（daily_rollup）永久保留，
            # 所以这里改小不会让历史电费统计消失，只是没有分钟级细节了。
            def _validate_retention(v: str) -> str:
                v = v.strip()
                if v in ("", "0", "不限制"):
                    return ""
                if not v.isdigit():
                    return "请输入整数天数，或留空表示不限制"
                if int(v) < 7:
                    return "保留天数不能少于 7 天"
                return ""

            current = (str(cfg.collector.retention_days)
                       if cfg.collector.retention_days else "")
            value = prompt.ask(
                "原始采样保留多少天？（留空＝不限制；天级汇总始终保留）",
                current, validator=_validate_retention,
            )
            if value is not None:
                v = value.strip()
                cfg.collector.retention_days = int(v) if v and v != "0" else None
                self.app.flash("新的保留天数要重启采集器后才会生效")

        problems = cfg.validate()
        if problems:
            self.app.flash(problems[0], ok=False)
        else:
            self.app.flash("已修改 —— 按 s 保存到磁盘")

    def _add_period(self):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)
        t = self.app.config.tariff
        name = prompt.ask("时段名称（例如 尖峰 / 高峰 / 平段 / 低谷）：")
        if not name:
            return
        price = prompt.ask(
            f"“{name.strip()}”的电价，每 kWh（{t.currency}）：",
            validator=_validate_price,
        )
        if price is None:
            return
        hours = prompt.ask(
            "该时段覆盖的小时（例如 8-11,18-21）：", validator=_validate_hours
        )
        if hours is None:
            return
        t.tou_periods.append(
            TouPeriod(
                name=name.strip(),
                price=float(price.strip()),
                hours=parse_hours(hours),
            )
        )
        self.app.flash(f"已添加时段“{name.strip()}”—— 按 s 保存")

    def _remove_period(self):
        t = self.app.config.tariff
        if not t.tou_periods:
            return
        labels = [
            f"{p.name}  电价 {p.price:.4f}  小时 {format_hours(p.hours)}"
            for p in t.tou_periods
        ]
        idx = choose(self.app.stdscr, "删除哪个时段？", labels)
        if idx is None:
            return
        removed = t.tou_periods.pop(idx)
        self.app.flash(f"已删除时段“{removed.name}”—— 按 s 保存")

    def _add_tier(self):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)
        t = self.app.config.tariff
        name = prompt.ask("阶梯名称（例如 第一档）：")
        if name is None:
            return
        limit = prompt.ask(
            "该阶梯覆盖到本月累计多少 kWh（留空表示不设上限，即最高阶梯）：",
            validator=lambda v: "" if not v.strip() or _is_number(v)
            else "请输入一个数字，或留空",
        )
        if limit is None:
            return
        surcharge = prompt.ask(
            f"在基础电价之上每 kWh 的加价（{t.currency}）：", "0.0",
            validator=lambda v: "" if _is_number(v) else "请输入一个数字",
        )
        if surcharge is None:
            return
        t.tiers.append(
            Tier(
                limit_kwh=None if not limit.strip() else float(limit.strip()),
                surcharge=float(surcharge.strip()),
                name=name.strip(),
            )
        )
        # Keep bounded tiers ordered, unbounded last, so lookup is correct.
        t.tiers.sort(key=lambda x: (x.limit_kwh is None, x.limit_kwh or 0))
        t.tiered_enabled = True
        self.app.flash("已添加阶梯 —— 按 s 保存")

    def _remove_tier(self):
        t = self.app.config.tariff
        if not t.tiers:
            return
        labels = [
            f"{tier.name or '-'}  上限 "
            f"{'无上限' if tier.limit_kwh is None else tier.limit_kwh}  "
            f"加价 {tier.surcharge:+.4f}"
            for tier in t.tiers
        ]
        idx = choose(self.app.stdscr, "删除哪个阶梯？", labels)
        if idx is None:
            return
        t.tiers.pop(idx)
        self.app.flash("已删除阶梯 —— 按 s 保存")

    def _load_preset(self):
        stdscr = self.app.stdscr
        if not confirm(
            stdscr,
            "用示例的中国分时电价方案替换当前电价吗？方案里的价格只是示例，"
            "必须改成与你自己的账单一致。",
        ):
            return
        preset = default_china_tou()
        preset.currency = self.app.config.tariff.currency
        self.app.config.tariff = preset
        self.app.flash("已载入示例方案 —— 请修改价格，再按 s 保存")

    def _reprice(self):
        stdscr = self.app.stdscr
        stats = self.app.storage.stats()
        if not confirm(
            stdscr,
            f"要按当前电价重新计算已存储的全部 {stats.get('samples', '0')} 条"
            "采样的费用吗？已记录的电量不会改变，只改费用。"
            "仅当此前录入的电价一直是错的时才这么做；"
            "如果电价只是后来发生了真实调整，就不要重算 —— "
            "每条采样都已按采样当时生效的电价计过费了。",
            danger=True,
        ):
            return
        count = self.app.storage.reprice(self.app.config.tariff)
        self.data.invalidate("energy")
        show_message(stdscr, "重算完成", f"已重新计算 {count} 条采样的费用。")


def _is_number(value: str) -> bool:
    try:
        float(value.strip())
        return True
    except ValueError:
        return False
