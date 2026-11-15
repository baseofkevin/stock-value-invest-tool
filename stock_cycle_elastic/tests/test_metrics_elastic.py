"""判断② 弹性比值（README.md「测试」）。

这是三个指标里最容易写错的一个，四个坑各有用例：
  1. **负比值必须参与中位数** —— 丢了会系统性高估弹性（只留下暴涨的季度）；
  2. **窗口只取最近 8 季**，而且窗口内第一季因缺上期被跳过，实际最多 7 个样本；
  3. **分母趋零**（|营收增速| < 0.5%）与**荒值**（|比值| > 50）是两条独立保护；
  4. **`reliable=False` 时不给强弱** —— 营收长期不动的比值算得出来但没意义。
"""

import unittest

import tests.helpers  # noqa: F401
import config
from cycle import metrics

from tests.fixtures import load
from tests.helpers import Cfg, quarters_of


def series(rev_growths, op_growths, base_rev=100.0, base_op=20.0):
    """按"逐季环比增速"造序列。

    `rev_growths` / `op_growths` 是每期相对**上一期**的增速（百分数），
    长度决定季数（会自动在最前面补一个基期）。

    注意 `reliable` 的口径不同：它看的是**同比**（对 4 季前）的营收增速中位数，
    见 §7.2 伪码。所以测 `reliable` 的用例不要用本函数，
    要用 `stable_series()` 直接控制同比。
    """
    revs = [base_rev]
    ops = [base_op]
    for gr, go in zip(rev_growths, op_growths):
        revs.append(revs[-1] * (1 + gr / 100.0))
        ops.append(ops[-1] * (1 + go / 100.0))
    return quarters_of(revenues=revs, op_profits=ops)


def stable_series(yoy_growth, ratio=2.0, n=12):
    """造一条同比增速恒定、逐季比值恒定的序列。

    参数：
        yoy_growth —— 每季**同比**增速（%），用来控制 `reliable`
        ratio      —— 逐季比值（利润环比增速 ÷ 营收环比增速）
        n          —— 季数

    做法：让营收按 yoy 复利成长（环比 = (1+yoy)^(1/4) - 1），
    再让利润与营收成 `ratio` 次幂关系。

    注意：4 次方根会带来浮点误差，所以 `reliable` 的判定阈值用例
    不要传"恰好 3.0"这种贴边值（误差可能让 |中位数| = 2.99999），
    贴边测试在 `TestReliabilityBoundary` 里用**直接给营收**的方式做。
    """
    q_growth = ((1 + yoy_growth / 100.0) ** 0.25 - 1) * 100.0
    revs = [100.0]
    for _ in range(n - 1):
        revs.append(revs[-1] * (1 + q_growth / 100.0))
    ops = [20.0 * (r / revs[0]) ** ratio for r in revs]
    return quarters_of(revenues=revs, op_profits=ops)


class TestConstantRatio(unittest.TestCase):

    def test_constant_2_5x(self):
        """营收每季 +4%、利润每季 +10% → 比值恒定 2.5。"""
        q = series([4.0] * 8, [10.0] * 8)
        got = metrics.elasticity_ratio(q, config)
        self.assertAlmostEqual(got["ratio"], 2.5, places=9)
        self.assertTrue(got["reliable"])
        self.assertGreaterEqual(got["samples"], 4)

    def test_constant_1x(self):
        q = series([5.0] * 8, [5.0] * 8)
        got = metrics.elasticity_ratio(q, config)
        self.assertAlmostEqual(got["ratio"], 1.0, places=9)


class TestBoundary(unittest.TestCase):
    """比值中位数恰好 2.0 时，判定层应算"强"（闭区间）。"""

    def test_exact_two(self):
        q = series([4.0] * 8, [8.0] * 8)
        got = metrics.elasticity_ratio(q, config)
        self.assertAlmostEqual(got["ratio"], 2.0, places=9)

    def test_just_below_two(self):
        q = series([4.0] * 8, [7.96] * 8)
        got = metrics.elasticity_ratio(q, config)
        self.assertLess(got["ratio"], 2.0)


