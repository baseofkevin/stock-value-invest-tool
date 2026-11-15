# -*- coding: utf-8 -*-
"""入口脚本 `run.py` 的纯函数测试（不联网、不建库）。

    python3 -m unittest discover -s tests -t .

只测三件容易错、错了又很难发现的事：

1. **基准日怎么定**。盘中（未过 15:05）必须排除今天——否则拿半天的数据算
   「收盘站上 MA20」；非交易日（周末复盘）要回落到上一个交易日而不是报错。
2. **CSV 行数**。表头算不算、文件不存在怎么表示，决定了「0 只入选」能不能被拦住。
3. **透传参数的切分**。`--cycle-args "--workers 2"` 必须拆成两个 argv 元素。
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run  # noqa: E402


class _FakeDT(dt.datetime):
    _now: dt.datetime | None = None

    @classmethod
    def now(cls, tz=None):                     # noqa: D401 - 对齐 datetime 接口
        assert cls._now is not None
        return cls._now


class DatePickTest(unittest.TestCase):
    days = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]

    def setUp(self) -> None:
        self._real = run.dt
        run.dt = type("D", (), {"datetime": _FakeDT, "date": dt.date,
                                "time": dt.time, "timedelta": dt.timedelta})

    def tearDown(self) -> None:
        run.dt = self._real

    def _pick(self, now: dt.datetime, days=None) -> str:
        _FakeDT._now = now
        return run.pick_date(None, self.days if days is None else days)

    def test_after_close_uses_today(self) -> None:
        self.assertEqual(self._pick(dt.datetime(2026, 11, 20, 15, 5)), "2026-11-20")

    def test_before_close_excludes_today(self) -> None:
        """盘中必须退到上一个交易日，不能拿今天的半日数据当收盘。"""
        self.assertEqual(self._pick(dt.datetime(2026, 11, 20, 14, 0)), "2026-11-19")
        self.assertEqual(self._pick(dt.datetime(2026, 11, 20, 15, 4)), "2026-11-19")

    def test_weekend_falls_back(self) -> None:
        self.assertEqual(self._pick(dt.datetime(2026, 11, 21, 10, 0)), "2026-11-20")
        self.assertEqual(self._pick(dt.datetime(2026, 11, 22, 20, 0)), "2026-11-20")

    def test_explicit_date_must_be_in_calendar(self) -> None:
        """显式给的日期不在日历里要报错，不许悄悄换成"最近的交易日"。"""
        with self.assertRaises(SystemExit):
            run.pick_date("2026-11-30", self.days)

    def test_explicit_date_passthrough(self) -> None:
        self.assertEqual(run.pick_date("2026-11-18", self.days), "2026-11-18")

    def test_empty_calendar_errors(self) -> None:
        with self.assertRaises(SystemExit):
            run.pick_date(None, [])


class CsvTest(unittest.TestCase):
    def test_csv_rows_counts_data_only(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "选股_2026-11-20.csv"
            p.write_text("代码,名称\nsh600000,浦发银行\nsz000001,平安银行\n",
                         encoding="utf-8-sig")
            self.assertEqual(run.csv_rows(p), 2, "表头不算一行")

    def test_csv_rows_missing_file(self) -> None:
        self.assertEqual(run.csv_rows(Path("/nonexistent/x.csv")), -1)

    def test_csv_rows_header_only(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.csv"
            p.write_text("代码,名称\n", encoding="utf-8-sig")
            self.assertEqual(run.csv_rows(p), 0, "0 只入选要能被识别出来")

    def test_csv_path_shape(self) -> None:
        self.assertEqual(run.csv_for("2026-11-20").name, "选股_2026-11-20.csv")


class PassthroughTest(unittest.TestCase):
    def test_split(self) -> None:
        self.assertEqual(run.split_passthrough("--workers 2"), ["--workers", "2"])
        self.assertEqual(run.split_passthrough("  "), [])
        self.assertEqual(run.split_passthrough("--force"), ["--force"])


class ParserTest(unittest.TestCase):
    def test_subcommands_exist(self) -> None:
        p = run.build_parser()
        for name in ("all", "weekly", "select", "cycle", "quality", "top",
                     "calendar", "menu"):
            self.assertIn(name, p._subparsers._group_actions[0].choices)

    def test_all_is_full_pipeline(self) -> None:
        a = run.build_parser().parse_args(["all"])
        self.assertEqual(a.cycle_args, "--workers 2")
        self.assertFalse(a.skip_cycle)

    def test_weekly_still_works_as_alias(self) -> None:
        """旧写法 ./run.py weekly 不能失效（README/脚本里可能还有）。"""
        a = run.build_parser().parse_args(["weekly"])
        self.assertFalse(a.skip_quality)

    def test_bare_run_prints_help_and_runs_nothing(self) -> None:
        """裸跑不能顺手跑流程：只出帮助，退出码 2。"""
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = run.main([])
        self.assertEqual(rc, 2)
        text = buf.getvalue()
        self.assertIn("run.py all", text)
        self.assertIn("./run.py menu", text)
        self.assertNotIn("① 选股", text)

    def test_top_requires_code(self) -> None:
        p = run.build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["top"])

    def test_weekly_defaults(self) -> None:
        a = run.build_parser().parse_args(["weekly"])
        self.assertEqual(a.cycle_args, "--workers 2")
        self.assertFalse(a.keep_going)

    def test_cycle_and_quality_single_stock(self) -> None:
        """3/4 两项也支持单只：`--stock` 转成各自的参数，不再需要 CSV。"""
        p = run.build_parser()
        self.assertEqual(p.parse_args(["cycle", "--stock", "600519"]).stock, "600519")
        self.assertEqual(p.parse_args(["quality", "--stock", "600519"]).stock, "600519")
        self.assertIsNone(p.parse_args(["cycle"]).stock)

    def test_single_mode_rejects_other_date(self) -> None:
        """单只模式的基准日由程序自己算，显式给个别的日期要拒绝，别让人以为跑的是那天。"""
        import io
        import tempfile
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            tee = run.Tee(Path(tmp) / "t.log")
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(run.note_single_date(tee, "2026-11-13", "2026-10-01"), 2)
                    self.assertEqual(run.note_single_date(tee, "2026-11-13", None), 0)
                    self.assertEqual(run.note_single_date(tee, "2026-11-13", "2026-11-13"), 0)
            finally:
                tee.close()


class PassthroughUnknownTest(unittest.TestCase):
    def test_unknown_flags_go_to_extra(self) -> None:
        """`--force` 这类子进程参数必须能透传，不能被 argparse 判成 unrecognized。"""
        p = run.build_parser()
        args, unknown = p.parse_known_args(["cycle", "--stock", "600519", "--force"])
        self.assertEqual(args.stock, "600519")
        self.assertEqual(unknown, ["--force"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
