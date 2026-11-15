# -*- coding: utf-8 -*-
"""score.py 的测试 —— 质量分 Q、风险检查、决策矩阵、一票否决。

这是本工具的心脏。它没有任何外部依赖（吃 dict、吐 dict），所以测试可以精确到
「改一个字段、看哪一格分变了」的程度。

测试分四组：
  1. `_quality()` 的组内规则（缺失按 0 分、覆盖度、满分上限）
  2. 各子项的方向性（越大越好 / 越小越好，改坏数据分要下降）
  3. `_fatal()` 与风险闸门（一票否决必须真的否决）
  4. `_decision()`（矩阵查表、硬下限、数据前置条件）

**贯穿全文的断言风格**：不只断言「等于某个数」，还要断言「改坏之后分数下降」。
前者只能证明当前实现对，后者能证明这条规则是真的在起作用 ——
把 `direction` 写反、把阈值写反，前者照样通过，后者会立刻红。
"""
import unittest

from tests import helpers
from screen import score
import config


def q_of(annual, **extra):
    """跑一遍 `_quality()`，返回结果 dict。"""
    return score._quality(helpers.data_with_annual(annual, **extra))


class TestQualityBasics(unittest.TestCase):
    def test_too_few_years_returns_none(self):
        """年报不足 `MIN_ANNUAL_YEARS` 年 → 分数为 None，不是 0。

        None 表示「算不出来」，0 表示「算出来是零分」。前者让上层标「数据不足」，
        后者会让一家上市两年的公司被当成垃圾股。
        """
        q = q_of(helpers.annual_series(config.MIN_ANNUAL_YEARS - 1))
        self.assertIsNone(q["score"])
        self.assertEqual(q["grade"], "数据不足")

    def test_exactly_min_years_is_computable(self):
        q = q_of(helpers.annual_series(config.MIN_ANNUAL_YEARS))
        self.assertIsNotNone(q["score"])

    def test_score_within_0_100(self):
        q = q_of(helpers.annual_series(5))
        self.assertGreaterEqual(q["score"], 0.0)
        self.assertLessEqual(q["score"], 100.0)

    def test_all_five_groups_present(self):
        q = q_of(helpers.annual_series(5))
        self.assertEqual([g["key"] for g in q["groups"]], list(config.QUALITY_WEIGHTS))

    def test_group_score_never_exceeds_weight(self):
        """组内缺失按 0 分计，所以组得分**不可能**超过组权重。"""
        for annual in (helpers.annual_series(5), helpers.annual_series(3)):
            q = q_of(annual)
            for g in q["groups"]:
                with self.subTest(n=len(annual), group=g["key"]):
                    if g["score"] is not None:
                        self.assertLessEqual(g["score"], g["weight"] + 1e-9)

    def test_full_coverage_means_all_items_scored(self):
        q = q_of(helpers.annual_series(5))
        self.assertEqual(q["coverage"], 100.0)

    def test_missing_item_lowers_coverage_not_denominator(self):
        """缺一个子项：覆盖度下降，但分母仍是**全部**子项满分之和。

        这正是 config 注释里那条：缺失按 0 分计入组总分。
        若分母改为「可用子项满分之和」，一家只披露了 ROE 的公司会因此在
        returns 组拿满分 —— 数据越少分越高，是这类实现最典型的坑。
        """
        full = q_of(helpers.annual_series(5))
        # 把 ROIC 全抹掉 → returns 组里的 spread / above_wacc 两项失效
        blank = helpers.annual_series(5, roic=None)
        partial = q_of(blank)

        returns_full = next(g for g in full["groups"] if g["key"] == "returns")
        returns_partial = next(g for g in partial["groups"] if g["key"] == "returns")

        self.assertLess(partial["coverage"], 100.0)
        self.assertLess(returns_partial["score"], returns_full["score"],
                        "缺数据不该让分数变高")
        self.assertEqual(returns_partial["max"], returns_full["max"],
                         "满分上限必须不变")

    def test_all_missing_returns_none_not_zero(self):
        """整组都缺 → 该组 None。0 分和「不知道」要分开。"""
        blank = helpers.annual_series(5, roic=None, roe=None)
        q = q_of(blank)
        returns = next(g for g in q["groups"] if g["key"] == "returns")
        self.assertIsNone(returns["score"])


