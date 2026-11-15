#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染：只剩终端报告（HTML 已由 web 看板取代，2026-11-15 移除）。"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_judge_top.render import text as text_mod      # noqa: E402
from stock_judge_top.tests import fixtures as fx               # noqa: E402


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
