"""Tariff view: edit electricity pricing and collector settings.

Changing a price does not change history by itself: stored samples keep
the cost they were billed at. The `R` action re-prices the whole database
under the current tariff, which is the right move after correcting a rate
that was wrong all along, and the wrong move after a genuine rate change
partway through — hence it is a separate, confirmed action rather than a
side effect of saving.
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
    draw_box,
    hbar,
    pad,
    safe_addstr,
    show_message,
    truncate,
)
from .base import View


def _validate_price(value: str) -> str:
    try:
        v = float(value.strip())
    except ValueError:
        return "Enter a number"
    if v < 0:
        return "Price cannot be negative"
    if v > 100:
        return "That looks implausible — enter price per kWh, not per month"
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
                raise ValueError(f"bad range '{chunk}'")
            lo_i, hi_i = int(lo), int(hi)
            if not (0 <= lo_i <= 23 and 0 <= hi_i <= 23) or lo_i > hi_i:
                raise ValueError(f"range '{chunk}' must be within 0-23, ascending")
            hours.update(range(lo_i, hi_i + 1))
        else:
            if not chunk.isdigit() or not 0 <= int(chunk) <= 23:
                raise ValueError(f"'{chunk}' is not an hour 0-23")
            hours.add(int(chunk))
    if not hours:
        raise ValueError("specify at least one hour")
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
    title = "Tariff"
    hotkeys = [("Enter", "edit"), ("s", "save"), ("R", "reprice")]

    def _rows(self):
        t = self.app.config.tariff
        c = self.app.config.collector
        rows = [
            ("mode", "Billing mode", t.mode),
            ("currency", "Currency", t.currency),
            ("flat_price", "Base price per kWh", f"{t.flat_price:.4f}"),
            ("service", "Monthly service fee", f"{t.monthly_service_fee:.2f}"),
            ("tiered", "Tiered pricing", "on" if t.tiered_enabled else "off"),
            ("interval", "Sample interval", f"{c.interval_seconds}s"),
            ("gap", "Gap threshold", f"{c.max_gap_seconds}s"),
        ]
        return rows

    def draw(self, win, height: int, width: int) -> None:
        cfg = self.app.config
        t = cfg.tariff
        half = width // 2

        rows = self._rows()
        draw_box(win, 0, 0, 11, half, "Settings",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        for i, (_, label, value) in enumerate(rows):
            y = 1 + i
            if y >= 10:
                break
            selected = i == self.cursor
            attr = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)
            safe_addstr(win, y, 2, pad(label, 22),
                        attr if selected else color(CP_DIM))
            safe_addstr(win, y, 25, pad(truncate(str(value), half - 27), half - 27),
                        attr)

        # ---- current price ----
        draw_box(win, 0, half, 11, width - half, "Right now",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        now = dt.datetime.now()
        mtd = self.data.month.kwh
        price = t.price_at(now, mtd)
        base, label = t.base_price(now)
        surcharge, tier_label = t.tier_surcharge(mtd)
        lines = [
            ("Time", now.strftime("%H:%M  %A")),
            ("Active window", label),
            ("Base rate", f"{base:.4f} {t.currency}/kWh"),
        ]
        if t.tiered_enabled:
            lines.append(("Month-to-date", f"{mtd:.1f} kWh"))
            lines.append(("Tier", f"{tier_label} (+{surcharge:.4f})"))
        lines.append(("Effective", f"{price:.4f} {t.currency}/kWh"))
        watts = self.data.current_watts()
        if watts:
            hourly = watts / 1000.0 * price
            lines.append(("At current load", f"{hourly:.3f} {t.currency}/hour"))
            lines.append(("", f"{hourly * 24:.2f} {t.currency}/day  "
                              f"{hourly * 24 * 30:.0f} {t.currency}/month"))
        for i, (k, v) in enumerate(lines):
            y = 1 + i
            if y >= 10:
                break
            safe_addstr(win, y, half + 2, pad(k, 16), color(CP_DIM))
            attr = color(CP_OK, bold=True) if k == "Effective" else color(CP_NORMAL)
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
        draw_box(win, y, x, h, w, "Time-of-use schedule",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        if not t.tou_periods:
            safe_addstr(win, y + 2, x + 2,
                        "No periods defined. Press 'P' to load a sample "
                        "Chinese TOU schedule, or 'a' to add one.",
                        color(CP_WARN))
            return

        safe_addstr(win, y + 1, x + 2,
                    pad(f"{'Period':<12}{'Price':>10}  {'Hours':<28}Days", w - 4),
                    color(CP_DIM, bold=True))
        line = y + 2
        for period in t.tou_periods:
            if line >= y + h - 4:
                break
            days = "all" if not period.days else ",".join(
                ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"][d] for d in period.days
            )
            safe_addstr(
                win, line, x + 2,
                f"{truncate(period.name, 11):<12}"
                f"{period.price:>10.4f}  "
                f"{truncate(format_hours(period.hours), 27):<28}"
                f"{days}",
                color(CP_NORMAL),
            )
            line += 1

        # A 24-cell strip showing which window each hour falls in makes an
        # accidental gap in coverage obvious at a glance.
        if line < y + h - 2:
            line += 1
            safe_addstr(win, line, x + 2, "Coverage", color(CP_DIM))
            names = {}
            for idx, period in enumerate(t.tou_periods):
                for hour in period.hours:
                    names.setdefault(hour, idx)
            strip = ""
            for hour in range(24):
                strip += "▒" if hour not in names else "█"
            safe_addstr(win, line, x + 12, strip, color(CP_ACCENT))
            safe_addstr(win, line + 1, x + 12, "0   4   8   12  16  20  ",
                        color(CP_DIM))
            missing = [h for h in range(24) if h not in names]
            if missing:
                safe_addstr(win, line + 1, x + 40,
                            f"⚠ hours {format_hours(missing)} bill at base price",
                            color(CP_WARN, bold=True))

        safe_addstr(win, y + h - 2, x + 2,
                    "a: add period   x: remove period   P: load preset   "
                    "m: switch to flat",
                    color(CP_DIM))

    def _draw_tiers(self, win, y, x, h, w):
        t = self.app.config.tariff
        draw_box(win, y, x, h, w, "Tiered pricing (stepped by monthly total)",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        if not t.tiered_enabled:
            safe_addstr(win, y + 2, x + 2,
                        "Tiered pricing is off. Select 'Tiered pricing' above "
                        "and press Enter to turn it on.",
                        color(CP_DIM))
        elif not t.tiers:
            safe_addstr(win, y + 2, x + 2,
                        "Enabled but no tiers defined — press 'a' to add one.",
                        color(CP_WARN))
        else:
            safe_addstr(win, y + 1, x + 2,
                        pad(f"{'Tier':<14}{'Up to (kWh)':>14}{'Surcharge':>12}"
                            f"{'Effective':>12}", w - 4),
                        color(CP_DIM, bold=True))
            line = y + 2
            mtd = self.data.month.kwh
            for tier in t.tiers:
                if line >= y + h - 3:
                    break
                limit = "unlimited" if tier.limit_kwh is None else f"{tier.limit_kwh:.0f}"
                active = (tier.limit_kwh is None or mtd < tier.limit_kwh)
                # Only the first matching tier is the live one.
                attr = color(CP_OK, bold=True) if active else color(CP_NORMAL)
                safe_addstr(
                    win, line, x + 2,
                    f"{truncate(tier.name or '-', 13):<14}"
                    f"{limit:>14}"
                    f"{tier.surcharge:>+12.4f}"
                    f"{t.flat_price + tier.surcharge:>12.4f}",
                    attr,
                )
                if active:
                    safe_addstr(win, line, x + 56, "← current", color(CP_OK, bold=True))
                    break
                line += 1
        safe_addstr(win, y + h - 2, x + 2,
                    "a: add tier   x: remove tier   m: switch to time-of-use",
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
            self.app.flash(f"Billing mode: {t.mode} (press s to save)")
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
            idx = choose(stdscr, "Billing mode",
                         ["flat — one price at all hours",
                          "tou  — time-of-use windows"])
            if idx is not None:
                t.mode = ["flat", "tou"][idx]
        elif field == "currency":
            value = prompt.ask("Currency code:", t.currency)
            if value:
                t.currency = value.strip().upper()
        elif field == "flat_price":
            value = prompt.ask(
                f"Base price per kWh ({t.currency}):",
                f"{t.flat_price:.4f}", validator=_validate_price,
            )
            if value is not None:
                t.flat_price = float(value.strip())
        elif field == "service":
            value = prompt.ask(
                f"Monthly service fee ({t.currency}):",
                f"{t.monthly_service_fee:.2f}",
                validator=lambda v: "" if _is_number(v) else "Enter a number",
            )
            if value is not None:
                t.monthly_service_fee = float(value.strip())
        elif field == "tiered":
            t.tiered_enabled = not t.tiered_enabled
        elif field == "interval":
            value = prompt.ask(
                "Sample interval in seconds:",
                str(cfg.collector.interval_seconds),
                validator=lambda v: "" if v.strip().isdigit() and int(v) >= 5
                else "Enter a whole number of seconds, at least 5",
            )
            if value is not None:
                cfg.collector.interval_seconds = int(value.strip())
                self.app.flash(
                    "Restart the collector for the new interval to take effect"
                )
        elif field == "gap":
            value = prompt.ask(
                "Treat a sample gap longer than how many seconds as downtime?",
                str(cfg.collector.max_gap_seconds),
                validator=lambda v: "" if v.strip().isdigit() and int(v) > 0
                else "Enter a whole number of seconds",
            )
            if value is not None:
                cfg.collector.max_gap_seconds = int(value.strip())

        problems = cfg.validate()
        if problems:
            self.app.flash(problems[0], ok=False)
        else:
            self.app.flash("Changed — press 's' to save to disk")

    def _add_period(self):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)
        t = self.app.config.tariff
        name = prompt.ask("Period name (e.g. 高峰 / peak):")
        if not name:
            return
        price = prompt.ask(
            f"Price per kWh for '{name.strip()}' ({t.currency}):",
            validator=_validate_price,
        )
        if price is None:
            return
        hours = prompt.ask(
            "Hours it applies to (e.g. 8-11,18-21):", validator=_validate_hours
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
        self.app.flash(f"Added period '{name.strip()}' — press 's' to save")

    def _remove_period(self):
        t = self.app.config.tariff
        if not t.tou_periods:
            return
        labels = [
            f"{p.name}  {p.price:.4f}  hours {format_hours(p.hours)}"
            for p in t.tou_periods
        ]
        idx = choose(self.app.stdscr, "Remove which period?", labels)
        if idx is None:
            return
        removed = t.tou_periods.pop(idx)
        self.app.flash(f"Removed '{removed.name}' — press 's' to save")

    def _add_tier(self):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)
        t = self.app.config.tariff
        name = prompt.ask("Tier name (e.g. 第一档):")
        if name is None:
            return
        limit = prompt.ask(
            "Monthly kWh this tier covers up to (blank = unlimited, top tier):",
            validator=lambda v: "" if not v.strip() or _is_number(v)
            else "Enter a number, or leave blank",
        )
        if limit is None:
            return
        surcharge = prompt.ask(
            f"Surcharge per kWh on top of the base price ({t.currency}):", "0.0",
            validator=lambda v: "" if _is_number(v) else "Enter a number",
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
        self.app.flash("Tier added — press 's' to save")

    def _remove_tier(self):
        t = self.app.config.tariff
        if not t.tiers:
            return
        labels = [
            f"{tier.name or '-'}  up to "
            f"{'unlimited' if tier.limit_kwh is None else tier.limit_kwh}  "
            f"{tier.surcharge:+.4f}"
            for tier in t.tiers
        ]
        idx = choose(self.app.stdscr, "Remove which tier?", labels)
        if idx is None:
            return
        t.tiers.pop(idx)
        self.app.flash("Tier removed — press 's' to save")

    def _load_preset(self):
        stdscr = self.app.stdscr
        if not confirm(
            stdscr,
            "Replace the current tariff with the sample Chinese TOU schedule? "
            "Prices are examples and must be set to match your own bill.",
        ):
            return
        preset = default_china_tou()
        preset.currency = self.app.config.tariff.currency
        self.app.config.tariff = preset
        self.app.flash("Preset loaded — edit the prices, then press 's' to save")

    def _reprice(self):
        stdscr = self.app.stdscr
        stats = self.app.storage.stats()
        if not confirm(
            stdscr,
            f"Recalculate the cost of all {stats.get('samples', '0')} stored "
            "samples under the current tariff? Measured energy is unchanged. "
            "Do this only if the tariff was wrong all along, not after a "
            "genuine price change.",
            danger=True,
        ):
            return
        count = self.app.storage.reprice(self.app.config.tariff)
        self.data.invalidate("energy")
        show_message(stdscr, "Reprice complete", f"Recalculated {count} samples.")


def _is_number(value: str) -> bool:
    try:
        float(value.strip())
        return True
    except ValueError:
        return False