class TestQualityDirectionality(unittest.TestCase):
    """改坏某个指标，对应的分必须下降 —— 证明 direction 没写反。"""

    def group_score(self, q, key):
        return next(g for g in q["groups"] if g["key"] == key)["score"]

    def item_score(self, q, group, label):
        g = next(x for x in q["groups"] if x["key"] == group)
        for it in g["items"]:
            if it["label"] == label:
                return it["score"]
        return None

    def test_higher_roic_gives_higher_returns(self):
        good = q_of(helpers.annual_series(5, roic=20.0, roe=22.0))
        bad = q_of(helpers.annual_series(5, roic=9.5, roe=5.0))
        self.assertGreater(self.group_score(good, "returns"),
                           self.group_score(bad, "returns"))

    def test_higher_fcf_conversion_gives_higher_cash(self):
        """FCF/净利 越高越好。1.0 满分、0 分下限。"""
        good = q_of(helpers.annual_series(5, fcf=12e8, net_profit=12e8))
        bad = q_of(helpers.annual_series(5, fcf=0.5e8, net_profit=12e8))
        self.assertGreater(self.group_score(good, "cash"),
                           self.group_score(bad, "cash"))

    def test_higher_debt_gives_lower_balance(self):
        """净有息负债/EBIT 越小越好。"""
        light = q_of(helpers.annual_series(5, debt=1e8, cash=30e8))
        heavy = q_of(helpers.annual_series(5, debt=60e8, cash=5e8))
        self.assertGreater(self.group_score(light, "balance"),
                           self.group_score(heavy, "balance"))

    def test_higher_goodwill_gives_lower_balance(self):
        clean = q_of(helpers.annual_series(5, goodwill=0.5e8))
        dirty = q_of(helpers.annual_series(5, goodwill=40e8))
        self.assertGreater(self.group_score(clean, "balance"),
                           self.group_score(dirty, "balance"))

    def test_higher_growth_gives_higher_durability(self):
        fast = q_of(helpers.annual_series(5, growth=0.20))
        flat = q_of(helpers.annual_series(5, growth=0.0))
        self.assertGreater(self.group_score(fast, "durability"),
                           self.group_score(flat, "durability"))

    def test_more_accrual_gives_lower_accounting(self):
        """应计利润（净利 − 经营现金流）/总资产 越小越好。"""
        clean = q_of(helpers.annual_series(5, ocf=13e8, net_profit=12e8))
        dirty = q_of(helpers.annual_series(5, ocf=2e8, net_profit=12e8))
        self.assertGreater(self.group_score(clean, "accounting"),
                           self.group_score(dirty, "accounting"))

    def test_higher_deducted_ratio_gives_higher_accounting(self):
        """扣非占比越高越好（利润不是靠卖资产撑的）。"""
        solid = q_of(helpers.annual_series(5, deducted=11.5e8, net_profit=12e8))
        flimsy = q_of(helpers.annual_series(5, deducted=5e8, net_profit=12e8))
        self.assertGreater(self.group_score(solid, "accounting"),
                           self.group_score(flimsy, "accounting"))


class TestGradeOf(unittest.TestCase):
    def test_none_is_insufficient(self):
        self.assertEqual(score.grade_of(None), "数据不足")

    def test_each_cut_maps_to_its_label(self):
        for cut, label in config.QUALITY_GRADES:
            with self.subTest(cut=cut):
                self.assertEqual(score.grade_of(cut), label)

    def test_just_below_cut_drops_a_grade(self):
        """分档用 `>=`：85.0 是「优秀」，84.99 落到「良好」。"""
        self.assertEqual(score.grade_of(85.0), "优秀")
        self.assertEqual(score.grade_of(84.99), "良好")

    def test_extremes(self):
        self.assertEqual(score.grade_of(0.0), "差")
        self.assertEqual(score.grade_of(100.0), "优秀")


