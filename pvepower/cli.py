"""Command-line entry point.

Subcommands:
  tui       launch the interface (default)
  collect   run the sampling daemon
  sample    take a single reading, for testing
  import    pull the legacy cron CSVs into the database
  report    print consumption summaries
  status    one-shot health check, suitable for scripting
  config    show or initialise the configuration
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from .config import Config, DEFAULT_CONFIG_PATH, default_china_tou
from .collector import Collector, import_legacy_csv
from .ipmi import IpmiError, IpmiTool
from .storage import Storage


def _load(args) -> Config:
    config = Config.load(args.config)
    problems = config.validate()
    if problems:
        print("Configuration problems:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        if not getattr(args, "force", False):
            print("Re-run with --force to continue anyway.", file=sys.stderr)
            raise SystemExit(2)
    return config


def cmd_tui(args) -> int:
    from .tui.app import run

    return run(_load(args), args.config)


def cmd_collect(args) -> int:
    return Collector(_load(args)).run()


def cmd_sample(args) -> int:
    config = _load(args)
    collector = Collector(config)
    sample = collector.sample_once()
    if sample is None:
        print("No usable reading (see events in the database).", file=sys.stderr)
        return 1
    print(
        f"{dt.datetime.fromtimestamp(sample.ts):%Y-%m-%d %H:%M:%S}  "
        f"{sample.watts:.0f} W  interval {sample.interval_s}s  "
        f"{sample.kwh:.6f} kWh  {sample.cost:.4f} {config.tariff.currency}"
        f"  @ {sample.price:.4f}/kWh [{sample.period}]"
        + ("  (GAP — no energy attributed)" if sample.is_gap else "")
    )
    return 0


def cmd_import(args) -> int:
    config = _load(args)
    imported, skipped = import_legacy_csv(config, args.dir)
    print(f"Imported {imported} rows, skipped {skipped}.")
    if imported:
        with Storage(config.db_path) as storage:
            stats = storage.stats()
            print(
                f"Database now holds {stats['samples']} samples "
                f"({stats.get('first', '?')} .. {stats.get('last', '?')}), "
                f"{stats['total_kwh']} kWh, {stats['total_cost']} "
                f"{config.tariff.currency}."
            )
    return 0


def cmd_report(args) -> int:
    config = _load(args)
    cur = config.tariff.currency
    with Storage(config.db_path) as storage:
        if args.month:
            year, month = (int(x) for x in args.month.split("-"))
            agg = storage.aggregate_month(year, month)
            print(f"{agg.label}:  {agg.kwh:.3f} kWh   {agg.cost:.2f} {cur}"
                  f"   avg {agg.avg_watts:.0f} W")
            start = dt.datetime(year, month, 1)
            end = (dt.datetime(year + (month == 12), (month % 12) + 1, 1)
                   - dt.timedelta(seconds=1))
            rows = storage.period_breakdown(start, end)
            if len(rows) > 1:
                print("\nBy tariff period:")
                for name, kwh, cost in rows:
                    print(f"  {name:<12}{kwh:>10.3f} kWh{cost:>12.2f} {cur}")
            return 0

        series = storage.daily_series(args.days)
        print(f"{'Date':<12}{'kWh':>10}{'Cost':>12}{'Avg W':>9}{'Samples':>9}")
        for agg in series:
            print(f"{agg.label:<12}{agg.kwh:>10.3f}{agg.cost:>12.2f}"
                  f"{agg.avg_watts:>9.0f}{agg.samples:>9}")
        total_kwh = sum(a.kwh for a in series)
        total_cost = sum(a.cost for a in series)
        print("-" * 52)
        print(f"{'Total':<12}{total_kwh:>10.3f}{total_cost:>12.2f} {cur}")
        active = [a for a in series if a.kwh > 0]
        if active:
            per_day = total_cost / len(active)
            print(f"\nPer active day: {total_kwh / len(active):.2f} kWh, "
                  f"{per_day:.2f} {cur}")
            print(f"At this rate:   {per_day * 30:.0f} {cur}/month, "
                  f"{per_day * 365:.0f} {cur}/year")
    return 0


def cmd_status(args) -> int:
    config = Config.load(args.config)
    ipmi = IpmiTool(
        binary=config.ipmi.binary,
        host=config.ipmi.host,
        user=config.ipmi.user,
        password=config.ipmi.password,
        interface=config.ipmi.interface,
        timeout=config.ipmi.timeout,
    )
    out: dict = {"config": args.config, "db": config.db_path}

    try:
        reading = ipmi.power_reading()
        out["watts"] = reading.instantaneous
        out["watts_min"] = reading.minimum
        out["watts_max"] = reading.maximum
        out["watts_avg"] = reading.average
        out["bmc"] = "ok"
    except IpmiError as exc:
        out["bmc"] = "error"
        out["bmc_error"] = str(exc)

    with Storage(config.db_path) as storage:
        now = dt.datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = storage.aggregate_between(today_start, now)
        month = storage.aggregate_between(today_start.replace(day=1), now)
        last = storage.last_sample()
        out["today_kwh"] = round(today.kwh, 4)
        out["today_cost"] = round(today.cost, 4)
        out["month_kwh"] = round(month.kwh, 4)
        out["month_cost"] = round(month.cost, 4)
        out["currency"] = config.tariff.currency
        out["coverage_today"] = round(storage.coverage(today_start, now), 4)
        if last:
            age = now.timestamp() - last.ts
            out["last_sample_age_s"] = int(age)
            out["collector"] = (
                "stale" if age > config.collector.interval_seconds * 3 else "ok"
            )
        else:
            out["collector"] = "no-data"

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for key, value in out.items():
            print(f"{key:<20}{value}")
    return 0 if out.get("collector") == "ok" and out.get("bmc") == "ok" else 1


def cmd_config(args) -> int:
    if args.init:
        config = Config()
        if args.preset == "china-tou":
            config.tariff = default_china_tou()
        config.save(args.config)
        print(f"Wrote {args.config}")
        print("Edit the prices to match your own bill, then run: "
              "pve-power tui")
        return 0
    config = Config.load(args.config)
    print(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))
    problems = config.validate()
    if problems:
        print("\nProblems:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pve-power",
        description="IPMI power metering and BMC management for Proxmox VE",
    )
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG_PATH,
        help=f"configuration file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="continue despite configuration problems",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="launch the interface").set_defaults(func=cmd_tui)
    sub.add_parser("collect", help="run the sampling daemon").set_defaults(
        func=cmd_collect
    )
    sub.add_parser("sample", help="take one reading and store it").set_defaults(
        func=cmd_sample
    )

    p_import = sub.add_parser("import", help="import the legacy cron CSVs")
    p_import.add_argument("--dir", default=None, help="directory holding the CSVs")
    p_import.set_defaults(func=cmd_import)

    p_report = sub.add_parser("report", help="print consumption summaries")
    p_report.add_argument("--days", type=int, default=30)
    p_report.add_argument("--month", help="report one month, as YYYY-MM")
    p_report.set_defaults(func=cmd_report)

    p_status = sub.add_parser("status", help="one-shot health check")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_config = sub.add_parser("config", help="show or create the configuration")
    p_config.add_argument("--init", action="store_true", help="write a default file")
    p_config.add_argument(
        "--preset", choices=["flat", "china-tou"], default="flat",
        help="tariff preset to start from when using --init",
    )
    p_config.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["tui"])
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
