#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据层映射测试：hub kline 行 → judge node（raw 价格 / raw 量 / 折算换手）。

迁移新增（2026-11-15）。`_nodes` 是纯函数，离线可测；`fetch` 的编排
（ensure / finance_full）留给真跑冒烟（README §4）。

2026-11-15 起价格改为**不复权**，与 stock_kline_screen 统一口径：换来
「两个工具不会互相矛盾」，代价是除权日附近的历史高点带缺口（见 data.py 模块头）。
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


class NodesTest(unittest.TestCase):
    def test_price_is_raw_not_qfq(self):
        """价格一律 raw：同一只票不会出现「选股说站上 MA20、这里说跌破」。"""
        raw = raw_row("2026-11-13", 10.0, 10.5, 1_000_000, 10_000_000)
        n = data_mod._nodes([raw], 1_000_000_000)[0]
        self.assertEqual(n["last"], 10.0)                # 原样，不复权
        self.assertEqual(n["high"], 10.5)
        self.assertEqual(n["open"], 10.0)
        self.assertEqual(n["volume"], 1_000_000)
        self.assertAlmostEqual(n["exchange"], 1.0)      # 1e7 / 1e9 × 100

    def test_exchange_rounds_to_4_decimals(self):
        raw = raw_row("2026-11-13", 10.0, 10.5, 1, 123_456.7)
        n = data_mod._nodes([raw], 987_654_321)[0]
        self.assertEqual(n["exchange"], round(123_456.7 / 987_654_321 * 100, 4))

    def test_amount_missing_falls_back_to_close_times_volume(self):
        raw = raw_row("2026-11-13", 10.0, 10.5, 200_000, None)
        # amount 缺失时用 close × volume 兜底（两者都是 raw 口径）
        n = data_mod._nodes([raw], 2_000_000)[0]
        self.assertAlmostEqual(n["exchange"], 10.0 * 200_000 / 2_000_000 * 100)

    def test_no_cap_means_no_exchange(self):
        raw = raw_row("2026-11-13", 10.0, 10.5, 1, 100.0)
        n = data_mod._nodes([raw], None)[0]
        self.assertIsNone(n["exchange"])
        self.assertEqual(n["amount"], 100.0)             # amount 保留原值

    def test_zero_amount_also_falls_back(self):
        # amount=0 视为缺失（与 turnover.turnover20 的口径一致：a>0 才算）
        raw = raw_row("2026-11-13", 10.0, 10.5, 100_000, 0)
        n = data_mod._nodes([raw], 1_000_000)[0]
        self.assertAlmostEqual(n["exchange"], 10.0 * 100_000 / 1_000_000 * 100)

    def test_date_is_string(self):
        raw = {"date": "2026-11-13", "close": 1, "high": 1, "volume": 1,
               "amount": 1}
        n = data_mod._nodes([raw], 1)[0]
        self.assertEqual(n["date"], "2026-11-13")

    def test_rows_keep_order(self):
        rows = [raw_row(f"2026-11-{d:02d}", 1.0, 1.0, 1, 1) for d in (1, 2, 3)]
        out = data_mod._nodes(rows, 100)
        self.assertEqual([n["date"] for n in out],
                         ["2026-11-01", "2026-11-02", "2026-11-03"])

    def test_exchange_above_sane_max_is_nulled(self):
        """换手率超上限（市值/成交额单位 bug）时置空，退回成交额分位。

        复现 2026-11-17 起的单位事故：circ_mv 被当元存（实际是亿元），
        分母小 1e8 倍，换手率放大到几千万%。护栏把它拦在源头。
        """
        raw = raw_row("2026-11-16", 10.0, 10.5, 1_000_000, 30_000_000_0)
        n = data_mod._nodes([raw], 3642)[0]             # 3642 元 = 被写错的亿元值
        self.assertIsNone(n["exchange"])                 # 不落脏值
        self.assertEqual(n["amount"], 30_000_000_0)      # amount 原样保留

    def test_exchange_at_boundary_kept(self):
        """恰好 100% 不触发（用严格大于判断），正常数据不受误伤。"""
        raw = raw_row("2026-11-16", 10.0, 10.5, 1, 100.0)
        n = data_mod._nodes([raw], 100.0)[0]             # 100/100×100 = 100%
        self.assertEqual(n["exchange"], 100.0)

    def test_sane_max_constant(self):
        self.assertEqual(data_mod.TURNOVER_SANE_MAX, 100.0)


class ConstantsTest(unittest.TestCase):
    def test_kline_depth_matches_original(self):
        # 原版：3 段×365 天 ≈ 750 根；根数门槛 30（_kline_error 的下限）
        self.assertEqual(data_mod.KLINE_BARS, 750)
        self.assertEqual(data_mod.KLINE_MIN_BARS, 30)

    def test_finance_periods_matches_original(self):
        self.assertEqual(data_mod.FIN_PERIODS, 60)       # 原版 FIN_NUM=60


if __name__ == "__main__":
    unittest.main()
