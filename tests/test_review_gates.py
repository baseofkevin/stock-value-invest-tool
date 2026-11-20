# -*- coding: utf-8 -*-
"""硬门槛（`stock_review.gates`）的规则测试。

    python3 -m unittest discover -s tests -t .

门槛是**否决**，所以这里钉的都是"什么情况下必须判不过"，以及三条刻意
留出来的宽松：

1. **`unknown` 不否决**。工具查不到的事不替人做决定（比如金融股没有营业成本，
   供需永远算不出来），要是它也挡买入，整页一只都进不来。
2. **门槛只用数据库里有的数**。没有任何手写备注能改变结论——供需就是三季
   毛利率的方向，不看"提价""交期"这类字段库里根本没有的信息。
3. **扣非/归母 ≥ 80% 比"扣非为正"更关键**。靠补助转正的公司报表不亏。
"""

from __future__ import annotations

import unittest

from stock_review import gates


def fin(**kw):
    """一份门槛输入，只写要验的字段；缺的当没有。"""
    base = {"period": "2026-09-30", "rev_yoy_q": None, "cut_yoy_q": None,
            "cut_q": None, "cut_cum": None, "np_cum": None, "cut_ttm": None,
            "gm_q": []}
    base.update(kw)
    return base


def state(gates_list, key):
    return next(g for g in gates_list if g["key"] == key)["state"]


class TestProfit(unittest.TestCase):
    def test_all_unknown_without_finance(self):
        g = gates.check(None)
        self.assertEqual([x["state"] for x in g], ["unknown"] * 3)

    def test_losing_company_fails(self):
        g = gates.check(fin(cut_ttm=-1e8, cut_cum=-1e8, np_cum=-2e8))
        self.assertEqual(state(g, "profit"), "fail")
        self.assertIn("亏损", next(x for x in g if x["key"] == "profit")["note"])

    def test_negative_single_quarter_fails_even_if_ttm_positive(self):
        """TTM 是正的、当季转亏：当期在失血，必须挡。"""
        g = gates.check(fin(cut_ttm=5e8, cut_cum=4e8, np_cum=4e8, cut_q=-0.2e8))
        self.assertEqual(state(g, "profit"), "fail")

    def test_nonrecurring_profit_fails(self):
        """扣非只有归母的 50%：利润靠补助/卖资产，不买。"""
        g = gates.check(fin(cut_ttm=5e8, cut_cum=5e8, np_cum=10e8))
        self.assertEqual(state(g, "profit"), "fail")
        self.assertIn("50%", next(x for x in g if x["key"] == "profit")["value"])

    def test_clean_profit_passes(self):
        g = gates.check(fin(cut_ttm=12e8, cut_cum=12e8, np_cum=12.2e8, cut_q=6e8))
        self.assertEqual(state(g, "profit"), "pass")


class TestGrowth(unittest.TestCase):
    def test_below_25_revenue_fails(self):
        g = gates.check(fin(rev_yoy_q=12.0, cut_yoy_q=40.0))
        self.assertEqual(state(g, "growth"), "fail")

    def test_profit_must_beat_revenue(self):
        """营收 +30% 但扣非只 +10%：增收不增利，是铺货不是景气。"""
        g = gates.check(fin(rev_yoy_q=30.0, cut_yoy_q=10.0))
        self.assertEqual(state(g, "growth"), "fail")

    def test_the_example_passes(self):
        """用户举的那只：营收 +33%、Q3 扣非 +161%——按 40% 总量线本来会被误杀。"""
        g = gates.check(fin(rev_yoy_q=33.0, cut_yoy_q=161.0))
        self.assertEqual(state(g, "growth"), "pass")

    def test_40_percent_is_not_the_total_line(self):
        """总量线是 25%：33% 能过。想回到 40% 改 `GATE2_REV_YOY_MIN`。"""
        from stock_review import config
        self.assertEqual(config.GATE2_REV_YOY_MIN, 25.0)


