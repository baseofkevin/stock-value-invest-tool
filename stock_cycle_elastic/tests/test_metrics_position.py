"""判断③ 产能周转率分位（README.md「测试」）。

四个必须钉住的点：
  1. **当前值必须来自最新一季本身** —— 不能用"最后一个有效值"冒充，
     否则最新数据缺失会被静默当成"还是上季的位置"；
  2. **None 不进分母** —— 分母是有效个数，不是窗口长度；
  3. **窗口只取最近 40 季**；
  4. **全等值 → 恰好 50.0**（中位秩性质，也是为什么用它）。

分位本身的分支（低位/中性/高位）在 test_decide 测 —— 这里只测数值。
"""

import unittest

import tests.helpers  # noqa: F401
import config
from cycle import metrics

from tests.fixtures import load
from tests.helpers import Cfg, quarters_of


class TestNormal(unittest.TestCase):

    def test_monotonic_increasing_latest_is_highest(self):
        values = list(range(1, 41))
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertAlmostEqual(got["current"], 40.0)
        # 唯一最大值 → 中位秩 (39 + 0.5)/40*100 = 98.75
        self.assertAlmostEqual(got["pct"], 98.75, places=9)

    def test_monotonic_decreasing_latest_is_lowest(self):
        values = list(range(40, 0, -1))
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertAlmostEqual(got["current"], 1.0)
        self.assertAlmostEqual(got["pct"], 1.25, places=9)

    def test_all_equal_is_fifty(self):
        """**中位秩的关键性质**：全等值时恰好 50.0，不是 100。

        这也是选它而不选"小于等于占比"的直接理由 —— 后者会给 100，
        把"十年没变"误判成"历史最高位"。
        """
        got = metrics.capacity_percentile(quarters_of(turnover=[2.0] * 40), config)
        self.assertEqual(got["pct"], 50.0)
        self.assertEqual(got["current"], 2.0)


class TestBoundary(unittest.TestCase):
    """§11.4 第 2 条：40.0 / 60.0 都是**开区间**。

    指标层只出数值，标签的分支在 test_decide 覆盖。这里构造出精确的
    40.0 / 60.0 分位，为那边的断言做准备，同时验证分位算法本身。
    """

    def test_construct_exact_40(self):
        """让当前值落在第 40 分位：40 个值里 16 个小于它、0 个等于它。

        (16 + 0.5*0)/40*100 = 40.0
        构造：前 16 个 1，后 23 个 2，最后一个 3（当前值）。
        小于 3 的有 39 个 → (39+0.5)/40 = 98.75，不对。

        正确做法：让当前值等于某个重复值。
        35 个 1、4 个 5、当前 = 1 且 1 出现 35 次 → (0 + 17.5)/40 = 43.75。

        直接算想要的组合：想要 (below + 0.5*equal)/40 = 0.4 → below + 0.5*equal = 16。
        取 equal=0、below=16：16 个值小于当前，24 个值大于当前（含当前）。
        即：16 个 1、23 个 2、最后 1 个 = 2（当前）。
        此时 below=16、equal=24 → (16+12)/40 = 70。不行，因为当前值本身也在 equal 里。

        所以要让当前值唯一：equal 恒为 1，below + 0.5 = 16 无整数解。
        改为 equal=2（当前值出现两次）：below + 1 = 16 → below = 15。
        15 个 1、2 个 2（含当前）、23 个 3 → 总数 40。
        """
        values = [1.0] * 15 + [3.0] * 23 + [2.0, 2.0]
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertAlmostEqual(got["current"], 2.0)
        self.assertAlmostEqual(got["pct"], (15 + 0.5 * 2) / 40 * 100, places=9)

    def test_construct_exact_60(self):
        """60 分位：below + 0.5*equal = 24。取 equal=2、below=23。

        23 个 1、2 个 5（含当前）、15 个 9 → 总 40。
        """
        values = [1.0] * 23 + [9.0] * 15 + [5.0, 5.0]
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertAlmostEqual(got["current"], 5.0)
        self.assertAlmostEqual(got["pct"], (23 + 0.5 * 2) / 40 * 100, places=9)


