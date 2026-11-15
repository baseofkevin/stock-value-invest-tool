"""纯函数测试（README.md「测试」）。

这个文件守的是一条铁律：**缺失一律返回 None，绝不返回 0 或 False**。
`assertIsNone` 而不是 `assertFalse` —— 后者会把 0 也放过，而 0 是有意义的数据
（毛利率 0%、增长率 0%），混淆它们会让报告把"没数据"画成"数值为零"。
"""

import unittest

import tests.helpers  # noqa: F401
from cycle import util


class TestNum(unittest.TestCase):

    def test_string_to_float(self):
        self.assertEqual(util.num("1.5"), 1.5)
        self.assertEqual(util.num("-3"), -3.0)
        self.assertEqual(util.num("1e5"), 100000.0)
        self.assertEqual(util.num("2084485744.75"), 2084485744.75)

    def test_empty_and_bad(self):
        for raw in ("", "   ", "abc", "--", "N/A", None):
            with self.subTest(raw=raw):
                self.assertIsNone(util.num(raw))

    def test_numeric_passthrough(self):
        self.assertEqual(util.num(3), 3.0)
        self.assertEqual(util.num(3.5), 3.5)
        self.assertIsInstance(util.num("2"), float)

    def test_bool_is_not_a_number(self):
        """bool 是 int 的子类，但 True 当 1 用是错的 —— 接口不会返 bool，
        一旦出现说明结构变了，应该当缺失处理。"""
        self.assertIsNone(util.num(True))
        self.assertIsNone(util.num(False))

    def test_zero_is_zero_not_none(self):
        self.assertEqual(util.num("0"), 0.0)
        self.assertIsNotNone(util.num("0"))


class TestSafeDiv(unittest.TestCase):

    def test_normal(self):
        self.assertEqual(util.safe_div(10, 4), 2.5)

    def test_zero_denominator(self):
        self.assertIsNone(util.safe_div(1, 0))
        self.assertIsNone(util.safe_div(1, 0.0))
        self.assertIsNone(util.safe_div(1, -0.0))

    def test_none_inputs(self):
        self.assertIsNone(util.safe_div(None, 2))
        self.assertIsNone(util.safe_div(2, None))
        self.assertIsNone(util.safe_div(None, None))

    def test_no_rounding(self):
        """计算层保留原始精度，四舍五入只在展示层做。"""
        self.assertEqual(util.safe_div(1, 3), 1 / 3)
        self.assertNotEqual(util.safe_div(1, 3), 0.33)

    def test_negative_result(self):
        self.assertEqual(util.safe_div(-10, 4), -2.5)


class TestMedian(unittest.TestCase):

    def test_odd(self):
        self.assertEqual(util.median([3, 1, 2]), 2)

    def test_even(self):
        self.assertEqual(util.median([1, 2, 3, 4]), 2.5)

    def test_single(self):
        self.assertEqual(util.median([7]), 7)

    def test_ignores_none(self):
        self.assertEqual(util.median([None, 1, 3, None]), 2.0)

    def test_all_none(self):
        self.assertIsNone(util.median([None, None]))

    def test_empty(self):
        self.assertIsNone(util.median([]))

    def test_negative_values_participate(self):
        """负比值必须能当中位数 —— 这是判断② 的核心要求。"""
        self.assertEqual(util.median([-3, -1, 5]), -1)
        self.assertEqual(util.median([-1, 1]), 0.0)


class TestStdev(unittest.TestCase):

    def test_one_sample_is_none(self):
        """样本标准差在 n=1 时无定义 —— 必须 None，不能给 0。"""
        self.assertIsNone(util.stdev([5]))

    def test_two_identical(self):
        self.assertAlmostEqual(util.stdev([2, 2]), 0.0, places=12)

    def test_ddof_is_one(self):
        """[0.30,0.32,0.34] 的**样本**标准差 = 0.02；总体标准差约 0.01633。

        这条断言钉死 ddof=1 口径，防止有人换成 statistics.pstdev。
        """
        got = util.stdev([0.30, 0.32, 0.34])
        self.assertAlmostEqual(got, 0.02, places=10)
        self.assertNotAlmostEqual(got, 0.01632993, places=6)

    def test_ignores_none(self):
        self.assertAlmostEqual(util.stdev([None, 0.30, 0.32, 0.34]), 0.02, places=10)

    def test_all_none(self):
        self.assertIsNone(util.stdev([None, None]))

    def test_empty(self):
        self.assertIsNone(util.stdev([]))


