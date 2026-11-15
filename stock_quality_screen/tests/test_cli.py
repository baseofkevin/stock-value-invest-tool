# -*- coding: utf-8 -*-
"""CLI 测试：`stock_report.py` 的参数解析、退出码、输出目录推导。

CLI 测试分两层，**都不碰网络**：

1. **纯函数层**（`build_parser` / `_out_dir_for` / `_date_from_*` / `_safe_name`）
   直接调，不需要 mock。这是大部分价值所在：输出目录的推导规则改一次错一次，
   而这几个函数决定了「报告落在哪」，错了会留下散落各处的历史产物。
2. **`main()` 层**：把 `_generate_report` / `_run_batch` 换成桩，只验证
   **异常 → 退出码**的映射。退出码是脚本约定：
   0 成功、2 用户/数据错误、3 未预期错误、130 Ctrl-C。
"""
from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401  （顺带把项目根塞进 sys.path）
import config
import stock_report
from screen.batch import SKIP_SCORE_TEXT, ReportResult
from screen.fetch import DataError
from screen.validate import ScreenError, SkipStock


def parse(argv):
    return stock_report.build_parser().parse_args(argv)


class ParserCase(unittest.TestCase):
    def assert_rejected(self, argv):
        """参数被拒绝 → argparse 抛 SystemExit(2)。"""
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            parse(argv)
        self.assertEqual(cm.exception.code, 2, err.getvalue())
        return err.getvalue()


# ---------------------------------------------------------------------------
# 一、参数解析
# ---------------------------------------------------------------------------
class TestParserSource(ParserCase):
    """`--stock-code` 与 `--csv` 互斥且必填。"""

    def test_stock_code(self):
        self.assertEqual(parse(["--stock-code", "600519"]).stock_code, "600519")

    def test_csv(self):
        self.assertEqual(parse(["--csv", "a.csv"]).csv_input, "a.csv")

    def test_csv_legacy_alias(self):
        """`--csv-input` 是旧名，必须仍然可用且落到同一个 dest。"""
        self.assertEqual(parse(["--csv-input", "a.csv"]).csv_input, "a.csv")

    def test_neither_is_required(self):
        msg = self.assert_rejected([])
        self.assertIn("required", msg.lower())

    def test_both_rejected(self):
        msg = self.assert_rejected(["--stock-code", "600519", "--csv", "a.csv"])
        self.assertIn("not allowed with", msg)

    def test_stock_code_not_required_when_csv_given(self):
        args = parse(["--csv", "a.csv"])
        self.assertIsNone(args.stock_code)


class TestParserDefaults(unittest.TestCase):
    def test_defaults(self):
        args = parse(["--stock-code", "600519"])
        self.assertEqual(args.name, "")
        self.assertEqual(args.bundle, "")
        self.assertEqual(args.workers, 4)
        self.assertEqual(args.timeout, 90.0)
        self.assertEqual(args.retries, 2)
        self.assertEqual(args.retry_wait, 60.0)
        self.assertFalse(args.force)
        self.assertFalse(args.quiet)

    def test_force_quiet_are_flags(self):
        args = parse(["--stock-code", "600519", "--force", "--quiet"])
        self.assertTrue(args.force)
        self.assertTrue(args.quiet)


