"""两表对齐测试（README.md「测试」）。

这个文件最要紧的两条：
  1. **Q4 回推陷阱** —— 三季报缺失时，Q4 单季必须为 None，
     绝不能拿年报累计冒充单季（否则 Q4 营收被放大 4 倍，判断②直接崩）。
  2. **字段白名单** —— 输出的 key 集合恰好 9 个。用 `set(keys) == EXPECTED` 而不是
     逐个 `assertNotIn`，多一个字段就红。
"""

import unittest

import tests.helpers  # noqa: F401
from cycle import build

from tests.fixtures import load
from tests.fixtures import make, scenarios


EXPECTED_KEYS = {
    "date", "revenue_q", "cost_q", "op_profit_q", "fixed_asset", "cip",
    "contract_liab", "gross_margin", "capacity_turnover",
}

#: 明确不该出现的字段（财务杠杆、现金流、偿债相关）
FORBIDDEN_KEYS = {
    "net_margin", "cfo_q", "short_debt", "total_debt", "total_profit_q",
    "fin_expense_q", "interest_expense_q", "dfl", "equity", "total_assets",
}


class TestAlignment(unittest.TestCase):

    def test_two_tables_merge_on_same_date(self):
        stock = build.build(load("full_40q"))
        self.assertEqual(len(stock["quarters"]), 43)
        q = stock["quarters"][-1]
        self.assertEqual(q["date"], "2026-09-30")
        self.assertIsNotNone(q["fixed_asset"])
        self.assertIsNotNone(q["revenue_q"])

    def test_missing_balance_only_kills_judgement_three(self):
        """`balance_missing`：balance 只有前 12 季。

        断言：后面的季度 revenue_q / gross_margin **仍有值**（判断①② 不受影响），
        但 fixed_asset / capacity_turnover 为 None（判断③ 降级）。
        """
        stock = build.build(load("balance_missing"))
        qs = stock["quarters"]
        self.assertEqual(len(qs), 40)

        last = qs[-1]
        self.assertIsNotNone(last["revenue_q"], "判断① 依赖 revenue，不该受影响")
        self.assertIsNotNone(last["gross_margin"], "判断① 的输入不该受影响")
        self.assertIsNone(last["fixed_asset"])
        self.assertIsNone(last["capacity_turnover"], "判断③ 应降级为 None")

        # 前 12 季仍然完整。注意 qs[0] 的周转率仍是 None ——
        # TTM 口径需要「最近 4 季营收」，第 1 季凑不满 4 季，所以从第 4 季起才有值。
        self.assertIsNotNone(qs[0]["fixed_asset"])
        self.assertIsNone(qs[0]["capacity_turnover"], "TTM 需要 4 季，第 1 季为 None")
        self.assertIsNotNone(qs[3]["capacity_turnover"], "第 4 季起凑满 4 季，应有值")
        self.assertIsNotNone(qs[11]["capacity_turnover"], "余额表未缺失的最后一季")

    def test_balance_gap_recorded(self):
        stock = build.build(load("balance_missing"))
        gaps = stock["meta"]["data_gaps"]
        self.assertTrue(any("缺资产负债表" in g for g in gaps), gaps[:3])


