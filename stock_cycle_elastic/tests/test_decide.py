"""阈值 → 标签（README.md「测试」）。

四类断言：
  1. **5 个 key 恰好**，多一个就红（防旧字段回流）；
  2. **三个枚举值域**，遍历所有输入组合证明输出永远合法；
  3. **`na` 不是 `0`/`False`/`weak`** —— 数据缺失必须能区分出来；
  4. **四个边界值**的开关区间（5.0/2.0 闭，40.0/60.0 开）。

最后一条最重要：边界方向反了，日常数据里看不出来，只有恰好踩线时才暴露，
而那时结论已经悄悄反了。
"""

import itertools
import unittest

import tests.helpers  # noqa: F401
import config
from cycle import decide

from tests.helpers import Cfg, metrics_of, quarters_of


def _metrics(gm_std=None, ratio=None, reliable=None, samples=8,
             pct=None, pos_samples=40):
    return metrics_of(gm_std=gm_std, ratio=ratio, reliable=reliable,
                      samples=samples, pct=pct, pos_samples=pos_samples)


#: 20 季（默认不算"历史不足"）
TWENTY = quarters_of(gross_margins=[0.3] * 20, revenues=[1.0] * 20,
                     op_profits=[0.3] * 20, turnover=[1.0] * 20)


class TestOutputShape(unittest.TestCase):
    """§11.3：没有 `pool`、没有 `risk`（旧字段不许回流）。"""

    def test_exactly_five_keys(self):
        d = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=3.0, reliable=True,
                                           pct=20.0), "有色金属", config)
        self.assertEqual(set(d.keys()), {"cycle", "elastic", "position", "flags", "meta"})
        self.assertEqual(len(d), 5)
        self.assertEqual(set(d.keys()), set(decide.DECIDE_KEYS))

    def test_no_legacy_fields(self):
        d = decide.decide(TWENTY, _metrics(gm_std=6.0), "", config)
        for legacy in ("pool", "risk", "score", "grade", "rank", "total_score"):
            self.assertNotIn(legacy, d)

    def test_sub_dicts_have_two_keys(self):
        d = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=3.0, reliable=True,
                                           pct=20.0), "", config)
        self.assertEqual(set(d["cycle"].keys()),
                         {"label", "gm_std", "gm_cv", "gm_median", "channel"})
        self.assertEqual(set(d["elastic"].keys()), {"label", "ratio", "reason"})
        self.assertEqual(set(d["position"].keys()),
                         {"label", "pct", "capacity_direction",
                          "asset_growth", "revenue_growth"})
        self.assertEqual(set(d["flags"].keys()), {"financial", "insufficient_history"})
        self.assertEqual(set(d["meta"].keys()), {"quarters"})


class TestCycle(unittest.TestCase):
    """判断①：`gm_std >= 5.0` → 1（**闭区间**）。"""

    def test_above_threshold(self):
        d = decide.decide(TWENTY, _metrics(gm_std=6.0), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_YES)

    def test_exactly_five_is_yes(self):
        """恰好 5.0 → **是**周期股（`>=`，闭区间）。"""
        d = decide.decide(TWENTY, _metrics(gm_std=5.0), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_YES)

    def test_just_below_five_is_no(self):
        d = decide.decide(TWENTY, _metrics(gm_std=4.9999), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_NO)

    def test_zero_is_no_not_na(self):
        """σ=0（毛利率完全平稳）是**明确结论**"非周期"，不是缺数据。"""
        d = decide.decide(TWENTY, _metrics(gm_std=0.0), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_NO)
        self.assertIsNotNone(d["cycle"]["label"])
        self.assertNotEqual(d["cycle"]["label"], decide.NA)

    def test_none_is_na(self):
        d = decide.decide(TWENTY, _metrics(gm_std=None), "", config)
        self.assertEqual(d["cycle"]["label"], decide.NA)
        self.assertIsNone(d["cycle"]["gm_std"])

    def test_value_passthrough(self):
        d = decide.decide(TWENTY, _metrics(gm_std=7.25), "", config)
        self.assertAlmostEqual(d["cycle"]["gm_std"], 7.25)


