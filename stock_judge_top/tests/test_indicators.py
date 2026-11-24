#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""K 线指标：分位、滚动均值、kline_metrics 全字段、市场阶段分支。"""
import unittest

from stock_judge_top import indicators as ind
from stock_judge_top.tests import fixtures as fx


class PctRankTest(unittest.TestCase):
    def test_none_value_returns_none(self):
        self.assertIsNone(ind.pct_rank([1, 2, 3], None))

    def test_sample_too_small_returns_none(self):
        self.assertIsNone(ind.pct_rank(list(range(10)), 5, min_n=11))
        self.assertIsNotNone(ind.pct_rank(list(range(10)), 5, min_n=10))

    def test_rank_is_inclusive_of_itself(self):
        # 0..59 里 30 是第 31 个（含自身），所以是 31/60
        self.assertAlmostEqual(ind.pct_rank(list(range(60)), 30, min_n=60),
                               31 / 60 * 100)

    def test_ties_count_as_full_percentile(self):
        self.assertEqual(ind.pct_rank([5.0] * 60, 5.0), 100.0)

    def test_none_entries_are_ignored(self):
        vals = [None] * 30 + list(range(60))
        self.assertAlmostEqual(ind.pct_rank(vals, 30, min_n=60), 31 / 60 * 100)


