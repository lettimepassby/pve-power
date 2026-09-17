"""日报的内容组装与渲染。

这个模块只负责「报告里有什么」和「长什么样」，不碰 SMTP —— 发信在
mailer.py。分开是为了能在没有邮件服务器的情况下把报告打到终端上看
（`pve-power mail-report --dry-run`），测试也不用起服务。

报告分两部分，可靠性不一样，所以在正文里也是分开呈现的：

  * 用电部分来自 SQLite，只要数据库在就一定能出。
  * 硬件部分（事件日志、异常传感器）要现场问 BMC。BMC 可能正好不理人，
    这时候报告照发，只在那一节里写明没读到 —— 不能因为 ipmitool 抽风
    就让一天的电费统计也发不出去。

渲染成 text/plain 和 text/html 两版。纯文本不是 HTML 的降级品，它自己
就是完整的：有人用 mutt 看邮件，有人把日报转发进只显示纯文本的群机器人。
"""

from __future__ import annotations

import datetime as dt
import html
import socket
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .ipmi import IpmiError, IpmiTool, Sensor, SelEntry
from .storage import Aggregate, Storage
from .tui.theme import DESIGN_HEX as C


def _watts(agg: Aggregate) -> float:
    """avg_watts 在空白的一天上是 None（storage.aggregate_day 的回退路径），
    渲染前统一成 0，免得正文里出现 "None W"。"""
    return float(agg.avg_watts or 0.0)


def _delta_text(current: float, previous: float, unit: str) -> str:
    """和前一天比的变化量。前一天没有数据时不硬算百分比。"""
    if previous <= 0:
        return "（前一天无数据，不作比较）" if current > 0 else ""
    diff = current - previous
    # 差值小到显示出来是 0.00 时就别写 "−0.00（−0.1%）"，那是在拿舍入
    # 噪音冒充信息。
    if abs(diff) < 0.005:
        return "基本持平"
    pct = diff / previous * 100.0
    sign = "+" if diff >= 0 else "−"
    return f"{sign}{abs(diff):.2f} {unit}（{sign}{abs(pct):.1f}%）"


@dataclass
class DailyReport:
    """一天的用电与硬件摘要。渲染函数只读这个对象，不再回查数据库。"""

    day: dt.date
    window_label: str          # "全天" 或 "00:00–14:32（截至发信时刻）"
    partial: bool              # 覆盖的是不完整的一天
    host: str
    product: str
    currency: str

    day_agg: Aggregate
    prev_agg: Aggregate
    month_agg: Aggregate
    month_projected_kwh: float
    month_projected_cost: float

    coverage: float
    hourly: list[Aggregate] = field(default_factory=list)
    periods: list[tuple[str, float, float]] = field(default_factory=list)
    tariff_desc: str = ""

    sel: list[SelEntry] = field(default_factory=list)
    faults: list[Sensor] = field(default_factory=list)
    hottest: Optional[Sensor] = None
    bmc_error: str = ""
    bmc_checked: bool = False

    # ---------------- 派生 ----------------

    @property
    def peak_hour(self) -> Optional[Aggregate]:
        """用电最多的那个小时。整天没用电时返回 None。"""
        active = [a for a in self.hourly if a.kwh > 0]
        return max(active, key=lambda a: a.kwh) if active else None

    @property
    def has_hourly(self) -> bool:
        """逐小时表值不值得画。整天没用电时铺 24 行空条只是噪音。"""
        return bool(self.hourly) and any(a.kwh > 0 for a in self.hourly)

    @property
    def has_problems(self) -> bool:
        """标题里要不要带警示标记。

        只看真正需要人过去看一眼的东西：硬件故障、事件日志里的告警，
        以及采集覆盖率明显不全 —— 覆盖率低意味着这天的电费本身就是
        少算的，不说清楚会让人以为那天真的省电了。
        """
        return bool(
            self.faults
            or any(e.severity != "info" for e in self.sel)
            or self.coverage < 0.9
        )

    def subject(self, prefix: str = "") -> str:
        mark = "⚠ " if self.has_problems else ""
        head = f"{mark}{self.product or self.host} {self.day:%m-%d} 用电日报"
        body = (f"{self.day_agg.kwh:.2f} kWh / "
                f"{self.day_agg.cost:.2f} {self.currency}")
        return f"{prefix} {head} — {body}".strip() if prefix else f"{head} — {body}"


