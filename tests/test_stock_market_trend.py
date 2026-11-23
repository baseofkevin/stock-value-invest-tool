# -*- coding: utf-8 -*-
"""`stock_market_trend.rules` 的边界，不联网、不碰库。

只钉三件事：

1. **判定是纯函数**：同样的输入永远同样的输出，阈值边上不抖；
2. **缺数据不等于崩**：某一路为空时返回 None，由调用方显示"—"；
3. **不合成**：`judge()` 只把三路装进一个 dict，谁都不参与谁的判断。
"""
from __future__ import annotations

import datetime as dt
import unittest

from stock_market_trend import rules


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


class VolumeStateTest(unittest.TestCase):
    """量能五档：地量 / 缩量 / 平稳 / 放量 / 天量。

    口径是**一个水位派生四条线**：水位 M = 近 52 周中位数，缩量线 0.9M、
    放量线 1.1M、天量线 1.5M，只有地量线（1%）是绝对值。下面用一条**恒定 2%**
    的周线做基准，M 一定是 2.00 → 三条线一定是 1.8 / 2.2 / 3.0，不依赖随机性。
    """

    BASE = [2.0] * 100                # 100 周恒为 2%，中位数锁死在 2.00
    LOW = 1.8                         # 2.00 × 0.9
    HIGH = 2.2                        # 2.00 × 1.1
    HEAVY = 3.0                       # 2.00 × 1.5

    def _weeks(self, last: float, base=None) -> list[dict]:
        start = dt.date(2020, 1, 3)
        vals = list(base or self.BASE) + [float(last)]
        return [{"date": (start + dt.timedelta(weeks=i)).isoformat(), "turnover": v}
                for i, v in enumerate(vals)]

    def _out(self, last: float, base=None) -> dict:
        return rules.sentiment(_daily(300, [4.0] * 300, "value"),
                               self._weeks(last, base))

    def test_lines_are_all_derived_from_one_median(self):
        """四条线同源：中位 ×1.5 / ×1.1 / ×0.9，取整方向决定压线那周算哪档。"""
        self.assertEqual(rules.volume_lines([2.0] * 60),
                         {"median": 2.0, "low": 1.8, "high": 2.2, "heavy": 3.0})
        # 2.10 × 1.5 = 3.15 → 天量线向上取整到 3.5（四舍五入会得 3.0）
        self.assertEqual(rules.heavy_line([2.1] * 60), 3.5)
        # 市场整体抬上去，四条线一起抬
        self.assertEqual(rules.volume_lines([4.0] * 60),
                         {"median": 4.0, "low": 3.6, "high": 4.4, "heavy": 6.0})

    def test_five_buckets(self):
        cases = [(0.5, "地量"), (1.5, "缩量"), (2.0, "平稳"),
                 (2.5, "放量"), (3.0, "天量"), (5.0, "天量")]
        for last, want in cases:
            with self.subTest(last=last):
                self.assertEqual(self._out(last)["volume_state"], want)

    def test_boundary_belongs_to_the_line(self):
        """界值闭区间：正好 1% 是地量、1.8% 起是平稳、2.2% 起是放量、3% 起是天量。"""
        cases = [(1.0, "地量"), (1.01, "缩量"), (1.79, "缩量"), (1.8, "平稳"),
                 (2.19, "平稳"), (2.2, "放量"), (2.99, "放量"), (3.0, "天量")]
        for last, want in cases:
            with self.subTest(last=last):
                self.assertEqual(self._out(last)["volume_state"], want)

    def test_line_follows_the_market_level(self):
        """市场整体翻倍，同一根 3% 的周线从天量一路退回缩量——

        这正是改成相对口径要买到的东西：4% 写死的时候，市场水位一变它就失真。
        """
        self.assertEqual(self._out(3.0)["volume_state"], "天量")
        self.assertEqual(self._out(3.0, base=[4.0] * 100)["volume_state"], "缩量")

    def test_cool_band_collapses_into_floor(self):
        """市场蹲到地板上时，「缩量」这一档没有空间——线被 1% 兜住，不是漏判。

        M=1.1 → 缩量线 0.9M=0.99 低于地量线，取 max 后贴到 1.0%；
        任何 t>1% 都已经 ≥ 缩量线，所以缩量永不触发。
        """
        out = self._out(1.05, base=[1.1] * 100)
        self.assertEqual(out["low_line"], rules.TURNOVER_DRY)
        self.assertEqual(out["volume_state"], "平稳")
        self.assertNotEqual(self._out(0.9, base=[1.1] * 100)["volume_state"], "缩量")

    def test_dry_flag_agrees_with_state(self):
        """`volume_dry` 是给老页面留的布尔，不能和分档说法打架。"""
        for last in (0.5, 1.5, 2.5, 5.0):
            with self.subTest(last=last):
                out = self._out(last)
                self.assertEqual(out["volume_dry"], out["volume_state"] == "地量")

    def test_percentile_is_reference_only(self):
        """分位照样给，但不参与分档：它是"排第几"，不是"量够不够"。"""
        out = self._out(3.0)
        self.assertEqual(out["volume_state"], "天量")
        self.assertIsInstance(out["turnover_pct"], int)
        self.assertEqual(out["dry_line"], rules.TURNOVER_DRY)
        self.assertEqual(out["low_line"], self.LOW)
        self.assertEqual(out["high_line"], self.HIGH)
        self.assertEqual(out["heavy_line"], self.HEAVY)
        self.assertEqual(out["median52"], 2.0)

    def test_short_window_has_no_lines(self):
        """攒不满半年（26 周）就不给线——宁可不说，也不拿半个窗口硬算。"""
        self.assertIsNone(rules.volume_lines([2.0] * 20))
        self.assertIsNone(rules.heavy_line([2.0] * 20))
        self.assertEqual(rules.volume_lines([2.0] * 26)["heavy"], 3.0)

    def test_no_weeks_means_no_state(self):
        out = rules.sentiment(_daily(300, [4.0] * 300, "value"), [])
        self.assertIsNone(out["volume_state"])
        self.assertIsNone(out["volume_dry"])
        self.assertIsNone(out["low_line"])


