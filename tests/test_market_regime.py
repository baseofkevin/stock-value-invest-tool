# -*- coding: utf-8 -*-
"""`market_regime.rules` 的边界，不联网、不碰库。

只钉三件事：

1. **判定是纯函数**：同样的输入永远同样的输出，阈值边上不抖；
2. **缺数据不等于崩**：某一路为空时返回 None，由调用方显示"—"；
3. **不合成**：`judge()` 只把三路装进一个 dict，谁都不参与谁的判断。
"""
from __future__ import annotations

import datetime as dt
import unittest

from market_regime import rules


def _weeks(n: int, closes: list[float]) -> list[dict]:
    """把收盘价序列包成周K（日期取连续周五，只为排序用）。"""
    start = dt.date(2020, 1, 3)
    return [{"date": (start + dt.timedelta(weeks=i)).isoformat(), "close": c}
            for i, c in enumerate(closes)]


def _daily(n: int, values: list[float], key: str) -> list[dict]:
    start = dt.date(2020, 1, 1)
    return [{"date": (start + dt.timedelta(days=i)).isoformat(), key: v}
            for i, v in enumerate(values)]


class TrendTest(unittest.TestCase):
    def test_downtrend_is_confirmed(self):
        # 100 周单边阴跌：价格在 MA20 下、MA20 在 MA60 下、MA60 还在走低
        closes = [5000 - 20 * i for i in range(100)]
        out = rules.trend(_weeks(100, closes))
        self.assertEqual(out["state"], "空头确认")
        self.assertEqual(out["score"], 2)
        self.assertGreater(out["weeks_below_ma20"], 5)

    def test_uptrend_is_not_bearish(self):
        closes = [3000 + 15 * i for i in range(100)]
        out = rules.trend(_weeks(100, closes))
        self.assertEqual(out["state"], "偏强")
        self.assertLessEqual(out["score"], 1)

    def test_short_history_returns_none(self):
        """不足 64 周（MA60 + 4 周斜率）时宁可没有，也不拿半截数据硬算。"""
        self.assertIsNone(rules.trend(_weeks(40, [4000.0] * 40)))

    def test_single_week_break_is_not_enough(self):
        """只跌一周不算"转弱"——连续两周才作数，这是原文最含糊的那条。"""
        closes = [4000.0] * 80
        closes[-1] = 3000.0          # 最后一周插针
        out = rules.trend(_weeks(80, closes))
        self.assertNotEqual(out["state"], "空头确认")


class ValuationTest(unittest.TestCase):
    def test_extreme_percentile(self):
        pe = [10.0] * 300
        bonds = [2.0] * 299 + [0.0]        # 最后一天国债归零 → ERP 跳到最高
        out = rules.valuation(_daily(300, pe, "pe_ttm"), _daily(300, bonds, "value"))
        self.assertGreaterEqual(out["pct"], rules.ERP_EXTREME)
        self.assertEqual(out["state"], "极值")
        self.assertAlmostEqual(out["erp"], 10.0, places=2)

    def test_empty_input(self):
        self.assertIsNone(rules.valuation([], []))


class SentimentTest(unittest.TestCase):
    def test_panic_when_record_high(self):
        below = _daily(500, [4.0] * 499 + [12.0], "value")
        out = rules.sentiment(below, [])
        self.assertEqual(out["state"], "恐慌")
        self.assertGreaterEqual(out["below_na_pct"], rules.NA_PANIC)

    def test_volume_dry_false_without_weeks(self):
        out = rules.sentiment(_daily(200, [5.0] * 200, "value"), [])
        self.assertIsNone(out["volume_dry"])


class JudgeTest(unittest.TestCase):
    def test_missing_sources_do_not_raise(self):
        out = rules.judge(weeks=[], perf=[], bonds=[], below=[])
        self.assertIsNone(out["trend"])
        self.assertIsNone(out["valuation"])
        self.assertIsNone(out["sentiment"])
        self.assertIsNone(out["data_asof"])

    def test_no_combined_score(self):
        """三路各自独立：没有总分、没有互相修正。"""
        out = rules.judge(
            weeks=_weeks(100, [5000 - 20 * i for i in range(100)]),
            perf=_daily(300, [10.0] * 300, "pe_ttm"),
            bonds=_daily(300, [2.0] * 300, "value"),
            below=_daily(300, [4.0] * 300, "value"))
        self.assertEqual(set(out), {"trend", "valuation", "sentiment",
                                    "data_asof", "summary"})
        self.assertEqual(out["trend"]["state"], "空头确认")
        # 趋势空头不影响估值那一路：常数序列的分位是 0（没有比它更低的），
        # 所以这里是"偏贵"，而不是被趋势拉成"空头所以看空"。
        self.assertEqual(out["valuation"]["pct"], 0)
        self.assertEqual(out["valuation"]["state"], "偏贵")


if __name__ == "__main__":
    unittest.main()
