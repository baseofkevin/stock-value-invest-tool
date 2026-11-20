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
import os
import sys
import tempfile
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
        with self.assertRaises(run.BadDate) as ctx:
            run.pick_date("2026-11-30", self.days)
        self.assertIn("不在交易日历里", str(ctx.exception))

    def test_explicit_date_passthrough(self) -> None:
        self.assertEqual(run.pick_date("2026-11-18", self.days), "2026-11-18")

    def test_empty_calendar_errors(self) -> None:
        with self.assertRaises(run.BadDate):
            run.pick_date(None, [])

    def test_bad_date_has_its_own_exit_code(self) -> None:
        """日期错必须退 4，不能和"某步有票失败"的 1 混在一起。

        原来走 `raise SystemExit(字符串)`，而带字符串的 SystemExit 解释器只退 1：
        `./run.py all && ...` 于是分不清"日期敲错"和"上游缺数据"。
        """
        self.assertEqual(run.EXIT_BAD_DATE, 4)
        self.assertEqual(run.BadDate.code, run.EXIT_BAD_DATE)
        self.assertNotIn(run.EXIT_BAD_DATE, (1, 2, 3, 130))


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


class AllFlowTest(unittest.TestCase):
    """`cmd_all` 的接线：五步都跑到、顺序对、默认不起看板。

    真跑一遍要三十多分钟还会打上游，所以这里把各个 step 换成假的，
    只验"谁被调用、以什么参数、先后顺序"——这几种错（漏一步、顺序乱、
    看板被当成默认最后一步）恰恰是改代码时最容易犯又最难在真跑前发现的。
    """

    def setUp(self):
        """给每个用例一个临时结果库：judge_step 要读 run 台账。"""
        import os
        import tempfile
        self.tmp_db = tempfile.mkdtemp(prefix="run-all-")
        self._old_db = os.environ.get("STOCK_RESULTS_DB")
        os.environ["STOCK_RESULTS_DB"] = os.path.join(self.tmp_db, "results.db")

    def tearDown(self):
        import os
        if self._old_db is None:
            os.environ.pop("STOCK_RESULTS_DB", None)
        else:
            os.environ["STOCK_RESULTS_DB"] = self._old_db

    def _run(self, cycle_rc=0, **over):
        import io
        import tempfile
        from contextlib import redirect_stdout

        calls = []

        def fake(name):
            def step(*a, **kw):
                calls.append((name, a, kw))
                run.LAST_SPENT["s"] = 0.1
                if name == "step_cycle":
                    return cycle_rc
                if name == "step_regime":
                    return over.get("regime_rc", 0)
                return 0
            return step

        patch = {name: fake(name) for name in
                 ("step_select", "step_cycle", "step_quality", "step_top",
                  "step_regime", "step_web")}
        originals = {k: getattr(run, k) for k in patch}
        namespace = dict(
            date="2026-11-13", no_calendar=True, select_args=[], cycle_args=[],
            quality_args=[], top_args=[], top_workers=2, skip_cycle=False,
            skip_quality=False, skip_top=False, skip_regime=False,
            regime_args=[], keep_going=False, strict=False,
            web=False, host="127.0.0.1", port=5000)
        namespace.update(over)
        args = type("A", (), namespace)()
        with tempfile.TemporaryDirectory() as tmp:
            tee = run.Tee(Path(tmp) / "t.log")
            try:
                for k, v in patch.items():
                    setattr(run, k, v)
                run.ensure_calendar = lambda *a, **kw: ("2026-11-13", 0)
                with redirect_stdout(io.StringIO()):
                    rc = run.cmd_all(args, tee)
            finally:
                for k, v in originals.items():
                    setattr(run, k, v)
                tee.close()
        return rc, [c[0] for c in calls], calls

    def test_five_steps_in_order(self):
        rc, names, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(names, ["step_select", "step_cycle", "step_quality",
                                 "step_top", "step_regime"])
        self.assertNotIn("step_web", names, "all 默认不再起看板")

    def test_top_is_batch_mode(self):
        """全流程里的顶部判断走批量（读库里的名单），不是单只。"""
        _rc, _names, calls = self._run()
        top = dict((c[0], c) for c in calls)["step_top"]
        self.assertEqual(top[2].get("csv_path"), "results")

    def test_regime_failure_is_not_fatal(self):
        """大势算不成（上游挂/三条都空）只影响横幅，不改整条流程的退出码。"""
        rc, names, _ = self._run(regime_rc=2)
        self.assertEqual(rc, 0)
        self.assertIn("step_regime", names)

    def test_skip_regime(self):
        _rc, names, _ = self._run(skip_regime=True)
        self.assertNotIn("step_regime", names)

    def test_web_skips_server_by_default(self):
        rc, names, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertNotIn("step_web", names)

    def test_web_flag_starts_server(self):
        rc, names, _ = self._run(web=True)
        self.assertEqual(rc, 0)
        self.assertIn("step_web", names)

    def test_web_gets_host_and_port(self):
        _rc, _names, calls = self._run(web=True, port=8081)
        web = dict((c[0], c) for c in calls)["step_web"]
        self.assertEqual(web[1][0].port, 8081)

    def test_partial_failure_continues(self):
        """个别票上游缺数据导致退出码 1，但只要写进了行，就该继续往下跑。

        2026-11-15 的真实场景：周期弹性 35 只成功、3 只「利润表为空」，
        退出码 1，整条流程停在第二步，质量和顶部再也没跑。
        """
        self._fake_run(kind="batch", n_ok=35, n_fail=3)
        rc, names, _ = self._run(cycle_rc=1)
        self.assertEqual(rc, 0)
        self.assertIn("step_top", names, "该继续走到顶部判断")

    def test_zero_output_stops(self):
        """一只都没成功（名单对不上/库被锁）必须停，别产出缺腿的结果。"""
        self._fake_run(kind="batch", n_ok=0, n_fail=0)
        rc, names, _ = self._run(cycle_rc=1)
        self.assertEqual(rc, 1)
        self.assertNotIn("step_quality", names)

    def test_strict_stops_on_partial(self):
        self._fake_run(kind="batch", n_ok=35, n_fail=3)
        rc, names, _ = self._run(cycle_rc=1, strict=True)
        self.assertEqual(rc, 1)
        self.assertNotIn("step_quality", names)

    def test_single_runs_do_not_count_as_output(self):
        """单只运行不算"这一步有产出"，别让一次单跑把失败的一批放过。"""
        self._fake_run(kind="single", n_ok=1, n_fail=0)
        rc, names, _ = self._run(cycle_rc=1)
        self.assertEqual(rc, 1)
        self.assertNotIn("step_quality", names)

    def _fake_run(self, kind: str, n_ok: int, n_fail: int) -> None:
        from stock_data_fetch import results
        import os
        path = os.environ["STOCK_RESULTS_DB"]
        results.save("cycle", [{"symbol": "sh600019"}], "2026-11-13",
                     db=path, kind=kind)
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("update run set n_ok=?, n_fail=? where tool='cycle'",
                     (n_ok, n_fail))
        conn.commit()
        conn.close()