class TestBand(unittest.TestCase):
    """`_band()` 把原始值映射成分数，方向和区间都来自 config。"""

    def _m(self, group):
        return {group: config.QUALITY_ITEM_MAX[group]}

    def test_up_direction_at_bounds(self):
        m = self._m("returns")
        low, high, _ = config.QUALITY_ITEM_RANGES["returns"]["spread"]
        self.assertEqual(score._band("returns", "spread", low, m), 0.0)
        self.assertEqual(score._band("returns", "spread", high, m),
                         config.QUALITY_ITEM_MAX["returns"]["spread"])

    def test_down_direction_at_bounds(self):
        m = self._m("balance")
        low, high, _ = config.QUALITY_ITEM_RANGES["balance"]["net_debt_ebit"]
        # down：值越小越好，下界拿满分
        self.assertEqual(score._band("balance", "net_debt_ebit", low, m),
                         config.QUALITY_ITEM_MAX["balance"]["net_debt_ebit"])
        self.assertEqual(score._band("balance", "net_debt_ebit", high, m), 0.0)

    def test_none_returns_none(self):
        self.assertIsNone(score._band("returns", "spread", None, self._m("returns")))

    def test_out_of_range_clamped(self):
        m = self._m("returns")
        self.assertEqual(score._band("returns", "spread", 999.0, m),
                         config.QUALITY_ITEM_MAX["returns"]["spread"])
        self.assertEqual(score._band("returns", "spread", -999.0, m), 0.0)


class TestGroupAggregation(unittest.TestCase):
    """`_group()` 的算术：分数按「得分/满分 × 权重」缩放。"""

    def test_all_items_full_score(self):
        parts = [{"score": 10, "max": 10}, {"score": 20, "max": 20}]
        g = score._group(parts, "returns")
        self.assertAlmostEqual(g["score"], config.QUALITY_WEIGHTS["returns"])

    def test_half_score(self):
        parts = [{"score": 5, "max": 10}, {"score": 10, "max": 20}]
        g = score._group(parts, "returns")
        expected = 15 / 30 * config.QUALITY_WEIGHTS["returns"]
        self.assertAlmostEqual(g["score"], expected)

    def test_all_missing(self):
        parts = [{"score": None, "max": 10}, {"score": None, "max": 20}]
        g = score._group(parts, "returns")
        self.assertIsNone(g["score"])
        self.assertEqual(g["coverage"], 0.0)

    def test_partial_missing_divides_by_full_max(self):
        """关键：分母用**全部**子项满分，不是可用子项。"""
        parts = [{"score": 10, "max": 10}, {"score": None, "max": 20}]
        g = score._group(parts, "returns")
        self.assertAlmostEqual(g["score"], 10 / 30 * config.QUALITY_WEIGHTS["returns"])
        self.assertAlmostEqual(g["coverage"], 10 / 30 * 100)

    def test_label_and_weight_from_config(self):
        g = score._group([{"score": 1, "max": 1}], "returns")
        self.assertEqual(g["label"], config.QUALITY_GROUP_LABELS["returns"])
        self.assertEqual(g["weight"], config.QUALITY_WEIGHTS["returns"])
        self.assertEqual(g["max"], config.QUALITY_WEIGHTS["returns"])