class TestDeriveSingleQuarter(unittest.TestCase):

    def test_derive_from_cumulative(self):
        """单季字段全缺、累计字段齐全 → 用"本期累计 − 上期累计"回推。"""
        stock = build.build(scenarios.derive_from_cumulative())
        qs = stock["quarters"]
        self.assertEqual(len(qs), 4)
        # 累计 200/420/640/880 → 单季 200/220/220/240
        self.assertAlmostEqual(qs[0]["revenue_q"], 2.0e9, delta=1.0)
        self.assertAlmostEqual(qs[1]["revenue_q"], 2.2e9, delta=1.0)
        self.assertAlmostEqual(qs[2]["revenue_q"], 2.2e9, delta=1.0)
        self.assertAlmostEqual(qs[3]["revenue_q"], 2.4e9, delta=1.0)

    def test_q1_cumulative_equals_single(self):
        """一季报的累计就是单季，可以直接用（唯一安全的"无上期"回推）。"""
        income = [make.income_row("2025-03-31", revenue_cum=2.0e9, cost_cum=1.4e9,
                                  include_q=False)]
        stock = build.build({"code": "600001", "income": income, "balance": []})
        self.assertAlmostEqual(stock["quarters"][0]["revenue_q"], 2.0e9, delta=1.0)

    def test_q4_trap_never_uses_annual_as_quarter(self):
        """**核心回归**：三季报缺失 → Q4 单季必须拿不到，绝不用年报累计冒充。

        `q4_trap` 的 Q4 有资产负债表（`fixed_asset` 有值），所以它不会被
        "整行无数据"剔除 —— 该季会保留，但所有利润表派生值都是 None。
        这正好能验证"保留行 + 字段为 None"这个组合，比整季消失更能说明问题。
        """
        stock = build.build(scenarios.q4_trap())
        by_date = {q["date"]: q for q in stock["quarters"]}
        self.assertIn("2025-12-31", by_date, "有 balance 就不该整季剔除")

        q4 = by_date["2025-12-31"]
        self.assertIsNone(q4["revenue_q"], "绝不能拿年报累计冒充单季")
        self.assertIsNone(q4["cost_q"])
        self.assertIsNone(q4["gross_margin"])
        # 资产负债表字段仍然可用
        self.assertIsNotNone(q4["fixed_asset"])

        # 全局断言：没有任何一季的 revenue_q 等于年报累计 9e9
        for q in stock["quarters"]:
            self.assertNotEqual(q["revenue_q"], 9.0e9,
                                f"{q['date']} 拿年报累计冒充了单季")

    def test_q4_trap_recorded_in_gaps(self):
        stock = build.build(scenarios.q4_trap())
        gaps = stock["meta"]["data_gaps"]
        self.assertTrue(any("无法回推" in g for g in gaps), gaps)

    def test_cross_year_never_subtracted(self):
        """上一期跨年时，累计口径不同，直接相减无意义 → None。"""
        income = [
            make.income_row("2024-09-30", revenue_cum=6.0e9, include_q=False),
            make.income_row("2024-12-31", revenue_cum=8.0e9, include_q=False),
            make.income_row("2025-06-30", revenue_cum=4.5e9, include_q=False),
        ]
        stock = build.build({"code": "600002", "income": income, "balance": []})
        by_date = {q["date"]: q for q in stock["quarters"]}
        # 2025-06-30 的上期是 2024-12-31（跨年）→ 不能相减。
        # 该季所有字段都拿不到 → 整季被"整行无数据"剔除（不产出 9 个 None 的空壳）。
        # 所以这里断言的是**它不在 quarters 里**，而不是"它在但值为 None"。
        self.assertNotIn("2025-06-30", by_date)
        self.assertTrue(any("跨年" in g for g in stock["meta"]["data_gaps"]),
                        stock["meta"]["data_gaps"])
        # 2024-12-31 的上期是 2024-09-30（同年相邻）→ 可相减得 2e9
        self.assertAlmostEqual(by_date["2024-12-31"]["revenue_q"], 2.0e9, delta=1.0)

    def test_non_adjacent_quarter_never_used(self):
        """三季报缺失时，"年报 − 半年报" 是 Q3+Q4 合计，不能当 Q4 单季。

        这是 Q4 陷阱的另一面：只要"上一期必须恰好是上一季度"这条没写，
        数据看起来是"能回推"的，错误值会静默流进判断②。
        """
        income = [
            make.income_row("2025-06-30", revenue_cum=4.2e9, include_q=False),
            make.income_row("2025-12-31", revenue_cum=9.0e9, include_q=False),
        ]
        stock = build.build({"code": "600008", "income": income, "balance": []})
        by_date = {q["date"]: q for q in stock["quarters"]}
        # 2025-12-31 无法回推（上期不相邻）→ 该季被剔除
        self.assertNotIn("2025-12-31", by_date,
                         "年报 − 半年报 = Q3+Q4 合计，不能冒充 Q4")
        self.assertTrue(any("不相邻" in g for g in stock["meta"]["data_gaps"]),
                        stock["meta"]["data_gaps"])