class TestCycleCvChannel(unittest.TestCase):
    """判断① 通道 B：相对波动 CV。

    典型场景：毛利率只有 15% 的公司摆动 4pp，σ 到不了 5pp，但相对幅度很大。
    """

    @staticmethod
    def _m(gm_std, cv, median=15.0):
        return {
            "gm_std": gm_std,
            "gm_cv": {"cv": cv, "median": median, "samples": 20},
            "elasticity": {"ratio": None, "reliable": None, "samples": 0},
            "position": {"pct": None, "current": None, "samples": 0},
        }

    def test_cv_channel_makes_yes_when_std_fails(self):
        d = decide.decide(TWENTY, self._m(3.8, 23.5), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_YES)
        self.assertEqual(d["cycle"]["channel"], "cv")

    def test_exactly_twenty_percent_is_yes(self):
        """CV 边界也是闭区间：恰好 20% 判「是」。"""
        d = decide.decide(TWENTY, self._m(3.5, 20.0), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_YES)
        self.assertEqual(d["cycle"]["channel"], "cv")

    def test_just_below_twenty_percent_is_no(self):
        d = decide.decide(TWENTY, self._m(3.5, 19.99), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_NO)
        self.assertIsNone(d["cycle"]["channel"])

    def test_std_wins_when_both_pass(self):
        """两条都过时记 "std" —— 绝对口径更权威，也更少需要解释。"""
        d = decide.decide(TWENTY, self._m(6.0, 40.0), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_YES)
        self.assertEqual(d["cycle"]["channel"], "std")

    def test_cv_none_falls_back_to_std_only(self):
        """cv 为 None（守卫拦下）时只看通道 A，不能当成「否」。"""
        d = decide.decide(TWENTY, self._m(4.9, None), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_NO)
        self.assertIsNone(d["cycle"]["channel"])

    def test_std_none_is_still_na_even_if_cv_present(self):
        """σ 算不出来 → 样本不足 → na。CV 也来自同一批样本，不能单独立论。"""
        d = decide.decide(TWENTY, self._m(None, 50.0), "", config)
        self.assertEqual(d["cycle"]["label"], decide.NA)

    def test_missing_gm_cv_key_is_tolerated(self):
        """老 metrics 结构（没有 gm_cv）不能让判定崩掉。"""
        m = self._m(3.8, 40.0)
        del m["gm_cv"]
        d = decide.decide(TWENTY, m, "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_NO)


class TestContradictionBadge(unittest.TestCase):
    """①否 且 ②强 → 加提示标记（不改判定）。"""

    def _decide(self, cycle_std, ratio, reliable=True):
        return decide.decide(TWENTY,
                             _metrics(gm_std=cycle_std, ratio=ratio, reliable=reliable),
                             "", config)

    def test_badge_present_when_no_and_strong(self):
        d = self._decide(3.0, 5.0)
        texts = [t for _c, t in decide.badges(d)]
        self.assertIn(decide.BADGE_CONTRADICTION, texts)

    def test_no_badge_when_yes_and_strong(self):
        d = self._decide(6.0, 5.0)
        texts = [t for _c, t in decide.badges(d)]
        self.assertNotIn(decide.BADGE_CONTRADICTION, texts)

    def test_no_badge_when_no_and_weak(self):
        d = self._decide(3.0, 1.0)
        texts = [t for _c, t in decide.badges(d)]
        self.assertNotIn(decide.BADGE_CONTRADICTION, texts)

    def test_badge_does_not_change_labels(self):
        d = self._decide(3.0, 5.0)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_NO)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_STRONG)


