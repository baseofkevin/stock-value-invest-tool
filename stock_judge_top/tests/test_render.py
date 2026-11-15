#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染：HTML 结构、转义、文件名、落盘，以及终端报告。"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_judge_top.render import html as html_mod      # noqa: E402
from stock_judge_top.render import text as text_mod      # noqa: E402
from stock_judge_top.tests import fixtures as fx               # noqa: E402


class HtmlReportTest(unittest.TestCase):
    def setUp(self):
        self.html = html_mod.html_report(fx.result())

    def test_four_cards_and_headings(self):
        self.assertIn("四因子独立观察", self.html)
        for title in ("动量", "换手", "业绩", "量价"):
            self.assertIn(title, self.html)

    def test_no_total_score_language(self):
        self.assertNotIn("总分：", self.html)
        self.assertNotIn("顶部结论：", self.html)
        self.assertIn("不合并总分", self.html)

    def test_position_basis_is_stated(self):
        self.assertIn("最高价", self.html)

    def test_phase_is_rendered(self):
        self.assertIn("位置阶段", self.html)
        self.assertIn("中高位回落", self.html)

    def test_name_is_escaped(self):
        html = html_mod.html_report(fx.result(name="<script>alert(1)</script>"))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_missing_name_falls_back_to_code(self):
        html = html_mod.html_report(fx.result(name=""))
        self.assertIn("sh600519", html)
        self.assertNotIn('<span class="code">', html)

    def test_none_values_render_as_dash(self):
        html = html_mod.html_report(fx.result(local={"pos60": None}))
        self.assertIn("—", html)

    def test_warnings_block(self):
        html = html_mod.html_report(fx.result(warnings=["K线仅 120 根"]))
        self.assertIn("K线仅 120 根", html)
        self.assertNotIn("无额外数据提示", html)

    def test_no_warning_is_silent_not_placeholder(self):
        """没有 warnings 时不再输出占位句：缺口清单表格已经承担了"这里空着"的表达。"""
        self.assertNotIn("无额外数据提示", self.html)
        self.assertIn("取数缺口", self.html)

    def test_gaps_table_lists_each_gap(self):
        r = dict(fx.result())
        r["gaps"] = [{"domain": "snapshot", "symbol": "sh600519",
                      "why": "上游返回空表"}]
        html = html_mod.html_report(r)
        self.assertIn("snapshot", html)
        self.assertIn("上游返回空表", html)
        self.assertIn("不影响四因子", html)

    def test_evidence_section_has_four_rows(self):
        r = dict(fx.result())
        r["extras"] = {
            "valuation": {"metrics": {"pe": {"label": "PE(TTM)", "current": 21.8,
                                             "percentile": 18.0}},
                          "sample_start": "2018-01-05", "weeks": 454},
            "consensus": {"target": 1692.3, "upside": 0.18, "forecasts": 3},
            "risk": {"items": [{"label": "经营现金流 / 净利润", "text": "1.30",
                                "state": "clear"}], "hit": 0, "watch": 0},
            "cycle": {"is_cycle": False, "gm_std": 1.0, "gm_cv": None,
                      "elastic_label": "弱", "position_pct": 70.0,
                      "position_label": "高位"},
        }
        html = html_mod.html_report(r)
        for label in ("估值分位", "机构目标价", "基本面反证", "周期状态"):
            self.assertIn(label, html)
        self.assertIn("PE(TTM) 21.80（P18）", html)

    def test_raw_price_caliper_is_stated(self):
        """价格口径必须写在报告里——它和选股工具一致，但和"复权"直觉相反。"""
        self.assertIn("不复权（raw）", self.html)

    def test_wide_card_comes_last(self):
        # 业绩卡（wide）应排在其余三张之后，前端网格才不会出现空洞
        self.assertLess(self.html.index("量价"), self.html.index("同比"))


class FilenameTest(unittest.TestCase):
    def test_name_and_timestamp(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 8, 15, 40, 12)
        self.assertEqual(html_mod.html_filename({"code": "sh600519", "name": "贵州茅台"}, now),
                         "贵州茅台_20261108_154012.html")

    def test_illegal_characters_are_stripped(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 8, 15, 40, 12)
        self.assertEqual(html_mod.html_filename({"code": "sh600519", "name": 'ST 股:*?'}, now),
                         "ST股_20261108_154012.html")

    def test_empty_name_falls_back_to_code(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 8, 15, 40, 12)
        self.assertEqual(html_mod.html_filename({"code": "sh600519", "name": ""}, now),
                         "sh600519_20261108_154012.html")

    def test_all_empty_uses_unknown(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 8, 15, 40, 12)
        self.assertEqual(html_mod.html_filename({"code": "", "name": ""}, now),
                         "unknown_20261108_154012.html")


class WriteHtmlFileTest(unittest.TestCase):
    def test_writes_into_out_dir(self):
        original = html_mod.OUT_DIR
        with tempfile.TemporaryDirectory() as tmp:
            html_mod.OUT_DIR = tmp
            try:
                path = html_mod.write_html_file(fx.result())
                self.assertTrue(Path(path).exists())
                self.assertEqual(Path(path).read_text(encoding="utf-8"),
                                 html_mod.html_report(fx.result()))
            finally:
                html_mod.OUT_DIR = original


class TextReportTest(unittest.TestCase):
    def test_prints_all_cards(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            text_mod.print_report(fx.result())
        out = buf.getvalue()
        self.assertIn("四因子独立观察", out)
        for title in ("动量", "换手", "业绩", "量价"):
            self.assertIn(title, out)
        self.assertIn("不输出总分", out)

    def test_survives_missing_fields(self):
        r = fx.result(name="", close=None)
        r["kline"] = {"date": None}
        buf = io.StringIO()
        with redirect_stdout(buf):
            text_mod.print_report(r)
        self.assertIn("—", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