class TestEmptyRows(unittest.TestCase):

    def test_all_none_quarter_dropped(self):
        income = [
            make.income_row("2025-03-31", revenue_q=1e9, cost_q=7e8, op_profit_q=2e8),
            make.income_row("2025-06-30"),        # 整行只有日期
        ]
        stock = build.build({"code": "600003", "income": income, "balance": []})
        dates = [q["date"] for q in stock["quarters"]]
        self.assertIn("2025-03-31", dates)
        self.assertNotIn("2025-06-30", dates)
        self.assertTrue(any("整行无数据" in g for g in stock["meta"]["data_gaps"]))

    def test_empty_input(self):
        stock = build.build({"code": "600004", "income": [], "balance": []})
        self.assertEqual(stock["quarters"], [])
        self.assertEqual(stock["meta"]["quarters"], 0)

    def test_bad_input_does_not_raise(self):
        stock = build.build({"income": None, "balance": None})
        self.assertEqual(stock["quarters"], [])


class TestDateParsing(unittest.TestCase):

    def test_slash_format(self):
        income = [make.income_row("2025/03/31", revenue_q=1e9, cost_q=7e8, op_profit_q=2e8)]
        stock = build.build({"code": "600001", "income": income, "balance": []})
        self.assertEqual(stock["quarters"][0]["date"], "2025-03-31")

    def test_datetime_with_time_suffix(self):
        income = [make.income_row("2025-03-31 00:00:00 +0800 CST",
                                  revenue_q=1e9, cost_q=7e8, op_profit_q=2e8)]
        stock = build.build({"code": "600001", "income": income, "balance": []})
        self.assertEqual(stock["quarters"][0]["date"], "2025-03-31")

    def test_bad_date_skipped_not_raise(self):
        income = [
            make.income_row("", revenue_q=1e9, cost_q=7e8, op_profit_q=2e8),
            make.income_row("2025-13-45", revenue_q=1e9, cost_q=7e8, op_profit_q=2e8),
            make.income_row("2025-03-31", revenue_q=1e9, cost_q=7e8, op_profit_q=2e8),
        ]
        stock = build.build({"code": "600001", "income": income, "balance": []})
        self.assertEqual(len(stock["quarters"]), 1)
        self.assertTrue(any("日期格式异常" in g for g in stock["meta"]["data_gaps"]))

    def test_reverse_input_sorted_ascending(self):
        income = [
            make.income_row("2025-09-30", revenue_q=3e9, cost_q=2e9, op_profit_q=5e8),
            make.income_row("2025-06-30", revenue_q=2e9, cost_q=1.4e9, op_profit_q=4e8),
            make.income_row("2025-03-31", revenue_q=1e9, cost_q=7e8, op_profit_q=3e8),
        ]
        stock = build.build({"code": "600001", "income": income, "balance": []})
        dates = [q["date"] for q in stock["quarters"]]
        self.assertEqual(dates, sorted(dates))

    def test_duplicate_date_keeps_first(self):
        income = [
            make.income_row("2025-03-31", revenue_q=1e9, cost_q=7e8, op_profit_q=3e8),
            make.income_row("2025-03-31", revenue_q=9e9, cost_q=8e9, op_profit_q=1e8),
        ]
        stock = build.build({"code": "600001", "income": income, "balance": []})
        self.assertEqual(len(stock["quarters"]), 1)
        self.assertAlmostEqual(stock["quarters"][0]["revenue_q"], 1e9, delta=1.0)
        self.assertTrue(any("重复" in g for g in stock["meta"]["data_gaps"]))