class TestCvChannelBadge(unittest.TestCase):
    """通道 B 过的周期股要多一个小标记。"""

    def test_cv_channel_badge(self):
        m = {
            "gm_std": 3.8,
            "gm_cv": {"cv": 23.5, "median": 16.2, "samples": 20},
            "elasticity": {"ratio": None, "reliable": None, "samples": 0},
            "position": {"pct": None, "current": None, "samples": 0},
        }
        d = decide.decide(TWENTY, m, "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertIn(decide.BADGE_CV_CHANNEL, texts)

    def test_std_channel_has_no_cv_badge(self):
        d = decide.decide(TWENTY, _metrics(gm_std=6.0), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertNotIn(decide.BADGE_CV_CHANNEL, texts)


class TestElastic(unittest.TestCase):
    """判断②：`ratio >= 2.0` → strong（闭区间）；`reliable=False` → unstable 优先。"""

    def test_strong(self):
        d = decide.decide(TWENTY, _metrics(ratio=3.0, reliable=True), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_STRONG)

    def test_exactly_two_is_strong(self):
        """恰好 2.0 → **强**（`>=`，闭区间）。"""
        d = decide.decide(TWENTY, _metrics(ratio=2.0, reliable=True), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_STRONG)

    def test_just_below_two_is_weak(self):
        d = decide.decide(TWENTY, _metrics(ratio=1.99, reliable=True), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_WEAK)

    def test_unstable_overrides_strong(self):
        """**最重要的一条**：比值 5 但不可信 → 标 unstable，不能标 strong。

        营收长期不动的公司算出来的比值会爆炸，那个爆炸不是"弹性强"。
        """
        d = decide.decide(TWENTY, _metrics(ratio=5.0, reliable=False), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_UNSTABLE)
        self.assertNotEqual(d["elastic"]["label"], decide.ELASTIC_STRONG,
                            "不可信时绝不能给强标签")

    def test_unstable_overrides_weak(self):
        """同样不能标 weak —— 没说它弱，是说不稳。"""
        d = decide.decide(TWENTY, _metrics(ratio=0.5, reliable=False), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_UNSTABLE)
        self.assertNotEqual(d["elastic"]["label"], decide.ELASTIC_WEAK)

    def test_ratio_none_is_na(self):
        d = decide.decide(TWENTY, _metrics(ratio=None, reliable=None), "", config)
        self.assertEqual(d["elastic"]["label"], decide.NA)

    def test_reliable_none_gives_na(self):
        """比值有值但拿不到同比 → 不敢下结论 → na（不是 weak）。"""
        d = decide.decide(TWENTY, _metrics(ratio=3.0, reliable=None), "", config)
        self.assertEqual(d["elastic"]["label"], decide.NA)
        self.assertAlmostEqual(d["elastic"]["ratio"], 3.0)

    def test_negative_ratio_is_weak(self):
        d = decide.decide(TWENTY, _metrics(ratio=-1.5, reliable=True), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_WEAK)

    def test_zero_ratio_is_weak(self):
        """比值 0 是明确结论，不是 na。"""
        d = decide.decide(TWENTY, _metrics(ratio=0.0, reliable=True), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_WEAK)


class TestPosition(unittest.TestCase):
    """判断③：`< 40` low，`> 60` high，中间 neutral。**两个边界都是开区间**。"""

    def test_low(self):
        d = decide.decide(TWENTY, _metrics(pct=20.0), "", config)
        self.assertEqual(d["position"]["label"], decide.POSITION_LOW)

    def test_high(self):
        d = decide.decide(TWENTY, _metrics(pct=80.0), "", config)
        self.assertEqual(d["position"]["label"], decide.POSITION_HIGH)

    def test_exactly_40_is_neutral(self):
        """**40.0 → 中性，不是低位**（开区间，规则是 `< 40`）。"""
        d = decide.decide(TWENTY, _metrics(pct=40.0), "", config)
        self.assertEqual(d["position"]["label"], decide.POSITION_NEUTRAL)

    def test_39_9_is_low(self):
        d = decide.decide(TWENTY, _metrics(pct=39.9), "", config)
        self.assertEqual(d["position"]["label"], decide.POSITION_LOW)

    def test_exactly_60_is_neutral(self):
        """**60.0 → 中性，不是高位**（开区间，规则是 `> 60`）。"""
        d = decide.decide(TWENTY, _metrics(pct=60.0), "", config)
        self.assertEqual(d["position"]["label"], decide.POSITION_NEUTRAL)

    def test_60_1_is_high(self):
        d = decide.decide(TWENTY, _metrics(pct=60.1), "", config)
        self.assertEqual(d["position"]["label"], decide.POSITION_HIGH)

    def test_fifty_is_neutral(self):
        """全等值 → 50 → 中性。"""
        d = decide.decide(TWENTY, _metrics(pct=50.0), "", config)
        self.assertEqual(d["position"]["label"], decide.POSITION_NEUTRAL)

    def test_none_is_na(self):
        d = decide.decide(TWENTY, _metrics(pct=None), "", config)
        self.assertEqual(d["position"]["label"], decide.NA)
        self.assertIsNone(d["position"]["pct"])


class TestFinancial(unittest.TestCase):

    def test_bank_gets_na_position(self):
        d = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=3.0, reliable=True,
                                           pct=20.0), "银行", config)
        self.assertTrue(d["flags"]["financial"])
        self.assertEqual(d["position"]["label"], decide.NA)
        # 另两个判断照算 —— 银行也可以有周期性、有经营杠杆
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_YES)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_STRONG)

    def test_financial_keywords_substring(self):
        for industry in ("银行", "股份制银行", "保险及其他", "证券Ⅱ", "多元金融"):
            with self.subTest(industry=industry):
                self.assertTrue(decide.is_financial(industry, config), industry)

    def test_non_financial(self):
        for industry in ("有色金属", "白酒", "半导体", "房地产", ""):
            with self.subTest(industry=industry):
                self.assertFalse(decide.is_financial(industry, config), industry)

    def test_none_industry(self):
        self.assertFalse(decide.is_financial(None, config))