class TestSupply(unittest.TestCase):
    def test_two_quarters_of_gross_margin_gain_passes(self):
        """最近三季毛利率逐季抬高（新→旧降序）：自动判过。"""
        g = gates.check(fin(gm_q=[31.0, 28.5, 26.0]))
        self.assertEqual(state(g, "supply"), "pass")

    def test_two_quarters_of_decline_fails(self):
        g = gates.check(fin(gm_q=[22.0, 25.0, 28.0]))
        self.assertEqual(state(g, "supply"), "fail")

    def test_flat_margin_is_unknown(self):
        g = gates.check(fin(gm_q=[24.0, 24.0, 24.0]))
        self.assertEqual(state(g, "supply"), "unknown")

    # ---- 第二条证据：合同负债（同比，且要占营收够重） ----

    def test_contract_liability_up_alone_passes(self):
        """毛利率没有方向，但合同负债同比 +100% 且占营收 10%：订单在变厚。"""
        fin_ = {**fin(gm_q=[24.0, 24.0, 24.0]), "cl": 2e8, "cl_prev": 1e8,
                "rev_q": 5e8}
        g = gates.check(fin_)
        self.assertEqual(state(g, "supply"), "pass")

    def test_contract_liability_down_fails(self):
        fin_ = {**fin(gm_q=[24.0, 24.0, 24.0]), "cl": 8e7, "cl_prev": 1e8,
                "rev_q": 2e8}          # 8e7 / (2e8*4) = 10%，够重
        g = gates.check(fin_)
        self.assertEqual(state(g, "supply"), "fail")

    def test_small_contract_liability_is_ignored(self):
        """占比只有 2%：翻一倍也不作数（中位公司就是 2.4%）。"""
        fin_ = {**fin(gm_q=[24.0, 24.0, 24.0]), "cl": 4e7, "cl_prev": 2e7,
                "rev_q": 5e8}
        g = gates.check(fin_)
        self.assertEqual(state(g, "supply"), "unknown")

    def test_conflicting_signals_are_unknown(self):
        """毛利率在涨、合同负债在缩：可能只是原材料降价，不下结论。"""
        fin_ = {**fin(gm_q=[31.0, 28.5, 26.0]), "cl": 8e7, "cl_prev": 1e8,
                "rev_q": 2e8}
        g = gates.check(fin_)
        self.assertEqual(state(g, "supply"), "unknown")

    def test_only_three_quarters_are_enough(self):
        """只有两季数据判不了方向（要三季才能比出两次环比）。"""
        g = gates.check(fin(gm_q=[31.0, 28.5]))
        self.assertEqual(state(g, "supply"), "unknown")

class TestIntegration(unittest.TestCase):
    def _row(self, **kw):
        row = {"symbol": "sh600000", "name": "测试",
               "select": {"market_cap_yi": 40.0},
               "cycle": {}, "top": {},
               "quality": {"q": 70.0, "v": 70.0, "conclusion": "观察"}}
        row.update(kw)
        return row

    def test_gate_fail_does_not_change_tier(self):
        """门槛只做标识：**明确不过也不动档位**，原来的名次和现在一样。

        这条是本模块最容易被"顺手改坏"的地方——把门槛接进分档只要一行，
        而用户要的是"先看标识、自己判断"，不是让工具替他排除。
        """
        from stock_review import build
        rows = [self._row(quality={})]
        rep = build(rows, {}, {"sh600000": fin(rev_yoy_q=5.0, cut_yoy_q=3.0)})
        self.assertEqual(rep["rows"][0]["tier"], "nodata")   # 不看门槛，还是缺质量
        self.assertEqual(rep["rows"][0]["gate_state"], "fail")

    def test_gate_fail_keeps_buy_rank(self):
        """一只赔率分够的票，门槛不过也照样留在「优先研究」，名次都不变。"""
        from stock_review import build
        ok = self._row()
        rep = build([ok], {"sh600000": "独家品种"},
                    {"sh600000": fin(rev_yoy_q=5.0, cut_yoy_q=3.0)})
        self.assertEqual(rep["rows"][0]["tier"], "buy")
        self.assertEqual(rep["counts"]["buy"], 1)

    def test_unknown_gate_does_not_block_buy(self):
        """某条算不出来（这里缺季度成本）不影响分档：未核实也不挡买入。"""
        from stock_review import build
        rep = build([self._row()], {"sh600000": "独家品种"},
                    {"sh600000": fin(rev_yoy_q=40.0, cut_yoy_q=90.0,
                                     cut_ttm=5e8, cut_cum=5e8,
                                     np_cum=5e8, cut_q=1e8)})
        self.assertEqual(rep["rows"][0]["tier"], "buy")
        self.assertEqual(rep["rows"][0]["gate_state"], "unknown")
        # 未核实只出现在「门槛」列，不往「提示」列里塞——那是原有内容。
        self.assertEqual(len(rep["rows"][0]["gates"]), 3)
        self.assertNotIn("门槛", " ".join(g["label"] for g in rep["rows"][0]["notes"]))

    def test_no_financials_keeps_old_behaviour(self):
        """不传财务数据 = 老行为：只有质量硬门槛，订单测试据此稳定。"""
        from stock_review import build
        rep = build([self._row()], {"sh600000": "独家品种"}, None)
        self.assertEqual(rep["rows"][0]["gates"], [])
        self.assertEqual(rep["rows"][0]["tier"], "buy")
        self.assertEqual(rep["gate_stats"], [])


if __name__ == "__main__":
    unittest.main()
