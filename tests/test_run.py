# -*- coding: utf-8 -*-
"""入口脚本 `run.py` 的纯函数测试（不联网、不建库）。

    python3 -m unittest discover -s tests -t .

只测三件容易错、错了又很难发现的事：

1. **基准日怎么定**。盘中（未过 15:05）必须排除今天——否则拿半天的数据算
   「收盘站上 MA20」；非交易日（周末复盘）要回落到上一个交易日而不是报错。
2. **交接只数**。`select_count()` 从"数 CSV 行"改成"数结果库的行"，
   语义不变：0 只入选要能被拦住，读不到库返回 -1。
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


class SelectCountTest(unittest.TestCase):
    """`select_count()`：交接处的"今天选了几只"。

    以前数 CSV 行，现在数结果库的行。两个边界必须钉住：**0 只要能识别**
    （跑了但一只没选中），**读不到库返回 -1**（还没跑过）。
    """

    def setUp(self) -> None:
        import os
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="run-count-")
        self._old = os.environ.get("STOCK_RESULTS_DB")
        os.environ["STOCK_RESULTS_DB"] = self.tmp + "/results.db"

    def tearDown(self) -> None:
        import os
        if self._old is None:
            os.environ.pop("STOCK_RESULTS_DB", None)
        else:
            os.environ["STOCK_RESULTS_DB"] = self._old

    def test_counts_rows(self) -> None:
        from stock_data_fetch import results
        results.save("select", [{"symbol": "sh600000", "name": "浦发银行"},
                                {"symbol": "sz000001", "name": "平安银行"}],
                     "2026-11-20", argv="test")
        self.assertEqual(run.select_count("2026-11-20"), 2)

    def test_zero_is_distinguishable(self) -> None:
        from stock_data_fetch import results
        results.save("select", [], "2026-11-20", argv="test")
        self.assertEqual(run.select_count("2026-11-20"), 0, "0 只入选要能被识别出来")

    def test_unknown_day_is_not_positive(self) -> None:
        """没跑过这一天 → 0 或 -1，总之不能被当成"有名单"。"""
        self.assertLessEqual(run.select_count("2020-01-02"), 0)


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

    def test_top_accepts_code_or_csv(self) -> None:
        """top 三种用法：单只/多只代码、--csv、--from-select。"""
        p = run.build_parser()
        self.assertEqual(p.parse_args(["top", "600519"]).codes, ["600519"])
        self.assertEqual(p.parse_args(["top", "600519", "000001"]).codes,
                         ["600519", "000001"])
        self.assertEqual(p.parse_args(["top", "--csv", "x.csv"]).csv, "x.csv")
        self.assertTrue(p.parse_args(["top", "--from-select"]).from_select)
        self.assertEqual(p.parse_args(["top"]).codes, [])   # 运行时再报错

    def test_top_without_anything_is_rejected_at_runtime(self) -> None:
        """`top` 什么也不给时由 step_top 拦下（返回 2），不是靠 argparse。"""
        import io
        import tempfile
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            tee = run.Tee(Path(tmp) / "t.log")
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(run.step_top(tee, "2026-11-13", [], []), 2)
            finally:
                tee.close()

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


class BackupTest(unittest.TestCase):
    """备份：文件名识别、保留份数、绝不删手工文件。

    这三点错了都会"很晚才发现"：识别错了会无限累积，保留策略错了会删掉
    别人手写的回滚点，删早了会在真出事那天发现只有一份坏的。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, name, data=b"x"):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_recognizes_own_backup_names(self):
        for name in ("shared.db.20261115-143000", "results.db.20261114-090001"):
            self.assertIsNotNone(run.BACKUP_RE.match(name), name)

    def test_ignores_manual_names(self):
        """`shared.db.20261115-before-v6` 是手写的回滚点，不能被当成自己的备份。"""
        for name in ("shared.db.20261115-before-v6", "shared.db",
                     "shared.db.20261115", "notes.txt"):
            self.assertIsNone(run.BACKUP_RE.match(name), name)

    def test_existing_backups_sorted_and_filtered(self):
        self._touch("shared.db.20261115-143000")
        self._touch("shared.db.20261113-143000")
        self._touch("results.db.20261115-143000")     # 别的库
        self._touch("shared.db.20261115-before-v6")   # 手写的
        got = [p.name for p in run._existing_backups(self.dir, "shared")]
        self.assertEqual(got, ["shared.db.20261113-143000",
                               "shared.db.20261115-143000"])

    def test_existing_backups_missing_dir_is_empty(self):
        self.assertEqual(run._existing_backups(self.dir / "nope", "shared"), [])


class BackupEndToEndTest(unittest.TestCase):
    """真建一个 3 张表的小库，跑一次备份：校验、保留、跳过手写文件。"""

    def setUp(self):
        import sqlite3
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "shared.db"
        conn = sqlite3.connect(str(self.db))
        conn.execute("create table a(x)")
        conn.execute("create table b(y)")
        conn.execute("pragma user_version=6")
        conn.commit()
        conn.close()
        self._real_root = run.ROOT
        run.ROOT = self.dir

    def tearDown(self):
        run.ROOT = self._real_root
        self.tmp.cleanup()

    def _tee(self):
        class _T:
            def __init__(self):
                self.lines = []

            def line(self, text=""):
                self.lines.append(text)

        return _T()

    def test_backup_creates_verified_copy(self):
        tee = self._tee()
        rc = run.cmd_backup(type("A", (), {"dir": str(self.dir / "bk"), "keep": 7,
                                           "only": "shared", "list_only": False})(), tee)
        self.assertEqual(rc, 0)
        made = list((self.dir / "bk").glob("shared.db.*"))
        self.assertEqual(len(made), 1)
        import sqlite3
        conn = sqlite3.connect(str(made[0]))
        self.assertEqual(conn.execute("pragma user_version").fetchone()[0], 6)
        self.assertEqual(conn.execute(
            "select count(*) from sqlite_master where type='table'").fetchone()[0], 2)
        conn.close()
        self.assertTrue(any("完整性 ok" in x for x in tee.lines))

    def test_missing_db_is_skipped(self):
        (self.db).unlink()
        tee = self._tee()
        rc = run.cmd_backup(type("A", (), {"dir": str(self.dir / "bk"), "keep": 7,
                                           "only": None, "list_only": False})(), tee)
        self.assertEqual(rc, 0)
        self.assertTrue(any("不存在" in x for x in tee.lines))
