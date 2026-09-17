"""电量页的列表顺序与下钻。

这一页原来按时间正序画，最新的一天在最底下——默认 30 天范围下，今天和
昨天被压在屏幕外，刚装几天的机器打开只能看到一屏 0.000。改成倒序之后，
最容易悄悄出错的是下钻：光标在第一行按 Enter 该进今天，而不是 30 天前。
顺序和索引一旦对不上，界面看着完全正常，只有按下去才发现进错了日子，
所以这里对着它做断言。

不用起 curses：这些逻辑只碰 self.data 和 self.days。
"""

from __future__ import annotations

import curses
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvepower.config import Config, TariffConfig
from pvepower.storage import Storage
from pvepower.tui.views.energy import EnergyView


class FakeData:
    """EnergyView 只用到 daily_series，直接转给真的 Storage。"""

    def __init__(self, storage: Storage):
        self.storage = storage

    def daily_series(self, days):
        return self.storage.daily_series(days)

    def hourly_series(self, day):
        return self.storage.hourly_series(day)


class FakeApp:
    def __init__(self, config, data):
        self.config = config
        self.data = data

    def content_size(self):
        return 34, 118


class EnergyViewFixture(unittest.TestCase):
    """最近三天有数据，再往前是空的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = Config()
        config.db_path = os.path.join(self.tmp.name, "power.db")
        config.tariff = TariffConfig(mode="flat", currency="CNY",
                                     flat_price=0.98)
        self.storage = Storage(config.db_path)
        self.addCleanup(self.storage.close)

        self.today = dt.date.today()
        # 只给最近三天灌数据，更早的日子留空——正是「30 天范围、机器刚
        # 装几天」的那个场景。
        t = dt.datetime.combine(self.today - dt.timedelta(days=2), dt.time.min)
        now = dt.datetime.now()
        # 步长必须小于 max_gap_seconds，否则每一段都被判成缺口、电量全是 0，
        # 下面那条「有数据的日子在第一屏」就成了空转。
        while t < now:
            self.storage.record(190.0, config.tariff, when=t,
                                max_gap_seconds=900)
            t += dt.timedelta(minutes=10)
        self.storage.commit()

        self.view = EnergyView(FakeApp(config, FakeData(self.storage)))


class TestOrdering(EnergyViewFixture):
    def test_newest_day_is_first(self):
        series = self.view._series()
        self.assertEqual(series[0].label, self.today.isoformat())
        self.assertEqual(
            series[-1].label,
            (self.today - dt.timedelta(days=self.view.days - 1)).isoformat(),
        )

    def test_order_is_strictly_descending(self):
        labels = [a.label for a in self.view._series()]
        self.assertEqual(labels, sorted(labels, reverse=True))

    def test_the_days_with_data_are_on_the_first_screen(self):
        """改这一版的理由本身：有数据的日子不该在屏幕外。"""
        first_screen = self.view._series()[:10]
        self.assertTrue(any(a.kwh > 0 for a in first_screen))

    def test_storage_still_returns_chronological(self):
        """倒序只是这一页的显示顺序。CSV 导出和日报都依赖正序，
        不能把 storage 自己的顺序改掉。"""
        labels = [a.label for a in self.storage.daily_series(self.view.days)]
        self.assertEqual(labels, sorted(labels))


class TestDrillDown(EnergyViewFixture):
    def drill(self):
        self.view.handle_key(10)          # Enter

    def test_first_row_drills_into_today(self):
        self.view.cursor = 0
        self.drill()
        self.assertEqual(self.view.mode, "hourly")
        self.assertEqual(self.view.day, self.today)

    def test_second_row_drills_into_yesterday(self):
        self.view.cursor = 1
        self.drill()
        self.assertEqual(self.view.day, self.today - dt.timedelta(days=1))

    def test_last_row_drills_into_the_oldest_day(self):
        self.view.cursor = self.view.days - 1
        self.drill()
        self.assertEqual(
            self.view.day,
            self.today - dt.timedelta(days=self.view.days - 1),
        )

    def test_drill_target_matches_the_row_that_was_highlighted(self):
        """把画出来的那一行和下钻结果对上，杜绝倒序引入的差一错位。"""
        for cursor in (0, 1, 5, self.view.days - 1):
            with self.subTest(cursor=cursor):
                view = EnergyView(self.view.app)
                shown = view._series()[cursor].label
                view.cursor = cursor
                view.handle_key(10)
                self.assertEqual(view.day.isoformat(), shown)

    def test_changing_the_range_keeps_the_newest_day_first(self):
        """[ ] 换范围之后第一行还得是今天。"""
        for key in (ord("["), ord("["), ord("]"), ord("]"), ord("]")):
            self.view.handle_key(key)
            self.assertEqual(self.view._series()[0].label,
                             self.today.isoformat())


class TestHourlyStaysChronological(EnergyViewFixture):
    def test_hours_ascend(self):
        """按小时是一天的曲线，不是日志：00:00 在上，和图表的横轴一致。"""
        self.view.cursor = 0
        self.view.handle_key(10)
        series = self.view.data.hourly_series(self.view.day)
        self.assertEqual([a.label for a in series][:3],
                         ["00:00", "01:00", "02:00"])
        self.assertEqual(len(series), 24)


if __name__ == "__main__":
    unittest.main()
