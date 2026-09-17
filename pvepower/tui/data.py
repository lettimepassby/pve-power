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
import shutil
import subprocess
import threading
from typing import Any, Optional

from ..config import Config
from ..ipmi import IpmiTool, IpmiError
from ..storage import Aggregate, Storage

# Seconds between refreshes, per data kind. Power moves constantly;
# inventory essentially never does.
#
# `energy` reads SQLite, not the BMC: measured at 0.006s for the whole
# refresh, against 5-7s for a `sensor` call. It was on a 30s interval,
# which meant the overview's "上次采样" age was computed from a sample
# record up to 30s old -- so with a 5s collector interval (staleness
# threshold 3x = 15s) the panel accused a perfectly healthy collector of
# having stalled. Refreshing it every 5s costs nothing and makes the age
# honest.
#
# `sensors` is the expensive one: `ipmitool sensor` measured 5-7s on this
# BMC, and the fan tab reads from the same cached table.
INTERVALS = {
    "power": 5,
    "sensors": 15,
    "chassis": 20,
    "energy": 5,
    "sel": 60,
    "fan_control": 3600,
    "users": 120,
    "lan": 120,
    "identity": 600,
    # 日报状态：问一次 systemctl 加一次 meta 表，都是毫秒级，但在 5Hz 的
    # 重绘路径上还是要缓存。人不会盯着定时器状态看，30 秒足够新。
    "report_status": 30,
}

# systemd 单元名，日报页用它显示定时器是不是真的armed。
REPORT_TIMER_UNIT = "pve-power-report.timer"


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
    def fans(self):
        """Fan readings, derived from the cached sensor table.

        Reuses the sensors already fetched for the sensors tab, so
        opening the fan tab costs no extra BMC round-trip.
        """
        sensors = self._values.get("sensors")
        if sensors is None:
            return None
        return IpmiTool.fans_from_sensors(sensors)

    @property
    def fan_control(self):
        return self._values.get("fan_control")

    @property
    def report_status(self) -> dict:
        """日报的运行状态。还没取到时返回空 dict，视图按「未知」显示。"""
        return self._values.get("report_status") or {}

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
        if kind == "fan_control":
            try:
                return self.ipmi.probe_fan_control()
            except IpmiError:
                return None
        if kind == "report_status":
            status = _systemd_timer_status(REPORT_TIMER_UNIT)
            # 上次发成功的日期由 mail-report 写进 meta；界面显示它，
            # 这样「今天到底发没发」不用去翻 journal。
            status["last_sent"] = self.storage.get_meta("last_report_sent")
            return status
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


def _systemd_timer_status(unit: str) -> dict:
    """查一个 systemd timer 的状态。

    拿不到就返回 {"available": False} —— 开发机上没有 systemctl，
    容器里也可能没有，这不是错误，界面据此换一种说法而已。
    """
    if not shutil.which("systemctl"):
        return {"available": False}
    props = ("UnitFileState", "ActiveState", "NextElapseUSecRealtime",
             "LastTriggerUSec", "LoadState")
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, *(f"--property={p}" for p in props)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {"available": False}
    status: dict = {"available": True}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key:
            status[key] = value.strip()
    # LoadState=not-found 说明单元文件根本没装（比如只升级了程序目录
    # 没跑安装脚本）。这和「装了但没启用」是两回事，要分开说。
    status["installed"] = status.get("LoadState") not in (None, "", "not-found")
    status["enabled"] = status.get("UnitFileState") == "enabled"
    return status
