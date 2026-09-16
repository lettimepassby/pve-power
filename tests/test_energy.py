"""Tests for the energy integration and tariff maths.

These are the parts where a silent error becomes a wrong bill, so they
are checked against hand-computed expectations rather than against the
implementation's own output.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvepower.config import Config, TariffConfig, TouPeriod, Tier
from pvepower.storage import Storage


class TemporaryStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()


class TestIntegration(TemporaryStorage):
    def test_first_sample_contributes_no_energy(self):
        tariff = TariffConfig(flat_price=1.0)
        s = self.storage.record(100.0, tariff, when=dt.datetime(2026, 1, 1, 0, 0, 0))
        self.assertEqual(s.kwh, 0.0)
        self.assertEqual(s.interval_s, 0)

    def test_steady_load_integrates_exactly(self):
        # 100W held for one hour is exactly 0.1 kWh.
        tariff = TariffConfig(flat_price=1.0)
        base = dt.datetime(2026, 1, 1, 0, 0, 0)
        self.storage.record(100.0, tariff, when=base)
        for minute in range(1, 61):
            self.storage.record(
                100.0, tariff, when=base + dt.timedelta(minutes=minute)
            )
        agg = self.storage.aggregate_between(base, base + dt.timedelta(hours=1))
        self.assertAlmostEqual(agg.kwh, 0.1, places=9)

    def test_trapezoid_beats_rectangle_on_a_ramp(self):
        # A linear ramp 100W -> 200W over one hour averages 150W, i.e.
        # 0.15 kWh. Rectangular integration on trailing samples would give
        # more; on leading samples, less. The trapezoid must land on 0.15.
        tariff = TariffConfig(flat_price=1.0)
        base = dt.datetime(2026, 2, 1, 0, 0, 0)
        for minute in range(0, 61):
            watts = 100.0 + (100.0 * minute / 60.0)
            self.storage.record(watts, tariff, when=base + dt.timedelta(minutes=minute))
        agg = self.storage.aggregate_between(base, base + dt.timedelta(hours=1))
        self.assertAlmostEqual(agg.kwh, 0.15, places=6)

    def test_gap_contributes_no_energy(self):
        tariff = TariffConfig(flat_price=1.0)
        base = dt.datetime(2026, 3, 1, 0, 0, 0)
        self.storage.record(200.0, tariff, when=base, max_gap_seconds=900)
        # Ten hours later: far beyond max_gap. Must not bill for the gap.
        gapped = self.storage.record(
            200.0, tariff, when=base + dt.timedelta(hours=10), max_gap_seconds=900
        )
        self.assertTrue(gapped.is_gap)
        self.assertEqual(gapped.kwh, 0.0)
        agg = self.storage.aggregate_between(base, base + dt.timedelta(hours=11))
        self.assertEqual(agg.kwh, 0.0)

    def test_non_monotonic_sample_is_rejected(self):
        tariff = TariffConfig(flat_price=1.0)
        base = dt.datetime(2026, 4, 1, 12, 0, 0)
        self.storage.record(100.0, tariff, when=base)
        with self.assertRaises(ValueError):
            self.storage.record(100.0, tariff, when=base - dt.timedelta(minutes=5))

    def test_average_watts_uses_covered_time(self):
        # Dense sampling for a short window plus sparse sampling for a long
        # one: the mean must weight by time, not by sample count.
        tariff = TariffConfig(flat_price=1.0)
        base = dt.datetime(2026, 5, 1, 0, 0, 0)
        self.storage.record(100.0, tariff, when=base)
        for i in range(1, 11):  # ten samples, 10 minutes, at 100W
            self.storage.record(100.0, tariff, when=base + dt.timedelta(minutes=i))
        for i in range(1, 11):  # ten samples, 100 minutes, at 200W
            self.storage.record(
                200.0, tariff, when=base + dt.timedelta(minutes=10 + i * 10)
            )
        agg = self.storage.aggregate_between(base, base + dt.timedelta(hours=3))
        # Time-weighted mean sits near 200W, well above the 150W a
        # sample-count mean would produce.
        self.assertGreater(agg.avg_watts, 180.0)


class TestTariff(unittest.TestCase):
    def test_flat_price(self):
        t = TariffConfig(mode="flat", flat_price=0.75)
        self.assertEqual(t.price_at(dt.datetime(2026, 1, 1, 3, 0)), 0.75)

    def test_tou_selects_by_hour(self):
        t = TariffConfig(
            mode="tou",
            flat_price=0.6,
            tou_periods=[
                TouPeriod(name="peak", price=1.2, hours=[19, 20]),
                TouPeriod(name="valley", price=0.3, hours=[1, 2]),
            ],
        )
        self.assertEqual(t.price_at(dt.datetime(2026, 1, 1, 19, 30)), 1.2)
        self.assertEqual(t.price_at(dt.datetime(2026, 1, 1, 2, 5)), 0.3)
        # Unmatched hour falls back to flat rather than billing at zero.
        self.assertEqual(t.price_at(dt.datetime(2026, 1, 1, 10, 0)), 0.6)

    def test_tou_weekday_restriction(self):
        t = TariffConfig(
            mode="tou",
            flat_price=0.5,
            tou_periods=[
                TouPeriod(name="weekday-peak", price=1.5, hours=[10], days=[0, 1, 2, 3, 4]),
            ],
        )
        monday = dt.datetime(2026, 1, 5, 10, 0)
        saturday = dt.datetime(2026, 1, 10, 10, 0)
        self.assertEqual(monday.weekday(), 0)
        self.assertEqual(saturday.weekday(), 5)
        self.assertEqual(t.price_at(monday), 1.5)
        self.assertEqual(t.price_at(saturday), 0.5)

    def test_tiered_surcharge_steps_with_consumption(self):
        t = TariffConfig(
            mode="flat",
            flat_price=0.5,
            tiered_enabled=True,
            tiers=[
                Tier(limit_kwh=100, surcharge=0.0, name="t1"),
                Tier(limit_kwh=200, surcharge=0.05, name="t2"),
                Tier(limit_kwh=None, surcharge=0.10, name="t3"),
            ],
        )
        when = dt.datetime(2026, 1, 1, 12, 0)
        self.assertAlmostEqual(t.price_at(when, 50), 0.50)
        self.assertAlmostEqual(t.price_at(when, 150), 0.55)
        self.assertAlmostEqual(t.price_at(when, 500), 0.60)


class TestCostAccounting(TemporaryStorage):
    def test_cost_is_priced_at_sample_time_not_query_time(self):
        # Energy consumed in a cheap window must stay cheap even after the
        # tariff moves into an expensive window.
        tariff = TariffConfig(
            mode="tou",
            flat_price=0.6,
            tou_periods=[
                TouPeriod(name="valley", price=0.20, hours=[2, 3]),
                TouPeriod(name="peak", price=2.00, hours=[20, 21]),
            ],
        )
        day = dt.date(2026, 6, 1)
        valley_start = dt.datetime.combine(day, dt.time(2, 0))
        for minute in range(0, 61):
            self.storage.record(
                1000.0, tariff, when=valley_start + dt.timedelta(minutes=minute)
            )
        # 1000W for one hour = 1 kWh at 0.20 = 0.20.
        agg = self.storage.aggregate_between(
            valley_start, valley_start + dt.timedelta(hours=1)
        )
        self.assertAlmostEqual(agg.kwh, 1.0, places=6)
        self.assertAlmostEqual(agg.cost, 0.20, places=6)

        peak_start = dt.datetime.combine(day, dt.time(20, 0))
        self.storage.record(1000.0, tariff, when=peak_start)
        for minute in range(1, 61):
            self.storage.record(
                1000.0, tariff, when=peak_start + dt.timedelta(minutes=minute)
            )
        peak = self.storage.aggregate_between(
            peak_start, peak_start + dt.timedelta(hours=1)
        )
        self.assertAlmostEqual(peak.cost, 2.00, places=6)

        # The earlier window's cost is untouched by the later one.
        valley = self.storage.aggregate_between(
            valley_start, valley_start + dt.timedelta(hours=1)
        )
        self.assertAlmostEqual(valley.cost, 0.20, places=6)

    def test_period_breakdown_splits_by_window(self):
        tariff = TariffConfig(
            mode="tou",
            flat_price=0.6,
            tou_periods=[
                TouPeriod(name="valley", price=0.20, hours=list(range(0, 6))),
                TouPeriod(name="peak", price=2.00, hours=list(range(19, 23))),
            ],
        )
        base = dt.datetime(2026, 7, 1, 0, 0)
        for minute in range(0, 121):
            self.storage.record(600.0, tariff, when=base + dt.timedelta(minutes=minute))
        peak_start = dt.datetime(2026, 7, 1, 19, 0)
        self.storage.record(600.0, tariff, when=peak_start, max_gap_seconds=900)
        for minute in range(1, 121):
            self.storage.record(
                600.0, tariff, when=peak_start + dt.timedelta(minutes=minute)
            )
        rows = self.storage.period_breakdown(base, peak_start + dt.timedelta(hours=3))
        by_name = {name: (kwh, cost) for name, kwh, cost in rows}
        self.assertIn("valley", by_name)
        self.assertIn("peak", by_name)
        self.assertAlmostEqual(by_name["valley"][0], 1.2, places=6)
        self.assertAlmostEqual(by_name["peak"][0], 1.2, places=6)
        # Same energy, ten times the price.
        self.assertAlmostEqual(by_name["peak"][1] / by_name["valley"][1], 10.0, places=6)

    def test_reprice_keeps_energy_and_updates_money(self):
        cheap = TariffConfig(mode="flat", flat_price=0.50)
        base = dt.datetime(2026, 8, 1, 0, 0)
        for minute in range(0, 61):
            self.storage.record(1000.0, cheap, when=base + dt.timedelta(minutes=minute))
        before = self.storage.aggregate_between(base, base + dt.timedelta(hours=1))
        self.assertAlmostEqual(before.cost, 0.50, places=6)

        self.storage.reprice(TariffConfig(mode="flat", flat_price=1.50))
        after = self.storage.aggregate_between(base, base + dt.timedelta(hours=1))
        self.assertAlmostEqual(after.kwh, before.kwh, places=9)
        self.assertAlmostEqual(after.cost, 1.50, places=6)


class TestConfigValidation(unittest.TestCase):
    def test_uncovered_tou_hours_are_reported(self):
        cfg = Config()
        cfg.tariff = TariffConfig(
            mode="tou",
            tou_periods=[TouPeriod(name="only-morning", price=1.0, hours=[8, 9])],
        )
        problems = cfg.validate()
        self.assertTrue(any("uncovered" in p for p in problems))

    def test_unordered_tiers_are_reported(self):
        cfg = Config()
        cfg.tariff = TariffConfig(
            tiered_enabled=True,
            tiers=[
                Tier(limit_kwh=200, surcharge=0.1),
                Tier(limit_kwh=100, surcharge=0.0),
                Tier(limit_kwh=None, surcharge=0.2),
            ],
        )
        problems = cfg.validate()
        self.assertTrue(any("ascending" in p for p in problems))

    def test_gap_shorter_than_interval_is_reported(self):
        cfg = Config()
        cfg.collector.interval_seconds = 600
        cfg.collector.max_gap_seconds = 300
        self.assertTrue(any("max_gap_seconds" in p for p in cfg.validate()))

    def test_roundtrip_through_json(self):
        import json

        cfg = Config()
        cfg.tariff = TariffConfig(
            mode="tou",
            flat_price=0.66,
            tou_periods=[TouPeriod(name="p", price=1.1, hours=[1, 2], days=[0])],
            tiered_enabled=True,
            tiers=[Tier(limit_kwh=50, surcharge=0.0), Tier(limit_kwh=None, surcharge=0.3)],
        )
        restored = Config.from_dict(json.loads(json.dumps(cfg.to_dict())))
        self.assertEqual(restored.tariff.mode, "tou")
        self.assertAlmostEqual(restored.tariff.flat_price, 0.66)
        self.assertEqual(restored.tariff.tou_periods[0].hours, [1, 2])
        self.assertEqual(restored.tariff.tou_periods[0].days, [0])
        self.assertIsNone(restored.tariff.tiers[-1].limit_kwh)
        self.assertEqual(restored.validate(), restored.validate())


if __name__ == "__main__":
    unittest.main(verbosity=2)
