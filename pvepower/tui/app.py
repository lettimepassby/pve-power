"""The TUI application: chrome, event loop, and view dispatch."""

from __future__ import annotations

import csv
import curses
import datetime as dt
import locale
import os
import time
from typing import Optional

from ..config import Config
from ..ipmi import IpmiTool
from ..storage import Storage
from .data import DataCache
from .views.base import View
from .views.bmc import BmcView
from .views.energy import EnergyView
from .views.overview import OverviewView
from .views.sel import SelView
from .views.sensors import SensorsView
from .views.tariff import TariffView
from .views.users import UsersView
from .widgets import (
    CP_ACCENT,
    CP_CRIT,
    CP_DIM,
    CP_HEADER,
    CP_HIGHLIGHT,
    CP_NORMAL,
    CP_OK,
    CP_TITLE,
    CP_WARN,
    Prompt,
    color,
    confirm,
    cwidth,
    init_colors,
    pad,
    safe_addstr,
    show_message,
    truncate,
)

# 七个中文标签页（总览 电量 传感器 BMC 用户 事件日志 电价）加上序号后
# 一共占 61 列，所以最小宽度取 62，否则最右边的「电价」会被挤掉。
MIN_WIDTH = 62
MIN_HEIGHT = 12

HELP_TEXT = """\
导航
  Tab / Shift-Tab     下一个 / 上一个标签页
  1 .. 7              直接跳到对应标签页
  ↑ ↓ / k j           在列表中移动
  PgUp PgDn g G       翻页、跳到首尾
  r                   立即刷新全部数据
  q                   退出

总览
  实时功率、今日与本月的用电量和电费、机箱健康状况，
  以及采集器是否在正常工作。

电量
  d / h               按天 / 按小时查看
  Enter               钻取选中那一天
  ← →                 上一天 / 下一天（小时视图）
  [ ]                 缩小 / 扩大日期范围
  e                   把当前范围导出为 CSV

传感器
  f / F               切换传感器类别筛选
  o                   只看超出规格的传感器

BMC
  Enter               编辑选中的网络字段
  c                   机箱电源操作
  p                   断电恢复后的上电策略
  i                   闪烁定位指示灯

用户
  p                   设置密码            n   改名
  v                   权限级别            e/d 启用 / 禁用

事件日志
  o                   只看告警和故障
  X                   清空 BMC 事件日志

电价
  Enter               编辑选中的设置项
  m                   在单一电价和分时电价之间切换
  a / x               添加 / 删除一个时段或阶梯
  P                   载入示例分时电价方案
  s                   保存配置到磁盘
  R                   按当前电价重算已存储的费用

说明
  电量由功率采样用梯形法积分得出。
  每笔费用按该采样时刻生效的电价记录，所以之后修改电价
  不会篡改历史账目 —— 需要重算时用 R，且仅在旧电价本身
  填错的情况下使用，而不是因为它只是"旧"。
"""