class TestDerivedValues(unittest.TestCase):

    def test_gross_margin_precision(self):
        income = [make.income_row("2025-03-31", revenue_q=1e9, cost_q=6.7e8, op_profit_q=3e8)]
        stock = build.build({"code": "600001", "income": income, "balance": []})
        self.assertAlmostEqual(stock["quarters"][0]["gross_margin"], 0.33, places=6)

    def test_zero_revenue_yields_none_not_zero(self):
        stock = build.build(load("zero_revenue"))
        zero_q = [q for q in stock["quarters"] if q.get("revenue_q") == 0]
        self.assertEqual(len(zero_q), 1)
        self.assertIsNone(zero_q[0]["gross_margin"])
        self.assertIsNone(zero_q[0]["capacity_turnover"])

    def test_capacity_turnover_formula_is_ttm(self):
        """产能周转率 = 最近 4 季营收之和 ÷ (固定资产 + 在建工程)。

        这是 TTM（滚动四季），不是「单季 × 4」—— 后者会把季节性等比例放大，
        让恰好在淡季的季度被误判成「低位」。
        """
        income = [
            make.income_row("2024-06-30", revenue_q=1.0e9, cost_q=7e8, op_profit_q=1e8),
            make.income_row("2024-09-30", revenue_q=1.0e9, cost_q=7e8, op_profit_q=1e8),
            make.income_row("2024-12-31", revenue_q=1.0e9, cost_q=7e8, op_profit_q=1e8),
            make.income_row("2025-03-31", revenue_q=1.0e9, cost_q=7e8, op_profit_q=1e8),
        ]
        balance = [make.balance_row(r["EndDate"], fixed_asset=6.0e9, cip=2.0e9)
                   for r in income]
        stock = build.build({"code": "600001", "income": income, "balance": balance})
        # 前 3 季凑不满 4 季
        for i in range(3):
            self.assertIsNone(stock["quarters"][i]["capacity_turnover"])
        # 第 4 季：4 季营收合计 4e9 ÷ 资产 8e9
        self.assertAlmostEqual(stock["quarters"][3]["capacity_turnover"],
                               4.0e9 / 8.0e9, places=10)

    def test_ttm_scales_with_seasonality_not_multiplied(self):
        """淡旺季交替：TTM 会把它抹平，×4 不会。"""
        revs = [0.5e9, 1.5e9, 0.5e9, 1.5e9]      # 两淡两旺
        income = [make.income_row(d, revenue_q=r, cost_q=r * 0.7, op_profit_q=r * 0.1)
                  for d, r in zip(("2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31"), revs)]
        balance = [make.balance_row(r["EndDate"], fixed_asset=8.0e9, cip=0.0) for r in income]
        stock = build.build({"code": "600001", "income": income, "balance": balance})
        # TTM = (0.5+1.5+0.5+1.5)e9 / 8e9 = 4e9/8e9 = 0.5
        self.assertAlmostEqual(stock["quarters"][3]["capacity_turnover"], 0.5, places=10)

    def test_zero_assets_yields_none(self):
        income = [make.income_row("2025-03-31", revenue_q=2.0e9, cost_q=1.4e9, op_profit_q=3e8)]
        balance = [make.balance_row("2025-03-31", fixed_asset=0.0, cip=0.0)]
        stock = build.build({"code": "600001", "income": income, "balance": balance})
        self.assertIsNone(stock["quarters"][0]["capacity_turnover"])

    def test_negative_assets_treated_as_missing(self):
        """脏数据（负债科目录成正数之类）导致合计为负 → 当不可用，不产生负周转率。"""
        income = [make.income_row("2025-03-31", revenue_q=2.0e9, cost_q=1.4e9, op_profit_q=3e8)]
        balance = [make.balance_row("2025-03-31", fixed_asset=-1.0e9, cip=0.0)]
        stock = build.build({"code": "600001", "income": income, "balance": balance})
        self.assertIsNone(stock["quarters"][0]["capacity_turnover"])

    def test_only_fixed_asset_no_cip(self):
        """只有一个科目时用另一个 —— 接口某些公司没有在建工程。"""
        income = [make.income_row(d, revenue_q=2.0e9, cost_q=1.4e9, op_profit_q=3e8)
                  for d in ("2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31")]
        balance = [make.balance_row(r["EndDate"], fixed_asset=8.0e9) for r in income]
        stock = build.build({"code": "600001", "income": income, "balance": balance})
        got = stock["quarters"][3]["capacity_turnover"]
        self.assertAlmostEqual(got, 8.0e9 / 8.0e9, places=10)


