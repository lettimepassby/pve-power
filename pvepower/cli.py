"""命令行入口。

子命令：
  tui       启动界面（默认）
  collect   运行采集守护进程
  sample    取一次读数，用于测试
  import    把旧的 cron CSV 导入数据库
  report    打印用电汇总
  status    一次性健康检查，适合脚本调用
  config    查看或初始化配置
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
from .textwidth import cwidth, lpad, rpad


def _load(args) -> Config:
    config = Config.load(args.config)
    problems = config.validate()
    if problems:
        print("配置存在问题：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        if not getattr(args, "force", False):
            print("加 --force 可忽略这些问题继续运行。", file=sys.stderr)
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
        print("没有可用读数（详见数据库中的事件记录）。", file=sys.stderr)
        return 1
    print(
        f"{dt.datetime.fromtimestamp(sample.ts):%Y-%m-%d %H:%M:%S}  "
        f"{sample.watts:.0f} W  间隔 {sample.interval_s}s  "
        f"{sample.kwh:.6f} kWh  {sample.cost:.4f} {config.tariff.currency}"
        f"  @ {sample.price:.4f}/kWh [{sample.period}]"
        + ("  （缺口 — 未计入电量）" if sample.is_gap else "")
    )
    return 0


def cmd_import(args) -> int:
    config = _load(args)
    imported, skipped = import_legacy_csv(config, args.dir)
    print(f"已导入 {imported} 行，跳过 {skipped} 行。")
    if imported:
        with Storage(config.db_path) as storage:
            stats = storage.stats()
            print(
                f"数据库现有 {stats['samples']} 条采样 "
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
                  f"   平均 {agg.avg_watts:.0f} W")
            start = dt.datetime(year, month, 1)
            end = (dt.datetime(year + (month == 12), (month % 12) + 1, 1)
                   - dt.timedelta(seconds=1))
            rows = storage.period_breakdown(start, end)
            if len(rows) > 1:
                print("\n按电价时段统计：")
                for name, kwh, cost in rows:
                    print(f"  {lpad(name, 12)}{rpad(f'{kwh:.3f}', 10)} kWh"
                          f"{rpad(f'{cost:.2f}', 12)} {cur}")
            return 0

        series = storage.daily_series(args.days)
        width = 12  # 日期列，按终端列宽计
        print(
            lpad("日期", width) + rpad("电量kWh", 10) + rpad("电费", 12)
            + rpad("平均W", 9) + rpad("采样数", 9)
        )
        for agg in series:
            print(
                lpad(agg.label, width)
                + rpad(f"{agg.kwh:.3f}", 10)
                + rpad(f"{agg.cost:.2f}", 12)
                + rpad(f"{agg.avg_watts:.0f}", 9)
                + rpad(str(agg.samples), 9)
            )
        total_kwh = sum(a.kwh for a in series)
        total_cost = sum(a.cost for a in series)
        print("-" * 52)
        print(
            lpad("合计", width)
            + rpad(f"{total_kwh:.3f}", 10)
            + rpad(f"{total_cost:.2f}", 12)
            + f" {cur}"
        )
        active = [a for a in series if a.kwh > 0]
        if active:
            per_day = total_cost / len(active)
            print(f"\n日均（仅统计有用电的天）：{total_kwh / len(active):.2f} kWh，"
                  f"{per_day:.2f} {cur}")
            print(f"按此推算：{per_day * 30:.0f} {cur}/月，"
                  f"{per_day * 365:.0f} {cur}/年")
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
        print(f"已写入 {args.config}")
        print("把电价改成你自己账单上的数字，然后运行："
              "pve-power tui")
        return 0
    config = Config.load(args.config)
    print(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))
    problems = config.validate()
    if problems:
        print("\n问题：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pve-power",
        description="Proxmox VE 的 IPMI 电量统计与 BMC 管理工具",
    )
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG_PATH,
        help=f"配置文件（默认：{DEFAULT_CONFIG_PATH}）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="即使配置有问题也继续运行",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="启动界面").set_defaults(func=cmd_tui)
    sub.add_parser("collect", help="运行采集守护进程").set_defaults(
        func=cmd_collect
    )
    sub.add_parser("sample", help="取一次读数并存储").set_defaults(
        func=cmd_sample
    )

    p_import = sub.add_parser("import", help="导入旧的 cron CSV 文件")
    p_import.add_argument("--dir", default=None, help="存放 CSV 的目录")
    p_import.set_defaults(func=cmd_import)

    p_report = sub.add_parser("report", help="打印用电汇总")
    p_report.add_argument("--days", type=int, default=30, help="统计最近多少天")
    p_report.add_argument("--month", help="按月份统计，格式 YYYY-MM")
    p_report.set_defaults(func=cmd_report)

    p_status = sub.add_parser("status", help="一次性健康检查")
    p_status.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_status.set_defaults(func=cmd_status)

    p_config = sub.add_parser("config", help="查看或创建配置")
    p_config.add_argument("--init", action="store_true", help="写入默认配置文件")
    p_config.add_argument(
        "--preset", choices=["flat", "china-tou"], default="flat",
        help="配合 --init 使用的电价预设方案",
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