# ---------------------------------------------------------------- 组装


def resolve_day(covers: str, now: Optional[dt.datetime] = None) -> dt.date:
    """把 report.covers 换算成具体日期。"""
    now = now or dt.datetime.now()
    if covers == "today":
        return now.date()
    return now.date() - dt.timedelta(days=1)


def build(
    config: Config,
    storage: Storage,
    day: Optional[dt.date] = None,
    ipmi: Optional[IpmiTool] = None,
    now: Optional[dt.datetime] = None,
) -> DailyReport:
    """组装某一天的报告。

    `ipmi` 给 None 就跳过硬件部分 —— `--dry-run` 和测试都走这条路，
    这样生成报告不需要一台真的 BMC。
    """
    now = now or dt.datetime.now()
    day = day or resolve_day(config.report.covers, now)

    start = dt.datetime.combine(day, dt.time.min)
    full_end = start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    # 报今天的时候窗口到此刻为止，不然覆盖率会被未来的几个小时拉低，
    # 看起来像采集器出了问题。
    partial = day == now.date()
    end = min(now, full_end) if partial else full_end

    if partial:
        day_agg = storage.aggregate_between(start, end, label=day.isoformat())
        window = f"00:00–{end:%H:%M}（截至发信时刻）"
    else:
        day_agg = storage.aggregate_day(day)
        window = "全天"

    prev_agg = storage.aggregate_day(day - dt.timedelta(days=1))
    month_agg = storage.aggregate_month(day.year, day.month)

    # 月度推算：按这个月已过去的比例外推。月初头一两天样本太少，
    # 推出来的数字会离谱，所以那时候不给推算值。
    month_start = dt.datetime.combine(day.replace(day=1), dt.time.min)
    next_month = dt.date(day.year + (day.month == 12), (day.month % 12) + 1, 1)
    month_total = (dt.datetime.combine(next_month, dt.time.min)
                   - month_start).total_seconds()
    elapsed = (end - month_start).total_seconds()
    fraction = elapsed / month_total if month_total else 0.0
    if fraction >= 0.02 and month_agg.kwh > 0:
        projected_kwh = month_agg.kwh / fraction
        projected_cost = (month_agg.cost / fraction
                          + config.tariff.monthly_service_fee)
    else:
        projected_kwh = projected_cost = 0.0

    report = DailyReport(
        day=day,
        window_label=window,
        partial=partial,
        host=socket.gethostname(),
        product="",
        currency=config.tariff.currency,
        day_agg=day_agg,
        prev_agg=prev_agg,
        month_agg=month_agg,
        month_projected_kwh=projected_kwh,
        month_projected_cost=projected_cost,
        coverage=storage.coverage(start, end),
        hourly=storage.hourly_series(day) if config.report.include_hourly else [],
        periods=storage.period_breakdown(start, end),
        tariff_desc=config.tariff.describe_at(start, month_agg.kwh),
    )

    if ipmi is not None and config.report.include_bmc:
        _fill_bmc(report, ipmi, day)
    return report


def _fill_bmc(report: DailyReport, ipmi: IpmiTool, day: dt.date) -> None:
    """尽力而为地补上硬件部分。任何一步失败都只记下原因，不往上抛。"""
    report.bmc_checked = True
    try:
        fru = ipmi.fru()
        report.product = fru.get("Product Name", "") or ""
    except IpmiError as exc:
        report.bmc_error = str(exc)

    try:
        sensors = ipmi.sensors()
    except IpmiError as exc:
        report.bmc_error = report.bmc_error or str(exc)
        sensors = []
    # 只挑 BMC 自己判为异常的：别拿阈值百分比去猜，那会把标称 12V 的
    # 电压轨算成告警（见 Sensor.severity 的说明）。
    report.faults = [s for s in sensors if s.readable and s.severity() != "ok"]
    temps = [s for s in sensors if s.kind == "temperature" and s.readable]
    report.hottest = max(temps, key=lambda s: s.value) if temps else None

    try:
        entries = ipmi.sel_entries(limit=300)
    except IpmiError as exc:
        report.bmc_error = report.bmc_error or str(exc)
        entries = []
    report.sel = [e for e in entries if _sel_on_day(e, day)]


