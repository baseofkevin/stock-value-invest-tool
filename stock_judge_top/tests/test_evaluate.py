#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评估编排：K 线不可用的四条报错、警告文案、结果骨架不含任何「总分」。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from stock_judge_top import evaluate as ev     # noqa: E402
from stock_judge_top import log as log_mod     # noqa: E402
from stock_judge_top.tests import fixtures as fx     # noqa: E402

END = "2026-10-23"


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        log_mod.configure(quiet=True)
        self._orig = ev.fetch
        self.nodes = fx.kline(250)
        self.rows = fx.quarter_rows()
        self.prof = {}
        # 迁移后签名：fetch(hub, code, end, refresh) → (nodes, rows, profile)
        ev.fetch = lambda hub, code, end, refresh=False: (self.nodes, self.rows, self.prof)

    def tearDown(self):
        ev.fetch = self._orig

    def test_no_kline_and_no_profile(self):
        self.nodes = []
        with self.assertRaises(RuntimeError) as ctx:
            ev.evaluate(None, "sh600519", END, False)
        self.assertIn("无行情数据", str(ctx.exception))

    def test_no_kline_but_profile_exists_means_suspended(self):
        self.nodes = []
        self.prof = {"name": "贵州茅台"}
        with self.assertRaises(RuntimeError) as ctx:
            ev.evaluate(None, "sh600519", END, False)
        self.assertIn("停牌", str(ctx.exception))

    def test_too_few_bars(self):
        self.nodes = fx.kline(20)
        with self.assertRaises(RuntimeError) as ctx:
            ev.evaluate(None, "sh600519", END, False)
        self.assertIn("至少需要 30 根", str(ctx.exception))

    def test_missing_high_field(self):
        self.nodes = fx.kline(250)
        self.nodes[3]["high"] = None
        with self.assertRaises(RuntimeError) as ctx:
            ev.evaluate(None, "sh600519", END, False)
        self.assertIn("high 字段", str(ctx.exception))

    def test_missing_close_field(self):
        self.nodes = fx.kline(250)
        self.nodes[3]["last"] = "n/a"
        with self.assertRaises(RuntimeError) as ctx:
            ev.evaluate(None, "sh600519", END, False)
        self.assertIn("last", str(ctx.exception))

    def test_result_skeleton(self):
        self.prof = {"name": "贵州茅台"}
        r = ev.evaluate(None, "sh600519", END, False)
        self.assertEqual(r["code"], "sh600519")
        self.assertEqual(r["name"], "贵州茅台")
        self.assertEqual(r["date"], END)
        self.assertEqual(r["pos_basis"], "high")
        self.assertIn("phase", r)
        self.assertEqual(len(r["views"]), 4)
        self.assertEqual([v["key"] for v in r["views"]],
                         ["momentum", "turnover", "earnings", "volume"])
        # 四因子独立观察：结果里不允许出现任何「合并总分」字段
        for banned in ("score", "total", "total_score", "verdict", "summary", "position"):
            self.assertNotIn(banned, r)
        # 旧版把 vol_stall/vol_up 塞进 result["volume"] 却没人消费，已删除
        self.assertNotIn("volume", r)

    def test_earnings_block_is_filled(self):
        r = ev.evaluate(None, "sh600519", END, False)
        self.assertEqual(r["earnings"]["state"], "强加速")
        self.assertAlmostEqual(r["finance"]["accel"], 1.0)

    def test_warning_for_short_kline(self):
        self.nodes = fx.kline(120)
        r = ev.evaluate(None, "sh600519", END, False)
        self.assertTrue(any("不足 250 日" in w for w in r["warnings"]))

    def test_warning_when_finance_missing(self):
        self.rows = []
        r = ev.evaluate(None, "sh600519", END, False)
        self.assertIsNone(r["finance"])
        self.assertTrue(any("无可用财务数据" in w for w in r["warnings"]))
        self.assertEqual(r["earnings"]["state"], "数据不足")

    def test_warning_when_accel_not_comparable(self):
        self.rows = [
            fx.fin_row("2025-06-30", "2025-08-20", 12, 80, 800, 9),
            fx.fin_row("2026-06-30", "2026-08-15", 20, 100, 900, 13),
        ]
        r = ev.evaluate(None, "sh600519", END, False)
        self.assertTrue(any("增速不连续" in w for w in r["warnings"]))

    def test_warning_when_report_is_stale(self):
        self.rows = fx.quarter_rows()
        for row in self.rows:
            row["InfoPublDate"] = row["InfoPublDate"].replace("2026-08-15", "2025-12-01")
        r = ev.evaluate(None, "sh600519", END, False)
        self.assertTrue(any("财报已" in w for w in r["warnings"]))

    def test_warning_when_below_ma20(self):
        self.nodes = fx.kline(250)
        self.nodes[-1]["last"] = 1.0
        self.nodes[-1]["high"] = 1.5
        r = ev.evaluate(None, "sh600519", END, False)
        self.assertTrue(any("跌破 MA20" in w for w in r["warnings"]))

    def test_without_quote(self):
        r = ev.evaluate(None, "sh600519", END, False, with_quote=False)
        self.assertEqual(r["name"], "")
        self.assertEqual(r["quote"], {})


if __name__ == "__main__":
    unittest.main()
