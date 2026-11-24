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
    def state(self, ret20, **over):
        return momentum_view({"kline": km(ret20=ret20, **over)})["state"]

    def test_states(self):
        """状态是二维：位置·涨速。fixture 的 pos250 = -13.31% → 中位。"""
        self.assertEqual(self.state(0.25), "中位·过热")
        self.assertEqual(self.state(0.20), "中位·过热")
        self.assertEqual(self.state(0.199), "中位·偏热")
        self.assertEqual(self.state(0.10), "中位·偏热")
        self.assertEqual(self.state(0.099), "中位·走平")
        self.assertEqual(self.state(0.0), "中位·走平")
        self.assertEqual(self.state(-0.001), "中位·偏弱")

    def test_position_dimension_uses_pos250(self):
        self.assertEqual(self.state(0.25, pos250=-0.02), "高位·过热")
        self.assertEqual(self.state(0.25, pos250=-0.30), "低位·过热")
        # 不足 250 根时退回 60 日，口径写在副标题里
        v = momentum_view({"kline": km(ret20=0.25, pos250=None, pos60=-0.02)})
        self.assertEqual(v["state"], "高位·过热")
        self.assertIn("60日", v["subtitle"])

    def test_low_position_surge_is_not_called_overheated(self):
        """低位反抽不能叫「过热」——那是最容易被读成见顶的误标。

        旧版只看 20 日涨幅：阿尔特 +40.7% 但离 60 日高点 -14.0% 被标"过热"。
        """
        v = momentum_view({"kline": km(ret20=0.407, pos250=-0.14)})
        self.assertEqual(v["state"], "中位·过热")
        self.assertIn("不是高位过热", v["headline"])
        v2 = momentum_view({"kline": km(ret20=0.30, pos250=-0.45)})
        self.assertEqual(v2["state"], "低位·过热")
        self.assertIn("低位反抽", v2["headline"])

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
        # 「高位」用 250 日（2026-11-15 起），pos60 只是显示用
        v = self.view(pos250=-0.02, pos60=-0.02, pvol_ratio5=1.6,
                      range5_high=1.02, range5_low=1.0, ret5=0.005)
        self.assertEqual(v["state"], "放量盘整")

    def test_high_level_volume_up(self):
        v = self.view(pos250=-0.02, pos60=-0.02, pvol_ratio5=1.6,
                      range5_high=1.10, range5_low=1.0, ret5=0.03)
        self.assertEqual(v["state"], "放量上涨")

    def test_volume_without_high_position(self):
        v = self.view(pos250=-0.30, pos60=-0.30, pvol_ratio5=2.0)
        self.assertEqual(v["state"], "放量（非高位）")

    def test_not_triggered(self):
        self.assertEqual(self.view(pvol_ratio5=1.0, pos250=-0.01)["state"], "未触发")

    def test_insufficient(self):
        self.assertEqual(self.view(bars=30)["state"], "数据不足")

    def test_note_uses_pos_basis_not_hardcoded(self):
        """note 里的「高位」口径必须跟 pos_basis 走，不能写死 60 日/250 日。"""
        # 正常票：pos250 有值 → note 说「250 日」，不能说「60 日」
        v = self.view(pos250=-0.30, pos60=-0.30, pvol_ratio5=2.0)
        self.assertIn("250日", v["note"])
        self.assertNotIn("60日", v["note"])
        # 次新股：pos250 缺失退 60 日 → note 说「60 日」
        v2 = self.view(pos250=None, pos60=-0.30, pvol_ratio5=2.0)
        self.assertIn("60日", v2["note"])
        self.assertNotIn("250日", v2["note"])

    def test_amplitude_has_no_sign(self):
        """振幅恒正，不应带 + 号（那是涨跌的记号）。"""
        v = self.view(pos250=-0.02, pos60=-0.02, pvol_ratio5=1.6,
                      range5_high=1.02, range5_low=1.0, ret5=0.005)
        for label, value in v["metrics"]:
            if label == "5日振幅":
                self.assertNotIn("+", value)          # 1.96%，不是 +1.96%
                self.assertIn("%", value)


class TurnoverAmountFallbackTest(unittest.TestCase):
    """流通市值缺失 → 用成交额自身分位，而不是整张卡片"数据不足"。"""

    def view(self, **over):
        base = dict(turnover_basis="amount", av20=None, av250=None,
                    pct={"AVG20": None}, amt_av20=8e8, amt_av250=5e8,
                    amt_ratio=1.6, amt_pct=82.0, amt_pct_n=240)
        base.update(over)
        return turnover_view({"kline": km(**base)})

    def test_falls_back_instead_of_insufficient(self):
        v = self.view()
        self.assertEqual(v["state"], "偏高")
        self.assertEqual(v["title"], "交易拥挤度")      # 不顶着"换手"的名字
        self.assertIn("成交额", v["subtitle"])

    def test_thresholds_mirror_turnover(self):
        self.assertEqual(self.view(amt_pct=80.0, amt_ratio=1.1)["state"], "中性")
        self.assertEqual(self.view(amt_pct=60.0)["state"], "中性")
        self.assertEqual(self.view(amt_pct=30.0)["state"], "偏低")

    def test_amount_history_too_short(self):
        self.assertEqual(self.view(amt_pct=None)["state"], "数据不足")


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