class TestFatal(unittest.TestCase):
    """一票否决：命中就得否决，不能只是「提示一下」。"""

    def test_no_fatal_on_healthy_company(self):
        data = helpers.data_with_annual(helpers.annual_series(5))
        checks = score._risk_checks(data)
        self.assertEqual(score._fatal(data, checks), [])

    def test_loss_making_with_debt_is_leverage_hit(self):
        """EBIT ≤ 0 时旧逻辑把「净负债/EBIT = 负数」判成通过，等于放过亏损加杠杆。

        实测新五丰：净有息负债 70 亿、EBIT −13 亿、比值 −5.42 → 旧代码「通过」。
        """
        annual = helpers.annual_series(5, ebit=-13e8, debt=80e8, cash=10e8)
        checks = score._risk_checks(helpers.data_with_annual(annual))
        lev = [c for c in checks if c["key"] == "leverage"][0]
        self.assertEqual(lev["status"], "hit", "亏损 + 净有息负债为正 → 命中")

    def test_loss_making_with_net_cash_is_watch_not_hit(self):
        """亏损但净现金：旧逻辑因比值为正（−净负债/−EBIT）误判成「加杠杆」。

        实测航天南湖 61.77、新炬网络 37.79、景嘉微 9.93 都是净现金公司被误判。
        """
        annual = helpers.annual_series(5, ebit=-2e8, debt=1e8, cash=16e8)
        checks = score._risk_checks(helpers.data_with_annual(annual))
        lev = [c for c in checks if c["key"] == "leverage"][0]
        self.assertEqual(lev["status"], "watch", "净现金不该被判加杠杆，也不该发绿勾")

    def test_profitable_leverage_unchanged(self):
        """盈利公司的口径一字不变（0 与 3 两个端点）。"""
        hot = helpers.annual_series(5, ebit=10e8, debt=50e8, cash=5e8)
        lev = [c for c in score._risk_checks(helpers.data_with_annual(hot))
               if c["key"] == "leverage"][0]
        self.assertEqual(lev["status"], "hit")
        calm = helpers.annual_series(5, ebit=10e8, debt=5e8, cash=30e8)
        lev = [c for c in score._risk_checks(helpers.data_with_annual(calm))
               if c["key"] == "leverage"][0]
        self.assertEqual(lev["status"], "clear")

    def test_zero_ebit_is_not_field_missing(self):
        """EBIT 恰好为 0：`div()` 返回 None，但这是「不赚钱」，不是「字段不足」。"""
        annual = helpers.annual_series(5, ebit=0.0, debt=20e8, cash=5e8)
        lev = [c for c in score._risk_checks(helpers.data_with_annual(annual))
               if c["key"] == "leverage"][0]
        self.assertEqual(lev["status"], "hit")
        self.assertNotIn("字段不足", lev["detail"])

    def test_loss_maker_gets_no_credit_on_leverage_item(self):
        """亏损公司在 Q 的「净负债/EBIT」项上不能拿满分（2026-11-15 修）。

        旧行为：净负债>0、EBIT<0 → 比值为负 → `inverse()` 判为「越小越好」的满分，
        亏损加杠杆的公司白拿 8 分，能把 Q 顶过 45 的硬地板。
        """
        annual = helpers.annual_series(5, ebit=-13e8, debt=80e8, cash=10e8)
        q = score._quality(helpers.data_with_annual(annual))
        bal = [g for g in q["groups"] if g["key"] == "balance"][0]
        item = [i for i in bal["items"] if i["label"] == "净负债/EBIT"][0]
        self.assertEqual(item["score"], 0.0, f"不该给分，实际 {item['score']}")
        self.assertIn("亏损", item["raw"])

    def test_loss_maker_with_net_cash_also_gets_no_credit(self):
        """净现金的亏损公司同样 0 分：这一项问的是「赚得到钱吗、靠不靠借钱」。

        给满分会让一家亏损公司靠这一项顶过 Q=45（实测 sh603138 正好被顶到 45.0，
        结论从「回避」松成「数据不足」）。亏损本身由 ROE/ROIC/毛利/现金等项扣。
        """
        annual = helpers.annual_series(5, ebit=-2e8, debt=1e8, cash=16e8)
        q = score._quality(helpers.data_with_annual(annual))
        bal = [g for g in q["groups"] if g["key"] == "balance"][0]
        item = [i for i in bal["items"] if i["label"] == "净负债/EBIT"][0]
        self.assertEqual(item["score"], 0.0)
        self.assertIn("EBIT", item["raw"])

    def test_profitable_company_still_scored_by_ratio(self):
        """盈利公司的这一项一字不变：4.5 倍 → 0 分，−2.5 倍 → 满分。"""
        for debt, cash, want_max in ((50e8, 5e8, False), (5e8, 30e8, True)):
            annual = helpers.annual_series(5, ebit=10e8, debt=debt, cash=cash)
            q = score._quality(helpers.data_with_annual(annual))
            bal = [g for g in q["groups"] if g["key"] == "balance"][0]
            item = [i for i in bal["items"] if i["label"] == "净负债/EBIT"][0]
            if want_max:
                self.assertEqual(item["score"], config.QUALITY_ITEM_MAX["balance"]["net_debt_ebit"])
            else:
                self.assertEqual(item["score"], 0.0)

    def test_low_interest_cover_is_fatal(self):
        """利息保障 < 1：经营利润不够付利息，直接否决。"""
        annual = helpers.annual_series(5, ebit=1e8, finance_expense=2e8)
        data = helpers.data_with_annual(annual)
        checks = score._risk_checks(data)
        fatal = score._fatal(data, checks)
        self.assertTrue(fatal, "利息保障 <1 必须触发一票否决")

    def test_chronic_negative_cfo_with_high_debt_is_fatal(self):
        """连续多年经营现金流为负 + 净负债/EBIT 过高 → 否决。"""
        annual = helpers.annual_series(
            5, ocf=-5e8, ocf_ttm=-5e8, debt=80e8, cash=2e8, ebit=10e8)
        data = helpers.data_with_annual(annual)
        checks = score._risk_checks(data)
        fatal = score._fatal(data, checks)
        self.assertTrue(fatal, "长期失血 + 高杠杆必须否决")

    def test_chronic_negative_cfo_with_loss_ebit_is_fatal(self):
        """EBIT ≤ 0 时比值恒为负，`> 4` 永不成立 → 旧版对亏损公司形同虚设。

        这一类恰恰最该否决：三年经营现金流为负、还在净借债、而且不赚钱。
        """
        annual = helpers.annual_series(
            5, ocf=-5e8, ocf_ttm=-5e8, debt=80e8, cash=2e8, ebit=-3e8,
            finance_expense=0)
        data = helpers.data_with_annual(annual)
        checks = score._risk_checks(data)
        fatal = score._fatal(data, checks)
        self.assertTrue(fatal, "亏损 + 三年失血 + 净负债必须否决")
        self.assertTrue(any("EBIT 为负" in f for f in fatal), fatal)

    def test_chronic_negative_cfo_with_loss_but_net_cash_is_not_fatal(self):
        """亏损但净现金：没靠借钱续命，不该触发这一条（别的项会扣分）。"""
        annual = helpers.annual_series(
            5, ocf=-1e8, ocf_ttm=-1e8, debt=1e8, cash=60e8, ebit=-3e8,
            finance_expense=0)
        data = helpers.data_with_annual(annual)
        checks = score._risk_checks(data)
        self.assertEqual(score._fatal(data, checks), [])

    def test_negative_cfo_alone_is_not_fatal(self):
        """只亏损不欠债，不该被一票否决 —— 那是「观察」，不是「排除」。"""
        annual = helpers.annual_series(
            5, ocf=-1e8, ocf_ttm=-1e8, debt=0.0, cash=50e8, ebit=10e8)
        data = helpers.data_with_annual(annual)
        checks = score._risk_checks(data)
        self.assertEqual(score._fatal(data, checks), [])