def _sel_on_day(entry: SelEntry, day: dt.date) -> bool:
    """SEL 的日期是 ipmitool 给的 mm/dd/yy 字符串，解析失败就不算这一天的。

    BMC 没设时间的记录会写成 "Pre-Init"，那种没法归到任何一天。
    """
    try:
        return dt.datetime.strptime(entry.date.strip(), "%m/%d/%y").date() == day
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- 纯文本


def render_text(report: DailyReport) -> str:
    cur = report.currency
    day = report.day_agg
    lines: list[str] = []
    add = lines.append

    add(f"{report.product or report.host} 用电日报")
    add(f"{report.day:%Y-%m-%d（%A）}  {report.window_label}")
    add("=" * 52)
    add("")

    add("用电")
    add(f"  电量      {day.kwh:.3f} kWh")
    add(f"  电费      {day.cost:.2f} {cur}")
    add(f"  平均功率  {_watts(day):.0f} W")
    if day.min_watts is not None and day.max_watts is not None:
        add(f"  功率范围  {day.min_watts:.0f} – {day.max_watts:.0f} W")
    delta = _delta_text(day.cost, report.prev_agg.cost, cur)
    if delta:
        add(f"  比前一天  {delta}")
    peak = report.peak_hour
    if peak:
        add(f"  用电高峰  {peak.label}  {peak.kwh:.3f} kWh"
            f"（{_watts(peak):.0f} W）")
    add("")

    if len(report.periods) > 1:
        add("按电价时段")
        for name, kwh, cost in report.periods:
            add(f"  {name:<10}{kwh:>9.3f} kWh{cost:>10.2f} {cur}")
        add("")

    add("本月累计")
    add(f"  已用      {report.month_agg.kwh:.2f} kWh"
        f"   {report.month_agg.cost:.2f} {cur}")
    if report.month_projected_cost:
        add(f"  预计月底  {report.month_projected_kwh:.1f} kWh"
            f"   {report.month_projected_cost:.2f} {cur}"
            "（按当前速率推算）")
    add(f"  当前电价  {report.tariff_desc} {cur}/kWh")
    add("")

    add("采集")
    add(f"  覆盖率    {report.coverage * 100:.1f}%"
        + ("" if report.coverage >= 0.99 else "   ← 有缺口，这天的电量是少算的"))
    add(f"  采样数    {day.samples}")
    add("")

    if report.bmc_checked:
        add("硬件")
        if report.bmc_error:
            add(f"  读取 BMC 失败：{report.bmc_error}")
            add("  （用电统计不受影响，上面的数字来自本地数据库）")
        else:
            if report.hottest:
                add(f"  最高温    {report.hottest.name} "
                    f"{report.hottest.value:.0f}°C")
            if report.faults:
                add(f"  异常传感器（{len(report.faults)} 个）")
                for s in report.faults[:10]:
                    value = f"{s.value:.2f} {s.unit}" if s.readable else "—"
                    add(f"    {s.name:<20}{value:<18}{s.status}")
                if len(report.faults) > 10:
                    add(f"    …… 另有 {len(report.faults) - 10} 个")
            else:
                add("  传感器    全部正常")
            problems = [e for e in report.sel if e.severity != "info"]
            if report.sel:
                add(f"  当天事件  {len(report.sel)} 条"
                    + (f"，其中 {len(problems)} 条需要关注" if problems else ""))
                for e in report.sel[:10]:
                    mark = "!" if e.severity != "info" else " "
                    add(f"    {mark} {e.time:<10}{e.sensor:<24}{e.event}")
                if len(report.sel) > 10:
                    add(f"    …… 另有 {len(report.sel) - 10} 条")
            else:
                add("  当天事件  无")
        add("")

    if report.has_hourly:
        add("逐小时")
        peak_kwh = max((a.kwh for a in report.hourly), default=0.0)
        for agg in report.hourly:
            bar = _text_bar(agg.kwh, peak_kwh, 24)
            add(f"  {agg.label}  {bar} {agg.kwh:6.3f} kWh "
                f"{_watts(agg):5.0f} W")
        add("")

    add("-" * 52)
    add(f"pve-power @ {report.host}    生成于 "
        f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    return "\n".join(lines)


def _text_bar(value: float, maximum: float, width: int) -> str:
    """纯文本的条形。用 # 和 · 而不是 Unicode 方块：日报可能被转发到
    只认 ASCII 的地方，方块在那里会变成一排问号。"""
    if maximum <= 0:
        return "·" * width
    filled = int(round(value / maximum * width))
    return "#" * filled + "·" * (width - filled)


# ---------------------------------------------------------------- HTML


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def render_html(report: DailyReport) -> str:
    """内联样式的 HTML。

    邮件客户端对 <style> 的支持参差不齐（Gmail 网页版会剥掉一部分，
    部分国内客户端整块丢弃），所以每条样式都写在元素上。也正因为如此
    这里不用 CSS 变量 —— 展开成字面值。配色取自 tui/theme.py 的
    DESIGN_HEX，和终端界面同源。
    """
    cur = _esc(report.currency)
    day = report.day_agg
    out: list[str] = []
    add = out.append

    body_font = ("-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
                 "'Hiragino Sans GB','Microsoft YaHei',sans-serif")
    mono = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

    add(f'<!DOCTYPE html><html lang="zh-CN"><head>'
        f'<meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_esc(report.day.isoformat())} 用电日报</title></head>')
    add(f'<body style="margin:0;padding:0;background:{C["muted"]};'
        f'font-family:{body_font};color:{C["text"]};'
        f'-webkit-text-size-adjust:100%;">')
    # 外层用表格而不是 div：Outlook 的排版引擎对 div 的 max-width 不可靠。
    add(f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" style="background:{C["muted"]};padding:16px 0;">'
        f'<tr><td align="center">')
    add(f'<table role="presentation" width="600" cellpadding="0" '
        f'cellspacing="0" style="width:100%;max-width:600px;'
        f'background:{C["bg"]};border:1px solid {C["border"]};'
        f'border-radius:8px;overflow:hidden;">')

    # ---- 页眉 ----
    mark = "⚠ " if report.has_problems else ""
    add(f'<tr><td style="background:{C["primary"]};color:{C["primary_fg"]};'
        f'padding:18px 22px;">'
        f'<div style="font-size:18px;font-weight:700;line-height:1.3;">'
        f'{mark}{_esc(report.product or report.host)} 用电日报</div>'
        f'<div style="font-size:13px;opacity:.85;margin-top:4px;">'
        f'{report.day:%Y-%m-%d（%A）} · {_esc(report.window_label)}</div>'
        f'</td></tr>')

    # ---- 三个大数字 ----
    delta = _delta_text(day.cost, report.prev_agg.cost, report.currency)
    add('<tr><td style="padding:22px 22px 6px 22px;">')
    add('<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0"><tr>')
    for label, value, unit in (
        ("电量", f"{day.kwh:.3f}", "kWh"),
        ("电费", f"{day.cost:.2f}", report.currency),
        ("平均功率", f"{_watts(day):.0f}", "W"),
    ):
        add(f'<td width="33%" style="vertical-align:top;">'
            f'<div style="font-size:12px;color:{C["text"]};opacity:.75;">'
            f'{_esc(label)}</div>'
            f'<div style="font-size:26px;font-weight:700;color:{C["primary"]};'
            f'font-family:{mono};line-height:1.2;margin-top:2px;">'
            f'{_esc(value)}<span style="font-size:13px;font-weight:400;'
            f'margin-left:3px;">{_esc(unit)}</span></div></td>')
    add('</tr></table>')
    if delta:
        add(f'<div style="font-size:13px;margin-top:10px;color:{C["text"]};">'
            f'比前一天 {_esc(delta)}</div>')
    add('</td></tr>')

    # ---- 需要关注的事 ----
    alerts = _alerts(report)
    if alerts:
        add('<tr><td style="padding:10px 22px 0 22px;">')
        for tone, text in alerts:
            edge = C["destructive"] if tone == "crit" else C["accent"]
            add(f'<div style="border-left:3px solid {edge};'
                f'background:{C["muted"]};padding:8px 12px;margin-bottom:6px;'
                f'font-size:13px;border-radius:0 4px 4px 0;">{text}</div>')
        add('</td></tr>')

    # ---- 逐小时 ----
    if report.has_hourly:
        add(_section_open("逐小时用电"))
        add(_hourly_table(report, mono))
        add('</td></tr>')

    # ---- 时段 ----
    if len(report.periods) > 1:
        add(_section_open("按电价时段"))
        rows = "".join(
            f'<tr>'
            f'<td style="padding:5px 0;font-size:13px;">{_esc(name)}</td>'
            f'<td align="right" style="padding:5px 0;font-size:13px;'
            f'font-family:{mono};">{kwh:.3f} kWh</td>'
            f'<td align="right" style="padding:5px 0 5px 14px;font-size:13px;'
            f'font-family:{mono};color:{C["primary"]};font-weight:600;">'
            f'{cost:.2f} {cur}</td></tr>'
            for name, kwh, cost in report.periods
        )
        add(f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{rows}</table></td></tr>')

    # ---- 本月 ----
    add(_section_open("本月累计"))
    month_rows = [
        ("已用", f"{report.month_agg.kwh:.2f} kWh",
         f"{report.month_agg.cost:.2f} {report.currency}"),
    ]
    if report.month_projected_cost:
        month_rows.append((
            "预计月底（按当前速率推算）",
            f"{report.month_projected_kwh:.1f} kWh",
            f"{report.month_projected_cost:.2f} {report.currency}",
        ))
    month_rows.append(("当前电价", "", f"{report.tariff_desc} {report.currency}/kWh"))
    add(_kv_table(month_rows, mono))
    add('</td></tr>')

    # ---- 硬件 ----
    if report.bmc_checked:
        add(_section_open("硬件"))
        add(_hardware_block(report, mono))
        add('</td></tr>')

    # ---- 页脚 ----
    add(f'<tr><td style="padding:14px 22px 20px 22px;'
        f'border-top:1px solid {C["border"]};font-size:11px;'
        f'color:{C["text"]};opacity:.7;">'
        f'pve-power @ {_esc(report.host)} · 采集覆盖率 '
        f'{report.coverage * 100:.1f}% · {day.samples} 条采样 · 生成于 '
        f'{dt.datetime.now():%Y-%m-%d %H:%M:%S}</td></tr>')

    add('</table></td></tr></table></body></html>')
    return "".join(out)


def _section_open(title: str) -> str:
    return (f'<tr><td style="padding:18px 22px 0 22px;">'
            f'<div style="font-size:13px;font-weight:700;color:{C["fg"]};'
            f'padding-bottom:8px;margin-bottom:10px;'
            f'border-bottom:1px solid {C["border"]};">{_esc(title)}</div>')


def _kv_table(rows, mono: str) -> str:
    cells = "".join(
        f'<tr><td style="padding:5px 0;font-size:13px;">{_esc(label)}</td>'
        f'<td align="right" style="padding:5px 0;font-size:13px;'
        f'font-family:{mono};">{_esc(left)}</td>'
        f'<td align="right" style="padding:5px 0 5px 14px;font-size:13px;'
        f'font-family:{mono};color:{C["primary"]};font-weight:600;">'
        f'{_esc(right)}</td></tr>'
        for label, left, right in rows
    )
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{cells}</table>')


def _hourly_table(report: DailyReport, mono: str) -> str:
    """24 行水平柱。

    用嵌套表格画柱子而不是 <div style="width:N%">：后者在 Outlook 里
    宽度会被忽略，整列糊成一条。柱子本身带 aria-hidden，读屏软件念旁边
    的数字就行，不用听 24 个"图像"。
    """
    peak = max((a.kwh for a in report.hourly), default=0.0)
    rows = []
    for agg in report.hourly:
        pct = int(round(agg.kwh / peak * 100)) if peak > 0 else 0
        # 有用电但不足 1% 时也留一格，否则那一小时看起来和停机一样。
        if agg.kwh > 0 and pct < 2:
            pct = 2
        fill = (f'<td width="{pct}%" style="background:{C["secondary"]};'
                f'font-size:0;line-height:0;height:12px;border-radius:2px;'
                f'">&nbsp;</td>') if pct else ""
        rest = 100 - pct
        gap = (f'<td width="{rest}%" style="font-size:0;line-height:0;'
               f'height:12px;">&nbsp;</td>') if rest > 0 else ""
        rows.append(
            f'<tr>'
            f'<td width="42" style="font-size:12px;font-family:{mono};'
            f'color:{C["text"]};padding:2px 0;white-space:nowrap;">'
            f'{_esc(agg.label)}</td>'
            f'<td style="padding:2px 8px;">'
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" aria-hidden="true" '
            f'style="background:{C["muted"]};border-radius:2px;">'
            f'<tr>{fill}{gap}</tr></table></td>'
            f'<td width="78" align="right" style="font-size:12px;'
            f'font-family:{mono};padding:2px 0;white-space:nowrap;">'
            f'{agg.kwh:.3f} kWh</td>'
            f'</tr>'
        )
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{"".join(rows)}</table>')


def _hardware_block(report: DailyReport, mono: str) -> str:
    if report.bmc_error:
        return (f'<div style="font-size:13px;">'
                f'<span style="color:{C["accent_text"]};font-weight:600;">'
                f'读取 BMC 失败</span>：{_esc(report.bmc_error)}'
                f'<div style="margin-top:4px;opacity:.75;">'
                f'用电统计不受影响，上面的数字来自本地数据库。</div></div>')
    parts = []
    if report.hottest:
        parts.append(
            f'<div style="font-size:13px;margin-bottom:8px;">最高温 '
            f'<span style="font-family:{mono};">'
            f'{_esc(report.hottest.name)} {report.hottest.value:.0f}°C'
            f'</span></div>')
    if report.faults:
        rows = "".join(
            f'<tr><td style="padding:3px 0;font-size:13px;font-family:{mono};">'
            f'{_esc(s.name)}</td>'
            f'<td align="right" style="padding:3px 0;font-size:13px;'
            f'font-family:{mono};">'
            f'{(f"{s.value:.2f} " + _esc(s.unit)) if s.readable else "—"}</td>'
            f'<td align="right" style="padding:3px 0 3px 14px;font-size:13px;'
            f'color:{C["destructive"]};font-weight:600;">{_esc(s.status)}</td>'
            f'</tr>'
            for s in report.faults[:10]
        )
        parts.append(
            f'<div style="font-size:13px;color:{C["destructive"]};'
            f'font-weight:600;margin-bottom:4px;">异常传感器 '
            f'{len(report.faults)} 个</div>'
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{rows}</table>')
    else:
        parts.append(f'<div style="font-size:13px;color:{C["ok"]};">'
                     f'传感器全部正常</div>')
    if report.sel:
        rows = "".join(
            f'<tr><td style="padding:3px 0;font-size:12px;font-family:{mono};'
            f'white-space:nowrap;">{_esc(e.time)}</td>'
            f'<td style="padding:3px 10px;font-size:12px;">{_esc(e.sensor)}</td>'
            f'<td style="padding:3px 0;font-size:12px;'
            + (f'color:{C["destructive"]};font-weight:600;'
               if e.severity != "info" else "")
            + f'">{_esc(e.event)}</td></tr>'
            for e in report.sel[:10]
        )
        parts.append(
            f'<div style="font-size:13px;font-weight:600;margin:10px 0 4px;">'
            f'当天事件 {len(report.sel)} 条</div>'
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{rows}</table>')
    else:
        parts.append(f'<div style="font-size:13px;margin-top:8px;opacity:.75;">'
                     f'当天无 BMC 事件</div>')
    return "".join(parts)


def _alerts(report: DailyReport) -> list[tuple[str, str]]:
    """页眉下面那几条提示。只放需要人动一动的事。"""
    alerts: list[tuple[str, str]] = []
    if report.faults:
        names = "、".join(_esc(s.name) for s in report.faults[:3])
        more = f" 等 {len(report.faults)} 个" if len(report.faults) > 3 else ""
        alerts.append(("crit", f"<b>传感器异常</b>：{names}{more}"))
    problems = [e for e in report.sel if e.severity != "info"]
    if problems:
        alerts.append((
            "crit",
            f"<b>当天有 {len(problems)} 条需要关注的 BMC 事件</b>："
            f"{_esc(problems[0].event)}",
        ))
    if report.coverage < 0.9:
        alerts.append((
            "warn",
            f"<b>采集覆盖率只有 {report.coverage * 100:.0f}%</b>，"
            f"这天的电量和电费是少算的。",
        ))
    return alerts
