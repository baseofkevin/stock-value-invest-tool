# -*- coding: utf-8 -*-
"""util.py / fmt.py 的测试 —— 纯函数，无 IO、无全局状态。

这两个模块被 score / compute / render 全部依赖，是最底层的地基：
`num()` 对 NaN 的处理、`linear()` 的边界、`cagr()` 对负值的行为，
任何一处错了都会在几十个指标上同时错，但**不会报错**，只会算出一个
「看起来合理」的数字。所以它们的边界情况值得逐个钉死。

`fmt.py` 另有一条硬要求：对 `nan` / `inf` / `None` 必须返回 `—` 而不是字符串
`"nan"`——报告是要打印和转发的，页面上出现 "nan" 是明显的破窗。
"""
import math
import unittest

from tests import helpers  # noqa: F401
from screen.util import (cagr, clamp, div, inverse, linear, mean, median, num,
                         stdev, year_of)
from screen.fmt import (num as fnum, num_sep, pct, signed_pct, yi, yi_suffix)


class TestNum(unittest.TestCase):
    """`num()` 是「把上游的任意值变成一个能用的 float 或 None」的唯一入口。"""

    def test_plain_numbers(self):
        self.assertEqual(num(1), 1.0)
        self.assertEqual(num(1.5), 1.5)
        self.assertEqual(num(-3), -3.0)

    def test_numeric_strings(self):
        self.assertEqual(num("12.5"), 12.5)
        self.assertEqual(num(" 7 "), 7.0)
        self.assertEqual(num("-0.5"), -0.5)

    def test_none_and_empty(self):
        self.assertIsNone(num(None))
        self.assertIsNone(num(""))
        self.assertIsNone(num("   "))
        self.assertIsNone(num("--"))
        self.assertIsNone(num("-"))

    def test_nan_and_inf_become_none(self):
        """NaN / inf 必须收敛成 None，不能漏到下游。

        下游遍地是 `if x is not None`，一个 nan 混进去就一路穿透：
        `nan > 0` 是 False、`nan < 0` 也是 False，两个分支都不走，
        最后在渲染时变成字符串 "nan"。
        """
        self.assertIsNone(num(float("nan")))
        self.assertIsNone(num(float("inf")))
        self.assertIsNone(num(float("-inf")))
        self.assertIsNone(num("nan"))
        self.assertIsNone(num("inf"))

    def test_bool_is_rejected(self):
        """bool 是 int 的子类，但 True 不该被当成 1 参与财务计算。"""
        self.assertIsNone(num(True))
        self.assertIsNone(num(False))

    def test_garbage(self):
        self.assertIsNone(num("abc"))
        self.assertIsNone(num([]))
        self.assertIsNone(num({}))


class TestDiv(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(div(10, 4), 2.5)

    def test_zero_denominator_returns_none(self):
        """分母为 0 返回 None 而不是抛错，也不是 inf。

        「除以 0」在财务里含义是「这个比率算不出来」，不是「无穷大」。
        """
        self.assertIsNone(div(10, 0))
        self.assertIsNone(div(0, 0))

    def test_none_inputs(self):
        self.assertIsNone(div(None, 4))
        self.assertIsNone(div(10, None))
        self.assertIsNone(div(None, None))


class TestMedianMeanStdev(unittest.TestCase):
    def test_median_odd(self):
        self.assertEqual(median([1, 2, 3]), 2)

    def test_median_even(self):
        self.assertEqual(median([1, 2, 3, 4]), 2.5)

    def test_median_skips_none(self):
        """None 是「算不出来」，不是 0，不能拉低中位数。"""
        self.assertEqual(median([1, None, 3]), 2)

    def test_median_all_none(self):
        self.assertIsNone(median([None, None]))

    def test_median_empty(self):
        self.assertIsNone(median([]))

    def test_mean(self):
        self.assertEqual(mean([1, 2, 3]), 2)
        self.assertEqual(mean([1, None, 3]), 2)

    def test_mean_all_none(self):
        self.assertIsNone(mean([None]))

    def test_stdev_uses_sample_formula(self):
        """`stdev` 用**样本标准差**（除以 n−1），不是总体标准差（除以 n）。

        这个区别不是学术细节：`QUALITY_ITEM_RANGES` 里 `gm_stability` / `roe_stability`
        的区间是按这里的口径标定的。换成 n 会让 σ 系统性偏小约 1/n，
        然后「稳定性」一项悄悄给高分。
        """
        xs = [2, 4, 4, 4, 5, 5, 7, 9]
        m = sum(xs) / len(xs)
        expected = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
        self.assertAlmostEqual(stdev(xs), expected, places=9)
        # 与总体标准差不同，确认不是走了 population 口径
        population = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))
        self.assertNotAlmostEqual(stdev(xs), population, places=6)

    def test_stdev_two_equal_points_is_zero(self):
        self.assertAlmostEqual(stdev([5, 5]), 0.0, places=9)

    def test_stdev_needs_two_points(self):
        """单点没有离散度可言，返回 None 而不是 0 —— 0 会被读成「极其稳定」。"""
        self.assertIsNone(stdev([5]))
        self.assertIsNone(stdev([]))

    def test_stdev_skips_none(self):
        self.assertAlmostEqual(stdev([2, None, 4]), stdev([2, 4]), places=9)

    def test_stdev_skips_nan(self):
        """nan 与 None 同义：都要被剔除，否则整列 σ 变成 nan。"""
        self.assertAlmostEqual(stdev([2, float("nan"), 4]), stdev([2, 4]), places=9)