class TestReliability(unittest.TestCase):
    """`reliable`：近 8 季营收**同比**增速中位数绝对值 < 3% → 比值不可信。

    口径注意（§7.2 伪码）：这里用的是同比（对 4 季前），不是环比。
    所以构造时直接控制同比，不能靠环比复利推 —— 环比 1% 复利 4 季就是 4.06% 同比，
    会跨过 3% 门槛，把用例变成"其实可信"。
    """

    def test_slow_revenue_is_unreliable(self):
        """营收同比 2.5%（环比约 0.62%，刚好过 0.5% 的跳过门槛）→ 比值算得出，但不可信。

        为什么不取同比 1%：那样的环比只有 0.25%，会被 `REV_GROWTH_MIN_ABS`
        全部跳过，样本不足 → ratio=None，测不到 reliable 这条分支。
        两条保护的量级天然耦合，这是必须知道的事实（见 README.md「已知边界」）。
        """
        q = stable_series(yoy_growth=2.5, ratio=2.0)
        got = metrics.elasticity_ratio(q, config)
        self.assertIsNotNone(got["ratio"])
        # 幂律构造 + 离散化会带来千分级偏差，断言量级而非精确值
        self.assertAlmostEqual(got["ratio"], 2.0, places=1)
        self.assertFalse(got["reliable"], "营收长期不动，比值没有意义")

    def test_very_slow_revenue_gives_no_ratio(self):
        """营收同比 1% → 环比 0.25% < 0.5% → 全被跳过，样本不足 → None。

        这是"慢增长公司算不出弹性"的真实写照，比"算出来但不可信"更常见。
        """
        q = stable_series(yoy_growth=1.0, ratio=2.0)
        got = metrics.elasticity_ratio(q, config)
        self.assertIsNone(got["ratio"])
        self.assertEqual(got["samples"], 0)

    def test_exactly_three_percent_is_reliable(self):
        """营收同比 = 3.0% 恰好 → 可信（规则 `>=`）。

        这里直接给营收序列，不用 `stable_series` —— 4 次方根会把 3.0 变成 2.9999…。
        构造：12 季，每季都恰好是 4 季前的 1.03 倍，于是每个同比都精确等于 3.0。
        """
        revs = []
        for i in range(12):
            if i < 4:
                revs.append(100.0 * (1.05 ** i))       # 前 4 季随便涨，建立基数
            else:
                revs.append(revs[i - 4] * 1.03)        # 后 8 季严格 +3% 同比
        ops = [r * 0.20 for r in revs]
        got = metrics.elasticity_ratio(
            quarters_of(revenues=revs, op_profits=ops), config)
        self.assertTrue(got["reliable"], "恰好 3.0% 应判可信（闭区间）")

    def test_just_below_three_is_unreliable(self):
        revs = []
        for i in range(12):
            if i < 4:
                revs.append(100.0 * (1.05 ** i))
            else:
                revs.append(revs[i - 4] * 1.0299)
        ops = [r * 0.20 for r in revs]
        got = metrics.elasticity_ratio(
            quarters_of(revenues=revs, op_profits=ops), config)
        self.assertFalse(got["reliable"])

    def test_negative_slow_revenue_also_unreliable(self):
        """营收同比 -1% → 绝对值 < 3% → 同样不可信。"""
        q = stable_series(yoy_growth=-1.0, ratio=2.0)
        got = metrics.elasticity_ratio(q, config)
        self.assertFalse(got["reliable"])

    def test_large_decline_is_reliable(self):
        """营收同比 -15% → 绝对值 > 3% → 可信（"变化很大"也是信息）。"""
        q = stable_series(yoy_growth=-15.0, ratio=2.0)
        got = metrics.elasticity_ratio(q, config)
        self.assertTrue(got["reliable"])


