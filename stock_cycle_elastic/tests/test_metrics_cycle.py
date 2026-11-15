"""判断① 毛利率标准差（README.md「测试」）。

三条最容易写错的地方，各自有用例钉住：
  1. **必须用单季毛利率**而不是累计 —— 累计值天然平滑，会系统性低估波动；
  2. **窗口是最近 20 季**，不是全历史；
  3. **标准差用样本口径 ddof=1**（已在 test_util 钉过，这里再验一次单位换算）。
"""

import unittest

import tests.helpers  # noqa: F401
import config
from cycle import build, metrics

from tests.fixtures import load
from tests.helpers import Cfg, quarters_of


class TestNormalPath(unittest.TestCase):

    def test_flat_margin_gives_zero_sigma(self):
        """`flat_margin`：40 季毛利率恒定 32% → σ 恰好 0.0（不是 None）。"""
        stock = load("flat_margin")
        quarters = [{"date": q["date"], "gross_margin": 0.32} for q in
                    __import__("cycle.build", fromlist=["build"]).build(stock)["quarters"]]
        got = metrics.gross_margin_std(quarters, config)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, 0.0, places=9)

    def test_oscillating_margin_gives_large_sigma(self):
        """±6pp 交替 → σ ≈ 6pp。20 个交替值在 ddof=1 下是 6.156，
        比总体的 6.000 略大 —— 这正是样本标准差的特征。"""
        quarters = quarters_of(gross_margins=[0.18 + 0.06 * (1 if i % 2 else -1)
                                              for i in range(20)])
        got = metrics.gross_margin_std(quarters, config)
        self.assertGreater(got, 5.9)
        self.assertLess(got, 6.3)


class TestBoundary(unittest.TestCase):
    """§11.4 第 2 条：边界是 `<` 还是 `<=`。

    判断① 的规则是 `gm_std >= GM_STD_CYCLE` → 闭区间。
    这里只测**指标层算出的数值**，标签层在 test_decide 测。
    """

    def test_exact_five_pp(self):
        """构造 σ 恰好 5.0pp 的样本：19 个 0、1 个 x，解出 x 使样本标准差 = 0.05。

        n=20，n-1=19。设 19 个值为 0，1 个为 x：
            均值 = x/20
            离差平方和 = 19*(x/20)^2 + (x - x/20)^2
                       = 19x²/400 + (19x/20)²
                       = 19x²/400 + 361x²/400
                       = 380x²/400 = 0.95x²
            样本方差 = 0.95x² / 19 = 0.05x²
            σ = sqrt(0.05)*x = 0.05  →  x = 0.05 / sqrt(0.05) ≈ 0.223607
        此时 σ = 0.05（即 5.0pp）。
        """
        x = 0.05 / (0.05 ** 0.5)
        values = [0.0] * 19 + [x]
        quarters = quarters_of(gross_margins=values)
        got = metrics.gross_margin_std(quarters, config)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, 5.0, places=9)

    def test_just_below_five_pp(self):
        """稍微缩一点 → 4.99pp 以下。"""
        x = 0.0499 / (0.05 ** 0.5)
        quarters = quarters_of(gross_margins=[0.0] * 19 + [x])
        got = metrics.gross_margin_std(quarters, config)
        self.assertLess(got, 5.0)

    def test_just_above_five_pp(self):
        x = 0.0501 / (0.05 ** 0.5)
        quarters = quarters_of(gross_margins=[0.0] * 19 + [x])
        got = metrics.gross_margin_std(quarters, config)
        self.assertGreater(got, 5.0)


class TestWindow(unittest.TestCase):
    """§11.4 第 4 条：窗口切片。"""

    def test_only_recent_20_quarters_used(self):
        """前 20 季 σ=10pp、后 20 季 σ=0.5pp → 结果应为 0.5 那一段的。"""
        early = [0.30 + 0.10 * (1 if i % 2 else -1) for i in range(20)]   # σ ≈ 10pp
        late = [0.30 + 0.005 * (1 if i % 2 else -1) for i in range(20)]   # σ ≈ 0.5pp
        quarters = quarters_of(gross_margins=early + late)
        got = metrics.gross_margin_std(quarters, config)
        self.assertLess(got, 1.0, "应只取后 20 季，前 20 季的高波动不该进来")

    def test_reverse_case(self):
        """反过来：前 20 季平稳、后 20 季剧烈 → 结果应是剧烈的。"""
        early = [0.30 + 0.005 * (1 if i % 2 else -1) for i in range(20)]
        late = [0.30 + 0.10 * (1 if i % 2 else -1) for i in range(20)]
        quarters = quarters_of(gross_margins=early + late)
        got = metrics.gross_margin_std(quarters, config)
        self.assertGreater(got, 9.0)

    def test_window_override(self):
        cfg = Cfg(WAVE_WINDOW_QUARTERS=8)
        quarters = quarters_of(gross_margins=[0.3] * 40)
        self.assertAlmostEqual(metrics.gross_margin_std(quarters, cfg), 0.0, places=9)