class TestRiskChecks(unittest.TestCase):
    def test_always_fourteen_checks(self):
        """无论数据好坏，检查项数量恒为 `RISK_CHECK_COUNT`。

        render 与 validate 都按固定数量渲染/校验；少一项会让整份报告
        在最后一步被 validate 拒掉，而报错信息只说「应为 14 项，实际 13 项」。
        """
        for annual in (helpers.annual_series(5), helpers.annual_series(3),
                       helpers.annual_series(5, roic=None, roe=None)):
            with self.subTest(n=len(annual)):
                checks = score._risk_checks(helpers.data_with_annual(annual))
                self.assertEqual(len(checks), config.RISK_CHECK_COUNT)

    def test_each_check_has_status_and_group(self):
        checks = score._risk_checks(helpers.data_with_annual(helpers.annual_series(5)))
        for c in checks:
            with self.subTest(key=c.get("key")):
                self.assertIn("key", c)
                self.assertIn("group", c)
                self.assertIn(c["group"], config.RISK_GROUPS)

    def test_financial_company_marks_inapplicable(self):
        """银行的毛利率 / 应收 / 存货口径不可比，这些项要标不适用而不是「命中」。"""
        data = helpers.data_with_annual(helpers.annual_series(5))
        data["meta"]["industry"] = "银行"
        checks = score._risk_checks(data)
        by_key = {c["key"]: c for c in checks}
        for key in config.FINANCIAL_INAPPLICABLE_CHECKS:
            with self.subTest(key=key):
                if key in by_key:
                    self.assertEqual(by_key[key].get("status"), "na",
                                     f"金融股的 {key} 应标不适用")

    def test_weak_cash_conversion_flags(self):
        """净现比长期 <0.5 → 现金流检查要报出来。"""
        annual = helpers.annual_series(5, ocf=3e8, ocf_ttm=3e8, net_profit=12e8)
        checks = score._risk_checks(helpers.data_with_annual(annual))
        by_key = {c["key"]: c for c in checks}
        # 风险检查的状态枚举是 clear / watch / hit / unknown / na，
        # 不是 clear/warn/danger。警报一律落 "hit"，轻微提示落 "watch"。
        self.assertIn(by_key["cash_flow"].get("status"), ("watch", "hit"))