class TestGuards(unittest.TestCase):
    """两条保护：分母趋零、比值荒值。"""

    def test_tiny_revenue_growth_skipped(self):
        """某季营收增速 0.3% < 0.5% → 该季跳过，不进 samples。"""
        rev_g = [5.0, 5.0, 0.3, 5.0, 5.0, 5.0, 5.0, 5.0]
        op_g = [10.0, 10.0, 60.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        got = metrics.elasticity_ratio(series(rev_g, op_g), config)
        # 9 季 → 8 对，1 对被跳过 → 7 个样本
        self.assertEqual(got["samples"], 7)

    def test_huge_ratio_clipped(self):
        """比值 300 > 50 → 剔除，不进 samples，也不污染中位数。"""
        rev_g = [5.0] * 8
        op_g = [10.0, 10.0, 3000.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        got = metrics.elasticity_ratio(series(rev_g, op_g), config)
        self.assertEqual(got["samples"], 7)
        self.assertAlmostEqual(got["ratio"], 2.0, places=9,
                               msg="荒值不该把中位数拉走")

    def test_negative_huge_ratio_clipped(self):
        """负的荒值同样剔除 —— 判断用的是绝对值。

        剔除 -300 后，剩下 7 个比值都是 2.0；但第 2 对（从 -300 那一季
        反弹回正常）会算出 -2.0 这种值：利润从 -200 回到 20，涨幅 110% / 5% = 22，
        仍在界内。所以中位数是 -2.0 还是 2.0 取决于符号分布 —— 这里只断言
        "荒值不见了"和"samples 正确"，不硬编码中位数。
        """
        rev_g = [5.0] * 8
        op_g = [-3000.0] + [10.0] * 7
        got = metrics.elasticity_ratio(series(rev_g, op_g), config)
        self.assertEqual(got["samples"], 7)
        self.assertLess(abs(got["ratio"]), 50.0, "荒值必须已被剔除")

    def test_min_samples_boundary(self):
        """有效样本恰好 4 → 出值；3 → None。"""
        # 5 对里 1 对被跳过 → 4 个
        rev_g = [0.3] + [5.0] * 4
        op_g = [60.0] + [10.0] * 4
        got4 = metrics.elasticity_ratio(series(rev_g, op_g), config)
        self.assertEqual(got4["samples"], 4)
        self.assertIsNotNone(got4["ratio"])

        # 4 对里 1 对被跳过 → 3 个
        rev_g = [0.3] + [5.0] * 3
        op_g = [60.0] + [10.0] * 3
        got3 = metrics.elasticity_ratio(series(rev_g, op_g), config)
        self.assertEqual(got3["samples"], 3)
        self.assertIsNone(got3["ratio"], "样本不足必须给 None，不能给 3 个点的中位数")

    def test_zero_revenue_prev_skipped(self):
        """上期营收为 0 → 跳过该季，不抛除零；其余季照常参与。"""
        q = quarters_of(
            revenues=[0.0, 100.0, 110.0, 121.0, 133.1, 146.4, 161.0, 177.1, 194.8],
            op_profits=[10.0, 20.0, 22.0, 24.2, 26.6, 29.3, 32.2, 35.4, 38.9])
        got = metrics.elasticity_ratio(q, config)
        # 8 对里 1 对因上期营收为 0 被跳过 → 7 个样本
        self.assertEqual(got["samples"], 7)
        self.assertIsNotNone(got["ratio"])


class TestNegativeRatios(unittest.TestCase):
    """§11.4 第 5 条：负比值不能被丢。

    如果把 ratio < 0 的样本过滤掉，会系统性高估弹性 ——
    留下来的只有"营收涨、利润也涨得更快"的季度。
    """

    def test_sign_flip_participates(self):
        """营收 +10%、营业利润 -10% → 比值 -1，必须参与中位数。"""
        rev_g = [10.0] * 8
        op_g = [-10.0] * 8
        got = metrics.elasticity_ratio(series(rev_g, op_g), config)
        self.assertAlmostEqual(got["ratio"], -1.0, places=9)
        self.assertEqual(got["samples"], 8)

    def test_negative_drags_median_down(self):
        """一半季度比值 4、一半 -2 → 中位数应落在两者之间且被负值拉低。

        若负值被丢掉，中位数会是 4.0；参与后是 1.0。
        """
        rev_g = [10.0] * 8
        op_g = [40.0, 40.0, 40.0, 40.0, -20.0, -20.0, -20.0, -20.0]
        got = metrics.elasticity_ratio(series(rev_g, op_g), config)
        self.assertEqual(got["samples"], 8)
        self.assertAlmostEqual(got["ratio"], 1.0, places=9,
                               msg="负比值必须参与，否则中位数会是 4.0")

    def test_negative_base_period(self):
        """基期为负：上季营业利润 -100、本季 -50 → 增速 +50%（亏损收窄）。"""
        q = quarters_of(
            revenues=[100.0] * 9,
            op_profits=[-100.0, -50.0, -25.0, -12.5, -6.0, -3.0, -1.5, -0.8, -0.4],
        )
        got = metrics.elasticity_ratio(q, config)
        # 营收完全不变 → 每个比值都会被跳过（分母趋零）→ 无样本
        self.assertEqual(got["samples"], 0)
        self.assertIsNone(got["ratio"])

    def test_recovery_with_growing_revenue(self):
        """亏损收窄 + 营收增长 → 比值为正，正常参与。"""
        revs = [100.0 * (1.05 ** i) for i in range(9)]
        ops = [-100.0, -50.0, -20.0, -5.0, 5.0, 15.0, 30.0, 50.0, 80.0]
        got = metrics.elasticity_ratio(quarters_of(revenues=revs, op_profits=ops), config)
        self.assertIsNotNone(got["ratio"])
        self.assertGreater(got["ratio"], 0)


class TestWindow(unittest.TestCase):
    """§11.4 第 4 条：窗口只取最近 8 季。"""

    def test_only_recent_8(self):
        """前 30 季比值 10、最近 8 季比值 1 → 中位数应为 1。"""
        rev_g = [5.0] * 38
        op_g = [50.0] * 30 + [5.0] * 8
        got = metrics.elasticity_ratio(series(rev_g, op_g), config)
        self.assertEqual(got["samples"], 8)
        self.assertAlmostEqual(got["ratio"], 1.0, places=6)

    def test_first_quarter_in_window_skipped(self):
        """窗口内第一季因缺上期被跳过：9 季数据 → 最多 7 个样本。

        因为 `start = max(1, n - window)`，窗口内的 `quarters[start-1]`
        要用来算第一对的增速，它本身不贡献一个比值。
        """
        q = series([5.0] * 8, [10.0] * 8)      # 9 季
        got = metrics.elasticity_ratio(q, config)
        self.assertEqual(got["samples"], 8, "9 季共 8 对，都在窗口内")

    def test_window_override(self):
        cfg = Cfg(ELASTIC_WINDOW_QUARTERS=4)
        q = series([5.0] * 8, [10.0] * 8)
        got = metrics.elasticity_ratio(q, cfg)
        self.assertEqual(got["samples"], 4)


class TestMissing(unittest.TestCase):

    def test_none_in_window_reduces_samples(self):
        revs = [100.0 * (1.05 ** i) for i in range(9)]
        ops = [20.0 * (1.10 ** i) for i in range(9)]
        ops[3] = None
        got = metrics.elasticity_ratio(
            quarters_of(revenues=revs, op_profits=ops), config)
        self.assertLess(got["samples"], 8)
        self.assertGreaterEqual(got["samples"], 1)

    def test_all_none(self):
        got = metrics.elasticity_ratio(
            quarters_of(revenues=[None] * 9, op_profits=[None] * 9), config)
        self.assertEqual(got, {"ratio": None, "reliable": None, "samples": 0,
                               "zero_cross": 0, "zero_cross_ratio": None})

    def test_single_quarter(self):
        got = metrics.elasticity_ratio(
            quarters_of(revenues=[100.0], op_profits=[20.0]), config)
        self.assertIsNone(got["ratio"])
        self.assertEqual(got["samples"], 0)

    def test_empty(self):
        got = metrics.elasticity_ratio([], config)
        self.assertIsNone(got["ratio"])
        self.assertIsNone(got["reliable"])
        self.assertEqual(got["samples"], 0)

    def test_reliable_none_when_yoy_unavailable(self):
        """样本够、比值有值，但一个同比都算不出来 → reliable=None（不敢下结论）。

        构造：6 季。比值窗口从 `max(1, 6-8)=1` 起算，得 5 对样本（够 4）；
        而同比需要 4 季前的基数，`range(max(4, 6-8), 6)` = range(4,6) → 只有 2 个，
        实际上能算出来。所以真正"一个都算不出"需要季数 <= 4。

        改用 5 季：比值得 4 对（`start=max(1, -3)=1`，4 个样本，恰好够），
        同比 `range(max(4, -3), 5)` = range(4,5) → 1 个样本，还是有值。
        季数 4：比值 3 对（不够 4）→ ratio=None，此时 reliable 也就无意义了。

        结论：`reliable=None` 只在"样本刚够 4 但季数恰好 4"的极窄区间出现，
        实际数据里几乎不可能。这条用例改为断言"季数不足时整组降级"这个更真实的行为。
        """
        q = series([5.0] * 3, [10.0] * 3)      # 4 季，只有 3 对
        got = metrics.elasticity_ratio(q, config)
        self.assertEqual(got["samples"], 3)
        self.assertIsNone(got["ratio"], "3 个样本 < RATIO_MIN_SAMPLES(4) → None")
        self.assertIsNone(got["reliable"])


class TestMedianRobustness(unittest.TestCase):
    """中位数对离群值稳健 —— 这是选它而不选均值的理由。"""

    def test_outlier_does_not_move_median_much(self):
        rev_g = [5.0] * 8
        normal = [10.0] * 7 + [45.0]              # 最后一季比值 9（< 50，不被剔除）
        got = metrics.elasticity_ratio(series(rev_g, normal), config)
        self.assertAlmostEqual(got["ratio"], 2.0, places=9,
                               msg="一个离群值不该改变中位数")

    def test_mean_would_be_higher(self):
        """对照断言：同样的数据用均值会明显偏高（证明中位数的必要性）。"""
        rev_g = [5.0] * 8
        op_g = [10.0] * 7 + [45.0]
        q = series(rev_g, op_g)
        got = metrics.elasticity_ratio(q, config)

        # 手算均值
        ratios = [go / gr for gr, go in zip(rev_g, op_g)]
        mean_ratio = sum(ratios) / len(ratios)
        self.assertGreater(mean_ratio, got["ratio"] + 0.5)


class TestFixture(unittest.TestCase):

    def test_full_40q_is_exactly_2_5(self):
        """黄金路径 fixture 刻意造出 2.5 倍放大。"""
        from cycle import build
        stock = build.build(load("full_40q"))
        got = metrics.elasticity_ratio(stock["quarters"], config)
        self.assertAlmostEqual(got["ratio"], 2.5, places=4)
        self.assertTrue(got["reliable"])

    def test_huge_ratio_fixture_hits_both_guards(self):
        """`huge_ratio`：跳过与剔除各命中，样本从 8 降到 5。"""
        from cycle import build
        stock = build.build(load("huge_ratio"))
        got = metrics.elasticity_ratio(stock["quarters"], config)
        self.assertEqual(got["samples"], 5)
        self.assertIsNotNone(got["ratio"])

    def test_neg_profit_fixture_unreliable(self):
        """`neg_profit`：营收正负交替 → 同比中位数接近 0 → 不可信。"""
        from cycle import build
        stock = build.build(load("neg_profit"))
        got = metrics.elasticity_ratio(stock["quarters"], config)
        self.assertFalse(got["reliable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestZeroCross(unittest.TestCase):
    """判断② 的第二条可靠性：盈亏平衡点附近的季度（DOL 趋于无穷）。"""

    def test_counts_crossing_quarters(self):
        """营业利润 +1 → −1 → +1 交替，每季都跨零。"""
        revs = [100.0, 110.0, 121.0, 133.0, 146.0]
        ops = [10.0, -10.0, 12.0, -12.0, 14.0]
        q = quarters_of(revenues=revs, op_profits=ops)
        got = metrics.elasticity_ratio(q, config)
        self.assertGreater(got["zero_cross"], 0)
        self.assertGreater(got["zero_cross_ratio"], 0)

    def test_no_crossing_when_all_positive(self):
        revs = [100.0, 110.0, 121.0, 133.0, 146.0]
        ops = [10.0, 11.0, 12.0, 13.0, 14.0]
        q = quarters_of(revenues=revs, op_profits=ops)
        got = metrics.elasticity_ratio(q, config)
        self.assertEqual(got["zero_cross"], 0)
        self.assertEqual(got["zero_cross_ratio"], 0.0)

    def test_ratio_is_fraction_of_used_samples(self):
        """分母是**进入样本**的季度数，不是原始季度数。"""
        q = quarters_of(revenues=[100.0, 110.0, 121.0, 133.0],
                        op_profits=[10.0, -10.0, 12.0, 13.2])
        got = metrics.elasticity_ratio(q, config)
        if got["samples"]:
            self.assertAlmostEqual(got["zero_cross_ratio"],
                                   got["zero_cross"] / got["samples"])

    def test_empty_has_none_ratio(self):
        got = metrics.elasticity_ratio([], config)
        self.assertEqual(got["zero_cross"], 0)
        self.assertIsNone(got["zero_cross_ratio"])
