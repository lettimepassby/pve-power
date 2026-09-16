"""Cached access to BMC and database state.

The TUI redraws on a timer and on every keypress. Talking to the BMC
takes hundreds of milliseconds per call, so nothing in a draw path may
call ipmitool directly: everything goes through this cache, which
refreshes each kind of data on its own interval and serves the last good
value in between.

Refreshes happen on a worker thread so a slow or wedged BMC cannot
freeze the interface.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any, Optional

from ..config import Config
from ..ipmi import IpmiTool, IpmiError
from ..storage import Aggregate, Storage

# Seconds between refreshes, per data kind. Power moves constantly;
# inventory essentially never does.
INTERVALS = {
    "power": 5,
    "sensors": 15,
    "chassis": 20,
    "energy": 30,
    "sel": 60,
    "users": 120,
    "lan": 120,
    "identity": 600,
}


class DataCache:
    def __init__(self, config: Config, ipmi: IpmiTool, storage: Storage):
        self.config = config
        self.ipmi = ipmi
        self.storage = storage
        self._lock = threading.Lock()
        self._fetched: dict[str, float] = {}
        self._values: dict[str, Any] = {}
        self.last_error: str = ""
        self._refreshing: set[str] = set()

        # Energy aggregates are cheap enough to compute synchronously on
        # first use, so the first frame is never empty.
        self.today: Aggregate = Aggregate(label="today")
        self.month: Aggregate = Aggregate(label="month")
        self.today_coverage: float = 0.0
        self.storage_stats: dict[str, str] = {}
        self.watt_history: list[tuple[int, float]] = []
        self.last_sample = None
        self._daily_cache: dict[int, list[Aggregate]] = {}
        self._hourly_cache: dict[dt.date, list[Aggregate]] = {}

        self.refresh_energy()

    # ---------------- accessors ----------------

    @property
    def power_reading(self):
        return self._values.get("power")

    @property
    def sensors(self):
        return self._values.get("sensors")

    @property
    def chassis_status(self):
        return self._values.get("chassis")

    @property
    def mc_info(self):
        return (self._values.get("identity") or {}).get("mc")

    @property
    def fru(self):
        return (self._values.get("identity") or {}).get("fru")

    @property
    def lan_config(self):
        return self._values.get("lan")

    @property
    def users(self):
        return self._values.get("users")

    @property
    def sel_entries(self):
        return (self._values.get("sel") or {}).get("entries")

    @property
    def sel_info(self):
        return (self._values.get("sel") or {}).get("info")

    @property
    def power_policies(self):
        return (self._values.get("chassis_policies") or [])

    def current_watts(self) -> Optional[float]:
        reading = self.power_reading
        if reading and reading.valid:
            return float(reading.instantaneous)
        if self.last_sample:
            return self.last_sample.watts
        return None

    # ---------------- refresh machinery ----------------

    def stale(self, kind: str, now: Optional[float] = None) -> bool:
        now = now or dt.datetime.now().timestamp()
        last = self._fetched.get(kind, 0.0)
        return now - last >= INTERVALS.get(kind, 30)

    def invalidate(self, kind: str) -> None:
        """Force the next tick to refetch `kind`."""
        with self._lock:
            self._fetched.pop(kind, None)
        if kind == "energy":
            self._daily_cache.clear()
            self._hourly_cache.clear()

    def tick(self) -> None:
        """Kick off any refreshes that are due. Never blocks."""
        now = dt.datetime.now().timestamp()
        for kind in INTERVALS:
            if not self.stale(kind, now):
                continue
            with self._lock:
                if kind in self._refreshing:
                    continue
                self._refreshing.add(kind)
            thread = threading.Thread(
                target=self._refresh_one, args=(kind,), daemon=True
            )
            thread.start()

    def _refresh_one(self, kind: str) -> None:
        try:
            if kind == "energy":
                self.refresh_energy()
            else:
                value = self._fetch(kind)
                if value is not None:
                    with self._lock:
                        self._values[kind] = value
            with self._lock:
                self._fetched[kind] = dt.datetime.now().timestamp()
        except Exception as exc:  # A cache refresh must never kill the UI.
            self.last_error = str(exc)
            with self._lock:
                # Back off rather than hammering a failing BMC every tick.
                self._fetched[kind] = dt.datetime.now().timestamp()
        finally:
            with self._lock:
                self._refreshing.discard(kind)

    def _fetch(self, kind: str):
        if kind == "power":
            try:
                return self.ipmi.power_reading()
            except IpmiError as exc:
                self.last_error = str(exc)
                return None
        if kind == "sensors":
            ok, _ = self.ipmi.try_run("sensor")
            return self.ipmi.sensors() if ok else None
        if kind == "chassis":
            try:
                status = self.ipmi.chassis_status()
            except IpmiError:
                return None
            with self._lock:
                self._values["chassis_policies"] = self.ipmi.supported_power_policies()
            return status
        if kind == "identity":
            out = {}
            try:
                out["mc"] = self.ipmi.mc_info()
            except IpmiError:
                out["mc"] = {}
            try:
                out["fru"] = self.ipmi.fru()
            except IpmiError:
                out["fru"] = {}
            return out
        if kind == "lan":
            try:
                return self.ipmi.lan_config(self.config.ipmi.lan_channel)
            except IpmiError:
                return None
        if kind == "users":
            try:
                return self.ipmi.users(self.config.ipmi.lan_channel)
            except IpmiError:
                return None
        if kind == "sel":
            entries = self.ipmi.sel_entries(limit=300)
            try:
                info = self.ipmi.sel_info()
            except IpmiError:
                info = {}
            return {"entries": entries, "info": info}
        return None

    # ---------------- energy ----------------

    def refresh_energy(self) -> None:
        now = dt.datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today_start.replace(day=1)
        self.today = self.storage.aggregate_between(today_start, now, label="today")
        self.month = self.storage.aggregate_between(month_start, now, label="month")
        self.today_coverage = self.storage.coverage(today_start, now)
        self.storage_stats = self.storage.stats()
        self.watt_history = self.storage.recent_watts(180)
        self.last_sample = self.storage.last_sample()
        self._daily_cache.clear()
        self._hourly_cache.clear()

    def daily_series(self, days: int) -> list[Aggregate]:
        if days not in self._daily_cache:
            self._daily_cache[days] = self.storage.daily_series(days)
        return self._daily_cache[days]

    def hourly_series(self, day: dt.date) -> list[Aggregate]:
        if day not in self._hourly_cache:
            self._hourly_cache[day] = self.storage.hourly_series(day)
        return self._hourly_cache[day]

    def period_breakdown(self, start: dt.datetime, end: dt.datetime):
        return self.storage.period_breakdown(start, end)

    # ---------------- projections ----------------

    def _month_elapsed_fraction(self, now: Optional[dt.datetime] = None) -> float:
        now = now or dt.datetime.now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now.month == 12:
            end = start.replace(year=now.year + 1, month=1)
        else:
            end = start.replace(month=now.month + 1)
        total = (end - start).total_seconds()
        return ((now - start).total_seconds() / total) if total else 0.0

    def projected_month_kwh(self) -> float:
        """Month-end energy, extrapolated from the rate so far.

        Uses *covered* time rather than elapsed time, so a month whose
        collector was down for two days is not projected as though the
        machine had been idle then.
        """
        fraction = self._month_elapsed_fraction()
        if fraction <= 0:
            return 0.0
        return self.month.kwh / fraction

    def projected_month_cost(self) -> Optional[float]:
        fraction = self._month_elapsed_fraction()
        # Too early in the month for an extrapolation to mean anything.
        if fraction < 0.02 or self.month.kwh <= 0:
            return None
        projected = self.month.cost / fraction
        return projected + self.config.tariff.monthly_service_fee
