#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""格式化：缺失值、精度，以及「显示值不许跨判定边界」。

这些用例原来住在 test_render.py 里（HTML 报告还在的时候）。报告移除后
HTML 部分整批删掉，但这批必须留下——它们守的不是排版，而是
**同一个数字在判据里和在人眼里必须是同一档**。
"""
import math
import unittest

import tests.helpers  # noqa: F401  —— 负责 sys.path 注入
from cycle import fmt


class TestMissing(unittest.TestCase):

    def test_none_becomes_dash(self):
        for fn in (fmt.fmt_num, fmt.fmt_yi, fmt.fmt_pp, fmt.fmt_pct):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(fn(None), fmt.DASH)

    def test_non_finite_becomes_dash(self):
        """上游 0/0 会漏进 nan / inf，直接 f-string 会把 "nan" 打给人看。"""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                self.assertEqual(fmt.fmt_num(bad), fmt.DASH)
                self.assertEqual(fmt.fmt_yi(bad), fmt.DASH)
                self.assertEqual(fmt.fmt_pp(bad), fmt.DASH)
                self.assertEqual(fmt.fmt_pct(bad), fmt.DASH)

    def test_bad_type_becomes_dash(self):
        self.assertEqual(fmt.fmt_num("abc"), fmt.DASH)


class TestFormatting(unittest.TestCase):

    def test_fmt_yi(self):
        self.assertEqual(fmt.fmt_yi(1.5e8), "1.5 亿")
        self.assertEqual(fmt.fmt_yi(0.0), "0.0 亿")

    def test_fmt_pp_and_pct_default_digits(self):
        # σ 默认 2 位：5pp 这条线上 1 位小数会把 4.95 显示成 5.0
        self.assertEqual(fmt.fmt_pp(6.137), "6.14 pp")
        self.assertEqual(fmt.fmt_pp(6.137, 1), "6.1 pp")
        self.assertEqual(fmt.fmt_pct(1.25), "1.2%")

    def test_fmt_num_digits(self):
        self.assertEqual(fmt.fmt_num(2.4999, 2), "2.50")
        self.assertEqual(fmt.fmt_num(0, 2), "0.00")
        self.assertEqual(fmt.fmt_num(1.2345, 2, suffix=" 倍"), "1.23 倍")


class TestBoundaryGuard(unittest.TestCase):

    def test_never_crosses_lower_threshold(self):
        """4.9505 判「非周期」，显示值不能是 5.00 —— 真实踩过的坑。"""
        self.assertEqual(fmt.fmt_pp(4.9505, lower=5.0), "4.95 pp")
        for value in (4.999, 4.9999, 4.995):
            shown = float(fmt.fmt_pp(value, lower=5.0).split()[0])
            self.assertLess(shown, 5.0, f"{value} 舍入后跨线了")

    def test_never_crosses_upper_threshold(self):
        """线上方的值也不许被舍入到线下方（阈值相等即「是」，两个方向都要守）。"""
        for value in (5.001, 5.0049):
            shown = float(fmt.fmt_pp(value, lower=5.0).split()[0])
            self.assertGreaterEqual(shown, 5.0, f"{value} 舍入后跨线了")

    def test_pct_respects_both_position_bounds(self):
        """分位 39.99% 是低位 → 不许显示成 40.0%（40.0 本身属中性）。"""
        self.assertLess(float(fmt.fmt_pct(39.99, lower=40.0).rstrip("%")), 40.0)
        self.assertGreater(float(fmt.fmt_pct(60.01, upper=60.0).rstrip("%")), 60.0)
        self.assertEqual(fmt.fmt_pct(40.01, lower=40.0), "40.0%")

    def test_display_never_flips_the_label(self):
        """跨档不变量：显示值重新套一遍阈值，得到的标签必须与真实值一致。

        比逐个 case 断言更结实——以后精度、阈值怎么调都仍然成立。
        """
        cases = []
        # 判断①：5pp 边界两侧
        for v in (4.94, 4.949, 4.9505, 4.995, 4.999, 5.0, 5.001, 5.0049, 5.01, 0.0, 20.9):
            cases.append((fmt.fmt_pp(v, lower=5.0), v, lambda x: x >= 5.0))
        # 判断②：2.0 边界两侧（负值也测，-2.0 是边界）
        for v in (1.994, 1.999, 2.0, 2.001, 2.0049, -1.999, -2.0, -2.001, 17.31):
            cases.append((fmt.fmt_num(v, 2, lower=2.0), v, lambda x: x >= 2.0))
        # 判断③：40 / 60 两条边界
        for v in (39.94, 39.96, 39.99, 40.0, 40.01, 59.99, 60.0, 60.01, 60.04):
            cases.append((fmt.fmt_pct(v, lower=40.0, upper=60.0), v,
                          lambda x: "low" if x < 40.0 else ("high" if x > 60.0 else "neutral")))
        for text, real, classify in cases:
            shown = float(text.split()[0].rstrip("%"))
            self.assertEqual(classify(shown), classify(real),
                             f"{real} 显示成 {text}，档位从 "
                             f"{classify(real)} 变成了 {classify(shown)}")

    def test_num_boundary_guard(self):
        self.assertEqual(fmt.fmt_num(1.999, 2, lower=2.0), "1.99")


if __name__ == "__main__":
    unittest.main()
