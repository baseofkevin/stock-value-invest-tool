# -*- coding: utf-8 -*-
"""CLI 测试：`stock_report.py` 的参数解析、退出码、名单来源。

2026-11-15 起产物只剩 results.db（HTML / 导航页 / CSV 列都取消了），
所以原来那一大批"报告落在哪个目录"的用例一起删掉，改为钉住新的三件事：

1. **参数**：`--stock-code` / `--csv` / `--from-select` 三选一，旧的 `--out-dir` 不许回来。
2. **退出码**：0 成功、2 用户/数据错误、3 未预期错误、130 Ctrl-C。
3. **名单来源与续跑**：`--from-select` 读结果库、日期对不上就退 2、
   库里已有的票不重复取数。
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests import helpers  # noqa: F401  （顺带把项目根塞进 sys.path）
import stock_report
from screen.fetch import DataError
from screen.validate import ScreenError, SkipStock
from stock_data_fetch import results as results_db


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
    """三个来源互斥且必填。"""

    def test_stock_code(self):
        self.assertEqual(parse(["--stock-code", "600519"]).stock_code, "600519")

    def test_csv(self):
        self.assertEqual(parse(["--csv", "a.csv"]).csv_input, "a.csv")

    def test_csv_legacy_alias(self):
        """`--csv-input` 是旧名，必须仍然可用且落到同一个 dest。"""
        self.assertEqual(parse(["--csv-input", "a.csv"]).csv_input, "a.csv")

    def test_from_select_with_date(self):
        self.assertEqual(parse(["--from-select", "2026-11-13"]).from_select,
                         "2026-11-13")

    def test_from_select_without_date(self):
        """省略日期 = 用最近一次选股运行。"""
        self.assertEqual(parse(["--from-select"]).from_select, "")

    def test_neither_is_required(self):
        self.assert_rejected([])

    def test_both_rejected(self):
        self.assert_rejected(["--stock-code", "600519", "--csv", "a.csv"])

    def test_from_select_and_csv_rejected(self):
        self.assert_rejected(["--from-select", "2026-11-13", "--csv", "a.csv"])


class TestParserDefaults(unittest.TestCase):
    def test_defaults(self):
        args = parse(["--stock-code", "600519"])
        self.assertEqual(args.workers, 4)
        self.assertEqual(args.timeout, 90.0)
        self.assertEqual(args.retries, 2)
        self.assertEqual(args.retry_wait, 60.0)
        self.assertEqual(args.name, "")
        self.assertEqual(args.bundle, "")

    def test_force_quiet_are_flags(self):
        args = parse(["--stock-code", "600519", "--force", "--quiet"])
        self.assertTrue(args.force)
        self.assertTrue(args.quiet)
        self.assertFalse(parse(["--stock-code", "600519"]).force)


class TestRemovedOptions(unittest.TestCase):
    def test_out_dir_is_gone(self):
        """报告取消后没有"输出目录"这回事，参数不该留下来误导人。"""
        self.assertFalse(hasattr(parse(["--stock-code", "600519"]), "out_dir"))

    def test_passing_out_dir_is_rejected(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            parse(["--stock-code", "600519", "--out-dir", "/tmp"])
        self.assertEqual(cm.exception.code, 2)


class TestResolveDate(unittest.TestCase):
    def test_accepts_common_shapes(self):
        for raw in ("2026-11-13", "2026/11/13", "20261113"):
            with self.subTest(raw=raw):
                self.assertEqual(stock_report._resolve_date(raw), "2026-11-13")

    def test_empty_means_latest(self):
        self.assertIsNone(stock_report._resolve_date(""))
        self.assertIsNone(stock_report._resolve_date(None))

    def test_unparsable_is_none(self):
        self.assertIsNone(stock_report._resolve_date("昨天"))


# ---------------------------------------------------------------------------
# 二、退出码
# ---------------------------------------------------------------------------
class TestExitCodes(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = stock_report.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_negative_retries_rejected(self):
        code, _, err = self.run_main(["--stock-code", "600519", "--retries", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("--retries", err)

    def test_negative_retry_wait_rejected(self):
        code, _, err = self.run_main(["--stock-code", "600519", "--retry-wait", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("--retry-wait", err)

    def test_skip_stock_is_success(self):
        with mock.patch.object(stock_report, "_generate",
                               side_effect=SkipStock("上市不足 3 年")):
            code, out, _ = self.run_main(["--stock-code", "600519"])
        self.assertEqual(code, 0)
        self.assertIn("跳过", out)

    def test_data_error_is_2(self):
        with mock.patch.object(stock_report, "_generate",
                               side_effect=DataError("取数失败")):
            code, _, err = self.run_main(["--stock-code", "600519"])
        self.assertEqual(code, 2)
        self.assertIn("取数失败", err)

    def test_screen_error_is_2(self):
        with mock.patch.object(stock_report, "_generate",
                               side_effect=ScreenError("口径问题")):
            self.assertEqual(self.run_main(["--stock-code", "600519"])[0], 2)

    def test_value_error_is_2(self):
        with mock.patch.object(stock_report, "_generate",
                               side_effect=ValueError("代码不合法")):
            self.assertEqual(self.run_main(["--stock-code", "zzz"])[0], 2)

    def test_unexpected_error_is_3(self):
        with mock.patch.object(stock_report, "_generate",
                               side_effect=RuntimeError("炸了")):
            code, _, err = self.run_main(["--stock-code", "600519"])
        self.assertEqual(code, 3)
        self.assertIn("未预期错误", err)

    def test_keyboard_interrupt_is_130(self):
        with mock.patch.object(stock_report, "_generate",
                               side_effect=KeyboardInterrupt):
            code, _, err = self.run_main(["--stock-code", "600519"])
        self.assertEqual(code, 130)
        self.assertIn("中断", err)

    def test_csv_routes_to_batch(self):
        with mock.patch.object(stock_report, "_run_batch", return_value=0) as mock_batch, \
                mock.patch.object(stock_report, "_run_single") as mock_single:
            self.run_main(["--csv", "a.csv"])
        self.assertTrue(mock_batch.called)
        self.assertFalse(mock_single.called)

    def test_from_select_routes_to_batch(self):
        with mock.patch.object(stock_report, "_run_batch", return_value=0) as mock_batch:
            self.run_main(["--from-select", "2026-11-13"])
        self.assertTrue(mock_batch.called)

    def test_stock_code_routes_to_single(self):
        with mock.patch.object(stock_report, "_run_single", return_value=0) as mock_single:
            self.run_main(["--stock-code", "600519"])
        self.assertTrue(mock_single.called)


# ---------------------------------------------------------------------------
# 三、Q/V 摘要行
# ---------------------------------------------------------------------------
class TestScoreLine(unittest.TestCase):
    def line(self, data):
        return stock_report._score_line(data)

    def test_numeric(self):
        text = self.line({"quality": {"score": 62.3},
                          "valuation_score": {"score": 71.0}})
        self.assertIn("62.3 / 100", text)
        self.assertIn("71.0 / 100", text)

    def test_not_applicable(self):
        """金融股 Q/V 是 None 但要写「不适用」，不能打 `—` 也不能抛 TypeError。"""
        text = self.line({"quality": {"score": None, "not_applicable": True},
                          "valuation_score": {"score": None, "not_applicable": True}})
        self.assertEqual(text.count("不适用"), 2)

    def test_unknown_is_dash(self):
        self.assertIn("—", self.line({"quality": {}, "valuation_score": {}}))

    def test_mixed(self):
        text = self.line({"quality": {"score": 50.0},
                          "valuation_score": {"not_applicable": True}})
        self.assertIn("50.0 / 100", text)
        self.assertIn("不适用", text)


# ---------------------------------------------------------------------------
# 四、批量：名单来源与续跑
# ---------------------------------------------------------------------------
class TestBatchList(unittest.TestCase):
    """`_run_batch` 的名单来源与续跑（用桩替掉取数，不碰网络）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quality_cli_")
        self.names = []

    def fake_generate(self, code, **kwargs):
        self.names.append(code)
        return stock_report.ReportResult(
            code=code, name=kwargs.get("name_override") or code,
            data={"decision": {"label": "通过", "reason": "测试"},
                  "quality": {"score": 70.0, "grade": "良好"},
                  "valuation_score": {"score": 80.0, "grade": "低估"},
                  "data_quality": {"risk_known": 9, "risk_total": 14, "risk_na": 0},
                  "risks": {"checks": [{"status": "hit"}]}})

    def write_csv(self, codes):
        path = os.path.join(self.tmp, "list.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("代码,名称\n")
            for code in codes:
                fh.write(f"{code},{code}\n")
        return path

    def run_batch(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(stock_report, "_generate", side_effect=self.fake_generate), \
                redirect_stdout(out), redirect_stderr(err):
            code = stock_report.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_from_select_reads_results_db(self):
        results_db.save("select", [{"symbol": "600519", "name": "贵州茅台"},
                                   {"symbol": "000001", "name": "平安银行"}],
                        "2026-11-10", argv="test")
        code, out, _ = self.run_batch(["--from-select", "2026-11-10"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.names), ["sh600519", "sz000001"])
        self.assertIn("结果库 选股 2026-11-10", out)

    def test_from_select_without_rows_returns_2(self):
        code, _, err = self.run_batch(["--from-select", "2020-01-02"])
        self.assertEqual(code, 2)
        self.assertIn("名单是空的", err)

    def test_second_run_reuses_rows(self):
        """同一基准日再跑一遍：不再取数（续跑靠结果库，不靠扫 HTML）。"""
        results_db.save("select", [{"symbol": "600519", "name": "贵州茅台"}],
                        "2026-11-11", argv="test")
        self.run_batch(["--from-select", "2026-11-11"])
        self.names.clear()
        code, out, _ = self.run_batch(["--from-select", "2026-11-11"])
        self.assertEqual(code, 0)
        self.assertEqual(self.names, [])
        self.assertIn("复用 1 只", out)

    def test_force_refetches(self):
        results_db.save("select", [{"symbol": "600519", "name": "贵州茅台"}],
                        "2026-11-12", argv="test")
        self.run_batch(["--from-select", "2026-11-12"])
        self.names.clear()
        self.run_batch(["--from-select", "2026-11-12", "--force"])
        self.assertEqual(self.names, ["sh600519"])

    def test_csv_source(self):
        path = self.write_csv(["600519", "000001"])
        code, out, _ = self.run_batch(["--csv", path])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.names), ["sh600519", "sz000001"])
        self.assertIn("list.csv", out)

    def test_results_are_saved(self):
        results_db.save("select", [{"symbol": "600519", "name": "贵州茅台"}],
                        "2026-11-09", argv="test")
        self.run_batch(["--from-select", "2026-11-09"])
        row = results_db.latest_for("quality", "2026-11-09")[0]
        self.assertEqual(row["symbol"], "sh600519")
        self.assertEqual(row["q"], 70.0)
        self.assertEqual(row["conclusion"], "通过")

    def test_rerun_keeps_q_and_v(self):
        """不带 --force 重跑（复用库里的行）不能把 Q/V 冲成空。

        复用路径拿到的是「入库时的行」（键是 q / v / conclusion），新算的
        是 `_payload()`（键 q_score / v_score / label）。只认一种形状的后果
        不是报错，而是这一批的 Q/V 全变「—」，人还以为上游没数据。
        """
        results_db.save("select", [{"symbol": "600519", "name": "贵州茅台"}],
                        "2026-11-08", argv="test")
        self.run_batch(["--from-select", "2026-11-08"])
        first = results_db.latest_for("quality", "2026-11-08")[0]
        self.names.clear()
        self.run_batch(["--from-select", "2026-11-08"])      # 走复用
        again = results_db.latest_for("quality", "2026-11-08")[0]
        for key in ("q", "v", "q_grade", "v_grade", "conclusion", "reason"):
            self.assertEqual(again[key], first[key], f"{key} 重跑一次就变了")

    def test_row_fields_accepts_both_shapes(self):
        """两种 payload 形状都要认（新算的 / 库里读回来的）。"""
        fresh = {"q_score": 70.0, "v_score": 55.0, "label": "通过",
                 "q_grade": "合格", "v_grade": "合理", "reason": "r"}
        stored = {"q": 70.0, "v": 55.0, "conclusion": "通过",
                  "q_grade": "合格", "v_grade": "合理", "reason": "r"}
        self.assertEqual(stock_report._row_fields(fresh),
                         stock_report._row_fields(stored))

    def test_invalid_code_counted_as_failure(self):
        path = os.path.join(self.tmp, "bad.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("代码,名称\nzzz,坏代码\n")
        code, _, err = self.run_batch(["--csv", path])
        self.assertEqual(code, 1)
        self.assertIn("跳过 zzz", err)


if __name__ == "__main__":
    unittest.main()