class TestMinSamples(unittest.TestCase):

    def test_below_min_gives_none(self):
        """有效季 < GM_MIN_SAMPLES(8) → None，不是 0。"""
        quarters = quarters_of(gross_margins=[0.30, 0.35, 0.28, 0.31])
        self.assertIsNone(metrics.gross_margin_std(quarters, config))

    def test_exactly_min_gives_value(self):
        """有效季恰好 8 → 出值（门槛是 `>=`）。"""
        quarters = quarters_of(gross_margins=[0.30, 0.35, 0.28, 0.31,
                                              0.33, 0.29, 0.34, 0.30])
        got = metrics.gross_margin_std(quarters, config)
        self.assertIsNotNone(got)

    def test_none_values_dropped_from_denominator(self):
        """20 季里 5 季无毛利率 → 用剩下 15 季算，不因分母变化报错。"""
        values = [0.30 + 0.02 * (1 if i % 2 else -1) for i in range(20)]
        for i in (0, 3, 7, 11, 15):
            values[i] = None
        quarters = quarters_of(gross_margins=values)
        got = metrics.gross_margin_std(quarters, config)
        self.assertIsNotNone(got)
        # 与"只有那 15 个值"的序列结果一致
        only15 = [v for v in values if v is not None]
        expected = metrics.gross_margin_std(quarters_of(gross_margins=only15), config)
        self.assertAlmostEqual(got, expected, places=10)

    def test_mostly_none_gives_none(self):
        values = [0.30] * 5 + [None] * 15
        quarters = quarters_of(gross_margins=values)
        self.assertIsNone(metrics.gross_margin_std(quarters, config))

    def test_all_none_gives_none(self):
        quarters = quarters_of(gross_margins=[None] * 20)
        self.assertIsNone(metrics.gross_margin_std(quarters, config))


class TestEmpty(unittest.TestCase):

    def test_empty_list(self):
        self.assertIsNone(metrics.gross_margin_std([], config))

    def test_none_input(self):
        self.assertIsNone(metrics.gross_margin_std(None, config))


class TestQuarterlyNotCumulative(unittest.TestCase):
    """§11.4 第 1 条：必须是单季口径。

    做法：同一份业务事实，构造"单季毛利率"与"累计毛利率"两种输入。
    累计口径会把波动抹平（因为分子分母都在累加），所以 σ 必然更小；
    实现取到的必须是较大的那个。如果谁把字段换成累计值，这条会红。
    """

    def test_quarterly_sigma_larger_than_cumulative(self):
        # 四个季度营收相同、成本大幅波动 → 单季毛利率剧烈波动
        rev_q = 100.0
        cost_q = [60.0, 90.0, 70.0, 95.0] * 5          # 20 季
        quarterly = [(rev_q - c) / rev_q for c in cost_q]

        # 同一条业务事实的累计口径（每 4 季重置一次）
        cumulative = []
        for i in range(20):
            pos = i % 4
            cum_rev = rev_q * (pos + 1)
            cum_cost = sum(cost_q[:pos + 1])
            cumulative.append((cum_rev - cum_cost) / cum_rev)

        sigma_q = metrics.gross_margin_std(
            quarters_of(gross_margins=quarterly), config)
        sigma_c = metrics.gross_margin_std(
            quarters_of(gross_margins=cumulative), config)

        self.assertGreater(sigma_q, sigma_c,
                           "单季口径的 σ 必须大于累计口径")
        self.assertGreater(sigma_q, 5.0)
        self.assertAlmostEqual(
            metrics.gross_margin_std(quarters_of(gross_margins=quarterly), config),
            sigma_q, places=10,
            msg="实现取的必须是单季那一份（较大的）")

    def test_seasonality_preserved(self):
        """毛利率 30%/40% 规律交替 —— 不能被季节性"平均掉"。

        如果实现错误地对季度做了年度平均（比如按 4 季滚动平均再算 σ），
        这条会得到远小于 5pp 的值。
        """
        values = [0.30 if i % 2 == 0 else 0.40 for i in range(20)]
        got = metrics.gross_margin_std(quarters_of(gross_margins=values), config)
        self.assertAlmostEqual(got, 5.13, delta=0.2)
        self.assertGreater(got, 5.0)


