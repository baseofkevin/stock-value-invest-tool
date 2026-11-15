#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业绩判断：阈值边界、不可比分支、同比/环比口径一致性。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from stock_judge_top import judgement as jdg     # noqa: E402


class EarningsJudgementTest(unittest.TestCase):
    def judge(self, accel, profit=1e8, note=""):
        return jdg.earnings_judgement({"accel": accel, "profit": profit,
                                       "accel_note": note})

    def test_thresholds_are_inclusive_on_the_upper_side(self):
        self.assertEqual(self.judge(0.50)["state"], "强加速")
        self.assertEqual(self.judge(0.499)["state"], "加速")
        self.assertEqual(self.judge(0.10)["state"], "加速")
        self.assertEqual(self.judge(0.099)["state"], "基本持平")
        self.assertEqual(self.judge(-0.099)["state"], "基本持平")
        self.assertEqual(self.judge(-0.10)["state"], "减速")
        self.assertEqual(self.judge(-0.499)["state"], "减速")
        self.assertEqual(self.judge(-0.50)["state"], "强减速")

    def test_direction_flags_match_state(self):
        acc = self.judge(0.30)
        self.assertEqual(acc["direction"], "加速")
        self.assertTrue(acc["is_accelerating"])
        self.assertFalse(acc["is_decelerating"])
        dec = self.judge(-0.30)
        self.assertEqual(dec["direction"], "减速")
        self.assertTrue(dec["is_decelerating"])
        flat = self.judge(0.0)
        self.assertEqual(flat["direction"], "持平")
        self.assertFalse(flat["is_accelerating"])
        self.assertFalse(flat["is_decelerating"])

    def test_note_contains_percentage(self):
        self.assertIn("+100.0%", self.judge(1.0)["note"])

    def test_no_finance_data(self):
        r = jdg.earnings_judgement(None)
        self.assertEqual(r["state"], "数据不足")
        self.assertEqual(r["direction"], "未知")

    def test_accel_none_is_not_comparable(self):
        r = jdg.earnings_judgement({"accel": None, "accel_note": "缺少上一期增速"})
        self.assertEqual(r["state"], "数据不足")
        self.assertIn("缺少上一期", r["note"])

    def test_negative_profit_short_circuits(self):
        r = self.judge(1.0, profit=-1.0)
        self.assertEqual(r["state"], "盈利为负")
        self.assertFalse(r["is_accelerating"])

    def test_outlier_adds_warning(self):
        self.assertIn("基数异常", self.judge(6.0)["note"])

    def test_accel_note_is_appended(self):
        self.assertIn("只作参考", self.judge(1.0, note="上期增速为负，相对加速度只作参考")["note"])


class QoqJudgementTest(unittest.TestCase):
    def test_missing_qoq(self):
        self.assertEqual(jdg.qoq_judgement(None)["state"], "不可比")
        self.assertEqual(jdg.qoq_judgement({"qoq": None, "qoq_note": "缺少上一季度数据"})["state"],
                         "不可比")

    def test_missing_accel(self):
        r = jdg.qoq_judgement({"qoq": 0.1, "qoq_accel": None, "qoq_note": "缺少上上季度数据"})
        self.assertEqual(r["state"], "不可比")
        self.assertIn("上上季", r["note"])

    def test_extreme_qoq_is_outlier(self):
        self.assertEqual(jdg.qoq_judgement({"qoq": 6.0, "qoq_accel": 0.2})["state"], "基数异常")
        self.assertEqual(jdg.qoq_judgement({"qoq": 0.2, "qoq_accel": -6.0})["state"], "基数异常")

    def test_states(self):
        self.assertEqual(jdg.qoq_judgement({"qoq": 0.2, "qoq_accel": 0.6})["state"], "强加速")
        self.assertEqual(jdg.qoq_judgement({"qoq": 0.2, "qoq_accel": 0.2})["state"], "加速")
        self.assertEqual(jdg.qoq_judgement({"qoq": 0.2, "qoq_accel": 0.0})["state"], "基本持平")
        self.assertEqual(jdg.qoq_judgement({"qoq": 0.2, "qoq_accel": -0.2})["state"], "减速")
        self.assertEqual(jdg.qoq_judgement({"qoq": 0.2, "qoq_accel": -0.6})["state"], "强减速")

    def test_same_thresholds_as_yoy(self):
        """同比与环比必须用同一套阈值，避免两处各写一份 if/elif 后走偏。"""
        for accel in (0.5, 0.3, 0.0, -0.3, -0.5):
            yoy = jdg.earnings_judgement({"accel": accel, "profit": 1e8})
            qoq = jdg.qoq_judgement({"qoq": 0.2, "qoq_accel": accel})
            self.assertEqual(yoy["state"], qoq["state"], accel)


class HelperTest(unittest.TestCase):
    def test_trend_arrow(self):
        self.assertEqual(jdg.trend_arrow(None), "—")
        self.assertEqual(jdg.trend_arrow(5), "↑ 加速")
        self.assertEqual(jdg.trend_arrow(-5), "↓ 减速")
        self.assertEqual(jdg.trend_arrow(4.9), "→ 持平")

    def test_consecutive_qoq_stops_at_gap(self):
        hist = [{"qoq": None}, {"qoq": 1}, {"qoq": 2}, {"qoq": 3}]
        self.assertEqual(len(jdg.consecutive_qoq(hist, limit=4)), 3)
        self.assertEqual(len(jdg.consecutive_qoq(hist, limit=2)), 2)
        self.assertEqual(jdg.consecutive_qoq(None), [])

    def test_earnings_combo_branches(self):
        self.assertEqual(jdg.earnings_combo({"direction": "加速", "qoq_direction": "加速"}),
                         "同比与环比同向（加速）")
        self.assertEqual(jdg.earnings_combo({"direction": "加速", "qoq_direction": "减速"}),
                         "同比与环比背离（同比加速、环比减速）")
        self.assertIn("以同比为主",
                      jdg.earnings_combo({"direction": "减速", "qoq_direction": "未知",
                                          "qoq_state": "不可比"}))
        self.assertIn("注意季节性",
                      jdg.earnings_combo({"direction": "持平", "qoq_direction": "加速"}))
        self.assertEqual(jdg.earnings_combo({"direction": "持平", "qoq_direction": "持平"}),
                         "同比与环比均基本持平")
        self.assertEqual(jdg.earnings_combo({}), "数据不足，无法判断")


if __name__ == "__main__":
    unittest.main()
