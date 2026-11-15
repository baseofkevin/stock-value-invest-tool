# -*- coding: utf-8 -*-
"""config.py 的契约测试 —— 防漂移、防「改一半」。

这个文件不测计算，只测**数量与自洽性**。理由：config 里全是阈值和权重，
它们是「同一批数字要多处引用」的重灾区——
  * `QUALITY_ITEM_MAX` 每项满分之和必须等于该组权重，否则该组系统性偏高/偏低；
  * `DECISION_MATRIX` 是结论的唯一来源，档位之间不能有空洞；
  * `VAL_BANDS` 必须覆盖 0~100 且不重叠；
  * `SCORE_BAR_BAD` 必须等于 `QUALITY_HARD_FLOOR`（配色和结论要指向同一条线）。
这些错误**都不会报错**，只会让分数悄悄偏一点、让某个分数区间永远落不到结论。
所以必须靠机械校验钉住。

运行：cd stock_quality_screen && python3 -m unittest discover -s tests -t .
"""
import unittest

from tests import helpers  # noqa: F401  —— 负责把项目根塞进 sys.path，必须最先导入
import config


class TestWeightsSumTo100(unittest.TestCase):
    """权重合计必须恰好 100 —— 不然算出来的分数不在 0~100 刻度上。"""

    def test_quality_weights_sum_to_100(self):
        self.assertEqual(sum(config.QUALITY_WEIGHTS.values()), 100)

    def test_valuation_weights_sum_to_100(self):
        self.assertEqual(sum(config.VALUATION_WEIGHTS.values()), 100)

    def test_quality_weights_all_positive(self):
        for key, w in config.QUALITY_WEIGHTS.items():
            with self.subTest(key=key):
                self.assertGreater(w, 0, f"{key} 的权重必须为正")


class TestItemMaxMatchesGroupWeight(unittest.TestCase):
    """每组子项满分之和 == 该组权重。

    这条最容易坏：`_group()` 里 `score = got / max_possible * weight`，
    而缺失项按 0 分计。若子项满分之和是 32 而组权重是 30，
    那么「全部子项满分」也永远拿不到 30 分 —— 该组被系统性压低 6%。
    反过来则偏高。两种情况都不报错。
    """

    def test_each_group_items_sum_to_weight(self):
        for group, weight in config.QUALITY_WEIGHTS.items():
            with self.subTest(group=group):
                items = config.QUALITY_ITEM_MAX.get(group)
                self.assertIsNotNone(items, f"{group} 没有定义 QUALITY_ITEM_MAX")
                self.assertEqual(sum(items.values()), weight,
                                 f"{group} 子项满分之和 {sum(items.values())} != 组权重 {weight}")

    def test_quality_item_max_covers_all_groups(self):
        """QUALITY_ITEM_MAX 的键集合必须与 QUALITY_WEIGHTS 完全一致。"""
        self.assertEqual(set(config.QUALITY_ITEM_MAX), set(config.QUALITY_WEIGHTS))

    def test_group_labels_cover_all_groups(self):
        """组的中文名不多不少 —— 少一个会在 `_group()` 里 KeyError。"""
        self.assertEqual(set(config.QUALITY_GROUP_LABELS), set(config.QUALITY_WEIGHTS))


class TestItemRanges(unittest.TestCase):
    """打分区间的形状：必须是 (low, high, direction)、low < high、direction 合法。"""

    def test_ranges_are_well_formed(self):
        for group, items in config.QUALITY_ITEM_RANGES.items():
            for item, spec in items.items():
                with self.subTest(group=group, item=item):
                    self.assertEqual(len(spec), 3, f"{group}.{item} 应为 (low, high, direction)")
                    low, high, direction = spec
                    self.assertLess(low, high, f"{group}.{item} 的 low 必须 < high")
                    self.assertIn(direction, ("up", "down"))

    def test_ranges_only_reference_known_items(self):
        """区间表里出现的子项必须真的在 QUALITY_ITEM_MAX 里存在。"""
        for group, items in config.QUALITY_ITEM_RANGES.items():
            with self.subTest(group=group):
                self.assertIn(group, config.QUALITY_ITEM_MAX)
                for item in items:
                    self.assertIn(item, config.QUALITY_ITEM_MAX[group],
                                  f"{group}.{item} 有区间定义但 QUALITY_ITEM_MAX 里没有")

    def test_ranges_do_not_cover_ratio_items(self):
        """比值型子项（fcf_years / above_wacc / accrual 之外的年数类）不该有区间。

        `_band()` 只处理有区间的项；比值项按「已知年数里占几成」单独算。
        如果给它们也加了区间，`_band()` 会走到，但 `_quality()` 里另一条分支
        才是真正生效的 —— 两份数值打架且没人发现。
        """
        ratio_items = {("cash", "fcf_years"), ("returns", "above_wacc")}
        for group, item in ratio_items:
            with self.subTest(item=item):
                self.assertNotIn(item, config.QUALITY_ITEM_RANGES.get(group, {}),
                                 f"{item} 是比值型，不该有打分区间")