class TestCurrentMustBeLatest(unittest.TestCase):
    """最容易写错的一条：current 不能从 valid 的末位取。"""

    def test_latest_missing_gives_none(self):
        """最新一季 turnover=None，但历史有 20 个有效值 → pct 必须 None。

        如果实现从 `valid[-1]` 取当前值，会静默返回"上一季的分位"，
        把"最新数据缺失"伪装成"位置没变"。
        """
        values = [float(i) for i in range(1, 21)] + [None]
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertIsNone(got["pct"], "最新季缺数据时不能拿倒数第二季冒充")
        self.assertIsNone(got["current"])
        self.assertEqual(got["samples"], 20, "历史有效值仍要计数")

    def test_latest_present_others_missing(self):
        """只有最新一季有值 → 样本不足 8 → None。"""
        values = [None] * 20 + [3.0]
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertEqual(got["samples"], 1)
        self.assertIsNone(got["pct"])

    def test_current_equals_latest_value(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertEqual(got["current"], 10.0)


class TestNoneDenominator(unittest.TestCase):

    def test_none_excluded_from_denominator(self):
        """12 个值里 3 个 None → 分母是 9，不是 12。"""
        values = [1.0, None, 2.0, None, 3.0, None, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertEqual(got["samples"], 9)
        # 当前值 9.0 是唯一最大值 → (8 + 0.5)/9*100
        self.assertAlmostEqual(got["pct"], (8 + 0.5) / 9 * 100, places=9)

    def test_below_min_samples(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]   # 7 < 8
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertIsNone(got["pct"])
        self.assertEqual(got["samples"], 7)

    def test_exactly_min_samples(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]   # 恰好 8
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertIsNotNone(got["pct"])


class TestWindow(unittest.TestCase):

    def test_only_recent_40(self):
        """43 季数据，前 3 季是极端值 → 分位不受它们影响。

        构造：前 3 季 1000（极高），后面 40 季 1..40。
        若把 43 季都算进去，分母会是 43；只取后 40 季则分母 40。
        """
        values = [1000.0] * 3 + [float(i) for i in range(1, 41)]
        got = metrics.capacity_percentile(quarters_of(turnover=values), config)
        self.assertEqual(got["samples"], 40, "只取最近 40 季")
        self.assertAlmostEqual(got["current"], 40.0)
        # 当前值 40 在后 40 季里是最大值 → (39+0.5)/40 = 98.75
        self.assertAlmostEqual(got["pct"], 98.75, places=9)

    def test_window_override(self):
        cfg = Cfg(POSITION_WINDOW_QUARTERS=10)
        values = [float(i) for i in range(1, 41)]
        got = metrics.capacity_percentile(quarters_of(turnover=values), cfg)
        self.assertEqual(got["samples"], 10)


class TestEmpty(unittest.TestCase):

    def test_empty(self):
        got = metrics.capacity_percentile([], config)
        self.assertEqual(got, {"pct": None, "current": None, "samples": 0})

    def test_all_none(self):
        got = metrics.capacity_percentile(quarters_of(turnover=[None] * 20), config)
        self.assertIsNone(got["pct"])
        self.assertEqual(got["samples"], 0)


class TestFixtures(unittest.TestCase):
    """金融股不该在指标层被特殊处理 —— `financial` 的判断在 decide 层。

    指标层照算（因为资金周转率对银行确实是算得出来的数），
    只是那个数没有经济意义，所以标"不适用"的责任在 decide。
    这样分层的好处：指标层是纯计算，可独立验证。
    """

    def test_financial_still_computes(self):
        from cycle import build
        stock = build.build(load("financial"))
        got = metrics.capacity_percentile(stock["quarters"], config)
        self.assertIsNotNone(got["pct"], "指标层照算，na 由 decide 决定")

    def test_balance_missing_no_pct(self):
        from cycle import build
        stock = build.build(load("balance_missing"))
        got = metrics.capacity_percentile(stock["quarters"], config)
        self.assertIsNone(got["pct"])
        # TTM 口径下最早 3 季凑不满 4 季滚动和 → 12 季里只有 9 季有周转率
        self.assertEqual(got["samples"], 9)

    def test_full_40q_is_low(self):
        """黄金路径 fixture：固定资产扩张快于营收 → 周转率下滑 → 低位。"""
        from cycle import build
        stock = build.build(load("full_40q"))
        got = metrics.capacity_percentile(stock["quarters"], config)
        self.assertLess(got["pct"], 10.0)

    def test_flat_margin_equal_turnover(self):
        from cycle import build
        stock = build.build(load("flat_margin_equal_turnover"))
        got = metrics.capacity_percentile(stock["quarters"], config)
        self.assertEqual(got["pct"], 50.0)


class TestComputeAll(unittest.TestCase):

    def test_returns_three_keys(self):
        from cycle import build
        stock = build.build(load("full_40q"))
        m = metrics.compute_all(stock["quarters"], config)
        self.assertEqual(set(m.keys()),
                         {"gm_std", "gm_cv", "elasticity", "position",
                          "capacity_direction"})

    def test_does_not_read_contract_liab(self):
        """§2.8：`contract_liab` 是透传字段，指标层**绝不能**读它。

        构造两个只差 contract_liab 的序列，三个指标必须完全一致。
        """
        from cycle import build
        stock = build.build(load("full_40q"))
        quarters = stock["quarters"]

        stripped = []
        for q in quarters:
            copy = dict(q)
            copy["contract_liab"] = None
            stripped.append(copy)

        m1 = metrics.compute_all(quarters, config)
        m2 = metrics.compute_all(stripped, config)
        self.assertEqual(m1["gm_std"], m2["gm_std"])
        self.assertEqual(m1["elasticity"], m2["elasticity"])
        self.assertEqual(m1["position"], m2["position"])

    def test_works_without_contract_liab_key_at_all(self):
        """连 key 都不给也不该崩 —— 证明没有任何地方 `q["contract_liab"]` 硬取。"""
        from cycle import build
        stock = build.build(load("full_40q"))
        quarters = []
        for q in stock["quarters"]:
            copy = dict(q)
            del copy["contract_liab"]
            quarters.append(copy)
        m = metrics.compute_all(quarters, config)
        self.assertIsNotNone(m["gm_std"])


class TestSeriesForChart(unittest.TestCase):

    def test_lengths(self):
        """三张图的点数：
            gross_margin —— 20（窗口长度）
            ratio        —— 最多 8（窗口内对数；43 季时窗口完整 → 8）
            turnover     —— 40（窗口长度）
        """
        from cycle import build
        stock = build.build(load("full_40q"))
        charts = metrics.series_for_chart(stock["quarters"], config)
        self.assertEqual(set(charts.keys()), {"gross_margin", "ratio", "turnover"})
        self.assertEqual(len(charts["gross_margin"]), config.WAVE_WINDOW_QUARTERS)
        self.assertEqual(len(charts["ratio"]), config.ELASTIC_WINDOW_QUARTERS)
        self.assertEqual(len(charts["turnover"]), config.POSITION_WINDOW_QUARTERS)

    def test_gross_margin_in_percent(self):
        from cycle import build
        stock = build.build(load("flat_margin"))
        charts = metrics.series_for_chart(stock["quarters"], config)
        for point in charts["gross_margin"]:
            self.assertAlmostEqual(point["value"], 32.0, places=6)

    def test_none_values_preserved(self):
        """缺数据要保留 None（渲染层据此断线），不能填 0。"""
        q = quarters_of(turnover=[None] * 5 + [1.0] * 5)
        charts = metrics.series_for_chart(q, config)
        self.assertIsNone(charts["turnover"][0]["value"])

    def test_short_input(self):
        q = quarters_of(gross_margins=[0.3], revenues=[100.0], op_profits=[20.0],
                        turnover=[1.0])
        charts = metrics.series_for_chart(q, config)
        self.assertEqual(len(charts["gross_margin"]), 1)
        self.assertEqual(len(charts["ratio"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCapacityDirection(unittest.TestCase):
    """判断③ 的产能方向限定语：区分「需求走了」和「产能来了」。"""

    def _q(self, asset_pairs, revenue_pairs):
        """asset_pairs/revenue_pairs 等长，前一半是窗口首季、后一半是末季。"""
        from tests.helpers import quarters_of
        n = len(asset_pairs)
        fa = [a for a, _ in asset_pairs]
        cip = [b for _, b in asset_pairs]
        rev = revenue_pairs
        q = quarters_of(revenues=rev, turnover=[1.0] * n)
        for i, item in enumerate(q):
            item["fixed_asset"] = fa[i]
            item["cip"] = cip[i]
        return q

    def test_expand_when_assets_grow_and_revenue_not_falling(self):
        n = 8
        q = self._q([(1.0e9, 0.0)] * 4 + [(1.3e9, 0.0)] * 4, [1.0e9] * n)
        got = metrics.capacity_direction(q, config)
        self.assertEqual(got["direction"], "expand")
        self.assertAlmostEqual(got["asset_growth"], 30.0, places=4)

    def test_expand_needs_revenue_not_falling(self):
        """资产涨但营收也跌 → 更可能是需求下滑时仍在硬投，不标 expand。"""
        n = 8
        q = self._q([(1.0e9, 0.0)] * 4 + [(1.5e9, 0.0)] * 4,
                    [1.0e9] * 4 + [0.7e9] * 4)
        got = metrics.capacity_direction(q, config)
        self.assertEqual(got["direction"], "flat")

    def test_shrink_when_assets_decline(self):
        n = 8
        q = self._q([(1.0e9, 0.0)] * 4 + [(0.8e9, 0.0)] * 4, [1.0e9] * n)
        got = metrics.capacity_direction(q, config)
        self.assertEqual(got["direction"], "shrink")
        self.assertAlmostEqual(got["asset_growth"], -20.0, places=4)

    def test_flat_in_between(self):
        n = 8
        q = self._q([(1.0e9, 0.0)] * 4 + [(1.05e9, 0.0)] * 4, [1.0e9] * n)
        self.assertEqual(metrics.capacity_direction(q, config)["direction"], "flat")

    def test_boundary_exactly_fifteen_percent_is_expand(self):
        n = 8
        q = self._q([(1.0e9, 0.0)] * 4 + [(1.15e9, 0.0)] * 4, [1.0e9] * n)
        self.assertEqual(metrics.capacity_direction(q, config)["direction"], "expand")

    def test_boundary_exactly_minus_five_is_shrink(self):
        n = 8
        q = self._q([(1.0e9, 0.0)] * 4 + [(0.95e9, 0.0)] * 4, [1.0e9] * n)
        self.assertEqual(metrics.capacity_direction(q, config)["direction"], "shrink")

    def test_too_short_returns_none(self):
        from tests.helpers import quarters_of
        q = quarters_of(revenues=[1.0e9] * 4, turnover=[1.0] * 4)
        for item in q:
            item["fixed_asset"] = 1.0e9
            item["cip"] = 0.0
        got = metrics.capacity_direction(q, config)
        self.assertIsNone(got["direction"])

    def test_missing_assets_returns_none(self):
        from tests.helpers import quarters_of
        q = quarters_of(revenues=[1.0e9] * 8, turnover=[1.0] * 8)
        self.assertIsNone(metrics.capacity_direction(q, config)["direction"])


class TestPositionWindowActual(unittest.TestCase):
    """名义 40 季 ≠ 实际可用季数。"""

    def test_counts_only_non_none(self):
        """取最近 40 季窗口后，数其中有多少季的周转率不是 None。"""
        from tests.helpers import quarters_of
        turnover = [1.0] * 47 + [None] * 3      # 最后 3 季缺失
        q = quarters_of(revenues=[1.0] * 50, turnover=turnover)
        # 窗口 = 最后 40 季，其中含最后 3 个 None → 37 个有效
        self.assertEqual(metrics.position_window_actual(q, config), 37)

    def test_ttm_shortens_window_by_three(self):
        from cycle import build
        stock = build.build(load("full_40q"))
        actual = metrics.position_window_actual(stock["quarters"], config)
        # TTM 需要 4 季滚动，最早 3 季为 None
        self.assertLessEqual(actual, config.POSITION_WINDOW_QUARTERS)
        self.assertGreater(actual, 0)

    def test_empty(self):
        self.assertEqual(metrics.position_window_actual([], config), 0)