class App:
    def __init__(self, config: Config, config_path: str):
        self.config = config
        self.config_path = config_path
        self.ipmi = IpmiTool(
            binary=config.ipmi.binary,
            host=config.ipmi.host,
            user=config.ipmi.user,
            password=config.ipmi.password,
            interface=config.ipmi.interface,
            timeout=config.ipmi.timeout,
        )
        self.storage = Storage(config.db_path)
        self.data = DataCache(config, self.ipmi, self.storage)
        self.views: list[View] = []
        self.active = 0
        self.stdscr = None
        self._flash = ""
        self._flash_until = 0.0
        self._flash_ok = True
        self._running = True

    # ---------------- lifecycle ----------------

    def run(self) -> int:
        # curses needs the locale set before initscr, or wide characters
        # are written as raw bytes and the Chinese labels come out as
        # mojibake. The empty string means "whatever the environment says".
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        return curses.wrapper(self._main)

    def _main(self, stdscr) -> int:
        self.stdscr = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        init_colors()

        self.views = [
            OverviewView(self),
            EnergyView(self),
            SensorsView(self),
            BmcView(self),
            UsersView(self),
            SelView(self),
            TariffView(self),
        ]

        last_draw = 0.0
        while self._running:
            self.data.tick()
            now = time.time()
            # Redraw at ~2 Hz when idle; a keypress forces one immediately.
            if now - last_draw >= 0.5:
                self._draw()
                last_draw = now
            key = stdscr.getch()
            if key == -1:
                time.sleep(0.05)
                continue
            self._handle_key(key)
            self._draw()
            last_draw = time.time()
        self.storage.close()
        return 0

    # ---------------- drawing ----------------

    def content_size(self) -> tuple[int, int]:
        if self.stdscr is None:
            return 24, 80
        height, width = self.stdscr.getmaxyx()
        # Header takes 2 rows, footer 2.
        return max(1, height - 4), width

    def _draw(self) -> None:
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        stdscr.erase()

        if height < 12 or width < MIN_WIDTH:
            safe_addstr(stdscr, 0, 0,
                        f"终端窗口太小：{width}x{height}，至少需要 {MIN_WIDTH}x{MIN_HEIGHT}",
                        color(CP_CRIT, bold=True))
            stdscr.refresh()
            return

        self._draw_header(stdscr, width)

        content_h = height - 4
        content = stdscr.derwin(content_h, width, 2, 0)
        try:
            self.views[self.active].draw(content, content_h, width)
        except curses.error:
            # A resize mid-draw can invalidate geometry; next frame recovers.
            pass

        self._draw_footer(stdscr, height, width)
        stdscr.refresh()

    def _draw_header(self, stdscr, width: int) -> None:
        host = self.config.ipmi.host or "本机"
        fru = self.data.fru or {}
        product = fru.get("Product Name", "")
        title = f" pve-power  {product} @ {host} "
        safe_addstr(stdscr, 0, 0, pad(title, width), color(CP_HEADER, bold=True))

        watts = self.data.current_watts()
        if watts is not None:
            badge = f" {watts:.0f} W  今日 {self.data.today.cost:.2f} " \
                    f"{self.config.tariff.currency} "
            safe_addstr(stdscr, 0, max(0, width - cwidth(badge) - 1), badge,
                        color(CP_HEADER, bold=True))

        x = 0
        for idx, view in enumerate(self.views):
            label = f" {idx + 1}:{view.title} "
            # 宽度不够时整块跳过，而不是画一半再被裁掉
            if x + cwidth(label) > width - 1:
                break
            attr = color(CP_HIGHLIGHT, bold=True) if idx == self.active else color(CP_DIM)
            safe_addstr(stdscr, 1, x, label, attr)
            x += cwidth(label)

    def _draw_footer(self, stdscr, height: int, width: int) -> None:
        y = height - 2
        safe_addstr(stdscr, y, 0, "─" * width, color(CP_DIM))
        if self._flash and time.time() < self._flash_until:
            attr = color(CP_OK, bold=True) if self._flash_ok else color(CP_CRIT, bold=True)
            safe_addstr(stdscr, y + 1, 1, truncate(self._flash, width - 2), attr)
            return

        keys = [("Tab", "切换"), ("r", "刷新"), ("?", "帮助"), ("q", "退出")]
        keys = list(self.views[self.active].hotkeys) + keys
        x = 1
        for key, label in keys:
            if x + cwidth(key) + cwidth(label) + 3 >= width:
                break
            safe_addstr(stdscr, y + 1, x, key, color(CP_ACCENT, bold=True))
            x += cwidth(key)
            safe_addstr(stdscr, y + 1, x, f":{label}  ", color(CP_DIM))
            x += cwidth(label) + 3

        clock = dt.datetime.now().strftime("%H:%M:%S")
        safe_addstr(stdscr, y + 1, max(0, width - len(clock) - 1), clock,
                    color(CP_DIM))

    # ---------------- input ----------------

    def _handle_key(self, key: int) -> None:
        if self.views[self.active].handle_key(key):
            return

        if key in (ord("q"), ord("Q")):
            self._running = False
        elif key == ord("\t") or key == curses.KEY_RIGHT and False:
            self.active = (self.active + 1) % len(self.views)
        elif key == curses.KEY_BTAB:
            self.active = (self.active - 1) % len(self.views)
        elif ord("1") <= key <= ord("9"):
            idx = key - ord("1")
            if idx < len(self.views):
                self.active = idx
        elif key in (ord("r"), ord("R")) and key == ord("r"):
            for kind in list(self.data._fetched):
                self.data.invalidate(kind)
            self.data.refresh_energy()
            self.flash("已刷新")
        elif key == ord("?"):
            show_message(self.stdscr, "pve-power — 按键与说明", HELP_TEXT)
        elif key == curses.KEY_RESIZE:
            curses.update_lines_cols()

    # ---------------- helpers used by views ----------------

    def flash(self, message: str, ok: bool = True, seconds: float = 4.0) -> None:
        self._flash = message
        self._flash_ok = ok
        self._flash_until = time.time() + seconds

    def save_config(self) -> None:
        problems = self.config.validate()
        if problems:
            show_message(
                self.stdscr,
                "配置未保存",
                "请先修正以下问题：\n\n" + "\n".join(f"  • {p}" for p in problems),
                is_error=True,
            )
            return
        try:
            self.config.save(self.config_path)
            self.storage.log_event("config_saved", self.config_path)
            self.flash(f"已保存到 {self.config_path}", ok=True)
        except OSError as exc:
            show_message(self.stdscr, "无法写入配置文件", str(exc),
                         is_error=True)

    def export_csv(self) -> None:
        prompt = Prompt(self.stdscr)
        default = os.path.join(
            os.path.expanduser("~"),
            f"pve-power-{dt.date.today().isoformat()}.csv",
        )
        path = prompt.ask("导出每日汇总到：", default)
        if not path:
            return
        path = os.path.expanduser(path.strip())
        view = self.views[self.active]
        days = getattr(view, "days", 30)
        series = self.data.daily_series(days)
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    ["date", "kwh", f"cost_{self.config.tariff.currency}",
                     "avg_watts", "min_watts", "max_watts", "samples",
                     "covered_seconds"]
                )
                for agg in series:
                    writer.writerow([
                        agg.label,
                        f"{agg.kwh:.4f}",
                        f"{agg.cost:.4f}",
                        f"{agg.avg_watts:.1f}",
                        f"{agg.min_watts:.0f}" if agg.min_watts is not None else "",
                        f"{agg.max_watts:.0f}" if agg.max_watts is not None else "",
                        agg.samples,
                        agg.duration_s,
                    ])
            self.flash(f"已导出 {len(series)} 天到 {path}", ok=True)
        except OSError as exc:
            show_message(self.stdscr, "导出失败", str(exc), is_error=True)


def run(config: Config, config_path: str) -> int:
    return App(config, config_path).run()
