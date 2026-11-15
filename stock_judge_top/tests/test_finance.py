#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""财务口径：季度推算、相对加速度、fin_metrics 的过滤/去重/不可比分支。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from stock_judge_top import finance as fin          # noqa: E402
from stock_judge_top.tests import fixtures as fx          # noqa: E402


class QuarterHelperTest(unittest.TestCase):
    def test_prev_quarter(self):
        self.assertEqual(fin.prev_quarter("2026-03-31"), "2025-12-31")
        self.assertEqual(fin.prev_quarter("2026-06-30"), "2026-03-31")
        self.assertEqual(fin.prev_quarter("2026-09-30"), "2026-06-30")
        self.assertEqual(fin.prev_quarter("2026-12-31"), "2026-09-30")
        self.assertIsNone(fin.prev_quarter("2026-05-31"))

    def test_year_ago(self):
        self.assertEqual(fin.year_ago("2026-03-31"), "2025-03-31")
        self.assertEqual(fin.year_ago("2026-12-31"), "2025-12-31")


class RelativeAccelTest(unittest.TestCase):
    def test_normal_growth(self):
        value, note = fin.relative_accel(0.2, 0.1, 0.01)
        self.assertAlmostEqual(value, 1.0)
        self.assertEqual(note, "")

    def test_negative_base_keeps_direction(self):
        value, note = fin.relative_accel(0.1, -0.2, 0.01)
        self.assertAlmostEqual(value, 1.5)
        self.assertIn("上期增速为负", note)

    def test_missing_inputs(self):
        self.assertEqual(fin.relative_accel(None, 0.1, 0.01)[0], None)
        self.assertEqual(fin.relative_accel(0.1, None, 0.01)[0], None)

    def test_near_zero_base_is_not_comparable(self):
        value, note = fin.relative_accel(0.1, 0.0, 0.01)
        self.assertIsNone(value)
        self.assertIn("接近 0", note)


class FinMetricsTest(unittest.TestCase):
    END = "2026-10-23"

    def test_empty_rows_returns_none(self):
        self.assertIsNone(fin.fin_metrics([], self.END))

    def test_report_published_after_baseline_is_ignored(self):
        rows = fx.quarter_rows()
        rows.append(fx.fin_row("2026-09-30", "2026-10-28", 99, 999, 999, 99))
        fm = fin.fin_metrics(rows, self.END)
        self.assertEqual(fm["latest_end"], "2026-06-30")
        self.assertEqual(fm["latest_yoy"], 20.0)

    def test_latest_quarter_metrics(self):
        fm = fin.fin_metrics(fx.quarter_rows(), self.END)
        self.assertEqual(fm["latest_end"], "2026-06-30")
        self.assertEqual(fm["latest_pub"], "2026-08-15")
        self.assertEqual(fm["prev_end"], "2026-03-31")
        self.assertEqual(fm["prev2_profit_q"], 100.0)
        # 同比加速度 = (20 - 10) / 10 = 1.0
        self.assertAlmostEqual(fm["accel"], 1.0)
        self.assertEqual(fm["accel_note"], "")
        # 环比 = 100/80-1 = 0.25；上季环比 = 80/100-1 = -0.2
        self.assertAlmostEqual(fm["qoq"], 0.25)
        self.assertAlmostEqual(fm["qoq_prev"], -0.2)
        self.assertAlmostEqual(fm["qoq_accel"], 2.25)
        # 营收环比 = 900/800-1
        self.assertAlmostEqual(fm["revenue_qoq"], 0.125)
        self.assertEqual(fm["yoy_base_end"], "2025-06-30")
        self.assertEqual(fm["yoy_base_profit_q"], 80.0)
        self.assertEqual(fm["prev_yoy_base_end"], "2025-03-31")
        self.assertEqual(fm["stale_days"], 69)
        self.assertEqual(fm["n_reports"], 6)
        self.assertLessEqual(len(fm["history"]), 6)

    def test_missing_previous_quarter_makes_accel_not_comparable(self):
        rows = [
            fx.fin_row("2025-06-30", "2025-08-20", 12, 80, 800, 9),
            fx.fin_row("2026-06-30", "2026-08-15", 20, 100, 900, 13),
        ]
        fm = fin.fin_metrics(rows, self.END)
        self.assertIsNone(fm["accel"])
        self.assertIn("缺少上一期", fm["accel_note"])
        self.assertIsNone(fm["qoq"])
        self.assertIn("缺少上一季度", fm["qoq_note"])

    def test_negative_previous_profit_blocks_qoq(self):
        rows = fx.quarter_rows()
        rows[-2]["NPParentCompanyOwners_Q"] = "-8000000000"
        fm = fin.fin_metrics(rows, self.END)
        self.assertIsNone(fm["qoq"])
        self.assertIn("≤0", fm["qoq_note"])

    def test_restatement_keeps_latest_publication(self):
        rows = [r for r in fx.quarter_rows() if r["EndDate"].startswith("2026-06-30")]
        rows.append(fx.fin_row("2026-06-30", "2026-09-20", 25, 110, 950, 14))  # 更正公告
        fm = fin.fin_metrics(rows, self.END)
        self.assertEqual(fm["latest_yoy"], 25.0)
        self.assertEqual(fm["n_reports"], 1)

    def test_zero_revenue_is_kept_not_skipped(self):
        rows = [
            fx.fin_row("2026-03-31", "2026-04-25", 10, 80, 800, 12),
            fx.fin_row("2026-06-30", "2026-08-15", 20, 100, 0, 13),
        ]
        rows[-1]["OperatingRevenue"] = "12345"      # 累计口径的诱饵
        fm = fin.fin_metrics(rows, self.END)
        self.assertEqual(fm["latest_revenue"], 0.0)

    def test_profit_falls_back_to_cumulative_when_q_missing(self):
        rows = fx.quarter_rows()
        rows[-1]["NPParentCompanyOwners_Q"] = None
        fm = fin.fin_metrics(rows, self.END)
        self.assertEqual(fm["profit"], 200.0)       # fixture 里累计值 = 单季 ×2
        self.assertIsNone(fm["qoq"])

    def test_rows_without_yoy_are_skipped(self):
        rows = fx.quarter_rows()
        rows[-1]["NPParentCompanyYOY_Q"] = None
        fm = fin.fin_metrics(rows, self.END)
        self.assertEqual(fm["latest_end"], "2026-03-31")


if __name__ == "__main__":
    unittest.main()