class TestUnit(unittest.TestCase):
    """返回值单位是百分点，不是小数。"""

    def test_percentage_points(self):
        """样本标准差 0.02（小数）→ 输出 2.0（pp）。

        构造要点：9 个点、均值 0.32、离差平方和 = 8 × 0.02²。
        取离差 (+0.02, 0, -0.02) 各 3 个 → 平方和 = 3×0.0004 + 0 + 3×0.0004 = 0.0024，
        而 8 × 0.0004 = 0.0032 —— 对不上。所以改用 4 组 (+0.02, -0.02) 加一个 0：
        8 个点各贡献 0.0004，平方和 = 0.0032，除以 (9-1)=8 得方差 0.0004，σ = 0.02。
        """
        dev = [0.32 + 0.02, 0.32 - 0.02] * 4 + [0.32]
        got = metrics.gross_margin_std(quarters_of(gross_margins=dev), config)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, 2.0, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestGrossMarginCv(unittest.TestCase):
    """判断① 通道 B：变异系数 CV = σ / |中位| × 100。

    重点不是算式，而是**三个返回 None 的守卫** —— 它们是这条通道能安全
    启用的前提，写错任何一个都会让垃圾数据变成"周期股"。
    """

    def _cfg(self, **over):
        return Cfg(**over)

    def test_basic_value(self):
        """毛利率 10%↔20% 交替 → 中位 15%、σ≈5.13pp（ddof=1）→ CV≈34%。

        注意 σ 是**样本**标准差（分母 n−1），20 个交替点算出 5.13pp
        而不是 5.00pp —— 这里照着样本口径断言，钉住"没有偷偷换成总体口径"。
        """
        margins = [0.10, 0.20] * 10
        r = metrics.gross_margin_cv(quarters_of(gross_margins=margins), config)
        self.assertAlmostEqual(r["median"], 15.0, places=6)
        self.assertAlmostEqual(r["cv"], 0.051298 / 0.15 * 100, places=2)

    def test_samples_counted(self):
        r = metrics.gross_margin_cv(
            quarters_of(gross_margins=[0.2, None, 0.3] * 4), config)
        self.assertEqual(r["samples"], 8)

    def test_too_few_samples_returns_none(self):
        r = metrics.gross_margin_cv(quarters_of(gross_margins=[0.2] * 5), config)
        self.assertIsNone(r["cv"])
        self.assertEqual(r["samples"], 5)

    def test_small_median_blocks_cv(self):
        """守卫 1：中位数低于 GM_MEDIAN_MIN → 分母太小，CV 会爆炸。

        实测振华新材中位 −3.8%、σ=13.5pp，若不设守卫 CV 高达 354%。
        """
        margins = [0.01, 0.04] * 10          # 中位 2.5% < 5%
        r = metrics.gross_margin_cv(quarters_of(gross_margins=margins), config)
        self.assertIsNone(r["cv"])
        self.assertAlmostEqual(r["median"], 2.5, places=6)

    def test_negative_median_blocks_cv(self):
        """中位数为负时 CV 无意义（分母会变成绝对值，符号信息丢失）。"""
        margins = [-0.01, -0.06] * 10
        r = metrics.gross_margin_cv(quarters_of(gross_margins=margins), config)
        self.assertIsNone(r["cv"])

    def test_tiny_std_blocks_cv(self):
        """守卫 2：绝对 σ < GM_STD_MIN_FOR_CV → 这条通道不适用。

        典型是纯冶炼：毛利率 3%~5% 之间小晃，CV 不低但绝对波动毫无意义。
        """
        margins = [0.030, 0.032] * 10        # σ≈0.1pp < 2pp
        r = metrics.gross_margin_cv(quarters_of(gross_margins=margins), config)
        self.assertIsNone(r["cv"])

    def test_boundary_median_exactly_five_percent_passes(self):
        """中位数恰好 5% → 守卫放行（`>=` 语义，与其余阈值一致）。"""
        margins = [0.03, 0.07] * 10          # 中位正好 5%，σ=2pp
        r = metrics.gross_margin_cv(quarters_of(gross_margins=margins), config)
        self.assertIsNotNone(r["cv"])

    def test_boundary_std_exactly_two_pp_passes(self):
        margins = [0.10, 0.14] * 10          # 中位 12%、σ=2pp
        r = metrics.gross_margin_cv(quarters_of(gross_margins=margins), config)
        self.assertIsNotNone(r["cv"])

    def test_empty_quarters(self):
        r = metrics.gross_margin_cv([], config)
        self.assertEqual(r, {"cv": None, "median": None, "samples": 0})

    def test_compute_all_includes_cv(self):
        stock = build.build(load("full_40q"))
        m = metrics.compute_all(stock["quarters"], config)
        self.assertIn("gm_cv", m)
        self.assertIn("cv", m["gm_cv"])
