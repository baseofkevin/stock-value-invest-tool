#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四张卡片的阈值边界与「数据不足」文案。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_judge_top.views.earnings import earnings_view     # noqa: E402
from stock_judge_top.views.momentum import momentum_view     # noqa: E402
from stock_judge_top.views.turnover import turnover_view     # noqa: E402
from stock_judge_top.views.volume import volume_view         # noqa: E402
from stock_judge_top.tests import fixtures as fx                   # noqa: E402


def km(**over):
    return fx.kline_dict(**over)


class MomentumViewTest(unittest.TestCase):
    def state(self, ret20):
        return momentum_view({"kline": km(ret20=ret20)})["state"]

    def test_states(self):
        self.assertEqual(self.state(0.25), "过热")
        self.assertEqual(self.state(0.20), "过热")
        self.assertEqual(self.state(0.199), "偏热")
        self.assertEqual(self.state(0.10), "偏热")
        self.assertEqual(self.state(0.099), "中性")
        self.assertEqual(self.state(0.0), "中性")
        self.assertEqual(self.state(-0.001), "偏弱")

    def test_short_history_is_insufficient(self):
        v = momentum_view({"kline": km(bars=50)})
        self.assertEqual(v["state"], "数据不足")
        self.assertEqual(v["headline"], "数据不足，不判断")

    def test_missing_pos60_is_insufficient(self):
        self.assertEqual(momentum_view({"kline": km(pos60=None)})["state"], "数据不足")

    def test_calc_rows_explain_formula(self):
        v = momentum_view({"kline": km()})
        labels = [k for k, _ in v["calc"]]
        self.assertIn("60日最高价", labels)        # 口径必须写「最高价」
        self.assertNotIn("60日最高收盘", labels)
        self.assertTrue(any("÷ 20日前收盘" in val for _, val in v["calc"]))

    def test_metrics_cover_four_factors(self):
        self.assertEqual(len(momentum_view({"kline": km()})["metrics"]), 4)


class TurnoverViewTest(unittest.TestCase):
    def view(self, **over):
        return turnover_view({"kline": km(**over)})

    def test_high_turnover_needs_both_rank_and_ratio(self):
        self.assertEqual(self.view(pct={"AVG20": 80}, ratio=1.5)["state"], "偏高")
        self.assertEqual(self.view(pct={"AVG20": 80}, ratio=1.1)["state"], "中性")
        self.assertEqual(self.view(pct={"AVG20": 74}, ratio=1.5)["state"], "中性")

    def test_low_turnover(self):
        self.assertEqual(self.view(pct={"AVG20": 49}, ratio=0.8)["state"], "偏低")

    def test_missing_250d_average_is_insufficient(self):
        v = self.view(av250=None, ratio=None)
        self.assertEqual(v["state"], "数据不足")

    def test_calc_never_leaks_none_into_formatting(self):
        v = self.view(av250=None, ratio=None)
        for _, value in v["calc"]:
            self.assertNotIn("None", value)


class VolumeViewTest(unittest.TestCase):
    def view(self, **over):
        return volume_view({"kline": km(**over)})

    def test_high_level_stall(self):
        v = self.view(pos60=-0.02, pvol_ratio5=1.6, range5_high=1.02, range5_low=1.0,
                      ret5=0.005)
        self.assertEqual(v["state"], "放量盘整")

    def test_high_level_volume_up(self):
        v = self.view(pos60=-0.02, pvol_ratio5=1.6, range5_high=1.10, range5_low=1.0,
                      ret5=0.03)
        self.assertEqual(v["state"], "放量上涨")

    def test_volume_without_high_position(self):
        v = self.view(pos60=-0.30, pvol_ratio5=2.0)
        self.assertEqual(v["state"], "放量（非高位）")

    def test_not_triggered(self):
        self.assertEqual(self.view(pvol_ratio5=1.0, pos60=-0.01)["state"], "未触发")

    def test_insufficient(self):
        self.assertEqual(self.view(bars=30)["state"], "数据不足")


class EarningsViewTest(unittest.TestCase):
    def view(self, fm=None, ej=None):
        r = {"finance": fm, "earnings": ej or {}}
        return earnings_view(r)

    def test_three_blocks_are_separated(self):
        v = self.view(fx.finance_dict(), fx.earnings_dict())
        titles = [b["title"] for b in v["blocks"]]
        self.assertEqual(titles, ["同比", "环比", "营收"])
        for block in v["blocks"]:
            self.assertTrue(block["results"])
            self.assertTrue(block["sources"])
            self.assertTrue(block["formulas"])
        self.assertTrue(v["wide"])

    def test_accelerating_state(self):
        self.assertEqual(self.view(fx.finance_dict(),
                                  fx.earnings_dict(direction="加速", qoq_direction="持平"))["state"],
                         "加速")

    def test_decelerating_state(self):
        self.assertEqual(self.view(fx.finance_dict(),
                                  fx.earnings_dict(direction="减速", qoq_direction="未知"))["state"],
                         "减速")

    def test_loss_making_state(self):
        self.assertEqual(self.view(fx.finance_dict(profit_q=-1e8),
                                  fx.earnings_dict(direction="未知"))["state"], "盈利为负")

    def test_not_comparable_state(self):
        v = self.view(fx.finance_dict(accel=None, qoq_accel=None),
                      fx.earnings_dict(direction="未知", qoq_direction="未知"))
        self.assertEqual(v["state"], "数据不足")
        self.assertIn("只展示原始数据", v["note"])

    def test_no_finance_data(self):
        v = self.view(None)
        self.assertEqual(v["state"], "数据不足")
        self.assertEqual(v["headline"], "数据不足，不判断")
        self.assertNotIn("blocks", v)

    def test_formula_mentions_single_quarter_profit(self):
        v = self.view(fx.finance_dict(), fx.earnings_dict())
        formulas = " ".join(v["blocks"][0]["formulas"])
        self.assertIn("÷ 上年同期", formulas)


if __name__ == "__main__":
    unittest.main()
