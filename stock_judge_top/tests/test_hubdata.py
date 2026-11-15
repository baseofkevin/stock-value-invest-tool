#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据层映射测试：hub kline 行 → judge node（qfq 价格 / raw 量 / 折算换手）。

迁移新增（2026-11-15）。`_nodes` 是纯函数，离线可测；`fetch` 的编排
（ensure / finance_full / dividend 前置）留给真跑冒烟（README §4）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from stock_judge_top import data as data_mod     # noqa: E402


def raw_row(date, close, high, volume, amount):
    return {"symbol": "sh600519", "date": date, "open": close, "high": high,
            "low": close, "close": close, "volume": volume, "amount": amount,
            "src": "tencent"}


def qfq_row(date, close, high):
    return {"date": date, "open": close, "high": high, "low": close, "close": close}


class NodesTest(unittest.TestCase):
    def test_price_takes_qfq_volume_takes_raw(self):
        raw = raw_row("2026-11-13", 10.0, 10.5, 1_000_000, 10_000_000)
        adj = qfq_row("2026-11-13", 5.0, 5.25)          # qfq 减半
        n = data_mod._nodes([raw], [adj], 1_000_000_000)[0]
        self.assertEqual(n["last"], 5.0)                 # 价格用 qfq
        self.assertEqual(n["high"], 5.25)
        self.assertEqual(n["volume"], 1_000_000)         # 量用 raw
        self.assertAlmostEqual(n["exchange"], 1.0)      # 1e7 / 1e9 × 100

    def test_exchange_rounds_to_4_decimals(self):
        raw = raw_row("2026-11-13", 10.0, 10.5, 1, 123_456.7)
        n = data_mod._nodes([raw], [qfq_row("2026-11-13", 10.0, 10.5)],
                            987_654_321)[0]
        self.assertEqual(n["exchange"], round(123_456.7 / 987_654_321 * 100, 4))

    def test_amount_missing_falls_back_to_close_times_volume(self):
        raw = raw_row("2026-11-13", 10.0, 10.5, 200_000, None)
        # 兜底用 raw close（amount 与 raw 价同口径），不是 qfq
        n = data_mod._nodes([raw], [qfq_row("2026-11-13", 5.0, 5.25)],
                            2_000_000)[0]
        self.assertAlmostEqual(n["exchange"], 10.0 * 200_000 / 2_000_000 * 100)

    def test_no_cap_means_no_exchange(self):
        raw = raw_row("2026-11-13", 10.0, 10.5, 1, 100.0)
        n = data_mod._nodes([raw], [qfq_row("2026-11-13", 10.0, 10.5)], None)[0]
        self.assertIsNone(n["exchange"])
        self.assertEqual(n["amount"], 100.0)             # amount 保留原值

    def test_zero_amount_also_falls_back(self):
        # amount=0 视为缺失（与 turnover.turnover20 的口径一致：a>0 才算）
        raw = raw_row("2026-11-13", 10.0, 10.5, 100_000, 0)
        n = data_mod._nodes([raw], [qfq_row("2026-11-13", 10.0, 10.5)],
                            1_000_000)[0]
        self.assertAlmostEqual(n["exchange"], 10.0 * 100_000 / 1_000_000 * 100)

    def test_date_is_string(self):
        raw = {"date": "2026-11-13", "close": 1, "high": 1, "volume": 1,
               "amount": 1}
        n = data_mod._nodes([raw], [qfq_row("2026-11-13", 1, 1)], 1)[0]
        self.assertEqual(n["date"], "2026-11-13")

    def test_rows_keep_order(self):
        rows = [raw_row(f"2026-11-{d:02d}", 1.0, 1.0, 1, 1) for d in (1, 2, 3)]
        adjs = [qfq_row(f"2026-11-{d:02d}", 1.0, 1.0) for d in (1, 2, 3)]
        out = data_mod._nodes(rows, adjs, 100)
        self.assertEqual([n["date"] for n in out],
                         ["2026-11-01", "2026-11-02", "2026-11-03"])


class ConstantsTest(unittest.TestCase):
    def test_kline_depth_matches_original(self):
        # 原版：3 段×365 天 ≈ 750 根；根数门槛 30（_kline_error 的下限）
        self.assertEqual(data_mod.KLINE_BARS, 750)
        self.assertEqual(data_mod.KLINE_MIN_BARS, 30)

    def test_finance_periods_matches_original(self):
        self.assertEqual(data_mod.FIN_PERIODS, 60)       # 原版 FIN_NUM=60


if __name__ == "__main__":
    unittest.main()
