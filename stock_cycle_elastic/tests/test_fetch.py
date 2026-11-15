"""取数层测试（README.md「测试」）。

## 这个文件守什么

原版守两件事：**只调 2 个上游接口**（`test_exactly_two_calls`）、
**纯文本返回必须判失败**（否则限频时会产出"0 季度"的假报告）。

改成共享数据层后，两件事的归属变了：

- "调几个接口" → 由 `stock_data_fetch` 的节流与缓保管，本层不再直连上游。
  这里改为守"**不碰上游**"：`fetch_one()` 只允许调 `hub.ensure()`，
  任何 `subprocess` / `urllib` 出现即失败（见 `TestNoDirectUpstream`）。
- "纯文本判失败" → 上游客户端由共享库负责，本层拿到的是结构化 dict。
  这里改为守"**空表必须抛错而不是返回空**"（见 `TestFetchFinanceContract`）。

原版 44 个用例测的是 node 通道的细节（节流闸门、退避分档、抖动防惊群、
`call_tool` 的退出码与纯文本判定）。这些机制**整体迁到共享库**，
对应的测试也随之交给 `stock_data_fetch/tests/`——在两个地方测同一段逻辑，
只会出现"这边改了那边没改"的假绿灯。本文件保留的是**仍属于本层**的部分：
`_dig_finance` 的嵌套容错、bundle 重放、以及新加的共享库契约。

## 最关键的一条（迁移时踩过）

`finance_full()` 返回的行带 `period`，**不带 `EndDate`**；而 `build._row_date()`
认 `EndDate` / `date` / `REPORT_DATE` / `EndDateStr`，**不认 `period`**。
不补 `EndDate` 的效果：每一行被判"日期格式异常，已跳过"，`quarters=0`，
报告照出但内容是空的。`test_rows_carry_end_date` 专门钉这一点。
"""

import json
import os
import unittest
from unittest import mock

import tests.helpers  # noqa: F401
import config
from cycle import fetch

from tests.fixtures import make as fx_make


def fake_hub(finance_rows=None, profile_row=None, scan_rows=None,
             empty_result=None):
    """造一个 `Hub` 替身。

    `finance_rows` 形如 `{"income": [...], "balance": [...]}`（`finance_full` 的返回）。
    `empty_result` 给字符串时，`ensure()` 会连抛那个 `EmptyResult` 若干次。
    """
    h = mock.MagicMock()
    h.target = "2026-11-13"
    full = {"income": [], "balance": [], "cashflow": []}
    if finance_rows:
        full.update(finance_rows)
    h.finance_full.return_value = full

    rng = mock.MagicMock()
    rng.rows = [profile_row] if profile_row else []
    h.read.return_value = rng
    h.scan.return_value = scan_rows or []

    if empty_result is not None:
        h.ensure.side_effect = empty_result
    return h


class FetchTestCase(unittest.TestCase):
    """公共 setUp：把重试等待压到 0，避免用例互相拖慢。

    原版这里还要 `fetch.reset_throttle()` 和 `config.CALL_INTERVAL_MS = 0`——
    节流闸门已经随 node 通道一起删掉了，现在只剩重试间隔要压。
    """

    def setUp(self):
        self._orig_wait = fetch._RETRY_WAIT_DEFAULT
        fetch._RETRY_WAIT_DEFAULT = 0

    def tearDown(self):
        fetch._RETRY_WAIT_DEFAULT = self._orig_wait


