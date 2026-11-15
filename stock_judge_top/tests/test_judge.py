#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行：代码归一化、参数组合、退出码、JSON 契约，以及脚本冒烟测试。"""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from stock_judge_top import cli as cli_mod     # noqa: E402
from stock_judge_top.tests import fixtures as fx     # noqa: E402


class NormalizeCodeTest(unittest.TestCase):
    def test_accepted_forms(self):
        cases = {
            "sh600519": "sh600519",
            "SH600519": "sh600519",
            "600519": "sh600519",
            "600519.SH": "sh600519",
            "600519.XSHG": "sh600519",
            "sz000001.SZ": "sz000001",     # 回归：旧版会拼成 szsz000001
            "000001.sz": "sz000001",
            "300750": "sz300750",
            "688981": "sh688981",
            "920002": "bj920002",
            "430047": "bj430047",
            "830799": "bj830799",
            "159915": "sz159915",
            " 600519 ": "sh600519",
        }
        for raw, want in cases.items():
            self.assertEqual(cli_mod.normalize_code(raw), want, raw)

    def test_invalid_forms_raise(self):
        for raw in ("", "   ", "60051", "6005199", "abc", "sh", "shabc", "12345x"):
            with self.assertRaises(ValueError, msg=raw):
                cli_mod.normalize_code(raw)


class _FakeHub:
    """preflight 的替身：一个能进 with 的空对象。

    `target` 必须给：基准日默认值取它（`a.date or hub.target`），
    原来默认 `date.today()` 会在周末跑成「末端不足」再报「可能停牌」。
    """

    target = "2026-11-13"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MainTest(unittest.TestCase):
    def setUp(self):
        self._orig = (cli_mod.evaluate, cli_mod.print_report, cli_mod.preflight)
        self.rendered = []
        cli_mod.preflight = lambda **kw: _FakeHub()
        # 迁移后签名：evaluate(hub, code, end, refresh)
        cli_mod.evaluate = lambda hub, code, end, refresh: fx.result()
        cli_mod.print_report = lambda r: self.rendered.append(r)

    def tearDown(self):
        (cli_mod.evaluate, cli_mod.print_report, cli_mod.preflight) = self._orig

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_mod.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_default_reports_db_write(self):
        """默认只报「已写入 results.db」，不再生成 HTML 文件。"""
        code, out, _ = self.run_cli("600519", "--date", "2026-10-23")
        self.assertEqual(code, 0)
        self.assertIn("results.db", out)
        self.assertIn("web", out)

    def test_default_date_comes_from_hub_target(self):
        """不给 --date 时用 hub.target，而不是 date.today()。

        旧写法在周末会算成今天：先报「末端不足（库 11-13 / 需 11-15）」，
        再报「当前无 K 线（可能停牌）」——把"今天不是交易日"说成了"停牌"。
        """
        seen = {}

        def _ev(hub, code, end, refresh):
            seen["end"] = end
            return fx.result()

        cli_mod.evaluate = _ev
        code, _, _ = self.run_cli("600519")
        self.assertEqual(code, 0)
        self.assertEqual(seen["end"], "2026-11-13")

    def test_json_goes_to_stdout(self):
        code, out, _ = self.run_cli("600519", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["code"], "sh600519")
        self.assertNotIn("quote", payload)          # 默认剔除原始行情
        self.assertIn("phase", payload)             # 位置阶段保留
        self.assertIn("kline", payload)

    def test_json_full_keeps_quote(self):
        _, out, _ = self.run_cli("600519", "--json", "--json-full")
        self.assertIn("quote", json.loads(out))

    def test_quiet_beats_verbose(self):
        """--quiet --verbose 不应打印任何内容（旧版会打出整份报告）。"""
        code, out, err = self.run_cli("600519", "--quiet", "--verbose")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        self.assertEqual(self.rendered, [])

    def test_verbose_prints_report(self):
        code, out, _ = self.run_cli("600519", "--verbose")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.rendered), 1)
        self.assertIn("results.db", out)

    def test_quiet_with_json_still_outputs_payload(self):
        code, out, _ = self.run_cli("600519", "--json", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["code"], "sh600519")

    def test_quiet_writes_nothing_to_stdout(self):
        """HTML 已移除，--quiet 之后唯一要守的不变量就是"一行都不打"。"""
        code, out, _ = self.run_cli("600519", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_bad_code_exits_2_without_evaluating(self):
        called = []
        cli_mod.evaluate = lambda *a, **k: called.append(a)
        code, out, err = self.run_cli("abc123")
        self.assertEqual(code, 2)
        self.assertIn("参数错误", err)
        self.assertEqual(called, [])

    def test_evaluate_failure_exits_1(self):
        def boom(*a, **k):
            raise RuntimeError("无行情数据")
        cli_mod.evaluate = boom
        code, _, err = self.run_cli("600519")
        self.assertEqual(code, 1)
        self.assertIn("判断失败", err)
        self.assertIn("无行情数据", err)


class ScriptSmokeTest(unittest.TestCase):
    def test_help_without_network(self):
        result = subprocess.run(
            [sys.executable, "-m", "stock_judge_top", "--help"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())
        self.assertIn("--json-full", result.stdout)


if __name__ == "__main__":
    unittest.main()