class TestCagr(unittest.TestCase):
    """`cagr()` 返回**百分数**（100 表示翻倍），并且要求 first / last 都 > 0。"""

    def test_doubling_over_one_year(self):
        self.assertAlmostEqual(cagr(100, 200, 1), 100.0, places=6)

    def test_doubling_over_two_years(self):
        self.assertAlmostEqual(cagr(100, 200, 2), (math.sqrt(2) - 1) * 100, places=6)

    def test_halving_over_two_years(self):
        self.assertAlmostEqual(cagr(200, 100, 2), (math.sqrt(0.5) - 1) * 100, places=6)

    def test_flat_is_zero(self):
        self.assertAlmostEqual(cagr(100, 100, 5), 0.0, places=9)

    def test_zero_years_returns_none(self):
        self.assertIsNone(cagr(100, 200, 0))

    def test_negative_years_returns_none(self):
        self.assertIsNone(cagr(100, 200, -1))

    def test_none_inputs(self):
        self.assertIsNone(cagr(None, 200, 2))
        self.assertIsNone(cagr(100, None, 2))
        self.assertIsNone(cagr(100, 200, None))

    def test_non_positive_endpoints_return_none(self):
        """first 或 last ≤ 0 一律 None。

        这是刻意的保守：从 −100 到 +100 的「复合增速」没有经济含义，
        且开负数分数次幂会得到复数（Python 里直接 ValueError）。
        """
        self.assertIsNone(cagr(0, 200, 2))
        self.assertIsNone(cagr(-100, 200, 2))
        self.assertIsNone(cagr(100, 0, 2))
        self.assertIsNone(cagr(100, -200, 2))


class TestClampLinearInverse(unittest.TestCase):
    def test_clamp_inside(self):
        self.assertEqual(clamp(50), 50)

    def test_clamp_below(self):
        self.assertEqual(clamp(-10), 0)

    def test_clamp_above(self):
        self.assertEqual(clamp(150), 100)

    def test_clamp_custom_bounds(self):
        self.assertEqual(clamp(5, lo=10, hi=20), 10)
        self.assertEqual(clamp(25, lo=10, hi=20), 20)

    def test_linear_midpoint(self):
        self.assertEqual(linear(5, 0, 10, 20), 10)

    def test_linear_clamps_below(self):
        self.assertEqual(linear(-5, 0, 10, 20), 0)

    def test_linear_clamps_above(self):
        self.assertEqual(linear(15, 0, 10, 20), 20)

    def test_linear_none(self):
        self.assertIsNone(linear(None, 0, 10, 20))

    def test_linear_degenerate_range(self):
        """low == high 时不能除零。"""
        self.assertIsNone(linear(5, 5, 5, 20))

    def test_inverse_direction(self):
        """inverse：值越小越好。下界拿满分。"""
        self.assertEqual(inverse(0, 0, 10, 20), 20)
        self.assertEqual(inverse(10, 0, 10, 20), 0)
        self.assertEqual(inverse(5, 0, 10, 20), 10)

    def test_inverse_clamps(self):
        self.assertEqual(inverse(-5, 0, 10, 20), 20)
        self.assertEqual(inverse(15, 0, 10, 20), 0)


class TestYearOf(unittest.TestCase):
    def test_iso_date(self):
        self.assertEqual(year_of("2025-12-31"), 2025)

    def test_compact_date(self):
        self.assertEqual(year_of("20251231"), 2025)

    def test_int_passthrough(self):
        self.assertEqual(year_of(2025), 2025)

    def test_none(self):
        self.assertIsNone(year_of(None))

    def test_garbage(self):
        self.assertIsNone(year_of("abc"))


