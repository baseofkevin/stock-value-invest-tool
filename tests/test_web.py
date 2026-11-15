# -*- coding: utf-8 -*-
"""看板（`web/`）的测试，不联网、不起服务。

    python3 -m unittest discover -s tests -t .     # 含本文件

用 Flask 的 `test_client` 直接打路由，所以验的是"页面能不能出来"，
而不是"服务能不能起"。四件事值得钉：

1. **只读**。库里不写东西——看板是观察工具，不是编辑工具。
2. **空库不崩**。第一次用的人还没有 `results.db`，要给"先跑 ./run.py all"，
   不是 500 堆栈。
3. **中文标签**。库里存的是 `high/low/na` 这类枚举，页面上必须是中文；
   两套词表混着摆是之前真出现过的问题。
4. **合并表**。一只票只有部分工具的结果时，缺的那几格是 `—`，不能整行消失。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from stock_data_fetch import results as results_db


def _save_demo(db_path: str, date: str = "2026-11-13", kind: str = "batch") -> None:
    """造一批和真实跑出来同形状的结果。"""
    results_db.save("select", [
        {"symbol": "sh600019", "name": "宝钢股份", "industry": "钢铁",
         "pool_turnover_rank": 1.6, "turnover20": 0.4, "stars": "***",
         "close": 6.18, "market_cap_yi": 1346.13, "launch_date": "2026-11-13",
         "days_since_launch": 0, "vol_multiple": 1.56, "launch_gain_pct": 2.15,
         "ma20_gap_pct": 1.83, "ma60_gap_pct": 4.36,
         "gross_margin_up": "否：5.41→5.43"},
    ], date, argv="test", db=db_path, kind=kind)
    results_db.save("cycle", [
        {"symbol": "sh600019", "name": "宝钢股份", "is_cycle": True,
         "gm_std_pp": 2.3, "gm_cv": 37.3, "elasticity": -1.17,
         "elastic_label": "weak", "position_pct": 63.7,
         "position_label": "high"},
    ], date, argv="test", db=db_path, kind=kind)
    results_db.save("quality", [
        {"symbol": "sh600019", "name": "宝钢股份", "q": 54.4, "q_grade": "合格",
         "v": 67.0, "v_grade": "合理", "conclusion": "观察",
         "reason": "估值便宜，但现金流质量待观察", "risks_hit": 1,
         "risks_watch": 2, "risks_known": 9},
    ], date, argv="test", db=db_path, kind=kind)
    results_db.save("top", [
        {"code": "sh600019", "name": "宝钢股份", "ok": True, "error": "",
         "date": "2026-11-13", "close": 6.18, "pos250": -0.21,
         "days_since_high": 242, "phase": "反弹接近局部高点",
         "views": [["动量", "低位·走平"], ["换手", "偏低"], ["业绩", "减速"],
                   ["量价", "未触发"], ["周期位置", "周期·高位"]],
         "views_full": []},
    ], date, argv="test", db=db_path, kind=kind)


class WebCase(unittest.TestCase):
    """每个用例一个临时库，互不干扰。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="web-test-")
        self.db_path = os.path.join(self.tmp, "results.db")
        self._old = os.environ.get("STOCK_RESULTS_DB")
        os.environ["STOCK_RESULTS_DB"] = self.db_path

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STOCK_RESULTS_DB", None)
        else:
            os.environ["STOCK_RESULTS_DB"] = self._old

    def client(self, with_data=True):
        from web.app import create_app
        if with_data:
            _save_demo(self.db_path)
        app = create_app(self.db_path)
        app.config.update(TESTING=True)
        return app.test_client()