class TestFetchFinanceContract(FetchTestCase):
    """`fetch_finance` 与共享库之间的契约。"""

    def test_rows_carry_end_date(self):
        """`finance_full` 的 `period` 必须被翻成 `EndDate`。

        不翻的话 `build._row_date()` 一个都认不出来，`quarters=0`，
        报告照出但全是空表 —— 迁移时就是这么踩的。
        """
        rows = {"income": [{"period": "2026-09-30", "OperatingRevenue_Q": "100"}],
                "balance": [{"period": "2026-09-30", "TotalFixedAsset": "50"}]}
        with mock.patch.object(fetch, "hub", return_value=fake_hub(rows)):
            income, balance = fetch.fetch_finance("601899", num=20)
        self.assertEqual(income[0]["EndDate"], "2026-09-30")
        self.assertEqual(balance[0]["EndDate"], "2026-09-30")
        # 原始 period 保留，不删（排查时有用）
        self.assertEqual(income[0]["period"], "2026-09-30")

    def test_existing_end_date_not_overwritten(self):
        """上游本来就带了 `EndDate` 时不许覆盖 —— 那个值比 `period` 更权威。"""
        rows = {"income": [{"period": "2026-09-30", "EndDate": "2026-09-29"}],
                "balance": []}
        with mock.patch.object(fetch, "hub", return_value=fake_hub(rows)):
            income, _ = fetch.fetch_finance("601899")
        self.assertEqual(income[0]["EndDate"], "2026-09-29")

    def test_empty_income_raises_not_returns_empty(self):
        """空表和"取到了"必须区分：返回空列表会让下游出一张 0 季度的假报告。

        `_ensure_retrying` 已经重试过，到这里还空就是真没有 —— 抛 DataError
        让 CLI 走退出码 2，而不是静默产出空报告。
        """
        with mock.patch.object(fetch, "hub", return_value=fake_hub()):
            with self.assertRaises(fetch.DataError) as ctx:
                fetch.fetch_finance("601899")
        self.assertIn("利润表为空", str(ctx.exception))

    def test_num_truncates_to_latest(self):
        """`num=2` 时只留最近两期：库里 43 期全塞给 build 只是白算。"""
        rows = {"income": [{"period": f"2025-{m:02d}-30"} for m in (3, 6, 9, 12)],
                "balance": [{"period": f"2025-{m:02d}-30"} for m in (3, 6, 9, 12)]}
        with mock.patch.object(fetch, "hub", return_value=fake_hub(rows)):
            income, balance = fetch.fetch_finance("601899", num=2)
        self.assertEqual([r["period"] for r in income],
                         ["2025-09-30", "2025-12-30"])
        self.assertEqual(len(balance), 2)

    def test_no_num_keeps_all(self):
        """不传 num 时全给（`config.HISTORY_QUARTERS` 是请求值，不是截断值）。"""
        rows = {"income": [{"period": f"2025-{m:02d}-30"} for m in (3, 6, 9, 12)],
                "balance": []}
        with mock.patch.object(fetch, "hub", return_value=fake_hub(rows)):
            income, _ = fetch.fetch_finance("601899")
        self.assertEqual(len(income), 4)

    def test_asks_for_44_periods(self):
        """向共享库要 44 期（上游实测只回 43，多要无害，少要就短一截）。"""
        rows = {"income": [{"period": "2026-09-30"}], "balance": []}
        h = fake_hub(rows)
        with mock.patch.object(fetch, "hub", return_value=h):
            fetch.fetch_finance("601899")
        need = h.ensure.call_args[0][2]          # ensure(domain, symbol, need)
        self.assertEqual(need.extra.get("periods"), config.HISTORY_QUARTERS)

    def test_min_points_is_one_not_40(self):
        """门槛必须是 1，不是 40 —— 否则次新股直接 InsufficientDepth 判死。

        README「次新股必然缺 10 年数据，直接报错等于放弃一整个股票池」。
        期数不够由 decide 标「历史不足」并照常出报告。
        """
        rows = {"income": [{"period": "2026-09-30"}], "balance": []}
        h = fake_hub(rows)
        with mock.patch.object(fetch, "hub", return_value=h):
            fetch.fetch_finance("601899")
        need = h.ensure.call_args[0][2]
        self.assertEqual(need.min_points, 1)

    def test_empty_result_is_retried(self):
        """上游间歇性返回空要重试 —— 这是本层而非共享层的责任。

        共享层把空表当终态是对的（它分不清"真没数据"和"上游抽风"）；
        本层知道"我在给一只正常交易的票出报告，财报应该存在"，所以重试在这里。
        """
        from stock_data_fetch.errors import EmptyResult

        rows = {"income": [{"period": "2026-09-30"}], "balance": []}
        h = fake_hub(rows)
        # 前两次抛 EmptyResult，第三次成功
        h.ensure.side_effect = [EmptyResult("空"), EmptyResult("空"), None]
        with mock.patch.object(fetch, "hub", return_value=h):
            income, _ = fetch.fetch_finance("601899")
        self.assertEqual(len(income), 1)
        self.assertEqual(h.ensure.call_count, 3)

    def test_empty_result_exhausted_raises(self):
        """重试耗尽后仍然空 → DataError，不能返回空表。"""
        from stock_data_fetch.errors import EmptyResult

        h = fake_hub({"income": [], "balance": []})
        h.ensure.side_effect = EmptyResult("只有空")
        with mock.patch.object(fetch, "hub", return_value=h):
            with self.assertRaises(fetch.DataError):
                fetch.fetch_finance("601899")

    def test_non_empty_error_not_retried(self):
        """`SourceUnavailable` 之类不重试：重试也没用，只是让整批更慢。"""
        from stock_data_fetch.errors import SourceUnavailable

        h = fake_hub({"income": [], "balance": []})
        h.ensure.side_effect = SourceUnavailable("接口改名了")
        with mock.patch.object(fetch, "hub", return_value=h):
            with self.assertRaises(Exception):
                fetch.fetch_finance("601899")
        self.assertEqual(h.ensure.call_count, 1)


