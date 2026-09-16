"""Sampling daemon and legacy CSV import.

The collector runs as a systemd service rather than from cron. Cron's
finest granularity is one minute, and each invocation pays interpreter
startup; a long-lived loop samples on a steady cadence and keeps the
SQLite connection warm. It also means a missed sample is visible as a
gap rather than as silence.
"""

from __future__ import annotations

import csv
import datetime as dt
import glob
import os
import signal
import sys
import time
from typing import Optional

from .config import Config
from .ipmi import IpmiTool, IpmiError
from .storage import Storage, Sample


class Collector:
    def __init__(self, config: Config):
        self.config = config
        self.ipmi = IpmiTool(
            binary=config.ipmi.binary,
            host=config.ipmi.host,
            user=config.ipmi.user,
            password=config.ipmi.password,
            interface=config.ipmi.interface,
            timeout=config.ipmi.timeout,
        )
        self.storage = Storage(config.db_path)
        self._stop = False
        self.consecutive_failures = 0

    def request_stop(self, *_args) -> None:
        self._stop = True

    def sane(self, watts: float) -> bool:
        c = self.config.collector
        return c.min_watts <= watts <= c.max_watts

    def sample_once(self, when: Optional[dt.datetime] = None) -> Optional[Sample]:
        """Take one reading and integrate it. Returns None if unusable."""
        try:
            reading = self.ipmi.power_reading()
        except IpmiError as exc:
            self.consecutive_failures += 1
            if self.consecutive_failures in (1, 5, 20) or (
                self.consecutive_failures % 100 == 0
            ):
                self.storage.log_event(
                    "ipmi_error",
                    f"failure #{self.consecutive_failures}: {exc}",
                )
            return None

        if not reading.valid:
            self.consecutive_failures += 1
            self.storage.log_event("bad_reading", "no instantaneous value in DCMI output")
            return None

        watts = float(reading.instantaneous)
        if not self.sane(watts):
            self.storage.log_event(
                "out_of_range",
                f"discarded {watts}W outside "
                f"[{self.config.collector.min_watts}, {self.config.collector.max_watts}]",
            )
            return None

        if self.consecutive_failures:
            self.storage.log_event(
                "recovered", f"after {self.consecutive_failures} failed reads"
            )
            self.consecutive_failures = 0

        try:
            return self.storage.record(
                watts,
                self.config.tariff,
                when=when,
                max_gap_seconds=self.config.collector.max_gap_seconds,
                default_interval=self.config.collector.interval_seconds,
            )
        except ValueError as exc:
            self.storage.log_event("clock_skew", str(exc))
            return None

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        interval = self.config.collector.interval_seconds
        self.storage.log_event(
            "collector_start",
            f"interval={interval}s db={self.config.db_path}",
        )
        # Align to the interval boundary so samples land on tidy timestamps
        # and hourly buckets get even coverage.
        while not self._stop:
            started = time.time()
            self.sample_once()
            elapsed = time.time() - started
            sleep_for = max(1.0, interval - elapsed)
            # Sleep in slices so SIGTERM is honoured promptly.
            deadline = time.time() + sleep_for
            while not self._stop and time.time() < deadline:
                time.sleep(min(1.0, deadline - time.time()))
        self.storage.log_event("collector_stop", "terminated")
        self.storage.close()
        return 0


def import_legacy_csv(
    config: Config, directory: Optional[str] = None, verbose: bool = True
) -> tuple[int, int]:
    """Import the old cron script's CSVs into the database.

    The legacy format is `time,watts` with a local-time `%Y-%m-%d %H:%M:%S`
    stamp. Rows are integrated the same way live samples are, so imported
    history is billed identically to anything collected since — including
    the gap rule, which matters because the old 5-minute cron left holes
    whenever the host was down.

    Returns (imported, skipped).
    """
    directory = directory or config.legacy_csv_dir
    storage = Storage(config.db_path)
    existing = {
        int(r["ts"])
        for r in storage.conn.execute("SELECT ts FROM samples").fetchall()
    }

    rows: list[tuple[int, float]] = []
    for path in sorted(glob.glob(os.path.join(directory, "*.csv"))):
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    stamp = (row.get("time") or "").strip()
                    raw_watts = (row.get("watts") or "").strip()
                    if not stamp or not raw_watts:
                        continue
                    try:
                        when = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                        watts = float(raw_watts)
                    except ValueError:
                        continue
                    rows.append((int(when.timestamp()), watts))
        except OSError as exc:
            if verbose:
                print(f"skipping {path}: {exc}", file=sys.stderr)

    rows.sort()
    imported = 0
    skipped = 0
    previous: Optional[tuple[int, float]] = None
    mtd_month: Optional[tuple[int, int]] = None
    mtd = 0.0
    max_gap = config.collector.max_gap_seconds

    for ts, watts in rows:
        if ts in existing:
            skipped += 1
            previous = (ts, watts)
            continue
        if not config.collector.min_watts <= watts <= config.collector.max_watts:
            skipped += 1
            continue

        when = dt.datetime.fromtimestamp(ts)
        month_key = (when.year, when.month)
        if month_key != mtd_month:
            mtd_month = month_key
            # Seed from anything already stored for that month so tiered
            # pricing continues from the right place.
            mtd = storage.month_to_date_kwh(when)

        if previous is None:
            interval_s, kwh, is_gap = 0, 0.0, False
        else:
            interval_s = ts - previous[0]
            if interval_s <= 0:
                skipped += 1
                continue
            if interval_s > max_gap:
                interval_s, kwh, is_gap = interval_s, 0.0, True
            else:
                kwh = (previous[1] + watts) / 2.0 * interval_s / 3_600_000.0
                is_gap = False

        price = config.tariff.price_at(when, mtd)
        _, label = config.tariff.base_price(when)
        storage.record_raw(
            Sample(
                ts=ts,
                watts=watts,
                interval_s=interval_s,
                kwh=kwh,
                cost=kwh * price,
                price=price,
                period=label,
                is_gap=is_gap,
            )
        )
        mtd += kwh
        imported += 1
        previous = (ts, watts)
        if imported % 2000 == 0:
            storage.commit()

    storage.commit()
    if imported:
        storage.log_event("import", f"imported {imported} rows from {directory}")
    storage.close()
    return imported, skipped