class PartialWeekTest(unittest.TestCase):
    """周中跑出来的最后一根周线是「本周至今」，量能不能按它判。

    2026-11-23（周一）那次就是活例子：换手 0.61% = 一个交易日，照三档判会得出
    「地量」，其实上一完整周 11-20 是 2.70%（平稳）。
    """

    def _weeks(self, last_date: str, last_val: float = 0.5) -> list[dict]:
        start = dt.date.fromisoformat(last_date) - dt.timedelta(weeks=100)
        vals = [2.0] * 100 + [last_val]
        return [{"date": (start + dt.timedelta(weeks=i)).isoformat(), "turnover": v}
                for i, v in enumerate(vals)]

    def _out(self, last_date: str, today: str, last_val: float = 0.5) -> dict:
        return rules.sentiment(_daily(300, [4.0] * 300, "value"),
                               self._weeks(last_date, last_val), today=today)

    def test_midweek_bar_is_dropped(self):
        """周一跑：那根只有一天的量，摘掉后用上一完整周判 → 平稳，不是地量。"""
        out = self._out("2026-11-23", today="2026-11-23")
        self.assertEqual(out["volume_state"], "平稳")
        self.assertEqual(out["turnover"], 2.0)
        self.assertEqual(out["partial_week"]["date"], "2026-11-23")
        self.assertEqual(out["partial_week"]["turnover"], 0.5)
        # 档位实际是按这一周判的
        self.assertEqual(out["partial_week"]["prev_date"], "2026-11-16")

    def test_friday_bar_counts(self):
        """周五盘后跑：同一根就是完整周，该判地量就判地量。"""
        out = self._out("2026-11-27", today="2026-11-27")
        self.assertEqual(out["volume_state"], "地量")
        self.assertIsNone(out["partial_week"])

    def test_weekend_bar_counts(self):
        """周末复盘：本周已经结束，最后一根照样是完整周。"""
        out = self._out("2026-11-27", today="2026-11-29")
        self.assertEqual(out["volume_state"], "地量")

    def test_backfill_after_week_ends(self):
        """跨周回填：那根早就走完了，不该再被当成"本周至今"。"""
        out = self._out("2026-11-23", today="2026-12-01")
        self.assertEqual(out["volume_state"], "地量")
        self.assertIsNone(out["partial_week"])

    def test_future_bar_is_not_partial(self):
        """bar 比 today 还晚（数据超前）时不认作"本周至今"，免得把未来的数当现在的。"""
        out = self._out("2026-11-27", today="2026-11-23")
        self.assertIsNone(out["partial_week"])


class AsOfTest(unittest.TestCase):
    """基准日截断：`as_of` 之后的数据不许参与判定。"""

    def test_trend_as_of_sees_only_past_weeks(self):
        # 前 100 周上涨，后 20 周暴跌。按第 100 周回填，不该看得见那 20 周。
        closes = [3000 + 15 * i for i in range(100)] + [4400 - 60 * i for i in range(20)]
        weeks = _weeks(120, closes)
        cut = weeks[99]["date"]
        back = rules.trend(weeks, as_of=cut)
        self.assertEqual(back["date"], cut)
        self.assertNotEqual(back["state"], "空头确认")
        self.assertNotEqual(rules.trend(weeks)["state"], back["state"])

    def test_valuation_as_of_uses_that_day(self):
        perf = _daily(300, [10.0 + i / 100 for i in range(300)], "pe_ttm")
        bonds = _daily(300, [2.0] * 300, "value")
        cut = perf[150]["date"]
        self.assertEqual(rules.valuation(perf, bonds, as_of=cut)["date"], cut)

    def test_as_of_before_first_row_returns_none(self):
        """基准日早于所有原件时宁可算不出，也不悄悄退化成"取最新"。"""
        self.assertIsNone(rules.trend(_weeks(80, [4000.0] * 80), as_of="1990-01-01"))
        out = rules.judge(weeks=[], perf=[], bonds=[], below=[], as_of="2026-06-22")
        self.assertIsNone(out["data_asof"])


if __name__ == "__main__":
    unittest.main()