class TestSymbolNormalization(FetchTestCase):
    """共享库的 symbol 一律带交易所前缀。"""

    def test_various_inputs_all_prefixed(self):
        rows = {"income": [{"period": "2026-09-30"}], "balance": []}
        for raw, expect in (("601899", "sh601899"), ("sh601899", "sh601899"),
                            ("601899.SH", "sh601899"), ("000001", "sz000001")):
            h = fake_hub(rows)
            with mock.patch.object(fetch, "hub", return_value=h):
                fetch.fetch_finance(raw)
            self.assertEqual(h.ensure.call_args[0][1], expect, raw)


class TestFetchProfile(FetchTestCase):

    def test_reads_name_and_industry(self):
        row = {"symbol": "sh601899", "name": "紫金矿业", "industry": "有色金属"}
        with mock.patch.object(fetch, "hub",
                               return_value=fake_hub(profile_row=row)):
            got = fetch.fetch_profile("601899")
        self.assertEqual(got["name"], "紫金矿业")
        self.assertEqual(got["industry"], "有色金属")

    def test_missing_profile_returns_empty_dict(self):
        """没有元数据不能炸 —— `build` 只是少一个表头字段。"""
        with mock.patch.object(fetch, "hub", return_value=fake_hub()):
            self.assertEqual(fetch.fetch_profile("601899"), {})

    def test_profile_error_swallowed(self):
        h = fake_hub()
        h.ensure.side_effect = RuntimeError("炸了")
        with mock.patch.object(fetch, "hub", return_value=h):
            self.assertEqual(fetch.fetch_profile("601899"), {})


class TestFetchOne(FetchTestCase):

    def test_returns_contract_shape(self):
        """返回结构必须**一个字不变**：build / metrics / decide / render 只认它。"""
        rows = {"income": [{"period": "2026-09-30", "OperatingRevenue_Q": "1"}],
                "balance": [{"period": "2026-09-30", "TotalFixedAsset": "2"}]}
        prof = {"symbol": "sh601899", "name": "紫金矿业", "industry": "有色金属"}
        with mock.patch.object(fetch, "hub",
                               return_value=fake_hub(rows, profile_row=prof)):
            raw = fetch.fetch_one("601899")
        self.assertEqual(set(raw),
                         {"code", "name", "industry", "income", "balance", "data_gaps"})
        self.assertEqual(raw["code"], "601899")
        self.assertEqual(raw["name"], "紫金矿业")
        self.assertEqual(len(raw["income"]), 1)
        self.assertEqual(raw["data_gaps"], [])

    def test_finance_failure_breaks(self):
        """财报是必需的：取不到就让整只票失败（退出码 2），不产出半成品报告。"""
        with mock.patch.object(fetch, "hub", return_value=fake_hub()):
            with self.assertRaises(fetch.DataError):
                fetch.fetch_one("601899")

    def test_profile_failure_does_not_break(self):
        """名称/行业是可选的：失败只记 data_gaps。"""
        rows = {"income": [{"period": "2026-09-30"}], "balance": []}
        h = fake_hub(rows)
        h.ensure.side_effect = [None, RuntimeError("profile 炸了")]
        with mock.patch.object(fetch, "hub", return_value=h):
            raw = fetch.fetch_one("601899")
        self.assertEqual(raw["name"], "")
        self.assertEqual(len(raw["income"]), 1)


class TestNoDirectUpstream(unittest.TestCase):
    """**不许绕过共享库直连上游。**

    这是迁移后最重要的一条约束。原版自带 node 客户端，绕过去很容易
    （"反正我有 `providers/westock_client.js`"），一旦绕过去就绕开了
    节流闸门、缓存与换源降级 —— 而症状只是"跑得慢一点/限频多一点"，
    不会报错，所以必须靠断言拦住。
    """

    def test_no_subprocess_or_urllib_in_fetch_module(self):
        import re
        from pathlib import Path
        src = Path(fetch.__file__).read_text(encoding="utf-8")
        # 只看 import 与函数调用，不看注释里的说明文字
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn("import subprocess", code)
        self.assertNotIn("import urllib", code)
        self.assertNotIn("import requests", code)
        # westock_client.js 之类的 node 调用痕迹也不许有
        self.assertIsNone(re.search(r"subprocess\.(run|Popen|check_output)", code))

    def test_removed_interfaces_raise_explicitly(self):
        """原版接口留了显式桩：报错信息要说清"它去哪了"，而不是 AttributeError。"""
        for name in ("call_tool", "call_with_retry", "reset_throttle",
                     "set_interval_ms"):
            with self.assertRaises(NotImplementedError) as ctx:
                getattr(fetch, name)()
            self.assertIn("已不存在", str(ctx.exception))