class TestIndependentReadings(unittest.TestCase):
    """§11.3：`cycle=False` 时后两个**照算照标**。

    用户要的是三个独立读数 —— 不是周期股不代表不该看它在周期里的位置。
    """

    def test_non_cycle_still_gets_elastic_and_position(self):
        d = decide.decide(TWENTY, _metrics(gm_std=1.0, ratio=3.0, reliable=True,
                                           pct=20.0), "", config)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_NO)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_STRONG,
                         "不是周期股不隐藏弹性读数")
        self.assertEqual(d["position"]["label"], decide.POSITION_LOW,
                         "不是周期股不隐藏位置读数")


class TestInsufficientHistory(unittest.TestCase):

    def test_short_history_flag(self):
        short = quarters_of(gross_margins=[0.3] * 12, revenues=[1.0] * 12,
                            op_profits=[0.3] * 12, turnover=[1.0] * 12)
        d = decide.decide(short, _metrics(gm_std=1.0, ratio=3.0, reliable=True,
                                          pct=20.0), "", config)
        self.assertTrue(d["flags"]["insufficient_history"])
        self.assertEqual(d["meta"]["quarters"], 12)
        # 三个判断仍给出值（有数据就标，只是加个标记）
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_STRONG)

    def test_exactly_twenty_not_insufficient(self):
        d = decide.decide(TWENTY, _metrics(gm_std=1.0), "", config)
        self.assertFalse(d["flags"]["insufficient_history"])

    def test_nineteen_is_insufficient(self):
        q = quarters_of(gross_margins=[0.3] * 19, revenues=[1.0] * 19,
                        op_profits=[0.3] * 19, turnover=[1.0] * 19)
        d = decide.decide(q, _metrics(gm_std=1.0), "", config)
        self.assertTrue(d["flags"]["insufficient_history"])

    def test_empty_quarters(self):
        d = decide.decide([], _metrics(), "", config)
        self.assertTrue(d["flags"]["insufficient_history"])
        self.assertEqual(d["meta"]["quarters"], 0)
        self.assertEqual(d["cycle"]["label"], decide.NA)
        self.assertEqual(d["elastic"]["label"], decide.NA)
        self.assertEqual(d["position"]["label"], decide.NA)