class RollMeanTest(unittest.TestCase):
    def test_basic_mean(self):
        self.assertEqual(ind.roll_mean([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])

    def test_window_with_none_is_skipped(self):
        self.assertEqual(ind.roll_mean([1, 2, None, 4, 5], 2),
                         [None, 1.5, None, None, 4.5])

    def test_window_longer_than_series(self):
        self.assertEqual(ind.roll_mean([1, 2, 3], 5), [None, None, None])

    def test_k_equals_one(self):
        self.assertEqual(ind.roll_mean([1, 2, 3], 1), [1.0, 2.0, 3.0])


class KlineMetricsTest(unittest.TestCase):
    def test_too_few_bars_returns_none(self):
        self.assertIsNone(ind.kline_metrics(fx.kline(29)))

    def test_missing_close_returns_none(self):
        nodes = fx.kline(60)
        nodes[10]["last"] = None
        self.assertIsNone(ind.kline_metrics(nodes))

    def test_missing_high_returns_none(self):
        nodes = fx.kline(60)
        nodes[10]["high"] = ""
        self.assertIsNone(ind.kline_metrics(nodes))

    def test_full_series_values(self):
        km = ind.kline_metrics(fx.kline(250))
        # close = 10 + 0.1*i → close[-1] = 34.90，close[-6] = 34.40
        self.assertAlmostEqual(km["ret5"], 34.9 / 34.4 - 1, places=9)
        self.assertAlmostEqual(km["ret20"], 34.9 / 32.9 - 1, places=9)
        self.assertAlmostEqual(km["ret60"], 34.9 / 28.9 - 1, places=9)
        # 位置口径是最高价：high[-1] = 34.95
        self.assertAlmostEqual(km["high250"], 34.95, places=6)
        self.assertAlmostEqual(km["pos250"], 34.9 / 34.95 - 1, places=9)
        self.assertEqual(km["days_since_high"], 0)
        self.assertEqual(km["high250_date"], fx.trading_days(250)[-1])
        self.assertEqual(km["pos_basis"], "high")
        # 换手恒为 0.50% → 均值相同、标准差 0、比值 1
        self.assertAlmostEqual(km["av20"], 0.5, places=9)
        self.assertAlmostEqual(km["av250"], 0.5, places=9)
        self.assertEqual(km["std20"], 0.0)
        self.assertAlmostEqual(km["ratio"], 1.0, places=9)
        self.assertAlmostEqual(km["pct"]["AVG20"], 100.0)
        self.assertEqual(km["pct_n"]["AVG20"], 231)      # 250-19
        # 量比：成交量恒定 → 1.0
        self.assertAlmostEqual(km["pvol_ratio5"], 1.0, places=9)
        self.assertAlmostEqual(km["range5_high"], 34.9, places=6)
        self.assertAlmostEqual(km["range5_low"], 34.5, places=6)
        self.assertFalse(km["below_ma20"])

    def test_short_series_leaves_local_windows_empty(self):
        km = ind.kline_metrics(fx.kline(30))
        self.assertEqual(km["bars"], 30)
        self.assertIsNone(km["pos60"])          # require=True 且不足 60 根
        self.assertIsNone(km["high60"])
        self.assertIsNone(km["av250"])
        self.assertIsNone(km["ratio"])
        self.assertIsNone(km["ret60"])
        # 250 日窗口同样 require=True：不足 250 根时 pos250 为 None，
        # 下游退回 60 日口径，不再拿「现有 30 根」冒充 250 日高点。
        self.assertIsNone(km["pos250"])
        self.assertIsNone(km["high250"])

    def test_below_ma20_flag(self):
        nodes = fx.kline(120)
        nodes[-1]["last"] = 1.0
        nodes[-1]["high"] = 1.5
        km = ind.kline_metrics(nodes)
        self.assertTrue(km["below_ma20"])

    def test_flat_prices(self):
        nodes = fx.kline(120, step=0.0)
        km = ind.kline_metrics(nodes)
        self.assertEqual(km["ret5"], 0.0)
        self.assertEqual(km["ret20"], 0.0)
        self.assertAlmostEqual(km["pos60"], 10.0 / 10.05 - 1, places=9)
        self.assertAlmostEqual(km["std20"], 0.0)


class MarketPhaseTest(unittest.TestCase):
    def phase(self, **km):
        return ind.market_phase(km)

    def test_past_high_and_rebound(self):
        self.assertEqual(
            self.phase(pos250=-0.30, pos60=-0.04, ret20=0.05),
            "历史顶已过，当前反弹接近局部高点")

    def test_past_high_local_high(self):
        self.assertEqual(
            self.phase(pos250=-0.30, pos60=-0.04, ret20=-0.01),
            "历史顶已过，当前在局部高位附近")

    def test_past_high_pullback(self):
        self.assertEqual(
            self.phase(pos250=-0.30, pos60=-0.15, ret20=0.05),
            "历史顶已过，当前处于回撤/震荡")

    def test_near_250_high(self):
        self.assertEqual(self.phase(pos250=-0.03, pos60=-0.01), "接近250日高点")

    def test_near_60_high(self):
        self.assertEqual(self.phase(pos250=-0.08, pos60=-0.02), "接近60日局部高点")

    def test_small_pullback(self):
        self.assertEqual(self.phase(pos250=-0.08, pos60=-0.10), "高位小幅回落")

    def test_mid_pullback(self):
        self.assertEqual(self.phase(pos250=-0.15, pos60=-0.20), "中高位回落")

    def test_past_high_pullback_is_not_reported_as_not_high(self):
        """pos250 ≤ -20% 一律归入「历史顶已过」，这正是它和「未处于明显高位」的区别。"""
        self.assertEqual(self.phase(pos250=-0.30, pos60=-0.40),
                         "历史顶已过，当前处于回撤/震荡")

    def test_not_high_when_only_pos60_known(self):
        # 只要 pos250 有值，就一定会落进上面某个分支；
        # 所以「未处于明显高位」只在 pos250 缺失、pos60 有值时出现。
        self.assertEqual(self.phase(pos250=None, pos60=-0.40), "未处于明显高位")

    def test_no_position_data_is_not_reported_as_not_high(self):
        """缺位置数据必须说「不判断」，不能说成「未处于明显高位」。"""
        self.assertEqual(self.phase(pos250=None, pos60=None, ret20=None),
                         "位置数据不足，不判断")
        self.assertEqual(self.phase(), "位置数据不足，不判断")


if __name__ == "__main__":
    unittest.main()