class TestFmtNonFinite(unittest.TestCase):
    """None / NaN / ±Inf 一律返回 `—`。

    这是报告的门面：页面上出现 "nan" / "nan%" / "inf" 会让读者立刻怀疑整份报告。
    nan 能通过 `float()` 转换，所以旧实现会把它们原样格式化出去 —— 这里钉死。
    """

    CASES = [None, float("nan"), float("inf"), float("-inf"),
             "nan", "inf", "-inf", "NaN", "Infinity"]

    def test_num(self):
        for v in self.CASES:
            with self.subTest(v=v):
                self.assertEqual(fnum(v), "—")

    def test_num_sep(self):
        for v in self.CASES:
            with self.subTest(v=v):
                self.assertEqual(num_sep(v), "—")

    def test_pct(self):
        for v in self.CASES:
            with self.subTest(v=v):
                self.assertEqual(pct(v), "—")

    def test_signed_pct(self):
        for v in self.CASES:
            with self.subTest(v=v):
                self.assertEqual(signed_pct(v), "—")

    def test_yi(self):
        for v in self.CASES:
            with self.subTest(v=v):
                self.assertEqual(yi(v), "—")

    def test_yi_suffix(self):
        for v in self.CASES:
            with self.subTest(v=v):
                self.assertEqual(yi_suffix(v), "—")


class TestFmtGarbagePassthrough(unittest.TestCase):
    """非数字的脏值**原样输出**，不抛错也不变成 `—`。

    这是 fmt.py 的约定 3：报告缺一格比整份报告生成失败好。
    与「nan 给 —」不矛盾 —— nan 是「算不出来」，脏值是「上游给了别的字样」，
    把后者显示出来，人能看出是数据源的问题。
    """

    def test_chinese_text_passthrough(self):
        self.assertEqual(fnum("未披露"), "未披露")
        self.assertEqual(pct("不适用"), "不适用")

    def test_dash_passthrough(self):
        self.assertEqual(fnum("--"), "--")

    def test_html_escaped(self):
        """`_raw()` 要转义，否则上游给的 `<script>` 会直接注入报告页面。"""
        self.assertEqual(fnum("<b>"), "&lt;b&gt;")
        self.assertIn("&amp;", fnum("a&b"))

    def test_empty_string(self):
        self.assertEqual(fnum(""), "")

    def test_bool_passthrough(self):
        """bool 是 int 子类，`True` 不能被格式化成 1.00。"""
        self.assertEqual(fnum(True), "True")


class TestFmtFormatting(unittest.TestCase):
    def test_pct_adds_sign_char(self):
        self.assertEqual(pct(12.34), "12.3%")
        self.assertEqual(pct(12.34, digits=2), "12.34%")

    def test_signed_pct_takes_fraction_and_multiplies_by_100(self):
        """入参是**小数**（0.1234），输出 +12.3%。

        这一点容易搞错：`pct()` 吃的是「已经是百分数的值」（12.34 → "12.3%"），
        `signed_pct()` 吃的是「比值」（0.1234 → "+12.3%"）。
        两套口径并存是历史原因，写测试把它固定下来，免得有人"统一"时改错。
        """
        self.assertEqual(signed_pct(0.032), "+3.2%")
        self.assertEqual(signed_pct(-0.032), "-3.2%")
        self.assertEqual(signed_pct(0.0), "+0.0%")

    def test_num_digits(self):
        self.assertEqual(fnum(1.234, digits=2), "1.23")
        self.assertEqual(fnum(1.0, digits=0), "1")

    def test_num_sep_uses_thousand_separator(self):
        self.assertIn(",", num_sep(1234567.89))

    def test_num_no_thousand_separator(self):
        """`num` 与 `num_sep` 的唯一区别就是千分位。"""
        self.assertNotIn(",", fnum(1234567.89))

    def test_yi_divides_by_1e8(self):
        """`yi` 吃元、输出亿，不带单位（单位由表头写）。"""
        self.assertEqual(yi(1e8), "1.00")
        self.assertEqual(yi(2.5e8), "2.50")

    def test_yi_suffix_adds_unit(self):
        self.assertEqual(yi_suffix(1e8), "1.00 亿")
        self.assertEqual(yi_suffix(2.5e8), "2.50 亿")

    def test_string_numbers_are_accepted(self):
        """上游常把数值给成字符串，格式化层要能接住。"""
        self.assertEqual(fnum("12.345", digits=2), "12.35")
        self.assertEqual(pct("12.345"), "12.3%")


if __name__ == "__main__":
    unittest.main()