class TestEnumCompleteness(unittest.TestCase):
    """**枚举完备性**：遍历所有 `{值, None}` 组合，输出永远落在合法值域内。

    这条用例的价值在于"以后加字段忘了加到值域"这类错误会被自动抓出来。
    """

    def test_all_combinations_in_range(self):
        gm_cases = (None, 0.0, 4.9, 5.0, 6.0)
        ratio_cases = (None, 0.0, 1.9, 2.0, 3.0)
        reliable_cases = (None, True, False)
        pct_cases = (None, 0.0, 39.9, 40.0, 50.0, 60.0, 60.1, 100.0)

        checked = 0
        for gm, ratio, reliable, pct in itertools.product(
                gm_cases, ratio_cases, reliable_cases, pct_cases):
            m = _metrics(gm_std=gm, ratio=ratio, reliable=reliable, pct=pct)
            d = decide.decide(TWENTY, m, "", config)

            self.assertIn(d["cycle"]["label"], decide.CYCLE_LABELS,
                          f"cycle 越界：gm={gm}")
            self.assertIn(d["elastic"]["label"], decide.ELASTIC_LABELS,
                          f"elastic 越界：ratio={ratio} reliable={reliable}")
            self.assertIn(d["position"]["label"], decide.POSITION_LABELS,
                          f"position 越界：pct={pct}")
            checked += 1

        self.assertEqual(checked, len(gm_cases) * len(ratio_cases)
                         * len(reliable_cases) * len(pct_cases))

    def test_label_types(self):
        """cycle 的标签只能是 int 1/0 或字符串 "na"，不能是 bool。"""
        for gm in (None, 0.0, 6.0):
            d = decide.decide(TWENTY, _metrics(gm_std=gm), "", config)
            label = d["cycle"]["label"]
            self.assertNotIsInstance(label, bool)
            self.assertIn(label, (1, 0, "na"))

    def test_financial_combination(self):
        for gm, ratio, reliable, pct in itertools.product(
                (None, 6.0), (None, 3.0), (None, True), (None, 20.0, 80.0)):
            m = _metrics(gm_std=gm, ratio=ratio, reliable=reliable, pct=pct)
            d = decide.decide(TWENTY, m, "银行", config)
            self.assertEqual(d["position"]["label"], decide.NA)
            self.assertIn(d["cycle"]["label"], decide.CYCLE_LABELS)
            self.assertIn(d["elastic"]["label"], decide.ELASTIC_LABELS)


class TestNaIsNotFalse(unittest.TestCase):
    """§11.4 第 3 条：`None` 不是 `False`。"""

    def test_na_string_not_falsy_confusion(self):
        d = decide.decide(TWENTY, _metrics(gm_std=None, ratio=None, pct=None),
                          "", config)
        self.assertIs(d["cycle"]["label"], decide.NA)
        self.assertIs(d["elastic"]["label"], decide.NA)
        self.assertIs(d["position"]["label"], decide.NA)
        self.assertNotEqual(d["cycle"]["label"], decide.CYCLE_NO)
        self.assertNotEqual(d["elastic"]["label"], decide.ELASTIC_WEAK)
        self.assertNotEqual(d["position"]["label"], decide.POSITION_NEUTRAL)

    def test_has_any_na(self):
        full = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=3.0, reliable=True,
                                              pct=20.0), "", config)
        self.assertFalse(decide.has_any_na(full))

        partial = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=3.0, reliable=True,
                                                 pct=None), "", config)
        self.assertTrue(decide.has_any_na(partial))


