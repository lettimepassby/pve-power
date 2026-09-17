"""Configuration and electricity tariff model.

Config lives in a single JSON document so the TUI can round-trip edits
without a third-party TOML writer (the target host has no pip). Every
field has a default, so a missing or partial file still yields a usable
configuration.

The tariff model supports three billing shapes, which compose:

  * flat      - one price per kWh
  * tou       - time-of-use: price depends on hour-of-day (and weekday)
  * tiered    - price steps up as month-to-date consumption crosses limits

When both TOU and tiered are enabled, the tiered surcharge is applied on
top of the TOU rate, which is how most Chinese residential/commercial
tariffs with 阶梯电价 + 分时电价 actually bill.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

DEFAULT_CONFIG_PATH = "/etc/pve-power/config.json"
DEFAULT_DB_PATH = "/var/lib/pve-power/power.db"


@dataclass
class TouPeriod:
    """A named time-of-use window.

    `hours` lists the hour-of-day slots (0-23) the price applies to.
    `days` optionally restricts to weekdays (0=Monday .. 6=Sunday);
    empty means every day.
    """

    name: str
    price: float
    hours: list[int] = field(default_factory=list)
    days: list[int] = field(default_factory=list)

    def matches(self, when: dt.datetime) -> bool:
        if self.hours and when.hour not in self.hours:
            return False
        if self.days and when.weekday() not in self.days:
            return False
        return True


@dataclass
class Tier:
    """A consumption tier. `limit_kwh` is the month-to-date upper bound;
    the last tier should use `None` (unbounded)."""

    limit_kwh: Optional[float]
    surcharge: float = 0.0
    name: str = ""


@dataclass
class TariffConfig:
    mode: str = "flat"  # flat | tou
    currency: str = "CNY"
    flat_price: float = 0.60
    tou_periods: list[TouPeriod] = field(default_factory=list)
    tiered_enabled: bool = False
    tiers: list[Tier] = field(default_factory=list)
    monthly_service_fee: float = 0.0

    def base_price(self, when: dt.datetime) -> tuple[float, str]:
        """Price per kWh at `when`, ignoring tiers. Returns (price, label)."""
        if self.mode == "tou" and self.tou_periods:
            for period in self.tou_periods:
                if period.matches(when):
                    return period.price, period.name
            # No window matched: fall back rather than bill at zero.
            return self.flat_price, "未匹配时段"
        return self.flat_price, "单一电价"

    def tier_surcharge(self, month_to_date_kwh: float) -> tuple[float, str]:
        """Surcharge per kWh for the tier that `month_to_date_kwh` falls in."""
        if not self.tiered_enabled or not self.tiers:
            return 0.0, ""
        for tier in self.tiers:
            if tier.limit_kwh is None or month_to_date_kwh < tier.limit_kwh:
                return tier.surcharge, tier.name or f"低于 {tier.limit_kwh}"
        last = self.tiers[-1]
        return last.surcharge, last.name or "最高阶梯"

    def price_at(self, when: dt.datetime, month_to_date_kwh: float = 0.0) -> float:
        """Effective per-kWh price, including any tier surcharge."""
        base, _ = self.base_price(when)
        surcharge, _ = self.tier_surcharge(month_to_date_kwh)
        return base + surcharge

    def describe_at(self, when: dt.datetime, month_to_date_kwh: float = 0.0) -> str:
        base, label = self.base_price(when)
        surcharge, tier_label = self.tier_surcharge(month_to_date_kwh)
        if surcharge:
            return f"{label} {base:.4f} + {tier_label} 加价 {surcharge:.4f}"
        return f"{label} {base:.4f}"


@dataclass
class IpmiConfig:
    binary: str = "ipmitool"
    host: str = ""       # empty = local KCS interface
    user: str = ""
    password: str = ""
    interface: str = "lanplus"
    timeout: int = 20
    lan_channel: int = 1


@dataclass
class CollectorConfig:
    interval_seconds: int = 5
    # An interval longer than this is treated as downtime: energy is not
    # extrapolated across the gap, it is recorded as a gap marker instead.
    max_gap_seconds: int = 900
    # Readings outside this band are discarded as BMC glitches.
    min_watts: float = 1.0
    max_watts: float = 10000.0
    # If set, samples older than this many days are automatically purged.
    retention_days: Optional[int] = 90


@dataclass
class SmtpConfig:
    """发信服务器。密码落在配置文件里，所以 Config.save() 强制 0600。

    `security` 决定加密方式，也决定默认端口：

      * starttls —— 587，先明文连接再升级（绝大多数服务商，包括
        QQ 邮箱、163、Gmail、企业微信邮箱）
      * ssl      —— 465，一上来就是 TLS（部分国内服务商只开这个口）
      * none     —— 25，不加密。只有在本机 relay 或内网 MTA 上才合理，
        配了它就等于把密码明文发出去，所以 validate() 会在同时设了
        密码时报错。

    密码也可以不写进配置文件，改用环境变量 PVE_POWER_SMTP_PASSWORD
    （systemd 单元里用 EnvironmentFile= 指向一个 0600 的文件）。
    环境变量优先。
    """

    host: str = ""
    port: int = 0            # 0 = 按 security 取默认端口
    user: str = ""
    password: str = ""
    security: str = "starttls"   # starttls | ssl | none
    sender: str = ""         # 发件地址，留空则用 user
    sender_name: str = "pve-power"
    timeout: int = 30

    def effective_port(self) -> int:
        if self.port:
            return self.port
        return {"ssl": 465, "none": 25}.get(self.security, 587)

    def effective_sender(self) -> str:
        return self.sender or self.user

    def effective_password(self) -> str:
        """环境变量优先，这样密码可以不落在配置文件里。"""
        return os.environ.get("PVE_POWER_SMTP_PASSWORD") or self.password

    @property
    def configured(self) -> bool:
        return bool(self.host and self.recipients_possible)

    @property
    def recipients_possible(self) -> bool:
        # 发件人是 SMTP 层面的必需项；收件人在 ReportConfig 里。
        return bool(self.effective_sender())


@dataclass
class ReportConfig:
    """日报。

    `enabled` 是给 systemd timer 和安装脚本看的开关；手动跑
    `pve-power mail-report --force` 不受它限制。

    同一天只发一封：发送成功后日期会记进数据库 meta 表，timer 重复触发
    或手动多跑几次都不会重复打扰收件人（`--force` 可以绕过）。
    """

    enabled: bool = False
    recipients: list[str] = field(default_factory=list)
    # 报告覆盖哪一天：yesterday（昨天一整天）或 today（今天到此刻为止）。
    # 默认昨天——日报通常在早上发，那时候「昨天」才是完整的一天。
    covers: str = "yesterday"
    subject_prefix: str = "[pve-power]"
    include_hourly: bool = True
    include_bmc: bool = True


@dataclass
class Config:
    db_path: str = DEFAULT_DB_PATH
    ipmi: IpmiConfig = field(default_factory=IpmiConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    tariff: TariffConfig = field(default_factory=TariffConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    # Where the legacy cron script wrote its CSVs; used by the importer.
    legacy_csv_dir: str = "/var/log/pve-power"

    # ---------------- persistence ----------------

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "Config":
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        cfg = cls()
        cfg.db_path = raw.get("db_path", cfg.db_path)
        cfg.legacy_csv_dir = raw.get("legacy_csv_dir", cfg.legacy_csv_dir)

        for section, target in (("ipmi", cfg.ipmi), ("collector", cfg.collector),
                                ("smtp", cfg.smtp), ("report", cfg.report)):
            for key, value in (raw.get(section) or {}).items():
                if hasattr(target, key):
                    setattr(target, key, value)
        # 收件人允许写成逗号分隔的一行，手写配置时比 JSON 数组顺手。
        if isinstance(cfg.report.recipients, str):
            cfg.report.recipients = [
                a.strip() for a in cfg.report.recipients.split(",") if a.strip()
            ]

        traw = raw.get("tariff") or {}
        t = cfg.tariff
        t.mode = traw.get("mode", t.mode)
        t.currency = traw.get("currency", t.currency)
        t.flat_price = float(traw.get("flat_price", t.flat_price))
        t.monthly_service_fee = float(
            traw.get("monthly_service_fee", t.monthly_service_fee)
        )
        t.tiered_enabled = bool(traw.get("tiered_enabled", t.tiered_enabled))
        t.tou_periods = [
            TouPeriod(
                name=p.get("name", "period"),
                price=float(p.get("price", 0.0)),
                hours=list(p.get("hours", [])),
                days=list(p.get("days", [])),
            )
            for p in traw.get("tou_periods", [])
        ]
        t.tiers = [
            Tier(
                limit_kwh=(
                    None
                    if tier.get("limit_kwh") in (None, "", "none")
                    else float(tier["limit_kwh"])
                ),
                surcharge=float(tier.get("surcharge", 0.0)),
                name=tier.get("name", ""),
            )
            for tier in traw.get("tiers", [])
        ]
        return cfg

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Serialise the unbounded tier limit as JSON null.
        for tier in data["tariff"]["tiers"]:
            if tier["limit_kwh"] is None:
                tier["limit_kwh"] = None
        return data

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        # Config may hold a BMC password; keep it off world-readable paths.
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # ---------------- helpers ----------------

    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means valid."""
        problems: list[str] = []
        if self.collector.interval_seconds < 5:
            problems.append("采集间隔 collector.interval_seconds 必须 >= 5")
        if self.collector.max_gap_seconds <= self.collector.interval_seconds:
            problems.append(
                "缺口阈值 collector.max_gap_seconds 必须大于采集间隔 collector.interval_seconds"
            )
        if self.collector.retention_days is not None and self.collector.retention_days < 7:
            problems.append("保留天数 collector.retention_days 必须 >= 7 或设为 null（不限制）")
        if self.tariff.mode not in ("flat", "tou"):
            problems.append("电价模式 tariff.mode 必须是 'flat' 或 'tou'")
        if self.tariff.mode == "tou":
            if not self.tariff.tou_periods:
                problems.append("电价模式为 'tou'（分时电价），但没有定义任何时段")
            covered: set[int] = set()
            for period in self.tariff.tou_periods:
                for hour in period.hours:
                    if not 0 <= hour <= 23:
                        problems.append(
                            f"分时时段「{period.name}」的小时数 {hour} 无效（应在 0-23 之间）"
                        )
                if not period.days:
                    covered.update(period.hours)
            missing = sorted(set(range(24)) - covered)
            if missing and not any(p.days for p in self.tariff.tou_periods):
                problems.append(
                    f"分时时段未覆盖以下小时：{missing}"
                    "（这些小时将按基础电价 flat_price 计费）"
                )
        if self.tariff.tiered_enabled:
            if not self.tariff.tiers:
                problems.append("启用了阶梯电价 tariff.tiered_enabled，但没有定义任何阶梯")
            bounded = [t for t in self.tariff.tiers if t.limit_kwh is not None]
            limits = [t.limit_kwh for t in bounded]
            if limits != sorted(limits):
                problems.append("阶梯必须按 limit_kwh 从小到大排列")
            if self.tariff.tiers and self.tariff.tiers[-1].limit_kwh is not None:
                problems.append(
                    "最后一个阶梯的 limit_kwh 应为 null（表示不设上限）"
                )
        if self.tariff.flat_price < 0:
            problems.append("基础电价 tariff.flat_price 必须 >= 0")
        problems.extend(self.validate_mail())
        return problems

    def validate_mail(self) -> list[str]:
        """只在启用了日报时才较真——没开这个功能的人不该被它的配置拦住。"""
        problems: list[str] = []
        smtp, report = self.smtp, self.report
        if smtp.security not in ("starttls", "ssl", "none"):
            problems.append(
                "smtp.security 必须是 'starttls'、'ssl' 或 'none'"
            )
        if smtp.security == "none" and smtp.effective_password():
            # 25 口不加密，密码会以明文过网。真要用内网 relay 的话
            # 那种 relay 通常也不需要认证。
            problems.append(
                "smtp.security 为 'none'（不加密）时不应设置密码，"
                "否则密码会明文发送；请改用 'starttls' 或 'ssl'"
            )
        if not report.enabled:
            return problems
        if not smtp.host:
            problems.append("启用了日报 report.enabled，但没有设置 smtp.host")
        if not smtp.effective_sender():
            problems.append(
                "启用了日报 report.enabled，但没有发件地址"
                "（设置 smtp.sender 或 smtp.user）"
            )
        if not report.recipients:
            problems.append("启用了日报 report.enabled，但 report.recipients 是空的")
        for address in report.recipients:
            if "@" not in address.strip("<> "):
                problems.append(f"收件地址「{address}」看起来不是邮箱")
        if report.covers not in ("yesterday", "today"):
            problems.append("report.covers 必须是 'yesterday' 或 'today'")
        return problems


def default_china_tou() -> TariffConfig:
    """A representative Chinese commercial 分时电价 schedule.

    Supplied as a starting point for the TUI's preset action; real prices
    vary by province and must be set to match the user's actual bill.
    """
    return TariffConfig(
        mode="tou",
        currency="CNY",
        flat_price=0.60,
        tou_periods=[
            TouPeriod(name="尖峰", price=1.20, hours=[19, 20, 21]),
            TouPeriod(name="高峰", price=1.00, hours=[8, 9, 10, 11, 18, 22]),
            TouPeriod(
                name="平段",
                price=0.65,
                hours=[7, 12, 13, 14, 15, 16, 17, 23],
            ),
            TouPeriod(
                name="低谷",
                price=0.35,
                hours=[0, 1, 2, 3, 4, 5, 6],
            ),
        ],
    )