class TestPercentileRank(unittest.TestCase):
    """中位秩法 —— 边界重点。"""

    def test_all_equal_is_fifty(self):
        """**中位秩的关键性质**：全部相等时恰好 50.0。

        这也是选它而不选"小于等于占比"的原因：后者会给 100。
        """
        self.assertEqual(util.percentile_rank([3, 3, 3, 3], 3), 50.0)
        self.assertEqual(util.percentile_rank([0] * 40, 0.0), 50.0)

    def test_unique_max(self):
        got = util.percentile_rank([1, 2, 3, 4], 4)
        self.assertAlmostEqual(got, 87.5, places=10)     # (3 + 0.5)/4*100
        self.assertLess(got, 100.0)

    def test_unique_min(self):
        got = util.percentile_rank([1, 2, 3, 4], 1)
        self.assertAlmostEqual(got, 12.5, places=10)     # (0 + 0.5)/4*100
        self.assertGreater(got, 0.0)

    def test_duplicates(self):
        """[1,1,1,2] 里 current=1 → (0 + 0.5*3)/4*100 = 37.5"""
        self.assertAlmostEqual(util.percentile_rank([1, 1, 1, 2], 1), 37.5, places=10)

    def test_monotonic_increase(self):
        series = list(range(1, 11))
        prev = -1.0
        for cur in series:
            got = util.percentile_rank(series, cur)
            self.assertGreater(got, prev)
            prev = got

    def test_ignores_none_and_denominator_is_valid_count(self):
        """None 不进分母：3 个有效值 + 2 个 None，分母是 3 不是 5。"""
        got = util.percentile_rank([None, 1, 2, None, 3], 3)
        self.assertAlmostEqual(got, (2 + 0.5) / 3 * 100, places=10)

    def test_none_current(self):
        self.assertIsNone(util.percentile_rank([1, 2, 3], None))

    def test_empty_series(self):
        self.assertIsNone(util.percentile_rank([], 1))
        self.assertIsNone(util.percentile_rank([None, None], 1))

    def test_result_in_range(self):
        for cur in (-5, 0, 2.5, 100):
            with self.subTest(cur=cur):
                got = util.percentile_rank([-5, 0, 1, 2.5, 4, 100], cur)
                self.assertGreaterEqual(got, 0.0)
                self.assertLessEqual(got, 100.0)


class TestPctChange(unittest.TestCase):

    def test_growth(self):
        self.assertAlmostEqual(util.pct_change(110, 100), 10.0, places=10)

    def test_decline(self):
        self.assertAlmostEqual(util.pct_change(90, 100), -10.0, places=10)

    def test_zero_base_is_none(self):
        self.assertIsNone(util.pct_change(100, 0))

    def test_negative_base_uses_abs(self):
        """基期为负 → 分母用 abs，亏损收窄记为正 —— 符合直觉，也不至于把符号弄反。"""
        # -1e8 → -0.5e8：亏损收窄 50%
        self.assertAlmostEqual(util.pct_change(-0.5e8, -1e8), 50.0, places=10)
        # -1e8 → -1.5e8：亏损扩大 50%（负号）
        self.assertAlmostEqual(util.pct_change(-1.5e8, -1e8), -50.0, places=10)

    def test_none_inputs(self):
        self.assertIsNone(util.pct_change(None, 100))
        self.assertIsNone(util.pct_change(100, None))

    def test_zero_change(self):
        self.assertEqual(util.pct_change(100, 100), 0.0)


class TestClip(unittest.TestCase):

    def test_inside(self):
        self.assertEqual(util.clip(5, 0, 10), 5)

    def test_above(self):
        self.assertEqual(util.clip(50, 0, 10), 10)

    def test_below(self):
        self.assertEqual(util.clip(-50, 0, 10), 0)

    def test_none(self):
        self.assertIsNone(util.clip(None, 0, 10))


class TestMean(unittest.TestCase):

    def test_normal(self):
        self.assertEqual(util.mean([1, 2, 3]), 2.0)

    def test_ignores_none(self):
        self.assertEqual(util.mean([None, 2, 4]), 3.0)

    def test_empty(self):
        self.assertIsNone(util.mean([]))
        self.assertIsNone(util.mean([None]))


class TestNoneNeverBecomesZero(unittest.TestCase):
    """把 §11.4 第 3 条单独钉一遍：这一族函数遇到缺失，永远 None。"""

    def test_all_functions(self):
        self.assertIsNone(util.num(None))
        self.assertIsNone(util.safe_div(None, 1))
        self.assertIsNone(util.median([]))
        self.assertIsNone(util.stdev([]))
        self.assertIsNone(util.percentile_rank([], 1))
        self.assertIsNone(util.pct_change(None, None))
        self.assertIsNone(util.clip(None, 0, 1))
        self.assertIsNone(util.mean([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