class TestGradesAndBands(unittest.TestCase):
    """分档表必须从高到低、覆盖 0 分、且只有最后一段兜底到 0。"""

    def test_grades_descending(self):
        cuts = [cut for cut, _ in config.QUALITY_GRADES]
        self.assertEqual(cuts, sorted(cuts, reverse=True), "分档必须从高到低")

    def test_grades_reach_zero(self):
        self.assertEqual(config.QUALITY_GRADES[-1][0], 0.0,
                         "最后一档必须从 0 开始，否则 0~最低档之间的分数没有名字")

    def test_grade_labels_unique(self):
        labels = [label for _, label in config.QUALITY_GRADES]
        self.assertEqual(len(labels), len(set(labels)), "档名不能重复")

    def test_score_bar_bad_equals_hard_floor(self):
        """红色分界必须与「禁止买入」的硬下限是同一条线。

        两处若不等，会出现「分数条画成红色、结论却说可以买」——读者会怀疑算错了。
        config 注释里也写了这条约束。
        """
        self.assertEqual(config.SCORE_BAR_BAD, config.QUALITY_HARD_FLOOR)

    def test_score_bar_good_above_bad(self):
        self.assertGreater(config.SCORE_BAR_GOOD, config.SCORE_BAR_BAD)


class TestValBands(unittest.TestCase):
    """七档分位必须首尾相接、覆盖 0~100，不能有空洞也不能重叠。"""

    def test_bands_start_at_zero(self):
        self.assertEqual(config.VAL_BANDS[0][0], 0.0)

    def test_bands_end_covers_100(self):
        self.assertGreaterEqual(config.VAL_BANDS[-1][1], 100.0)

    def test_bands_are_contiguous(self):
        for i in range(len(config.VAL_BANDS) - 1):
            with self.subTest(i=i):
                self.assertEqual(config.VAL_BANDS[i][1], config.VAL_BANDS[i + 1][0],
                                 f"第 {i} 档上界与第 {i+1} 档下界之间有缝隙")

    def test_band_labels_and_verdicts_present(self):
        for band in config.VAL_BANDS:
            with self.subTest(band=band[2]):
                self.assertEqual(len(band), 4)
                self.assertTrue(band[2], "档名不能为空")
                self.assertTrue(band[3], "结论词不能为空")

    def test_spread_tolerance_sane(self):
        self.assertLess(config.VAL_SPREAD_TOL, config.VAL_SPREAD_STRONG,
                        "「同一位置」的门槛必须小于「明显分歧」的门槛")


class TestDecisionMatrix(unittest.TestCase):
    """决策矩阵是本工具结论的唯一来源，结构必须严格。"""

    def test_matrix_descending_by_q_min(self):
        qs = [tier["q_min"] for tier in config.DECISION_MATRIX]
        self.assertEqual(qs, sorted(qs, reverse=True), "Q 门槛必须从高到低")

    def test_matrix_lowest_tier_equals_hard_floor(self):
        """矩阵最低档的 q_min 必须就是 QUALITY_HARD_FLOOR。

        低于它由 `_decision()` 直接返回「回避」，到不了矩阵。若矩阵最低档比它低，
        那一档永远进不去；比它高，就会有一段 Q 值落不进任何档。
        """
        self.assertEqual(config.DECISION_MATRIX[-1]["q_min"], config.QUALITY_HARD_FLOOR)

    def test_each_tier_has_fallback_rule(self):
        """每档必须有 v_min=None 的兜底，否则 V 低于最低阈值时无结论可给。"""
        for tier in config.DECISION_MATRIX:
            with self.subTest(q_min=tier["q_min"]):
                self.assertIsNone(tier["rules"][-1]["v_min"],
                                  "最后一档规则的 v_min 必须是 None（兜底）")

    def test_rules_descending_by_v_min(self):
        for tier in config.DECISION_MATRIX:
            v_mins = [r["v_min"] for r in tier["rules"] if r["v_min"] is not None]
            with self.subTest(q_min=tier["q_min"]):
                self.assertEqual(v_mins, sorted(v_mins, reverse=True),
                                 "同一档内 V 门槛必须从高到低")

    def test_rules_have_required_fields(self):
        for tier in config.DECISION_MATRIX:
            for rule in tier["rules"]:
                with self.subTest(q_min=tier["q_min"], key=rule.get("key")):
                    for field in ("key", "label", "reason", "text"):
                        self.assertTrue(rule.get(field), f"规则缺 {field}")

    def test_buy_keys_exist_in_matrix(self):
        """BUY_KEYS 里每个 key 都必须真能在矩阵里出现，否则那条判断永假。"""
        keys = {r["key"] for tier in config.DECISION_MATRIX for r in tier["rules"]}
        for key in config.BUY_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, keys, f"BUY_KEYS 里的 {key} 在矩阵中不存在")