class TestIsFinancial(unittest.TestCase):
    def test_keywords_hit(self):
        for kw in config.FINANCIAL_INDUSTRY_KEYWORDS:
            with self.subTest(kw=kw):
                self.assertTrue(score._is_financial({"meta": {"industry": kw}}))

    def test_non_financial(self):
        self.assertFalse(score._is_financial({"meta": {"industry": "白酒"}}))

    def test_missing_industry(self):
        self.assertFalse(score._is_financial({"meta": {}}))
        self.assertFalse(score._is_financial({}))


class TestDecision(unittest.TestCase):
    """`_decision()`：矩阵查表 + 硬下限 + 一致性闸门 + 数据前置条件。"""

    def _decide(self, q, v, fatal=None, *, blockers=None, risks=None):
        return score._decision(
            {"score": q, "grade": score.grade_of(q)},
            {"score": v},
            fatal or [],
            {"buy_blockers": blockers or []},
            risks or {},
        )

    # `risks` 不传时传的是 `{}`，`_gate_level(risks)` 会返回 0 命中/0 提示 → None，
    # 即闸门不触发。买入类结论能出得来。
    # 注意「数据齐全」不等于「pass」，闸门只认 `checks`，不认 `{}` 的其他含义。

    def test_below_hard_floor_always_avoid(self):
        """Q < 45 一律「回避」，V 再高也不行。"""
        d = self._decide(44.9, 100.0)
        self.assertEqual(d["key"], "avoid")

    def test_top_tier_with_high_v_buys(self):
        d = self._decide(80.0, 70.0)
        self.assertEqual(d["key"], "buy")

    def test_top_tier_with_low_v_waits(self):
        d = self._decide(80.0, 30.0)
        self.assertIn(d["key"], ("watch_price", "expensive"))

    def test_fatal_forces_avoid(self):
        d = self._decide(90.0, 95.0, fatal=["利息保障不足"])
        self.assertEqual(d["key"], "avoid")

    def test_fatal_beats_high_scores(self):
        """一票否决优先于任何分数组合。"""
        d = self._decide(100.0, 100.0, fatal=["长期失血"])
        self.assertEqual(d["key"], "avoid")
        self.assertIn("长期失血", d["reason"])

    def test_missing_v_is_unknown(self):
        """V 算不出来 → `unknown`，不是「买」也不是「回避」。

        「不知道」和「不好」必须分开：前者该等数据，后者该放弃。
        """
        d = self._decide(90.0, None)
        self.assertEqual(d["key"], "unknown")
        self.assertNotIn(d["key"], config.BUY_KEYS)

    def test_missing_q_is_unknown(self):
        d = self._decide(None, 95.0)
        self.assertEqual(d["key"], "unknown")

    def test_buy_blockers_downgrade_to_watch(self):
        """矩阵给了买入，但数据不全 → 降为「观察，等待数据完整」。"""
        d = self._decide(90.0, 95.0, blockers=["机构覆盖不足 3 家"])
        self.assertEqual(d["key"], "watch")
        self.assertIn("机构覆盖", d["reason"])

    def test_blockers_do_not_upgrade_avoid(self):
        """已有数据不全不该把「回避」变成别的结论。"""
        d = self._decide(30.0, 95.0, blockers=["机构覆盖不足"])
        self.assertEqual(d["key"], "avoid")

    def test_matrix_is_actually_used(self):
        """对每个档位取代表 Q，验证 V 从高到低时结论按矩阵变化。"""
        for tier in config.DECISION_MATRIX:
            q = tier["q_min"] + 1.0
            top_rule = tier["rules"][0]
            if top_rule["v_min"] is None:
                continue
            with self.subTest(q_min=tier["q_min"]):
                self.assertEqual(self._decide(q, 100.0)["key"], top_rule["key"])
                self.assertEqual(self._decide(q, 0.0)["key"],
                                 tier["rules"][-1]["key"])

    def test_all_matrix_outcomes_reachable(self):
        """矩阵里每个 key 都至少有一组 (Q,V) 能到达 —— 否则那是死代码。

        买入类（`config.BUY_KEYS`）另说：它们要过数据前置条件闸门，而 `_decide`
        默认按「数据齐全」放行（blockers 为空），所以这里不带 blockers 就够到。
        """
        reached = set()
        for tier in config.DECISION_MATRIX:
            q = tier["q_min"] + 1.0
            for rule in tier["rules"]:
                # 每档的范围是 [v_min, 上一档的 v_min)。取 v_min 本身（含端）才落进本档；
                # 取 v_min-0.1 会掉到下一档，兜底档取 0.0。
                v = rule["v_min"] if rule["v_min"] is not None else 0.0
                reached.add(self._decide(q, v)["key"])
        expected = {r["key"] for t in config.DECISION_MATRIX for r in t["rules"]}
        self.assertEqual(reached, expected)

    def test_buy_keys_survive_data_gate(self):
        """买入类结论在数据齐全时要真能给出，不被闸门吞掉。

        反向验证：同一组 (Q,V) 加上 blockers 就该降为「观察」，
        说明矩阵与闸门两条路径都通着，不是矩阵里写了却永远出不来。
        """
        for tier in config.DECISION_MATRIX:
            q = tier["q_min"] + 1.0
            for rule in tier["rules"]:
                if rule["key"] not in config.BUY_KEYS or rule["v_min"] is None:
                    continue
                with self.subTest(key=rule["key"], q_min=tier["q_min"]):
                    self.assertEqual(self._decide(q, rule["v_min"] + 1.0)["key"],
                                     rule["key"])
                    self.assertEqual(
                        self._decide(q, rule["v_min"] + 1.0, blockers=["机构覆盖不足"])["key"],
                        "watch")