class TestPages(WebCase):
    def test_all_pages_render(self):
        client = self.client()
        for url in ("/", "/select", "/cycle", "/quality", "/top", "/runs",
                    "/stock/sh600019"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_unknown_page_is_404_with_nav(self):
        resp = self.client().get("/nope")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("导航", resp.get_data(as_text=True))

    def test_missing_db_explains_what_to_run(self):
        """还没有 results.db：给一句"先跑 ./run.py all"，不是堆栈。"""
        resp = self.client(with_data=False).get("/")
        self.assertEqual(resp.status_code, 500)
        self.assertIn("./run.py all", resp.get_data(as_text=True))

    def test_merged_table_shows_partial_rows(self):
        """只跑过选股的票也要在合并表里出现（缺的格子是 —）。"""
        client = self.client()
        results_db.save("select", [{"symbol": "sz000001", "name": "平安银行"}],
                        "2026-11-13", argv="test", db=self.db_path)
        html = client.get("/").get_data(as_text=True)
        # 选股最新一次是"平安银行"，质量/顶部最新一次仍是"宝钢股份"：
        # 合并表按 symbol 并排，两边都得在，缺的格子留 —。
        self.assertIn("平安银行", html)
        self.assertIn("宝钢股份", html)


class TestBatches(WebCase):
    """批次与单只要分清：这是看板最容易出错的一处。

    同一只票可能周五批量算过、周中单只又算过。批次页与工具页只能看批量那一次；
    单只那次出现在个股页的时间线里。
    """

    def test_batch_page_lists_dates(self):
        client = self.client()
        _save_demo(self.db_path, date="2026-11-06")
        html = client.get("/batches").get_data(as_text=True)
        self.assertIn("2026-11-13", html)
        self.assertIn("2026-11-06", html)

    def test_single_run_does_not_join_batch(self):
        """单只跑（kind=single）不进批次统计，也不覆盖批量那一次。"""
        client = self.client()                     # 先有一批 2026-11-13
        _save_demo(self.db_path, kind="single")     # 同一基准日的单只结果
        html = client.get("/select").get_data(as_text=True)
        self.assertIn("1 只", html)                 # 批量那次的数量，不是单只

    def test_date_scopes_tool_page(self):
        """?date= 看历史批次，不能混进最新那一批的行。"""
        client = self.client()
        results_db.save("select", [{"symbol": "sz000001", "name": "平安银行"}],
                        "2026-11-06", argv="test", db=self.db_path)
        html = client.get("/select?date=2026-11-06").get_data(as_text=True)
        self.assertIn("平安银行", html)
        self.assertNotIn("宝钢股份", html)

    def test_run_param_shows_that_run(self):
        """?run=<id> 精确到某一次运行（同一天重跑也能分辨）。"""
        client = self.client()
        run_id = results_db.save("select", [{"symbol": "sz000001", "name": "平安银行"}],
                                 "2026-11-13", argv="test", db=self.db_path)
        html = client.get(f"/select?run={run_id}").get_data(as_text=True)
        self.assertIn("平安银行", html)
        self.assertIn(f"#{run_id}", html)

    def test_batch_merged_only_that_batch(self):
        """概览的合并表按批次对齐：上周那批的票不出现在本周的表里。"""
        client = self.client()
        results_db.save("select", [{"symbol": "sz000001", "name": "平安银行"}],
                        "2026-11-06", argv="test", db=self.db_path)
        html = client.get("/?date=2026-11-06").get_data(as_text=True)
        self.assertIn("平安银行", html)
        self.assertNotIn("宝钢股份", html)


class TestTimeline(WebCase):
    """个股页要把"同一只票的不同时间"逐条摆出来。"""

    def test_each_run_is_listed(self):
        client = self.client()
        results_db.save("quality", [{"symbol": "sh600019", "name": "宝钢股份",
                                     "q": 61.2, "v": 70.0, "conclusion": "观察"}],
                        "2026-11-13", argv="test", db=self.db_path, kind="single")
        html = client.get("/stock/sh600019").get_data(as_text=True)
        self.assertIn("历次结果", html)
        self.assertIn("单只", html)                 # 来源列
        self.assertIn("61.2", html)                 # 单只那次的 Q

    def test_brief_formats_percent(self):
        """距 250 日高点在库里是小数（-0.21），时间线上要显示成 -21.0%。"""
        from web.db import _brief
        self.assertIn("-21.0%", _brief("top", {"pos250": -0.21,
                                               "momentum_state": "低位·走平"}))

    def test_unknown_values_do_not_crash(self):
        from web.db import _brief
        for tool in ("select", "cycle", "quality", "top"):
            with self.subTest(tool=tool):
                self.assertIsInstance(_brief(tool, {}), str)


class TestLabels(WebCase):
    """枚举 → 中文。库里存英文，页面上必须写中文。"""

    def test_position_label_is_chinese(self):
        html = self.client().get("/").get_data(as_text=True)
        self.assertNotIn(">high<", html)
        self.assertIn("高位", html)

    def test_cycle_state_prefix_stripped(self):
        """顶部卡里的「周期·高位」在专门那一列只写「高位」。"""
        html = self.client().get("/top").get_data(as_text=True)
        self.assertNotIn("周期·高位", html)
        self.assertIn("高位", html)

    def test_filter_unknown_value_passthrough(self):
        from web.app import _poslabel
        self.assertEqual(_poslabel("na"), "不适用")
        self.assertEqual(_poslabel(None), "—")
        self.assertEqual(_poslabel("sideways"), "sideways")

    def test_tone_maps_states(self):
        from web.app import tone
        self.assertEqual(tone("高位·走平"), "warn")
        self.assertEqual(tone("低位·偏弱"), "muted")
        self.assertEqual(tone("不认识的词"), "info")


class TestReadOnly(WebCase):
    def test_queries_do_not_write(self):
        """看板只读：打开几个页面后，库文件的 mtime 与大小都不变。"""
        client = self.client()
        before = Path(self.db_path).stat()
        for url in ("/", "/top", "/runs", "/stock/sh600019"):
            client.get(url)
        after = Path(self.db_path).stat()
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)

    def test_missing_tool_does_not_break_page(self):
        """只有选股结果时，周期/质量/顶部三页要各自给"还没有"的提示。"""
        results_db.save("select", [{"symbol": "sh600019", "name": "宝钢股份"}],
                        "2026-11-13", argv="test", db=self.db_path)
        client = self.client(with_data=False)
        for url in ("/cycle", "/quality", "/top"):
            with self.subTest(url=url):
                resp = client.get(url)
                self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