class TestRiskChecks(unittest.TestCase):
    def test_risk_group_count_matches_checks(self):
        """14 项检查分 4 组 —— 组名列表不能空。"""
        self.assertEqual(len(config.RISK_GROUPS), 4)
        self.assertEqual(config.RISK_CHECK_COUNT, 14)

    def test_financial_inapplicable_subset_of_groups(self):
        """标不适用的检查项名不能是空字符串（否则 `in` 判断会误命中）。"""
        for name in config.FINANCIAL_INAPPLICABLE_CHECKS:
            with self.subTest(name=name):
                self.assertTrue(name.strip())

    def test_risk_headline_keys_are_strings(self):
        self.assertTrue(all(isinstance(k, str) and k for k in config.RISK_HEADLINE_KEYS))

    def test_goodwill_thresholds_ordered(self):
        """三档商誉线必须递增：WATCH < HIT < FATAL。"""
        self.assertLess(config.GOODWILL_EQUITY_WATCH, config.GOODWILL_EQUITY_HIT)
        self.assertLess(config.GOODWILL_EQUITY_HIT, config.GOODWILL_EQUITY_FATAL)

    def test_fatal_interest_cover_below_min(self):
        """一票否决的利息保障线必须比「关注线」更严。"""
        self.assertLess(config.INTEREST_COVER_FATAL, config.INTEREST_COVER_MIN)

    def test_cash_ni_ladder_ordered(self):
        """净现比四档必须递减：GOOD > OK > WEAK，且两年线低于单年 OK 线。"""
        self.assertGreater(config.CASH_NI_GOOD, config.CASH_NI_OK)
        self.assertGreater(config.CASH_NI_OK, config.CASH_NI_WEAK)
        self.assertLess(config.CASH_NI_TWO_YEAR, config.CASH_NI_OK)

    def test_cash_debt_ladder_ordered(self):
        self.assertGreater(config.CASH_DEBT_RATIO_MIN, config.CASH_DEBT_RATIO_FATAL)
        self.assertGreater(config.CASH_DEBT_RATIO_CALM, config.CASH_DEBT_RATIO_MIN)


class TestBuyPreconditions(unittest.TestCase):
    def test_critical_checks_are_non_empty_strings(self):
        self.assertTrue(config.BUY_CRITICAL_CHECKS)
        for name in config.BUY_CRITICAL_CHECKS:
            with self.subTest(name=name):
                self.assertTrue(name.strip())

    def test_min_coverage_in_percent_range(self):
        self.assertGreater(config.BUY_MIN_QUALITY_COVERAGE, 0)
        self.assertLessEqual(config.BUY_MIN_QUALITY_COVERAGE, 100)

    def test_min_institution_coverage_positive(self):
        self.assertGreater(config.BUY_MIN_INSTITUTION_COVERAGE, 0)


class TestWindows(unittest.TestCase):
    def test_trend_quarters_is_years_times_four(self):
        self.assertEqual(config.TREND_QUARTERS, config.TREND_YEARS * 4)

    def test_min_annual_years_positive(self):
        self.assertGreater(config.MIN_ANNUAL_YEARS, 1)

    def test_cash_anchor_years_ascending(self):
        self.assertEqual(list(config.CASH_ANCHOR_YEARS),
                         sorted(config.CASH_ANCHOR_YEARS))

    def test_val_min_sample_is_one_year(self):
        self.assertEqual(config.VAL_MIN_SAMPLE_WEEKS, 52)

    def test_wacc_above_risk_free(self):
        self.assertGreater(config.WACC, config.RISK_FREE)


class TestNoDeadConstants(unittest.TestCase):
    """删掉的东西不该被悄悄加回来（与 cycle-elastic 同一思路）。"""

    def test_removed_buy_min_v_not_reintroduced(self):
        """`BUY_MIN_V` 在 0.9.8 删掉：与 DECISION_MATRIX 是同一批数字，两份必然漂移。"""
        self.assertFalse(hasattr(config, "BUY_MIN_V"))

    def test_removed_decision_rules_not_reintroduced(self):
        """`DECISION_RULES` 在 0.9.8 删掉：无人读取且与矩阵已不一致。"""
        self.assertFalse(hasattr(config, "DECISION_RULES"))

    def test_version_present(self):
        self.assertTrue(config.VERSION)
        self.assertRegex(config.VERSION, r"^\d+\.\d+\.\d+$")


class TestPaths(unittest.TestCase):
    def test_output_dir_points_to_shared_output(self):
        """产物落 `<项目根>/output/quality-screen`，不落源码目录。"""
        from pathlib import Path
        out = Path(config.DEFAULT_OUT_DIR)
        self.assertEqual(out.parent.parent, Path(helpers.HUB))
        self.assertEqual(out.name, "quality-screen")

    def test_output_dir_not_in_source_tree(self):
        from pathlib import Path
        self.assertFalse(Path(config.DEFAULT_OUT_DIR).is_relative_to(Path(config.PROJECT_DIR)),
                         "产物不该落回 stock_quality_screen/ 里面")

    def test_hub_dir_is_parent_of_project_dir(self):
        from pathlib import Path
        self.assertEqual(Path(config.HUB_DIR), Path(config.PROJECT_DIR).parent)


if __name__ == "__main__":
    unittest.main()
