"""传感器严重度与条形比例的判定。

这里的回归对象是一个具体的误报：这台机器的 SYS_12V 标称 12V、上限
临界 14.288V，正常读数 11.844V 正好落在上限的 83%。早先的版本按
「读数 / 上限临界」推严重度，于是把一路最健康的电压画成了告警色。
温度那样单侧越高越危险的传感器可以这么算，电压不行——它在量程中间
有标称点，两头才是危险。BMC 的 nc / crit 阈值上下两侧都给了，直接用。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvepower.ipmi import Sensor


def volt(value):
    """本机 SYS_12V 的真实阈值（ipmitool sensor 读回来的）。"""
    return Sensor(
        name="SYS_12V", value=value, unit="Volts", status="ok",
        lower_crit=9.024, lower_nc=10.528,
        upper_nc=13.536, upper_crit=14.288,
    )


def temp(value, status="ok"):
    """本机 CPU0_Temp：只有上限阈值，没有下限。"""
    return Sensor(
        name="CPU0_Temp", value=value, unit="degrees C", status=status,
        upper_nc=100.0, upper_crit=103.0,
    )


class TestSeverity(unittest.TestCase):
    def test_nominal_voltage_is_not_a_warning(self):
        # 这条就是当初的误报本身。
        self.assertEqual(volt(11.844).severity(), "ok")

    def test_voltage_warns_on_both_sides(self):
        self.assertEqual(volt(13.6).severity(), "warn")
        self.assertEqual(volt(10.4).severity(), "warn")

    def test_voltage_crits_on_both_sides(self):
        self.assertEqual(volt(14.3).severity(), "crit")
        self.assertEqual(volt(9.0).severity(), "crit")

    def test_temperature_uses_upper_thresholds(self):
        self.assertEqual(temp(57.0).severity(), "ok")
        self.assertEqual(temp(101.0).severity(), "warn")
        self.assertEqual(temp(104.0).severity(), "crit")

    def test_unreadable_sensor_is_not_an_alarm(self):
        # 读不到值的传感器（本机有一批 na）不该被算成故障。
        s = Sensor(name="RAID_Temp", value=None, unit="degrees C", status="na")
        self.assertEqual(s.severity(), "ok")


class TestBarFraction(unittest.TestCase):
    def test_nominal_voltage_sits_mid_scale(self):
        # 标称 12V 在 [9.024, 14.288] 里大约居中，条形应该画到一半上下，
        # 而不是 83% 那么长。
        f = volt(11.844).bar_fraction()
        self.assertTrue(0.4 < f < 0.7, f)

    def test_temperature_fraction_is_distance_to_critical(self):
        # 单侧传感器保持原来的含义：条形长度 = 离过热还有多远。
        self.assertAlmostEqual(temp(51.5).bar_fraction(), 0.5, places=2)

    def test_fraction_is_clamped(self):
        self.assertEqual(volt(20.0).bar_fraction(), 1.0)
        self.assertEqual(volt(1.0).bar_fraction(), 0.0)

    def test_no_thresholds_means_no_bar(self):
        s = Sensor(name="CPU0_Margin_Temp", value=35.0,
                   unit="degrees C", status="ok")
        self.assertIsNone(s.bar_fraction())


if __name__ == "__main__":
    unittest.main()
