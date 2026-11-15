# -*- coding: utf-8 -*-
"""结果库：符号归一、写入、读回、保留策略。

    python3 -m unittest stock_data_fetch.tests.test_results

重点测三件容易静默出错的事：
1. **符号归一**——四个工具内部写法不同（`600519` / `sh600519`），不归一
   就会变成两张表里的两只票，查询静默查不到，且不报错。
2. **写失败不影响主流程**——结果库是附加产物，锁冲突/磁盘满不能拖垮出报告。
3. **payload 是原样的 JSON**——加字段不必改表结构。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stock_data_fetch import results


class NormSymbolTest(unittest.TestCase):
    def test_six_digit_gets_market_prefix(self):
        self.assertEqual(results.norm_symbol("600519"), "sh600519")
        self.assertEqual(results.norm_symbol("000001"), "sz000001")
        self.assertEqual(results.norm_symbol("300750"), "sz300750")
        self.assertEqual(results.norm_symbol("688981"), "sh688981")
        self.assertEqual(results.norm_symbol("430047"), "bj430047")

    def test_already_prefixed_is_untouched(self):
        for code in ("sh600519", "sz000001", "bj920002"):
            self.assertEqual(results.norm_symbol(code), code)

    def test_suffix_form(self):
        self.assertEqual(results.norm_symbol("600519.SH"), "sh600519")
        self.assertEqual(results.norm_symbol("000001.XSHE"), "sz000001")

    def test_empty(self):
        self.assertEqual(results.norm_symbol(""), "")
        self.assertEqual(results.norm_symbol(None), "")


class SaveAndReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "results.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_select_rows_then_read_back(self):
        rows = [{"symbol": "600519", "name": "贵州茅台", "industry": "食品饮料",
                 "launch_date": "2026-11-12", "days_since_launch": 1,
                 "vol_multiple": 1.5, "pool_turnover_rank": 0.0,
                 "stars": "***", "close": 1435.26},
                {"symbol": "000001", "name": "平安银行", "close": 12.3}]
        run_id = results.save("select", rows, "2026-11-13", db=self.db)
        self.assertGreater(run_id, 0)
        got = results.latest("select", db=self.db)
        self.assertEqual([r["symbol"] for r in got], ["sh600519", "sz000001"])
        self.assertEqual(got[0]["name"], "贵州茅台")
        self.assertAlmostEqual(got[0]["vol_multiple"], 1.5)

    def test_payload_is_json_and_keeps_extra_fields(self):
        rows = [{"symbol": "sh600019", "name": "宝钢股份",
                 "something_new": {"nested": [1, 2, 3]}}]
        results.save("cycle", rows, "2026-09-30", db=self.db)
        got = results.latest("cycle", db=self.db)
        payload = json.loads(got[0]["payload"])
        self.assertEqual(payload["something_new"], {"nested": [1, 2, 3]})

    def test_runs_are_appended_not_overwritten(self):
        for name in ("第一次", "第二次"):
            results.save("select", [{"symbol": "sh600519", "name": name}],
                         "2026-11-13", db=self.db)
        hist = results.history("select", "sh600519", db=self.db)
        self.assertEqual([h["name"] for h in hist], ["第一次", "第二次"])

    def test_unknown_tool_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            results.save("nope", [{"symbol": "sh600519"}], "2026-11-13", db=self.db)

    def test_broken_rows_are_skipped_not_fatal(self):
        """一行写不进去只能少一行，不能让整批结果丢失。"""
        rows = [{"symbol": "sh600519", "name": "茅台"},
                {"symbol": None, "name": "无代码"}]      # primary key 不能为空
        run_id = results.save("top", rows, "2026-11-13", db=self.db)
        self.assertGreater(run_id, 0)
        got = results.latest("top", db=self.db)
        self.assertEqual([r["symbol"] for r in got], ["sh600519"])

    def test_save_into_unwritable_dir_only_warns(self):
        bad = Path(self.tmp.name) / "afile" / "results.db"
        (Path(self.tmp.name) / "afile").write_text("not a dir")
        self.assertEqual(results.save("select", [{"symbol": "sh600519"}],
                                      "2026-11-13", db=bad), 0)

    def test_empty_rows_is_a_noop(self):
        self.assertEqual(results.save("select", [], "2026-11-13", db=self.db), 0)
        self.assertFalse(self.db.exists())


class KindTest(unittest.TestCase):
    """`run.kind`：批量还是单只。

    看板要回答两种问题——"这周五那批票怎么样"和"我刚才单跑的那只呢"。
    没有这一列时只能拿"行数 == 1"去猜，而批量也可能只选出 1 只。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "results.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_batch(self):
        results.save("select", [{"symbol": "600519"}], "2026-11-13", db=self.db)
        self.assertEqual(self._kinds(), ["batch"])

    def test_single_is_recorded(self):
        results.save("top", [{"code": "600519"}], "2026-11-13", db=self.db,
                     kind="single")
        self.assertEqual(self._kinds(), ["single"])

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            results.save("top", [{"code": "600519"}], "2026-11-13", db=self.db,
                         kind="weekly")

    def test_old_db_gets_the_column(self):
        """schema v1 的库要能原地升级：老行按 batch 算，不逼人删库。"""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            create table run(
                id integer primary key autoincrement, tool text not null,
                target_date text not null, started_at text not null,
                finished_at text, argv text,
                n_ok integer not null default 0, n_fail integer not null default 0,
                note text);
            insert into run(tool,target_date,started_at,n_ok) values
                ('select','2026-11-06','2026-11-06T15:10:00',3);
            pragma user_version=1;
        """)
        conn.commit()
        conn.close()

        results.connect(self.db)                     # 触发迁移
        self.assertEqual(self._kinds(), ["batch"], "老行按批量算")
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("pragma user_version").fetchone()[0],
                         results.SCHEMA_VERSION)
        conn.close()

    def _kinds(self):
        conn = results.connect(self.db)
        try:
            return [r[0] for r in conn.execute("select kind from run order by id")]
        finally:
            conn.close()


class TopRowMappingTest(unittest.TestCase):
    """顶部判断的 views 有两种形态（单只的 dict / 批量的元组），都要能落库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "results.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, views):
        return {"code": "sh600019", "name": "宝钢股份", "close": 6.18,
                "pos250": -0.21, "days_since_high": 12, "views": views,
                "extras": {"valuation": {"metrics": {"pe": {"percentile": 79.2}}},
                           "consensus": {"upside": 0.21}}}

    def test_tuple_views_from_batch(self):
        results.save("top", [self._row([("动量", "低位·走平"), ("换手", "偏低"),
                                        ("业绩", "减速"), ("量价", "未触发"),
                                        ("周期位置", "周期·高位")])],
                     "2026-11-13", db=self.db)
        got = results.latest("top", db=self.db)[0]
        self.assertEqual(got["momentum_state"], "低位·走平")
        self.assertEqual(got["cycle_state"], "周期·高位")
        self.assertAlmostEqual(got["pe_pct"], 79.2)
        self.assertAlmostEqual(got["target_upside"], 0.21)

    def test_dict_views_from_single(self):
        results.save("top", [self._row([{"key": "momentum", "title": "动量",
                                         "state": "高位·过热"}])],
                     "2026-11-13", db=self.db)
        got = results.latest("top", db=self.db)[0]
        self.assertEqual(got["momentum_state"], "高位·过热")
        self.assertIsNone(got["cycle_state"])


if __name__ == "__main__":
    unittest.main()