class PassthroughTest(unittest.TestCase):
    def test_split(self) -> None:
        self.assertEqual(run.split_passthrough("--workers 2"), ["--workers", "2"])
        self.assertEqual(run.split_passthrough("  "), [])
        self.assertEqual(run.split_passthrough("--force"), ["--force"])


class TeeFilterTest(unittest.TestCase):
    """屏幕：只有一条进度条。日志：一行不少。

    2026-11-15 两次反馈都是"显示太多了"：先是逐只失败刷屏，后来是"关键信息"
    也还是太多。现在屏幕上只留进度条 + 收尾两行，其余全进日志。
    """

    def _tee(self, verbose=False, progress=None):
        self.tmp = tempfile.mkdtemp(prefix="tee-")
        return run.Tee(Path(self.tmp) / "t.log", verbose=verbose,
                       progress=progress)

    def test_console_is_silent_when_not_a_tty(self):
        """重定向/管道里什么也不打（每步一行由 finish_bar 负责，不在这里）。"""
        import io
        from contextlib import redirect_stdout
        tee = self._tee()
        buf = io.StringIO()
        with redirect_stdout(buf):
            tee.line("kline sh600185 失败：末端不足")
            tee.line("[1/3] FAIL 300375 利润表为空")
            tee.line("⚠️  145 只历史不足（次新/长期停牌），按「K线不足」剔除")
            tee.line("")
        tee.close()
        self.assertEqual(buf.getvalue(), "")
        log = Path(self.tmp, "t.log").read_text()
        for text in ("kline sh600185 失败", "FAIL 300375", "145 只历史不足"):
            self.assertIn(text, log, "日志必须是全量")

    def test_progress_line_becomes_bar(self):
        """`[3/38] …` 不上屏幕，而是变成进度条上的 3/38。"""
        import io
        from contextlib import redirect_stdout
        tee = self._tee(progress=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            tee.start_bar("② 周期弹性（2026-11-13）")
            tee.line("[ 1/38] 宝钢股份 sh600019  观察")
            tee.line("[ 2/38] 洪都航空 sh600316  观察")
            tee.line("[38/38] 中国海诚 sz002116  观察")
            tee.finish_bar("✅ 38 只 · 1.2s")
        tee.close()
        console = buf.getvalue()
        self.assertNotIn("宝钢股份", console, "逐只明细不该上屏幕")
        self.assertIn("38/38", console)
        self.assertIn("② 周期弹性", console)
        self.assertIn("✅ 38 只", console)
        self.assertIn("[ 2/38] 洪都航空", Path(self.tmp, "t.log").read_text())

    def test_stage_progress_also_drives_bar(self):
        """选股的 `[2/6] 横截面粗筛…` 同样是 n/m，也要推着进度条走。"""
        import io
        from contextlib import redirect_stdout
        tee = self._tee(progress=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            tee.start_bar("① 选股")
            tee.line("[2/6] 横截面粗筛（5 天）...")
            tee.finish_bar("✅ 38 只 · 23.4s")
        tee.close()
        self.assertIn("2/6", buf.getvalue())

    def test_verbose_shows_everything(self):
        import io
        from contextlib import redirect_stdout
        tee = self._tee(verbose=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            tee.line("kline sh600185 失败：末端不足")
        tee.close()
        self.assertIn("kline sh600185", buf.getvalue())
        self.assertEqual(tee.muted, 0)

    def test_verbose_keeps_blank_lines_collapsed(self):
        import io
        from contextlib import redirect_stdout
        tee = self._tee(verbose=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            tee.line("a")
            for _ in range(3):
                tee.line("")
            tee.line("b")
        tee.close()
        self.assertEqual(buf.getvalue().splitlines(), ["a", "", "b"])

    def test_alert_always_reaches_the_screen(self):
        """名单是空的这种"流程要停"的消息，必须绕过进度条直接说。"""
        import io
        from contextlib import redirect_stdout
        tee = self._tee()
        buf = io.StringIO()
        with redirect_stdout(buf):
            tee.alert("❌ 结果库里没有 2026-11-13 的选股名单")
        tee.close()
        self.assertIn("没有 2026-11-13 的选股名单", buf.getvalue())
        self.assertIn("没有 2026-11-13 的选股名单",
                      Path(self.tmp, "t.log").read_text())


class ParserTest(unittest.TestCase):
    def test_subcommands_exist(self) -> None:
        p = run.build_parser()
        for name in ("all", "weekly", "select", "cycle", "quality", "top",
                     "price", "regime", "backup", "web", "menu"):
            self.assertIn(name, p._subparsers._group_actions[0].choices)

    def test_regime_passthrough_keeps_order(self) -> None:
        """`regime --json x.json` 的开关和值不能被拆散——拆散了 argparse 会报
        "expected one argument"，整步退出码 2。"""
        _a, unknown = run.build_parser().parse_known_args(
            ["regime", "--json", "/tmp/x.json", "--recompute"])
        self.assertEqual(unknown, ["--json", "/tmp/x.json", "--recompute"])

    def test_price_accepts_single_code(self) -> None:
        """`price --code` 要由 argparse 吃下，不能漏进 extra 变成子进程参数错。"""
        a = run.build_parser().parse_args(
            ["price", "--code", "002096", "--date", "2026-11-03"])
        self.assertEqual(a.code, "002096")
        self.assertEqual(a.date, "2026-11-03")
        self.assertEqual(a.extra, [])
        self.assertTrue(run.build_parser()
                        .parse_args(["price", "--code", "sz002096"]).code)

    def test_calendar_is_not_a_subcommand(self) -> None:
        """日历不再单列：它只是全流程的第 0 步，单列只会让人不知道怎么用。

        真要单独更新，用 `python3 -m stock_data_fetch.build_calendar`
        （`ensure_calendar` 也会在过期时自动跑它）。
        """
        p = run.build_parser()
        self.assertNotIn("calendar", p._subparsers._group_actions[0].choices)

    def test_web_debug_default_on(self) -> None:
        """`run.py web` 默认等同旧 `--debug`；要关得显式 --no-debug。"""
        p = run.build_parser()
        self.assertTrue(p.parse_args(["web"]).debug)
        self.assertTrue(p.parse_args(["web", "--debug"]).debug)
        self.assertFalse(p.parse_args(["web", "--no-debug"]).debug)

    def test_all_is_full_pipeline(self) -> None:
        """全流程 = 日历 → 选股 → 周期 → 质量 → 顶部 → 大势，默认不含看板。"""
        a = run.build_parser().parse_args(["all"])
        self.assertEqual(a.cycle_args, "--workers 2")
        self.assertEqual(a.quality_args, "--workers 2")
        self.assertEqual(a.top_workers, 4)
        self.assertFalse(a.web, "默认不起看板")
        self.assertFalse(a.skip_top)
        self.assertEqual(a.port, 5000)

    def test_all_web_is_opt_in(self) -> None:
        """想看结果时用 --web 显式起看板；旧 --no-web 仍可解析（兼容脚本）。"""
        p = run.build_parser()
        self.assertTrue(p.parse_args(["all", "--web"]).web)
        self.assertTrue(p.parse_args(["all", "--no-web"]).no_web)

    def test_all_flow_steps_in_order(self) -> None:
        """照着源码里的调用顺序核对，别让某一步被后来的人悄悄删掉。"""
        import inspect
        src = inspect.getsource(run.cmd_all)
        order = ["step_select", "step_cycle", "step_quality", "step_top",
                 "step_regime"]
        positions = [src.index(name) for name in order]
        self.assertEqual(positions, sorted(positions), "步骤顺序变了：应从小到大")
        self.assertIn('getattr(args, "web"', src, "看板必须是可选步骤")

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

    def test_menu_option_7_runs_regime(self) -> None:
        """菜单 7) 要真的调 `step_regime`——选项加了、分支没接是最容易漏的错。

        补这一条是因为 7 是 2026-11-20 新加的：菜单文案、`input()` 的 [0-7]、
        校验集合、分支四处必须同时改，少一处就静默走到「顶部判断」。
        """
        from unittest import mock

        calls = []

        def fake_step_regime(tee, day, extra):
            calls.append((day, list(extra)))
            return 0

        class _FakeTee:
            path = "fake.log"
            def line(self, *_a, **_k): pass
            def alert(self, *_a, **_k): pass
            def close(self): pass

        answers = iter(["7", "2026-11-20", "", "0"])
        with mock.patch("builtins.input", lambda *a: next(answers)), \
                mock.patch.object(run, "ensure_calendar",
                                  lambda *a, **k: ("2026-11-20", 0)), \
                mock.patch.object(run, "step_regime", fake_step_regime), \
                mock.patch.object(run, "load_calendar", lambda: []), \
                mock.patch.object(run, "Tee", lambda *a, **k: _FakeTee()), \
                mock.patch.object(run, "say", lambda *a, **k: None):
            rc = run.interactive()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("2026-11-20", [])])

    def test_help_lists_csv_examples_for_all_three_tools(self) -> None:
        """`-h` 里要能直接抄到 cycle/quality/top 的 --csv 用法。"""
        text = run.build_parser().format_help()
        self.assertIn("./run.py cycle   --csv", text)
        self.assertIn("./run.py quality --csv ~/MyData/选股_2026-11-06.csv --workers 2", text)
        self.assertIn("--workers 2 --retries 0", text)
        self.assertIn("./run.py top     --csv", text)

    def test_subcommand_help_lists_csv_examples(self) -> None:
        choices = run.build_parser()._subparsers._group_actions[0].choices
        self.assertIn("--csv ~/MyData/选股_2026-11-06.csv --workers 2 --retries 0",
                      choices["quality"].format_help())
        self.assertIn("--csv ~/MyData/选股_2026-11-06.csv --workers 4",
                      choices["cycle"].format_help())
        self.assertIn("./run.py top --csv ~/MyData/持仓.csv",
                      choices["top"].format_help())

    def test_top_accepts_code_or_csv(self) -> None:
        """top 三种用法：单只/多只代码、--csv、--from-select。"""
        p = run.build_parser()
        self.assertEqual(p.parse_args(["top", "600519"]).codes, ["600519"])
        self.assertEqual(p.parse_args(["top", "600519", "000001"]).codes,
                         ["600519", "000001"])
        self.assertEqual(p.parse_args(["top", "--csv", "x.csv"]).csv, "x.csv")
        self.assertTrue(p.parse_args(["top", "--from-select"]).from_select)
        self.assertTrue(p.parse_args(["top", "--refresh"]).refresh)
        self.assertEqual(p.parse_args(["top"]).codes, [])   # 运行时再报错

    def test_cycle_and_quality_accept_csv(self) -> None:
        """`--csv` 必须由 argparse 吃下：否则它会被透传到 extra 末尾，
        变成 `--from-select <date> x.csv --csv`，工具直接报参数错。"""
        p = run.build_parser()
        for cmd in ("cycle", "quality"):
            with self.subTest(cmd=cmd):
                a = p.parse_args([cmd, "--csv", "x.csv"])
                self.assertEqual(a.csv, "x.csv")
                self.assertEqual(a.extra, [])

    def test_cycle_and_quality_accept_workers_and_force(self) -> None:
        """并发/重试参数也得由 argparse 吃下，否则 `--workers 8` 会被拆散。"""
        p = run.build_parser()
        q = p.parse_args(["quality", "--csv", "x.csv", "--workers", "8",
                          "--retries", "0", "--force"])
        self.assertEqual((q.workers, q.retries, q.force), (8, 0, True))
        self.assertEqual(q.extra, [])
        c = p.parse_args(["cycle", "--workers", "8", "--force"])
        self.assertEqual((c.workers, c.force), (8, True))
        self.assertEqual(c.extra, [])

    def test_cycle_and_quality_forward_csv_without_from_select(self) -> None:
        """给了 CSV 就不要再注入 --from-select（两者在工具里互斥）。"""
        calls = []
        orig = run.run_step

        def fake(tee, title, argv, cwd, **kw):
            calls.append(list(argv))
            return 0, 0

        run.run_step = fake
        try:
            run.step_quality(None, "2026-11-13", ["--force"], csv_path=Path("x.csv"))
            run.step_cycle(None, "2026-11-13", [], csv_path=Path("x.csv"))
        finally:
            run.run_step = orig
        self.assertEqual(calls[0][1:], ["stock_report.py", "--csv", "x.csv", "--force"])
        self.assertEqual(calls[1][1:], ["cycle_report.py", "--csv", "x.csv"])
        for argv in calls:
            self.assertNotIn("--from-select", argv)

    def test_top_csv_is_forwarded_not_turned_into_from_select(self) -> None:
        """`top --csv` 必须把 CSV 交给 stock_judge_top，不能静默改跑库里名单。"""
        calls = []
        orig = run.run_step

        def fake(tee, title, argv, cwd, **kw):
            calls.append(list(argv))
            return 0, 0

        run.run_step = fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                csv = Path(tmp) / "持仓.csv"
                csv.write_text("代码\n600019\n", encoding="utf-8")
                run.step_top(type("T", (), {"verbose": False})(),
                             "2026-11-13", [], [], csv_path=csv)
        finally:
            run.run_step = orig
        self.assertIn("--csv", calls[0])
        self.assertEqual(calls[0][calls[0].index("--csv") + 1], str(csv))
        self.assertNotIn("--from-select", calls[0])

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
        """没声明的子进程参数必须能透传，不能被 argparse 判成 unrecognized。"""
        p = run.build_parser()
        args, unknown = p.parse_known_args(["cycle", "--stock", "600519", "--foo"])
        self.assertEqual(args.stock, "600519")
        self.assertEqual(unknown, ["--foo"])
        self.assertTrue(p.parse_args(["cycle", "--force"]).force)


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