class TestBadges(unittest.TestCase):

    def test_full_positive(self):
        d = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=3.0, reliable=True,
                                           pct=20.0), "", config)
        items = decide.badges(d)
        texts = [t for _c, t in items]
        self.assertEqual(texts, ["周期股", "弹性强", "低位"])

    def test_full_negative(self):
        d = decide.decide(TWENTY, _metrics(gm_std=1.0, ratio=1.0, reliable=True,
                                           pct=80.0), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertEqual(texts, ["非周期", "弹性弱", "高位"])

    def test_neutral_position_has_no_badge(self):
        """40~60 之间不给位置徽章 —— 中间地带没信息。"""
        d = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=3.0, reliable=True,
                                           pct=50.0), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertEqual(texts, ["周期股", "弹性强"])

    def test_unstable_badge_replaces_strong(self):
        d = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=5.0, reliable=False,
                                           pct=20.0), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertIn("比值不稳定", texts)
        self.assertNotIn("弹性强", texts)
        self.assertNotIn("弹性弱", texts)

    def test_financial_badge(self):
        d = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=3.0, reliable=True,
                                           pct=20.0), "银行", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertIn("产能不适用", texts)
        self.assertNotIn("低位", texts)
        self.assertNotIn("高位", texts)

    def test_insufficient_history_badge(self):
        short = quarters_of(gross_margins=[0.3] * 12, revenues=[1.0] * 12,
                            op_profits=[0.3] * 12, turnover=[1.0] * 12)
        d = decide.decide(short, _metrics(gm_std=1.0, ratio=3.0, reliable=True,
                                          pct=20.0), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertIn("历史不足", texts)
        self.assertIn("非周期", texts, "历史不足不取代判断① 的徽章")

    def test_badge_order_is_fixed(self):
        """顺序固定：周期性 → 弹性 → 位置 → 辅助标记。"""
        d = decide.decide(TWENTY, _metrics(gm_std=6.0, ratio=3.0, reliable=True,
                                           pct=20.0), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertEqual(texts.index("周期股"), 0)
        self.assertEqual(texts.index("弹性强"), 1)
        self.assertEqual(texts.index("低位"), 2)

    def test_no_badges_when_all_na(self):
        d = decide.decide([], _metrics(), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertNotIn("周期股", texts)
        self.assertNotIn("非周期", texts)
        self.assertIn("历史不足", texts)


class TestTextMaps(unittest.TestCase):

    def test_all_labels_have_text(self):
        for label in decide.CYCLE_LABELS:
            self.assertIn(label, decide.CYCLE_TEXT)
        for label in decide.ELASTIC_LABELS:
            self.assertIn(label, decide.ELASTIC_TEXT)
        for label in decide.POSITION_LABELS:
            self.assertIn(label, decide.POSITION_TEXT)

    def test_na_text_is_dash(self):
        self.assertEqual(decide.CYCLE_TEXT[decide.NA], "—")
        self.assertEqual(decide.ELASTIC_TEXT[decide.NA], "—")
        self.assertEqual(decide.POSITION_TEXT[decide.NA], "—")


class TestThresholdOverride(unittest.TestCase):
    """阈值通过 cfg 传入，不读模块级状态 —— 便于校准与单测。"""

    def test_custom_cfg(self):
        cfg = Cfg(GM_STD_CYCLE=10.0, ELASTIC_RATIO_MIN=5.0,
                  POSITION_LOW_PCT=20.0, POSITION_HIGH_PCT=90.0)
        d = decide.decide(TWENTY, _metrics(gm_std=7.0, ratio=3.0, reliable=True,
                                           pct=30.0), "", cfg)
        self.assertEqual(d["cycle"]["label"], decide.CYCLE_NO, "7.0 < 10.0")
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_WEAK, "3.0 < 5.0")
        self.assertEqual(d["position"]["label"], decide.POSITION_NEUTRAL,
                         "30 既不 < 20 也不 > 90")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestNearBreakevenUnstable(unittest.TestCase):
    """判断②：盈亏平衡点附近 → unstable（不剔除，只标不可信）。"""

    @staticmethod
    def _m(ratio, zero_cross_ratio, reliable=True):
        return {
            "gm_std": 6.0,
            "gm_cv": {"cv": 30.0, "median": 20.0, "samples": 20},
            "elasticity": {"ratio": ratio, "reliable": reliable, "samples": 8,
                           "zero_cross": 4,
                           "zero_cross_ratio": zero_cross_ratio},
            "position": {"pct": 50.0, "current": 1.0, "samples": 40},
            "capacity_direction": {"direction": "flat", "asset_growth": 1.0,
                                   "revenue_growth": 1.0},
        }

    def test_above_threshold_marks_unstable(self):
        d = decide.decide(TWENTY, self._m(5.0, 0.5), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_UNSTABLE)
        self.assertEqual(d["elastic"]["reason"], "near_breakeven")

    def test_exactly_threshold_is_not_unstable(self):
        """恰好等于阈值不算超过（`>` 语义，与营收那条一致）。"""
        d = decide.decide(TWENTY, self._m(5.0, config.ELASTIC_ZERO_CROSS_MAX), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_STRONG)

    def test_below_threshold_keeps_label(self):
        d = decide.decide(TWENTY, self._m(3.0, 0.1), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_STRONG)
        self.assertEqual(d["elastic"]["reason"], "ok")

    def test_ratio_still_visible_when_unstable(self):
        """数值照常显示 —— 这是「不剔除只标不稳定」的关键。"""
        d = decide.decide(TWENTY, self._m(7.5, 0.6), "", config)
        self.assertAlmostEqual(d["elastic"]["ratio"], 7.5)

    def test_flat_revenue_reason_kept_separate(self):
        d = decide.decide(TWENTY, self._m(5.0, 0.0, reliable=False), "", config)
        self.assertEqual(d["elastic"]["label"], decide.ELASTIC_UNSTABLE)
        self.assertEqual(d["elastic"]["reason"], "flat_revenue")


class TestCapacityDirectionFlag(unittest.TestCase):
    """判断③ 的产能方向限定语。"""

    def _m(self, direction, pos_pct=20.0):
        return {
            "gm_std": 6.0,
            "gm_cv": {"cv": 30.0, "median": 20.0, "samples": 20},
            "elasticity": {"ratio": 3.0, "reliable": True, "samples": 8,
                           "zero_cross": 0, "zero_cross_ratio": 0.0},
            "position": {"pct": pos_pct, "current": 1.0, "samples": 40},
            "capacity_direction": {"direction": direction, "asset_growth": 20.0,
                                   "revenue_growth": 5.0},
        }

    def test_expand_badge_on_low(self):
        d = decide.decide(TWENTY, self._m("expand"), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertIn("扩产中", texts)

    def test_shrink_badge_on_high(self):
        d = decide.decide(TWENTY, self._m("shrink", 90.0), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertIn("产能在退", texts)

    def test_no_badge_on_neutral_position(self):
        """中性区本来就没方向，再加一句会像第四个结论。"""
        d = decide.decide(TWENTY, self._m("expand", 50.0), "", config)
        texts = [t for _c, t in decide.badges(d)]
        self.assertNotIn("扩产中", texts)

    def test_financial_suppresses_direction(self):
        d = decide.decide(TWENTY, self._m("expand"), "银行", config)
        self.assertIsNone(d["position"]["capacity_direction"])
        texts = [t for _c, t in decide.badges(d)]
        self.assertNotIn("扩产中", texts)

    def test_direction_does_not_change_label(self):
        for direction in ("expand", "shrink", "flat", None):
            with self.subTest(direction=direction):
                d = decide.decide(TWENTY, self._m(direction), "", config)
                self.assertEqual(d["position"]["label"], decide.POSITION_LOW)