class TestDigFinance(FetchTestCase):
    """`_dig_finance` 对嵌套层数的容错 —— 接口结构变过，不能死写路径。"""

    def test_standard_nesting(self):
        payload = {"ok": True, "data": {"code": 0, "data": {"sh600519": {"income": [1],
                                                                        "balance": [2]}}}}
        node = fetch._dig_finance(payload, "sh600519", "600519")
        self.assertEqual(node["income"], [1])

    def test_shallower_nesting(self):
        payload = {"ok": True, "data": {"sh600519": {"income": [1], "balance": [2]}}}
        node = fetch._dig_finance(payload, "sh600519", "600519")
        self.assertEqual(node["income"], [1])

    def test_key_by_six_digits(self):
        payload = {"data": {"600519": {"income": [1], "balance": []}}}
        node = fetch._dig_finance(payload, "sh600519", "600519")
        self.assertEqual(node["income"], [1])

    def test_missing_raises(self):
        with self.assertRaises(fetch.DataError):
            fetch._dig_finance({"ok": True, "data": {}}, "sh600519", "600519")


class TestBundle(FetchTestCase):

    def test_load_bundle_roundtrip(self):
        import tempfile
        stock_payload = fx_make.bundle(
            "600519",
            [fx_make.income_row("2025-03-31", revenue_q=1e9, cost_q=7e8, op_profit_q=2e8)],
            [fx_make.balance_row("2025-03-31", fixed_asset=5e9, cip=1e9)],
            name="贵州茅台", industry="白酒")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(stock_payload, fh, ensure_ascii=False)
            raw = fetch.load_bundle(path)
        self.assertEqual(raw["code"], "600519")
        self.assertEqual(raw["name"], "贵州茅台")
        self.assertEqual(len(raw["income"]), 1)

    def test_code_from_params(self):
        """bundle 顶层没 code 时，从 calls 里的 params.code 兜出来（兼容旧格式）。"""
        import tempfile
        payload = {
            "calls": {
                "data_finance": {
                    "params": {"code": "sh600519"},
                    "data": {"sh600519": {"income": [], "balance": []}},
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            raw = fetch.load_bundle(path)
        self.assertEqual(raw["code"], "600519")

    def test_missing_file(self):
        with self.assertRaises(fetch.DataError) as ctx:
            fetch.load_bundle("/nonexistent/x.json")
        self.assertIn("不存在", str(ctx.exception))

    def test_bad_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with self.assertRaises(fetch.DataError) as ctx:
                fetch.load_bundle(path)
        self.assertIn("不是合法 JSON", str(ctx.exception))

    def test_no_calls_key_is_old_format(self):
        """旧格式：文件本身就是 data_finance 的返回。"""
        import tempfile
        payload = {"code": "600519",
                   "data": {"sh600519": {"income": [{"EndDate": "2025-03-31"}],
                                         "balance": []}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            raw = fetch.load_bundle(path)
        self.assertEqual(raw["code"], "600519")
        self.assertEqual(len(raw["income"]), 1)


class TestOfflineNoNetwork(unittest.TestCase):
    """`--bundle` 重放必须零网络：任何网络入口被调用即失败。"""

    def test_bundle_never_touches_network(self):
        import tempfile
        import urllib.request

        def boom(*args, **kwargs):
            raise AssertionError("离线重放不该发起任何网络请求")

        payload = fx_make.bundle(
            "600519",
            [fx_make.income_row("2025-03-31", revenue_q=1e9, cost_q=7e8, op_profit_q=2e8)],
            [fx_make.balance_row("2025-03-31", fixed_asset=5e9, cip=1e9)],
            name="贵州茅台", industry="白酒")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)

            with mock.patch.object(urllib.request, "urlopen", side_effect=boom), \
                 mock.patch("subprocess.run", side_effect=boom):
                raw = fetch.load_bundle(path)

        self.assertEqual(len(raw["income"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