class TestKeyWhitelist(unittest.TestCase):
    """§11.3 的核心断言：key 集合恰好 9 个。"""

    def test_exact_key_set(self):
        for name in ("full_40q", "financial", "short_12q", "no_cl"):
            with self.subTest(fixture=name):
                stock = build.build(load(name))
                for q in stock["quarters"]:
                    self.assertEqual(set(q.keys()), EXPECTED_KEYS,
                                     f"{q['date']} 的字段集合不对")

    def test_forbidden_keys_absent(self):
        stock = build.build(load("full_40q"))
        for q in stock["quarters"]:
            leaked = set(q.keys()) & FORBIDDEN_KEYS
            self.assertEqual(leaked, set(), f"不该出现的字段：{leaked}")

    def test_contract_liab_present_even_when_empty(self):
        """`no_cl`：接口没给 ContractLiability → 字段仍然存在，值为 None。

        透传字段的意义是"报告要显示这一行"，缺数据时显示 `—`，
        不能因为没值就把 key 删掉（渲染层会 KeyError）。
        """
        stock = build.build(load("no_cl"))
        for q in stock["quarters"]:
            self.assertIn("contract_liab", q)
            self.assertIsNone(q["contract_liab"])

    def test_contract_liab_passthrough_value(self):
        stock = build.build(load("full_40q"))
        self.assertIsNotNone(stock["quarters"][-1]["contract_liab"])

    def test_quarter_keys_constant_matches(self):
        self.assertEqual(set(build.QUARTER_KEYS), EXPECTED_KEYS)
        self.assertEqual(len(build.QUARTER_KEYS), 9)


class TestCrossCheck(unittest.TestCase):

    def test_q1_gross_mismatch_recorded(self):
        """自算毛利率与接口 `GrossIncomeRatio` 偏差 > 1pp → 记一条提示。"""
        income = [make.income_row("2025-03-31", revenue_q=1e9, cost_q=6e8,
                                  op_profit_q=3e8, gross_ratio=30.0)]
        balance = [make.balance_row("2025-03-31", fixed_asset=5e9, cip=1e9)]
        stock = build.build({"code": "600001", "income": income, "balance": balance})
        # 自算 40%，接口 30%，偏差 10pp
        self.assertTrue(any("偏差" in g for g in stock["meta"]["data_gaps"]))

    def test_no_cross_check_outside_q1(self):
        """累计口径只在 Q1 才有可比性 —— Q2/Q3/Q4 不该比。"""
        income = [make.income_row("2025-06-30", revenue_q=1e9, cost_q=6e8,
                                  op_profit_q=3e8, gross_ratio=10.0)]
        balance = [make.balance_row("2025-06-30", fixed_asset=5e9, cip=1e9)]
        stock = build.build({"code": "600001", "income": income, "balance": balance})
        self.assertFalse(any("偏差" in g for g in stock["meta"]["data_gaps"]))

    def test_q1_gross_consistent_no_gap(self):
        income = [make.income_row("2025-03-31", revenue_q=1e9, cost_q=6e8,
                                  op_profit_q=3e8, gross_ratio=40.0)]
        balance = [make.balance_row("2025-03-31", fixed_asset=5e9, cip=1e9)]
        stock = build.build({"code": "600001", "income": income, "balance": balance})
        self.assertEqual(stock["meta"]["data_gaps"], [])

    def test_no_cross_check_outside_q1(self):
        """累计口径只在 Q1 才有可比性 —— Q2/Q3/Q4 不该比。"""
        income = [make.income_row("2025-06-30", revenue_q=1e9, cost_q=6e8,
                                  op_profit_q=3e8, gross_ratio=10.0)]
        stock = build.build({"code": "600001", "income": income, "balance": []})
        self.assertFalse(any("偏差" in g for g in stock["meta"]["data_gaps"]))


class TestHelpers(unittest.TestCase):

    def test_latest(self):
        stock = build.build(load("full_40q"))
        self.assertEqual(build.latest(stock["quarters"])["date"], "2026-09-30")
        self.assertIsNone(build.latest([]))

    def test_window(self):
        qs = build.build(load("full_40q"))["quarters"]
        self.assertEqual(len(build.window(qs, 20)), 20)
        self.assertEqual(build.window(qs, 20)[-1], qs[-1])
        self.assertEqual(build.window(qs, 0), [])
        self.assertEqual(build.window(qs, -1), [])
        self.assertEqual(build.window(qs, 999), qs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