class TestRemovedOptions(unittest.TestCase):
    """已删除的参数必须真的不存在，否则文档说删了、代码还收着。"""

    REMOVED = ("out_dir", "output_dir")

    def test_out_dir_is_gone(self):
        args = parse(["--stock-code", "600519"])
        for name in self.REMOVED:
            with self.subTest(name=name):
                self.assertFalse(hasattr(args, name),
                                 f"{name} 应该已删除：输出目录由 _out_dir_for() 统一决定")

    def test_passing_out_dir_is_rejected(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            parse(["--stock-code", "600519", "--out-dir", "/tmp/x"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("unrecognized", err.getvalue())


# ---------------------------------------------------------------------------
# 二、输出目录推导
# ---------------------------------------------------------------------------
class TestOutDir(unittest.TestCase):
    """`_out_dir_for()`：批量按批次日期分层，单股不加日期层。"""

    def setUp(self):
        self.base = Path(config.DEFAULT_OUT_DIR).resolve()

    def test_single_stock_has_no_date_layer(self):
        """单股没有批次概念，直接落根下——加日期层会让每次跑都换个目录。"""
        self.assertEqual(stock_report._out_dir_for(), self.base)

    def test_batch_uses_filename_date(self):
        """`选股_2026-11-13.csv` → `.../2026-11-13/`。"""
        out = stock_report._out_dir_for("/tmp/选股_2026-11-13.csv")
        self.assertEqual(out, self.base / "2026-11-13")

    def test_batch_falls_back_to_rows(self):
        """文件名没日期 → 看「启动日」列的众数。"""
        rows = [{"启动日": "2026-11-10"}, {"启动日": "2026-11-13"},
                {"启动日": "2026-11-13"}]
        out = stock_report._out_dir_for("/tmp/自选.csv", rows)
        self.assertEqual(out, self.base / "2026-11-13")

    def test_rows_mode_not_first_row(self):
        """众数而不是第一行：混进别的交易日时，第一行是异类也不该决定整批目录。"""
        rows = [{"启动日": "2026-11-20"}, {"启动日": "2026-11-13"},
                {"启动日": "2026-11-13"}]
        out = stock_report._out_dir_for("/tmp/自选.csv", rows)
        self.assertEqual(out, self.base / "2026-11-13")

    def test_no_date_anywhere_falls_back_to_today(self):
        from datetime import datetime
        out = stock_report._out_dir_for("/tmp/自选.csv", [])
        self.assertEqual(out, self.base / f"{datetime.now():%Y-%m-%d}")

    def test_out_dir_is_under_output_quality_screen(self):
        """目录名与层级是 output/README.md 的约定，别的工具不能撞进来。"""
        self.assertEqual(self.base.name, "quality-screen")
        self.assertEqual(self.base.parent.name, "output")

    def test_returns_path_object(self):
        self.assertIsInstance(stock_report._out_dir_for("/tmp/a_2026-11-13.csv"), Path)


class TestDateFromName(unittest.TestCase):
    def test_iso_date(self):
        self.assertEqual(stock_report._date_from_name("选股_2026-11-13.csv"), "2026-11-13")

    def test_dot_and_dash(self):
        self.assertEqual(stock_report._date_from_name("a_2026.11.13.csv"), "2026-11-13")
        self.assertEqual(stock_report._date_from_name("a_2026-11-13.csv"), "2026-11-13")

    def test_slash_is_not_a_filename_date(self):
        """`/` 是路径分隔符：`Path(...).stem` 之后主名里不可能还有它。

        正则是三种都收（「启动日」列里的日期可能是 `2026/11/13`），
        但文件名这一路只可能遇到 `-` 和 `.`。
        """
        self.assertIsNone(stock_report._date_from_name("a_2026/11/13.csv"))

    def test_takes_last_match(self):
        """`选股_2026-11-13_备份-2026-11-14.csv` → 取后者：重命名时改的是它。"""
        self.assertEqual(
            stock_report._date_from_name("选股_2026-11-13_备份-2026-11-14.csv"),
            "2026-11-14")

    def test_no_date(self):
        self.assertIsNone(stock_report._date_from_name("自选股.csv"))

    def test_ignores_extension(self):
        """只在主名里找：`a.csv` 里的数字不算日期。"""
        self.assertIsNone(stock_report._date_from_name("20261113.csv"))


class TestDateFromRows(unittest.TestCase):
    def test_mode_not_first(self):
        rows = [{"启动日": "2026-11-01"}, {"启动日": "2026-11-13"},
                {"启动日": "2026-11-13"}]
        self.assertEqual(stock_report._date_from_rows(rows), "2026-11-13")

    def test_blank_values_ignored(self):
        rows = [{"启动日": ""}, {"启动日": "  "}, {"启动日": "2026-11-13"}]
        self.assertEqual(stock_report._date_from_rows(rows), "2026-11-13")

    def test_missing_column(self):
        self.assertIsNone(stock_report._date_from_rows([{"别的": "2026-11-13"}]))

    def test_empty_rows(self):
        self.assertIsNone(stock_report._date_from_rows([]))

    def test_tie_breaks_on_largest_date(self):
        """并列取最大日期：同样的输入必须稳定得到同一个目录。"""
        rows = [{"启动日": "2026-11-13"}, {"启动日": "2026-11-20"}]
        self.assertEqual(stock_report._date_from_rows(rows), "2026-11-20")

    def test_custom_column(self):
        rows = [{"日期": "2026-12-01"}]
        self.assertEqual(stock_report._date_from_rows(rows, column="日期"), "2026-12-01")


class TestSafeName(unittest.TestCase):
    """文件名净化：上游公司名可能带 `/`、`*`、换行，直接拼进路径会写失败。"""

    def test_strips_path_separators(self):
        self.assertNotIn("/", stock_report._safe_name("A/B"))
        self.assertNotIn("\\", stock_report._safe_name("A\\B"))

    def test_strips_windows_reserved(self):
        for ch in '*?"<>|':
            with self.subTest(ch=ch):
                self.assertNotIn(ch, stock_report._safe_name(f"A{ch}B"))

    def test_strips_newlines(self):
        self.assertEqual(stock_report._safe_name("A\nB"), "A_B")

    def test_empty_falls_back(self):
        self.assertEqual(stock_report._safe_name(""), "企业")
        self.assertEqual(stock_report._safe_name("  ...  "), "企业")

    def test_keeps_chinese(self):
        self.assertEqual(stock_report._safe_name("贵州茅台"), "贵州茅台")


# ---------------------------------------------------------------------------
# 三、main()：退出码映射
# ---------------------------------------------------------------------------
class TestExitCodes(unittest.TestCase):
    """退出码是脚本约定：0 成功、2 用户/数据错误、3 未预期错误、130 中断。

    走 `main()` 而不是直接调 `_run_single`：参数校验在 `main()` 里。
    """

    def run_main(self, argv, **patches):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with mock.patch.multiple(stock_report, **patches):
                code = stock_report.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_negative_retries_rejected(self):
        code, _out, err = self.run_main(["--stock-code", "600519", "--retries", "-1"],
                                        set_quiet=mock.Mock())
        self.assertEqual(code, 2)
        self.assertIn("--retries", err)

    def test_negative_retry_wait_rejected(self):
        code, _out, err = self.run_main(["--stock-code", "600519", "--retry-wait", "-1"],
                                        set_quiet=mock.Mock())
        self.assertEqual(code, 2)
        self.assertIn("--retry-wait", err)

    def test_zero_is_allowed(self):
        """0 是合法的「不重试」，不能被 `<0` 的校验误伤。"""
        result = ReportResult(code="600519", name="x", target=Path("/tmp/x.html"),
                              summary=None, data=helpers.render_data())
        code, _out, _err = self.run_main(
            ["--stock-code", "600519", "--retries", "0", "--quiet"],
            set_quiet=mock.Mock(),
            _generate_report=mock.Mock(return_value=result),
        )
        self.assertEqual(code, 0)

    def test_skip_stock_is_success(self):
        """上市不足 3 年 → 跳过，退出码 0：这不是错误，脚本不该因此中断。"""
        code, out, _err = self.run_main(
            ["--stock-code", "600519"],
            set_quiet=mock.Mock(),
            _generate_report=mock.Mock(side_effect=SkipStock("上市不足 3 年")),
        )
        self.assertEqual(code, 0)
        self.assertIn("跳过", out)

    def test_data_error_is_2(self):
        code, _out, err = self.run_main(
            ["--stock-code", "600519"],
            set_quiet=mock.Mock(),
            _generate_report=mock.Mock(side_effect=DataError("取数失败")),
        )
        self.assertEqual(code, 2)
        self.assertIn("取数失败", err)

    def test_screen_error_is_2(self):
        code, _out, err = self.run_main(
            ["--stock-code", "600519"],
            set_quiet=mock.Mock(),
            _generate_report=mock.Mock(side_effect=ScreenError("字段缺失")),
        )
        self.assertEqual(code, 2)
        self.assertIn("字段缺失", err)

    def test_value_error_is_2(self):
        """代码格式不对走 ValueError，也归为「用户输入问题」。"""
        code, _out, _err = self.run_main(
            ["--stock-code", "abc"],
            set_quiet=mock.Mock(),
            _generate_report=mock.Mock(side_effect=ValueError("代码应为 6 位数字")),
        )
        self.assertEqual(code, 2)

    def test_unexpected_error_is_3(self):
        """未预期异常要和「用户错误」分开：3 意味着该看堆栈了。"""
        code, _out, err = self.run_main(
            ["--stock-code", "600519"],
            set_quiet=mock.Mock(),
            _generate_report=mock.Mock(side_effect=RuntimeError("boom")),
        )
        self.assertEqual(code, 3)
        self.assertIn("未预期错误", err)
        self.assertIn("boom", err)

    def test_keyboard_interrupt_is_130(self):
        code, _out, err = self.run_main(
            ["--stock-code", "600519"],
            set_quiet=mock.Mock(),
            _generate_report=mock.Mock(side_effect=KeyboardInterrupt),
        )
        self.assertEqual(code, 130)
        self.assertIn("中断", err)

    def test_quiet_is_applied_before_work(self):
        """`--quiet` 要在取数之前生效，否则重试警告还是照打。"""
        seen = []
        result = ReportResult(code="600519", name="x", target=Path("/tmp/x.html"),
                              summary=None, data=helpers.render_data())
        self.run_main(
            ["--stock-code", "600519", "--quiet"],
            set_quiet=mock.Mock(side_effect=lambda v: seen.append(v)),
            _generate_report=mock.Mock(return_value=result),
        )
        self.assertEqual(seen, [True])

    def test_csv_routes_to_batch(self):
        """给了 `--csv` 就必须走批量，不能落进单股分支。"""
        single = mock.Mock()
        batch = mock.Mock(return_value=0)
        code, _out, _err = self.run_main(
            ["--csv", "a.csv"], set_quiet=mock.Mock(),
            _run_single=single, _run_batch=batch)
        self.assertEqual(code, 0)
        batch.assert_called_once()
        single.assert_not_called()

    def test_stock_code_routes_to_single(self):
        single = mock.Mock(return_value=0)
        batch = mock.Mock()
        code, _out, _err = self.run_main(
            ["--stock-code", "600519"], set_quiet=mock.Mock(),
            _run_single=single, _run_batch=batch)
        self.assertEqual(code, 0)
        single.assert_called_once()
        batch.assert_not_called()


class TestScoreLine(unittest.TestCase):
    """CLI 摘要里的 Q/V 行：`None` 不能直接格式化（金融股会 TypeError）。"""

    def test_numeric(self):
        line = stock_report._score_line({
            "quality": {"score": 72.34}, "valuation_score": {"score": 58.0}})
        self.assertIn("72.3 / 100", line)
        self.assertIn("58.0 / 100", line)

    def test_not_applicable(self):
        line = stock_report._score_line({
            "quality": {"score": None, "not_applicable": True},
            "valuation_score": {"score": None, "not_applicable": True}})
        self.assertEqual(line.count("不适用"), 2)

    def test_unknown_is_dash(self):
        """「算不出来」和「不适用」必须分开：前者该补数据，后者本就不该算。"""
        line = stock_report._score_line({"quality": {}, "valuation_score": {}})
        self.assertEqual(line.count("—"), 2)
        self.assertNotIn("不适用", line)

    def test_mixed(self):
        """两栏独立判断：金融股常出现 Q 不适用而 V 也缺，但不能一律打成同一个词。"""
        line = stock_report._score_line({
            "quality": {"score": None, "not_applicable": True},
            "valuation_score": {"score": 60.0}})
        self.assertIn("不适用", line)
        self.assertIn("60.0 / 100", line)


class TestApplyResult(unittest.TestCase):
    """批量结果回填导航页表格：五种状态各有固定文案。"""

    def make_table(self, n=1):
        rows = [{"代码": "600519"} for _ in range(n)]
        return argparse.Namespace(rows=rows)

    def test_skipped(self):
        table = self.make_table()
        stock_report._apply_result(table, [0], ReportResult(
            code="600519", name="x", target=None, summary=None, skipped=True))
        from screen.batch import LINK_COLUMN, SCORE_COLUMN
        self.assertEqual(table.rows[0][SCORE_COLUMN], SKIP_SCORE_TEXT)
        self.assertEqual(table.rows[0][LINK_COLUMN], "")

    def test_error(self):
        table = self.make_table()
        stock_report._apply_result(table, [0], ReportResult(
            code="600519", name="x", target=None, summary=None, error="取数失败"))
        from screen.batch import LINK_COLUMN, SCORE_COLUMN
        self.assertEqual(table.rows[0][SCORE_COLUMN], "生成失败")
        self.assertEqual(table.rows[0][LINK_COLUMN], "")

    def test_summary_missing(self):
        """报告生成了但解析不出评分：保留链接，让人能点进去看。"""
        table = self.make_table()
        stock_report._apply_result(table, [0], ReportResult(
            code="600519", name="x", target=Path("/tmp/贵州茅台-企业质量初筛-1.html"),
            summary=None))
        from screen.batch import SCORE_COLUMN
        self.assertEqual(table.rows[0][SCORE_COLUMN], "评分提取失败")

    def test_multiple_indices(self):
        """同一只票可能在 CSV 里出现多次，每个下标都要回填。"""
        table = self.make_table(3)
        stock_report._apply_result(table, [0, 2], ReportResult(
            code="600519", name="x", target=Path("/tmp/a.html"), summary=None,
            error="取数失败"))
        from screen.batch import SCORE_COLUMN
        self.assertEqual(table.rows[0][SCORE_COLUMN], "生成失败")
        self.assertEqual(table.rows[2][SCORE_COLUMN], "生成失败")


if __name__ == "__main__":
    unittest.main()