class TestComputeScoresShape(unittest.TestCase):
    """`compute_scores()` 的输出结构是 render / validate 的契约。"""

    def test_returns_expected_keys(self):
        data = helpers.data_with_annual(helpers.annual_series(5))
        r = score.compute_scores(data)
        for key in ("quality", "valuation_score", "risks", "decision",
                    "data_quality", "tracking", "meta"):
            with self.subTest(key=key):
                self.assertIn(key, r)

    def test_meta_code_preserved(self):
        data = helpers.data_with_annual(helpers.annual_series(5))
        r = score.compute_scores(data)
        self.assertEqual(r["meta"]["code"], "sh600519")

    def test_risks_have_fourteen_checks(self):
        data = helpers.data_with_annual(helpers.annual_series(5))
        r = score.compute_scores(data)
        self.assertEqual(len(r["risks"]["checks"]), config.RISK_CHECK_COUNT)

    def test_decision_has_label_and_reason(self):
        data = helpers.data_with_annual(helpers.annual_series(5))
        r = score.compute_scores(data)
        self.assertTrue(r["decision"]["label"])
        self.assertTrue(r["decision"]["reason"])

    def test_tracking_is_non_empty_list(self):
        data = helpers.data_with_annual(helpers.annual_series(5))
        r = score.compute_scores(data)
        self.assertIsInstance(r["tracking"], list)
        self.assertTrue(r["tracking"])

    def test_data_quality_reports_coverage(self):
        data = helpers.data_with_annual(helpers.annual_series(5))
        r = score.compute_scores(data)
        self.assertIn("q_coverage", r["data_quality"])
        self.assertEqual(r["data_quality"]["q_coverage"], 100.0)

    def test_data_quality_counts_risk_checks(self):
        """`risk_total` 恒为 14 —— 与 `validate()` 的断言同源。"""
        data = helpers.data_with_annual(helpers.annual_series(5))
        dq = score.compute_scores(data)["data_quality"]
        self.assertEqual(dq["risk_total"], config.RISK_CHECK_COUNT)
        self.assertEqual(dq["risk_known"] + dq["risk_unknown"] + dq["risk_na"],
                         dq["risk_total"])


if __name__ == "__main__":
    unittest.main()
