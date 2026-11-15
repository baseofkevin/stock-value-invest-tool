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

    def test_no_warning_placeholder(self):
        self.assertIn("无额外数据提示", self.html)

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
