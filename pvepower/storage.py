"""SQLite storage with energy integration.

Design notes
------------
The BMC reports *power* (watts, an instantaneous rate). Billing needs
*energy* (kWh, a quantity). Converting between them means integrating
power over time, and the accuracy of the bill depends entirely on doing
that integration honestly:

  * Energy for an interval uses the trapezoidal rule over the two
    bracketing samples, not `watts * interval`. With a 60s interval and
    a load that ramps, rectangular integration biases the total by the
    ramp direction; the trapezoid does not.

  * A sample whose gap from its predecessor exceeds `max_gap_seconds`
    contributes no energy. The machine may have been off, or the
    collector may have been stopped; inventing consumption across that
    window would silently inflate the bill. The gap is recorded so it is
    visible rather than merely absent.

  * Cost is computed and stored per-sample, at the price in force at the
    time of that sample. This is what makes time-of-use billing correct:
    re-pricing historical energy at today's rate would be wrong, and
    recomputing from daily aggregates loses the hour each kWh landed in.

Every sample therefore carries both the energy it contributed and the
money that energy cost, which makes every later aggregate a pure sum.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import Config, TariffConfig

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts            INTEGER PRIMARY KEY,   -- unix seconds, UTC
    watts         REAL    NOT NULL,
    interval_s    INTEGER NOT NULL,      -- seconds since previous sample
    kwh           REAL    NOT NULL,      -- energy attributed to this interval
    cost          REAL    NOT NULL,      -- money attributed to this interval
    price         REAL    NOT NULL,      -- effective per-kWh price used
    period        TEXT    NOT NULL DEFAULT '',  -- tariff window label
    is_gap        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS daily_rollup (
    day           TEXT PRIMARY KEY,      -- YYYY-MM-DD
    kwh           REAL    NOT NULL,
    cost          REAL    NOT NULL,
    samples       INTEGER NOT NULL,
    min_watts     REAL,
    max_watts     REAL,
    duration_s    INTEGER NOT NULL,
    avg_watts     REAL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    INTEGER NOT NULL,
    kind  TEXT    NOT NULL,
    detail TEXT   NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


@dataclass
class Sample:
    ts: int
    watts: float
    interval_s: int
    kwh: float
    cost: float
    price: float
    period: str
    is_gap: bool

    @property
    def when(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.ts)


@dataclass
class Aggregate:
    """A summed window of consumption."""

    label: str
    kwh: float = 0.0
    cost: float = 0.0
    samples: int = 0
    avg_watts: float = 0.0
    min_watts: Optional[float] = None
    max_watts: Optional[float] = None
    duration_s: int = 0

    @property
    def currency_per_kwh(self) -> float:
        return self.cost / self.kwh if self.kwh else 0.0


class Storage:
    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # WAL lets the TUI read while the collector writes.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._set_meta_default("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    def _migrate(self) -> None:
        """Upgrade existing databases to the current schema version."""
        cur_version = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if cur_version is None:
            # Fresh database: SCHEMA already ran, nothing to migrate.
            return
        ver = int(cur_version["value"])
        if ver >= SCHEMA_VERSION:
            return
        # Version 1 → 2: add daily_rollup table (already in SCHEMA), no data.
        if ver == 1:
            # SCHEMA already created the table via CREATE IF NOT EXISTS.
            self.conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),)
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------- meta ----------------

    def _set_meta_default(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def log_event(self, kind: str, detail: str = "", ts: Optional[int] = None) -> None:
        self.conn.execute(
            "INSERT INTO events(ts, kind, detail) VALUES (?, ?, ?)",
            (ts or int(dt.datetime.now().timestamp()), kind, detail),
        )
        self.conn.commit()

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---------------- ingest ----------------

    def last_sample(self) -> Optional[Sample]:
        row = self.conn.execute(
            "SELECT * FROM samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return _row_to_sample(row) if row else None

    def month_to_date_kwh(self, when: Optional[dt.datetime] = None) -> float:
        """Energy billed so far in `when`'s calendar month (local time)."""
        when = when or dt.datetime.now()
        start = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(kwh), 0) AS k FROM samples WHERE ts >= ? AND ts <= ?",
            (int(start.timestamp()), int(when.timestamp())),
        ).fetchone()
        return float(row["k"])

    def record(
        self,
        watts: float,
        tariff: TariffConfig,
        when: Optional[dt.datetime] = None,
        max_gap_seconds: int = 900,
        default_interval: int = 60,
    ) -> Sample:
        """Integrate one power reading into stored energy and cost.

        Returns the stored sample. The caller is expected to have already
        validated `watts` against the configured sanity band.
        """
        when = when or dt.datetime.now()
        ts = int(when.timestamp())
        previous = self.last_sample()

        if previous is None:
            # First ever sample: no interval to integrate over. Record it
            # as a zero-energy anchor so the next sample has a bracket.
            interval_s = 0
            kwh = 0.0
            is_gap = False
            mean_watts = watts
        else:
            interval_s = ts - previous.ts
            if interval_s <= 0:
                # Clock went backwards, or a duplicate timestamp. Refuse to
                # integrate; the row would corrupt the running total.
                raise ValueError(
                    f"non-monotonic sample: {ts} <= previous {previous.ts}"
                )
            if interval_s > max_gap_seconds:
                # Coverage gap: attribute no energy to it.
                is_gap = True
                kwh = 0.0
                mean_watts = watts
            else:
                is_gap = False
                # Trapezoidal rule: average the bracketing power readings.
                mean_watts = (previous.watts + watts) / 2.0
                kwh = mean_watts * interval_s / 3_600_000.0

        mtd = self.month_to_date_kwh(when)
        price = tariff.price_at(when, mtd)
        _, period_label = tariff.base_price(when)
        cost = kwh * price

        sample = Sample(
            ts=ts,
            watts=watts,
            interval_s=interval_s,
            kwh=kwh,
            cost=cost,
            price=price,
            period=period_label,
            is_gap=is_gap,
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO samples"
            "(ts, watts, interval_s, kwh, cost, price, period, is_gap) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample.ts,
                sample.watts,
                sample.interval_s,
                sample.kwh,
                sample.cost,
                sample.price,
                sample.period,
                int(sample.is_gap),
            ),
        )
        self.conn.commit()
        if is_gap:
            self.log_event(
                "gap",
                f"{interval_s}s without samples "
                f"(> max_gap {max_gap_seconds}s); no energy attributed",
                ts=ts,
            )
        return sample

    def record_raw(self, sample: Sample) -> None:
        """Insert a pre-computed sample. Used by the CSV importer."""
        self.conn.execute(
            "INSERT OR REPLACE INTO samples"
            "(ts, watts, interval_s, kwh, cost, price, period, is_gap) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample.ts,
                sample.watts,
                sample.interval_s,
                sample.kwh,
                sample.cost,
                sample.price,
                sample.period,
                int(sample.is_gap),
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    # ---------------- query ----------------

    def samples_between(self, start: dt.datetime, end: dt.datetime) -> list[Sample]:
        rows = self.conn.execute(
            "SELECT * FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (int(start.timestamp()), int(end.timestamp())),
        ).fetchall()
        return [_row_to_sample(r) for r in rows]

    def aggregate_between(
        self, start: dt.datetime, end: dt.datetime, label: str = ""
    ) -> Aggregate:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(kwh), 0)  AS kwh,
                   COALESCE(SUM(cost), 0) AS cost,
                   COUNT(*)               AS n,
                   MIN(watts)             AS wmin,
                   MAX(watts)             AS wmax,
                   COALESCE(SUM(CASE WHEN is_gap=0 THEN interval_s ELSE 0 END), 0)
                                          AS dur
            FROM samples WHERE ts >= ? AND ts <= ?
            """,
            (int(start.timestamp()), int(end.timestamp())),
        ).fetchone()
        agg = Aggregate(
            label=label or f"{start:%Y-%m-%d} .. {end:%Y-%m-%d}",
            kwh=float(row["kwh"]),
            cost=float(row["cost"]),
            samples=int(row["n"]),
            min_watts=row["wmin"],
            max_watts=row["wmax"],
            duration_s=int(row["dur"]),
        )
        # Average power over covered time, not over sample count: a mean of
        # samples would over-weight densely sampled stretches.
        if agg.duration_s > 0:
            agg.avg_watts = agg.kwh * 3_600_000.0 / agg.duration_s
        return agg

    def aggregate_day(self, day: dt.date) -> Aggregate:
        """Aggregate a single day from raw samples, or from rollup if purged."""
        start = dt.datetime.combine(day, dt.time.min)
        end = start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
        # Try raw samples first (fast path when data is present).
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(kwh), 0)  AS kwh,
                   COALESCE(SUM(cost), 0) AS cost,
                   COUNT(*)               AS n,
                   MIN(watts)             AS wmin,
                   MAX(watts)             AS wmax,
                   COALESCE(SUM(CASE WHEN is_gap=0 THEN interval_s ELSE 0 END), 0)
                                          AS dur
            FROM samples WHERE ts >= ? AND ts <= ?
            """,
            (int(start.timestamp()), int(end.timestamp())),
        ).fetchone()
        if row["n"] > 0:
            agg = Aggregate(
                label=day.isoformat(),
                kwh=float(row["kwh"]),
                cost=float(row["cost"]),
                samples=int(row["n"]),
                min_watts=row["wmin"],
                max_watts=row["wmax"],
                duration_s=int(row["dur"]),
            )
            if agg.duration_s > 0:
                agg.avg_watts = agg.kwh * 3_600_000.0 / agg.duration_s
            return agg
        # Fall back to the rollup if raw samples were purged.
        rollup = self.conn.execute(
            "SELECT * FROM daily_rollup WHERE day = ?", (day.isoformat(),)
        ).fetchone()
        if rollup:
            return Aggregate(
                label=day.isoformat(),
                kwh=float(rollup["kwh"]),
                cost=float(rollup["cost"]),
                samples=int(rollup["samples"]),
                min_watts=rollup["min_watts"],
                max_watts=rollup["max_watts"],
                duration_s=int(rollup["duration_s"]),
                avg_watts=rollup["avg_watts"],
            )
        # Neither raw nor rollup: an empty day.
        return Aggregate(
            label=day.isoformat(), kwh=0.0, cost=0.0, samples=0,
            min_watts=None, max_watts=None, duration_s=0, avg_watts=None,
        )

    def aggregate_month(self, year: int, month: int) -> Aggregate:
        """Month total, summing per-day figures so purged days still count."""
        start = dt.date(year, month, 1)
        next_month = dt.date(year + (month == 12), (month % 12) + 1, 1)
        days = (next_month - start).days
        series = self.daily_series(days=days, end=next_month - dt.timedelta(days=1))
        agg = Aggregate(label=f"{year}-{month:02d}")
        mins = [a.min_watts for a in series if a.min_watts is not None]
        maxes = [a.max_watts for a in series if a.max_watts is not None]
        for a in series:
            agg.kwh += a.kwh
            agg.cost += a.cost
            agg.samples += a.samples
            agg.duration_s += a.duration_s
        agg.min_watts = min(mins) if mins else None
        agg.max_watts = max(maxes) if maxes else None
        if agg.duration_s > 0:
            agg.avg_watts = agg.kwh * 3_600_000.0 / agg.duration_s
        return agg

    def daily_series(self, days: int = 30, end: Optional[dt.date] = None) -> list[Aggregate]:
        """Per-day aggregates, oldest first, computed in one pass."""
        end = end or dt.date.today()
        start = end - dt.timedelta(days=days - 1)
        start_ts = int(dt.datetime.combine(start, dt.time.min).timestamp())
        end_ts = int(
            dt.datetime.combine(end, dt.time.max).timestamp()
        )
        rows = self.conn.execute(
            """
            SELECT date(ts, 'unixepoch', 'localtime') AS d,
                   SUM(kwh)  AS kwh,
                   SUM(cost) AS cost,
                   COUNT(*)  AS n,
                   MIN(watts) AS wmin,
                   MAX(watts) AS wmax,
                   SUM(CASE WHEN is_gap=0 THEN interval_s ELSE 0 END) AS dur
            FROM samples WHERE ts >= ? AND ts <= ?
            GROUP BY d ORDER BY d
            """,
            (start_ts, end_ts),
        ).fetchall()
        by_day = {r["d"]: r for r in rows}
        # Days whose raw samples have been purged still have a rollup row;
        # read those so a 90-day retention does not blank the history.
        roll = {
            r["day"]: r
            for r in self.conn.execute(
                "SELECT * FROM daily_rollup WHERE day >= ? AND day <= ?",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        }
        series: list[Aggregate] = []
        for offset in range(days):
            day = start + dt.timedelta(days=offset)
            key = day.isoformat()
            row = by_day.get(key)
            if row is None:
                r = roll.get(key)
                if r is None:
                    series.append(Aggregate(label=key))
                else:
                    series.append(Aggregate(
                        label=key,
                        kwh=float(r["kwh"]),
                        cost=float(r["cost"]),
                        samples=int(r["samples"]),
                        min_watts=r["min_watts"],
                        max_watts=r["max_watts"],
                        duration_s=int(r["duration_s"]),
                        avg_watts=r["avg_watts"],
                    ))
                continue
            agg = Aggregate(
                label=key,
                kwh=float(row["kwh"] or 0),
                cost=float(row["cost"] or 0),
                samples=int(row["n"] or 0),
                min_watts=row["wmin"],
                max_watts=row["wmax"],
                duration_s=int(row["dur"] or 0),
            )
            if agg.duration_s:
                agg.avg_watts = agg.kwh * 3_600_000.0 / agg.duration_s
            series.append(agg)
        return series

    def hourly_series(self, day: dt.date) -> list[Aggregate]:
        """24 hourly aggregates for `day`, index 0 = 00:00."""
        start = dt.datetime.combine(day, dt.time.min)
        end = start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
        rows = self.conn.execute(
            """
            SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS h,
                   SUM(kwh)  AS kwh,
                   SUM(cost) AS cost,
                   COUNT(*)  AS n,
                   MIN(watts) AS wmin,
                   MAX(watts) AS wmax,
                   SUM(CASE WHEN is_gap=0 THEN interval_s ELSE 0 END) AS dur
            FROM samples WHERE ts >= ? AND ts <= ?
            GROUP BY h ORDER BY h
            """,
            (int(start.timestamp()), int(end.timestamp())),
        ).fetchall()
        by_hour = {int(r["h"]): r for r in rows}
        series = []
        for hour in range(24):
            row = by_hour.get(hour)
            label = f"{hour:02d}:00"
            if row is None:
                series.append(Aggregate(label=label))
                continue
            agg = Aggregate(
                label=label,
                kwh=float(row["kwh"] or 0),
                cost=float(row["cost"] or 0),
                samples=int(row["n"] or 0),
                min_watts=row["wmin"],
                max_watts=row["wmax"],
                duration_s=int(row["dur"] or 0),
            )
            if agg.duration_s:
                agg.avg_watts = agg.kwh * 3_600_000.0 / agg.duration_s
            series.append(agg)
        return series

    def period_breakdown(
        self, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[str, float, float]]:
        """(tariff period, kWh, cost) within a window, biggest cost first."""
        rows = self.conn.execute(
            """
            SELECT period, SUM(kwh) AS kwh, SUM(cost) AS cost
            FROM samples WHERE ts >= ? AND ts <= ?
            GROUP BY period ORDER BY cost DESC
            """,
            (int(start.timestamp()), int(end.timestamp())),
        ).fetchall()
        return [
            (r["period"] or "flat", float(r["kwh"] or 0), float(r["cost"] or 0))
            for r in rows
        ]

    def recent_watts(self, limit: int = 120) -> list[tuple[int, float]]:
        """(ts, watts) newest-last, for sparklines."""
        rows = self.conn.execute(
            "SELECT ts, watts FROM samples ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(int(r["ts"]), float(r["watts"])) for r in reversed(rows)]

    def coverage(self, start: dt.datetime, end: dt.datetime) -> float:
        """Fraction of the window actually covered by non-gap samples."""
        total = (end - start).total_seconds()
        if total <= 0:
            return 0.0
        row = self.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN is_gap=0 THEN interval_s ELSE 0 END), 0) AS d "
            "FROM samples WHERE ts >= ? AND ts <= ?",
            (int(start.timestamp()), int(end.timestamp())),
        ).fetchone()
        return min(1.0, float(row["d"]) / total)

    def stats(self) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last, "
            "SUM(kwh) AS kwh, SUM(cost) AS cost, "
            "SUM(is_gap) AS gaps FROM samples"
        ).fetchone()
        n = int(row["n"] or 0)
        out = {
            "samples": str(n),
            "gaps": str(int(row["gaps"] or 0)),
            "total_kwh": f"{float(row['kwh'] or 0):.3f}",
            "total_cost": f"{float(row['cost'] or 0):.2f}",
        }
        if n:
            out["first"] = dt.datetime.fromtimestamp(int(row["first"])).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            out["last"] = dt.datetime.fromtimestamp(int(row["last"])).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        try:
            out["db_size"] = f"{os.path.getsize(self.path) / 1024:.1f} KiB"
        except OSError:
            pass
        return out

    # ---------------- maintenance ----------------

    def reprice(self, tariff: TariffConfig) -> int:
        """Recompute cost for every stored sample under `tariff`.

        Needed after a tariff correction: the stored energy stays as
        measured, only the money is recalculated. Month-to-date energy is
        tracked as the walk proceeds so tiered pricing reproduces the same
        stepping it would have had live.
        """
        rows = self.conn.execute(
            "SELECT ts, kwh FROM samples ORDER BY ts"
        ).fetchall()
        updates = []
        mtd = 0.0
        current_month = None
        for row in rows:
            when = dt.datetime.fromtimestamp(int(row["ts"]))
            month_key = (when.year, when.month)
            if month_key != current_month:
                current_month = month_key
                mtd = 0.0
            price = tariff.price_at(when, mtd)
            _, label = tariff.base_price(when)
            kwh = float(row["kwh"])
            updates.append((price, kwh * price, label, int(row["ts"])))
            mtd += kwh
        self.conn.executemany(
            "UPDATE samples SET price=?, cost=?, period=? WHERE ts=?", updates
        )
        self.conn.commit()
        self.log_event("reprice", f"recomputed {len(updates)} samples")
        return len(updates)

    def purge_before(self, cutoff: dt.datetime) -> int:
        """Delete raw samples older than `cutoff`, rolling them up first.

        The rollup runs inside the same transaction as the delete, so a
        crash between the two cannot lose a day's history: either both
        happen or neither does. Reports fall back to the rollup for days
        whose raw samples are gone, so purging costs resolution, never
        the electricity bill itself.
        """
        cutoff_ts = int(cutoff.timestamp())
        with self.conn:
            self.rollup_before(cutoff, commit=False)
            cur = self.conn.execute(
                "DELETE FROM samples WHERE ts < ?", (cutoff_ts,)
            )
            return cur.rowcount

    def rollup_before(self, cutoff: dt.datetime, commit: bool = True) -> int:
        """Summarise every day that ends before `cutoff` into daily_rollup.

        Idempotent: a day already rolled up is recomputed from whatever
        raw samples remain, so calling this twice is harmless. Days with
        no raw samples left are not touched, which is what preserves
        history across repeated purges.
        """
        cutoff_ts = int(cutoff.timestamp())
        rows = self.conn.execute(
            """
            SELECT date(ts, 'unixepoch', 'localtime')  AS day,
                   COALESCE(SUM(kwh), 0)               AS kwh,
                   COALESCE(SUM(cost), 0)              AS cost,
                   COUNT(*)                            AS n,
                   MIN(watts)                          AS wmin,
                   MAX(watts)                          AS wmax,
                   COALESCE(SUM(CASE WHEN is_gap=0 THEN interval_s ELSE 0 END), 0)
                                                       AS dur
            FROM samples WHERE ts < ?
            GROUP BY day
            """,
            (cutoff_ts,),
        ).fetchall()
        written = []
        for r in rows:
            dur = int(r["dur"] or 0)
            avg = (float(r["kwh"]) * 3_600_000.0 / dur) if dur > 0 else None
            written.append((
                r["day"], float(r["kwh"]), float(r["cost"]), int(r["n"]),
                r["wmin"], r["wmax"], dur, avg,
            ))
        if written:
            self.conn.executemany(
                """
                INSERT INTO daily_rollup
                    (day, kwh, cost, samples, min_watts, max_watts,
                     duration_s, avg_watts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    kwh=excluded.kwh, cost=excluded.cost,
                    samples=excluded.samples, min_watts=excluded.min_watts,
                    max_watts=excluded.max_watts,
                    duration_s=excluded.duration_s,
                    avg_watts=excluded.avg_watts
                """,
                written,
            )
        if commit:
            self.conn.commit()
        return len(written)

    def rollup_day(self, day: dt.date) -> None:
        """Rollup a single day into daily_rollup. For testing."""
        cutoff = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min)
        self.rollup_before(cutoff, commit=True)


def _row_to_sample(row: sqlite3.Row) -> Sample:
    return Sample(
        ts=int(row["ts"]),
        watts=float(row["watts"]),
        interval_s=int(row["interval_s"]),
        kwh=float(row["kwh"]),
        cost=float(row["cost"]),
        price=float(row["price"]),
        period=row["period"] or "",
        is_gap=bool(row["is_gap"]),
    )
