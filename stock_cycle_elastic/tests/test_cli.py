"""CLI 测试（README.md「CLI 参考」/「测试」）。

CLI 是唯一对外的门面，错在细节上最伤用户体验：
    * 退出码错了 → 脚本自动化判断失效
    * 产物落到 CWD 了 → 用户找不到文件
    * 输入 CSV 被改了 → 用户的数据被工具污染
    * 名称歧义时还生成了半个文件 → 用户以为跑成功了

所以这里既测"成功路径"，也把每个失败路径的**退出码**和**副作用**逐个钉住。
取数一律走 `--bundle` 离线快照，不碰网络。
"""

import contextlib
import hashlib
import io
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import tests.helpers  # noqa: F401  —— 负责 sys.path 注入
from tests import helpers
from tests.fixtures import FIXTURE_DIR, load

import config
import cycle_report
from cycle import batch, fetch

# ---------------------------------------------------------------------------
# 运行辅助
# ---------------------------------------------------------------------------

#: 端到端用的离线 bundle（data_finance + data_profile 都在里面）
BUNDLE_NAME = "full_40q"


def bundle_path(name=BUNDLE_NAME):
    return os.path.join(FIXTURE_DIR, name + ".json")


#: 用例产生的临时目录，由 atexit 统一清理
_TEMP_DIRS = []


def _cleanup_temp_dirs():
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


import atexit  # noqa: E402  —— 只用于收尾，放这里避免污染顶部 import 顺序

atexit.register(_cleanup_temp_dirs)


def run_main(argv, out_dir=None):
    """跑 main()，返回 (exit_code, stdout, stderr, out_dir)。

    **`--out-dir` 参数已删除**（产物位置统一由 `_out_dir_for()` 决定，
    见 cycle_report 模块文档）。测试不能靠命令行改输出目录了，
    改为 patch 掉 `_out_dir_for` 让它返回临时目录：

    这样断言仍然是"产物落在 out_dir 里"，而不是落在真实的
    `output/cycle-elastic/` 里把真报告污染掉。

    **临时目录不在这里删** —— 调用方要在返回后检查产物，
    删掉的话所有 "文件是否生成" 的断言都会假失败。
    由 `_TEMP_DIRS` + atexit 统一清理。
    """
    argv = list(argv)
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="cycle_cli_")
    out_dir = os.path.abspath(out_dir)
    _TEMP_DIRS.append(out_dir)

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(cycle_report, "_out_dir_for", return_value=out_dir):
        code = cycle_report.main(argv)

    return code, out.getvalue(), err.getvalue(), out_dir


def list_files(directory):
    if not os.path.isdir(directory):
        return []
    return sorted(os.listdir(directory))


def md5_of(path):
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


# ---------------------------------------------------------------------------
# 1. 参数解析
# ---------------------------------------------------------------------------


class TestParser(unittest.TestCase):

    def setUp(self):
        self.parser = cycle_report.build_parser()

    def test_stock_and_csv_are_mutually_exclusive(self):
        """argparse 直接拒绝，退出码 2（SystemExit）。"""
        # argparse 会把 usage + 错误打到 stderr，这里收掉，免得污染测试输出
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self.parser.parse_args(["--stock", "600519", "--csv", "池.csv"])
        self.assertEqual(ctx.exception.code, 2)

    def test_defaults(self):
        args = self.parser.parse_args(["--stock", "600519"])
        self.assertEqual(args.workers, 4)
        self.assertEqual(args.sample, 500)
        self.assertFalse(args.force)
        self.assertFalse(args.no_index)
        self.assertFalse(args.quiet)

    def test_removed_options_are_gone(self):
        """`--out-dir` / `--timeout` / `--retries` / `--retry-wait` 都已删除。

        - `--out-dir`：产物目录由 `_out_dir_for()` 统一算，不给用户覆盖的口子。
        - 后三个：原本控制 node 客户端，迁移到共享数据层后超时由 Throttle 管、
          重试由 `_ensure_retrying` 管，留着不生效的参数比删掉更误导人
          （用户会以为调它能改变行为）。

        断言"不存在"而不是只删断言：删掉的东西不该被悄悄加回来。
        """
        args = self.parser.parse_args(["--stock", "600519"])
        for name in ("out_dir", "timeout", "retries", "retry_wait"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(args, name), f"{name} 不该还存在")

        # 传了要报 2（argparse 不认识的参数），不能静默忽略
        for argv in (["--stock", "600519", "--timeout", "30"],
                     ["--stock", "600519", "--retries", "3"],
                     ["--stock", "600519", "--retry-wait", "10"],
                     ["--stock", "600519", "--out-dir", "/tmp"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    self.parser.parse_args(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_help_mentions_both_modes(self):
        text = self.parser.format_help()
        self.assertIn("--stock", text)
        self.assertIn("--csv", text)
        self.assertIn("--workers", text)

    def test_epilog_has_examples(self):
        self.assertIn("--stock 600519", self.parser.epilog)
        self.assertIn("--csv 池.csv", self.parser.epilog)


class TestValidateArgs(unittest.TestCase):

    def check(self, argv):
        parser = cycle_report.build_parser()
        args = parser.parse_args(argv)
        return cycle_report.validate_args(args, parser)

    def test_no_source_rejected(self):
        self.assertIn("必须指定", self.check([]))

    def test_valid_stock(self):
        self.assertIsNone(self.check(["--stock", "600519"]))

    def test_valid_csv(self):
        self.assertIsNone(self.check(["--csv", "池.csv"]))

    def test_zero_workers_rejected(self):
        self.assertIn("--workers", self.check(["--stock", "600519", "--workers", "0"]))

    def test_bundle_requires_stock(self):
        self.assertIn("--bundle", self.check(["--csv", "池.csv", "--bundle", "x.json"]))

    def test_stats_zero_sample_rejected(self):
        self.assertIn("--sample", self.check(["--stats", "--sample", "0"]))

    def test_stats_negative_seed_rejected(self):
        self.assertIn("--seed", self.check(["--stats", "--seed", "-1"]))

    def test_stats_needs_no_source(self):
        """--stats 不需要 --stock/--csv。"""
        self.assertIsNone(self.check(["--stats"]))

    def test_refresh_names_needs_no_source(self):
        self.assertIsNone(self.check(["--refresh-names"]))


# ---------------------------------------------------------------------------
# 2. 退出码常量
# ---------------------------------------------------------------------------


class TestExitCodes(unittest.TestCase):
    """README.md「退出码」的五个码。"""

    def test_values(self):
        self.assertEqual(cycle_report.EXIT_OK, 0)
        self.assertEqual(cycle_report.EXIT_PARTIAL, 1)
        self.assertEqual(cycle_report.EXIT_USAGE, 2)
        self.assertEqual(cycle_report.EXIT_UNEXPECTED, 3)
        self.assertEqual(cycle_report.EXIT_INTERRUPT, 130)

    def test_usage_error_returns_2(self):
        code, out, err, _ = run_main([])
        self.assertEqual(code, cycle_report.EXIT_USAGE)
        self.assertIn("必须指定", err)
        # 参数错误要打用法提示
        self.assertIn("usage", err.lower())

    def test_bad_stock_returns_2(self):
        code, out, err, _ = run_main(["--stock", "abc"])
        self.assertEqual(code, cycle_report.EXIT_USAGE)

    def test_missing_csv_returns_2(self):
        code, out, err, out_dir = run_main(["--csv", "/nonexistent/池.csv"])
        self.assertEqual(code, cycle_report.EXIT_USAGE)
        self.assertIn("CSV 不存在", err)
        # 不能建出空目录里的半个东西
        self.assertEqual(list_files(out_dir), [])

    def test_unexpected_exception_returns_3(self):
        """任何未捕获异常 → 3，且写 traceback（不静默吞掉）。"""
        with mock.patch.object(cycle_report, "run_single",
                               side_effect=RuntimeError("boom")):
            code, out, err, _ = run_main(["--stock", "600519"])
        self.assertEqual(code, cycle_report.EXIT_UNEXPECTED)
        self.assertIn("未预期异常", err)
        self.assertIn("boom", err)

    def test_keyboard_interrupt_returns_130(self):
        with mock.patch.object(cycle_report, "run_single",
                               side_effect=KeyboardInterrupt):
            code, out, err, _ = run_main(["--stock", "600519"])
        self.assertEqual(code, cycle_report.EXIT_INTERRUPT)
        self.assertIn("中断", err)


# ---------------------------------------------------------------------------
# 3. 名称解析（§6.3）
# ---------------------------------------------------------------------------


class TestResolveStock(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cycle_names_")
        self.names_csv = os.path.join(self.tmp, "stock_names.csv")
        self.patch = mock.patch.object(config, "STOCK_NAMES_CSV", self.names_csv)
        self.patch.start()
        cycle_report.load_name_table.cache_clear = getattr(
            cycle_report.load_name_table, "cache_clear", lambda: None)

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_names(self, lines):
        with open(self.names_csv, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("代码,名称\n")
            for line in lines:
                fh.write(line + "\n")

    def test_plain_code(self):
        code, error, cands = cycle_report.resolve_stock("600519")
        self.assertEqual(code, "600519")
        self.assertIsNone(error)

    def test_prefixed_code(self):
        self.assertEqual(cycle_report.resolve_stock("sh600519")[0], "600519")
        self.assertEqual(cycle_report.resolve_stock("sz000001")[0], "000001")
        self.assertEqual(cycle_report.resolve_stock("bj920001")[0], "920001")

    def test_exact_name(self):
        self.write_names(["600519,贵州茅台", "000001,平安银行"])
        code, error, cands = cycle_report.resolve_stock("贵州茅台")
        self.assertEqual(code, "600519")
        self.assertIsNone(error)

    def test_st_prefix_stripped(self):
        """用户输"ST股名"，表里是"股名" —— 也能对上。"""
        self.write_names(["600001,某某股份"])
        code, error, _ = cycle_report.resolve_stock("ST某某股份")
        self.assertEqual(code, "600001")

    def test_ambiguous_name_lists_candidates(self):
        self.write_names(["600001,同名公司", "600002,同名公司"])
        code, error, cands = cycle_report.resolve_stock("同名公司")
        self.assertIsNone(code)
        self.assertIn("多个匹配", error)
        self.assertEqual(len(cands), 2)

    def test_close_match_suggestion(self):
        self.write_names(["600519,贵州茅台"])
        code, error, cands = cycle_report.resolve_stock("贵州矛台")
        self.assertIsNone(code)
        self.assertIn("是不是要找", error)
        self.assertTrue(cands)

    def test_unknown_without_table(self):
        """名称表不存在时，给出可操作提示（先 --refresh-names）。"""
        code, error, _ = cycle_report.resolve_stock("贵州茅台")
        self.assertIsNone(code)
        self.assertIn("--refresh-names", error)

    def test_load_name_table_skips_header(self):
        self.write_names(["600519,贵州茅台"])
        table = cycle_report.load_name_table()
        self.assertEqual(table, {"600519": "贵州茅台"})
        self.assertNotIn("名称", table.values())

    def test_load_name_table_tolerates_tab(self):
        with open(self.names_csv, "w", encoding="utf-8") as fh:
            fh.write("代码\t名称\n600519\t贵州茅台\n")
        self.assertEqual(cycle_report.load_name_table(), {"600519": "贵州茅台"})

    def test_load_name_table_missing_file(self):
        self.assertEqual(cycle_report.load_name_table(), {})


# ---------------------------------------------------------------------------
# 4. 单股票模式（端到端，走 --bundle 离线快照）
# ---------------------------------------------------------------------------


class TestSingleMode(unittest.TestCase):

    def test_bundle_end_to_end(self):
        """--bundle 跑通：出 HTML、退出码 0、打印三个判断。"""
        code, out, err, out_dir = run_main(
            ["--stock", "601899", "--bundle", bundle_path()])
        self.assertEqual(code, cycle_report.EXIT_OK)

        files = list_files(out_dir)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith(".html"))
        self.assertIn("601899", files[0])
        self.assertIn(config.REPORT_NAME_TAG, files[0])

        # 终端要打印三个判断
        self.assertIn("是不是周期股", out)
        self.assertIn("弹性", out)
        self.assertIn("周期位置", out)
        self.assertIn("报告", out)

    def test_bundle_report_has_ten_attrs(self):
        _, _, _, out_dir = run_main(
            ["--stock", "601899", "--bundle", bundle_path()])
        path = os.path.join(out_dir, list_files(out_dir)[0])
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()

        for attr in ("data-code", "data-name", "data-quarters", "data-gm-std",
                     "data-cycle", "data-elastic", "data-elastic-ratio",
                     "data-position", "data-position-pct", "data-financial"):
            self.assertIn(f'{attr}="', html)
        self.assertIn('data-code="601899"', html)
        self.assertIn('data-cycle="1"', html)

    def test_bundle_snapshot_is_stable(self):
        """同一份 bundle 跑两次，锚点值逐字相同（判定可复现）。"""
        def attrs_of():
            _, _, _, out_dir = run_main(
                ["--stock", "601899", "--bundle", bundle_path()])
            path = os.path.join(out_dir, list_files(out_dir)[0])
            with open(path, "r", encoding="utf-8") as fh:
                body = re.search(r"<body([^>]*)>", fh.read()).group(1)
            return dict(re.findall(r'(data-[a-z-]+)="([^"]*)"', body))

        self.assertEqual(attrs_of(), attrs_of())

    def test_name_override(self):
        _, _, _, out_dir = run_main(
            ["--stock", "601899", "--bundle", bundle_path(), "--name", "自定义名"])
        self.assertTrue(any("自定义名" in f for f in list_files(out_dir)))

    def test_default_out_dir_points_to_shared_output(self):
        """默认产物落 `stock-value-invest-tool/output/cycle-elastic`，与 CWD 无关。

        原版断言是"落在项目内 html-report/"。改这一条不是迁就新代码——
        原设计"产物进源码目录"本身就是问题：跑几次之后源码目录里躺着几十份
        HTML 和一个 html-report2/，说不清哪份最新。现在四个工具统一进
        `output/<工具名>/`（见 output/README.md）。
        """
        self.assertEqual(config.DEFAULT_OUT_DIR,
                         os.path.join(config.HUB_ROOT, "output", "cycle-elastic"))
        # 必须在 hub 根下，且不在源码目录下
        self.assertTrue(config.DEFAULT_OUT_DIR.startswith(config.HUB_ROOT))
        self.assertFalse(config.DEFAULT_OUT_DIR.startswith(config.PROJECT_ROOT))

    def test_out_dir_for_batch_uses_csv_date(self):
        """批量模式：分批次日录，日期优先取 CSV 文件名。"""
        got = cycle_report._out_dir_for("/x/选股_2026-11-13.csv", [])
        self.assertEqual(os.path.basename(got), "2026-11-13")
        self.assertEqual(os.path.dirname(got), config.DEFAULT_OUT_DIR)

    def test_out_dir_for_batch_falls_back_to_start_column(self):
        """文件名没日期 → 用「启动日」列。"""
        rows = [{"启动日": "2026-11-13"}, {"启动日": "2026-11-13"},
                {"启动日": "2026-11-12"}]
        got = cycle_report._out_dir_for("/x/池.csv", rows)
        self.assertEqual(os.path.basename(got), "2026-11-13")   # 众数，不是第一行

    def test_out_dir_for_single_has_no_date_layer(self):
        """单股没有批次概念，直接落根下 —— 不加日期层。"""
        self.assertEqual(cycle_report._out_dir_for(), config.DEFAULT_OUT_DIR)

    def test_no_cycle_output_dir_created(self):
        """§4 修正：不再生成 cycle-output/。"""
        tmp = tempfile.mkdtemp(prefix="cycle_noout_")
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            code, _, _, _ = run_main(
                ["--stock", "601899", "--bundle", bundle_path()])
            self.assertEqual(code, cycle_report.EXIT_OK)
            self.assertFalse(os.path.exists(os.path.join(tmp, "cycle-output")))
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_quiet_suppresses_stdout(self):
        code, out, err, out_dir = run_main(
            ["--stock", "601899", "--bundle", bundle_path(), "--quiet"])
        self.assertEqual(code, cycle_report.EXIT_OK)
        self.assertEqual(out.strip(), "")
        self.assertEqual(len(list_files(out_dir)), 1)

    def test_data_error_returns_2_and_writes_nothing(self):
        with mock.patch.object(fetch, "fetch_one",
                               side_effect=fetch.DataError("服务限频")):
            code, out, err, out_dir = run_main(["--stock", "600519"])
        self.assertEqual(code, cycle_report.EXIT_USAGE)
        self.assertIn("服务限频", err)
        self.assertEqual(list_files(out_dir), [])

    def test_empty_quarters_returns_2(self):
        raw = load("full_40q")
        empty = {"code": "601899", "name": "紫金矿业", "industry": "",
                 "income": [], "balance": [], "data_gaps": []}
        with mock.patch.object(fetch, "load_bundle", return_value=empty):
            code, _, err, out_dir = run_main(
                ["--stock", "601899", "--bundle", bundle_path()])
        self.assertEqual(code, cycle_report.EXIT_USAGE)
        self.assertIn("没有有效的季度数据", err)
        self.assertEqual(list_files(out_dir), [])

    def test_ambiguous_name_writes_nothing(self):
        """名称歧义 → 退出码 2，**一个文件都不生成**。"""
        tmp = tempfile.mkdtemp(prefix="cycle_amb_")
        names = os.path.join(tmp, "names.csv")
        with open(names, "w", encoding="utf-8") as fh:
            fh.write("代码,名称\n600001,同名\n600002,同名\n")
        try:
            with mock.patch.object(config, "STOCK_NAMES_CSV", names):
                code, out, err, out_dir = run_main(
                    ["--stock", "同名", "--bundle", bundle_path()],
                    out_dir=os.path.join(tmp, "out"))
            self.assertEqual(code, cycle_report.EXIT_USAGE)
            self.assertIn("多个匹配", err)
            self.assertIn("候选", err)
            self.assertEqual(list_files(os.path.join(tmp, "out")), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_short_history_still_exit_0(self):
        """历史不足照样出报告，退出码 0（不是失败）。"""
        code, out, err, out_dir = run_main(
            ["--stock", "301101", "--bundle", bundle_path("short_12q")])
        self.assertEqual(code, cycle_report.EXIT_OK)
        self.assertEqual(len(list_files(out_dir)), 1)
        self.assertIn("历史不足", out)

    def test_financial_prints_note(self):
        _, out, _, _ = run_main(
            ["--stock", "600036", "--bundle", bundle_path("financial")])
        self.assertIn("金融企业", out)

    def test_unstable_prints_label(self):
        _, out, _, _ = run_main(
            ["--stock", "600002", "--bundle", bundle_path("neg_profit")])
        self.assertIn("不稳定", out)


# ---------------------------------------------------------------------------
# 5. 批量模式
# ---------------------------------------------------------------------------


def make_csv(directory, rows, name="池.csv"):
    """rows: [(code, name), ...]"""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("代码,名称\n")
        for code, stock_name in rows:
            fh.write(f"{code},{stock_name}\n")
    return path


class TestBatchMode(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cycle_batch_")
        self.out = os.path.join(self.tmp, "html-report")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_fetch_one(self, code, **kwargs):
        """用 fixture 造一个假取数结果，避免真拉网络。"""
        mapping = {
            "601899": "full_40q",
            "600036": "financial",
            "301101": "short_12q",
        }
        name = mapping.get(code)
        if not name:
            raise fetch.DataError(f"{code} 取数失败")
        return load(name)

    def test_batch_produces_one_html_per_stock_and_index(self):
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业"),
                                       ("600036", "招商银行")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            code, out, err, _ = run_main(["--csv", csv_path], out_dir=self.out)

        self.assertEqual(code, cycle_report.EXIT_OK)
        files = list_files(self.out)
        self.assertIn("池.html", files)
        htmls = [f for f in files if f.endswith(".html") and f != "池.html"]
        self.assertEqual(len(htmls), 2)
        self.assertIn("完成 2 只", out)

    def test_index_links_resolve(self):
        """索引页里的每个 href 都能在本目录找到对应文件（编码正确）。"""
        from urllib.parse import unquote
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            run_main(["--csv", csv_path], out_dir=self.out)

        with open(os.path.join(self.out, "池.html"), "r", encoding="utf-8") as fh:
            index = fh.read()
        hrefs = re.findall(r'<a class="btn" href="([^"]+)"', index)
        self.assertTrue(hrefs)
        for href in hrefs:
            self.assertTrue(all(ord(c) < 128 for c in href), href)
            target = os.path.join(self.out, unquote(href))
            self.assertTrue(os.path.exists(target), target)

    def test_partial_failure_returns_1(self):
        """有失败行 → 退出码 1，但成功的那只照常出报告。"""
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业"),
                                       ("600999", "查无此股")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            code, out, err, _ = run_main(["--csv", csv_path], out_dir=self.out)
        self.assertEqual(code, cycle_report.EXIT_PARTIAL)
        self.assertIn("失败 1 只", out)
        self.assertIn("池.html", list_files(self.out))

    def test_invalid_code_row_counted_as_failure(self):
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业"), ("abc", "坏代码")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            code, out, err, _ = run_main(["--csv", csv_path], out_dir=self.out)
        self.assertEqual(code, cycle_report.EXIT_PARTIAL)

    def test_no_index_flag(self):
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            code, out, _, _ = run_main(["--csv", csv_path, "--no-index"],
                                       out_dir=self.out)
        self.assertEqual(code, cycle_report.EXIT_OK)
        self.assertNotIn("池.html", list_files(self.out))

    def test_csv_is_not_modified(self):
        """**输入 CSV 只读** —— 跑完 md5 必须一致。"""
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业"),
                                       ("600036", "招商银行")])
        before = md5_of(csv_path)
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            run_main(["--csv", csv_path], out_dir=self.out)
        self.assertEqual(before, md5_of(csv_path))

    def test_no_extra_result_files(self):
        """不产出任何汇总表/结果文件。"""
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            run_main(["--csv", csv_path], out_dir=self.out)
        extensions = {os.path.splitext(f)[1] for f in list_files(self.out)}
        self.assertEqual(extensions, {".html"})

    def test_no_cycle_output_created(self):
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            run_main(["--csv", csv_path], out_dir=self.out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cycle-output")))

    def test_duplicate_code_processed_once(self):
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业"),
                                       ("601899", "紫金矿业")])
        calls = []

        def counting(code, **kwargs):
            calls.append(code)
            return self.fake_fetch_one(code, **kwargs)

        with mock.patch.object(fetch, "fetch_one", side_effect=counting):
            code, out, err, _ = run_main(["--csv", csv_path], out_dir=self.out)
        self.assertEqual(calls, ["601899"])
        self.assertEqual(code, cycle_report.EXIT_OK)

    def test_reuses_existing_report_without_force(self):
        """已有报告默认复用，不重复取数。"""
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            run_main(["--csv", csv_path], out_dir=self.out)

        calls = []
        with mock.patch.object(fetch, "fetch_one",
                               side_effect=lambda c, **k: calls.append(c) or
                               self.fake_fetch_one(c, **k)):
            code, _, _, _ = run_main(["--csv", csv_path], out_dir=self.out)
        self.assertEqual(calls, [])
        self.assertEqual(code, cycle_report.EXIT_OK)

    def test_force_refetches(self):
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            run_main(["--csv", csv_path], out_dir=self.out)

        calls = []
        with mock.patch.object(fetch, "fetch_one",
                               side_effect=lambda c, **k: calls.append(c) or
                               self.fake_fetch_one(c, **k)):
            code, _, _, _ = run_main(["--csv", csv_path, "--force"],
                                     out_dir=self.out)
        self.assertEqual(calls, ["601899"])

    def test_csv_without_code_column_returns_2(self):
        path = os.path.join(self.tmp, "bad.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("行业,备注\n有色,ok\n")
        code, _, err, _ = run_main(["--csv", path], out_dir=self.out)
        self.assertEqual(code, cycle_report.EXIT_USAGE)
        self.assertIn("找不到代码列", err)

    def test_empty_csv_exit_0(self):
        path = os.path.join(self.tmp, "empty.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("代码,名称\n")
        code, out, _, _ = run_main(["--csv", path], out_dir=self.out)
        self.assertEqual(code, cycle_report.EXIT_OK)
        index = os.path.join(self.out, "empty.html")
        self.assertTrue(os.path.exists(index))
        with open(index, "r", encoding="utf-8") as fh:
            self.assertIn("共 0 只", fh.read())

    def test_keyboard_interrupt_returns_130(self):
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业")])
        with mock.patch.object(batch, "run_batch", side_effect=KeyboardInterrupt):
            code, _, err, _ = run_main(["--csv", csv_path], out_dir=self.out)
        self.assertEqual(code, cycle_report.EXIT_INTERRUPT)
        self.assertIn("中断", err)

    def test_quiet_prints_nothing_to_stdout(self):
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            code, out, err, _ = run_main(["--csv", csv_path, "--quiet"],
                                         out_dir=self.out)
        self.assertEqual(code, cycle_report.EXIT_OK)
        self.assertEqual(out.strip(), "")

    def test_workers_1_sequential(self):
        csv_path = make_csv(self.tmp, [("601899", "紫金矿业"),
                                       ("600036", "招商银行")])
        with mock.patch.object(fetch, "fetch_one", side_effect=self.fake_fetch_one):
            code, out, _, _ = run_main(["--csv", csv_path, "--workers", "1"],
                                       out_dir=self.out)
        self.assertEqual(code, cycle_report.EXIT_OK)
        self.assertIn("完成 2 只", out)


# ---------------------------------------------------------------------------
# 6. --stats / --refresh-names
# ---------------------------------------------------------------------------


class TestStatsAndNames(unittest.TestCase):

    def test_stats_without_name_table_returns_2(self):
        tmp = tempfile.mkdtemp(prefix="cycle_stats_")
        try:
            with mock.patch.object(config, "STOCK_NAMES_CSV",
                                   os.path.join(tmp, "nope.csv")):
                code, out, err, _ = run_main(["--stats", "--sample", "10"])
            self.assertEqual(code, cycle_report.EXIT_USAGE)
            self.assertIn("名称表", err)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stats_produces_no_files(self):
        tmp = tempfile.mkdtemp(prefix="cycle_stats2_")
        try:
            with mock.patch.object(config, "STOCK_NAMES_CSV",
                                   os.path.join(tmp, "nope.csv")):
                run_main(["--stats"], out_dir=os.path.join(tmp, "out"))
            self.assertFalse(os.path.exists(os.path.join(tmp, "out", "池.html")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_refresh_names_writes_table(self):
        """名称表现在来自共享库 `scan` 表（`hub.scan()`），不再调 data_stocklist。

        改的是数据来源，不是行为约定：`--refresh-names` 仍然写
        `data/stock_names.csv`，表头仍是 `代码,名称`，代码仍是 6 位。
        """
        tmp = tempfile.mkdtemp(prefix="cycle_refresh_")
        target = os.path.join(tmp, "data", "stock_names.csv")
        fake_scan = [
            {"symbol": "sh600519", "name": "贵州茅台"},
            {"symbol": "sz000001", "name": "平安银行"},
        ]
        fake_hub = mock.MagicMock()
        fake_hub.scan.return_value = fake_scan
        try:
            with mock.patch.object(config, "STOCK_NAMES_CSV", target), \
                    mock.patch.object(fetch, "hub", return_value=fake_hub):
                code, out, err, _ = run_main(["--refresh-names"], out_dir=tmp)
            self.assertEqual(code, cycle_report.EXIT_OK, err)
            self.assertTrue(os.path.exists(target))
            with open(target, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("代码,名称", text)
            self.assertIn("600519,贵州茅台", text)
            self.assertIn("000001,平安银行", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_refresh_names_api_failure_returns_2(self):
        with mock.patch.object(fetch, "hub",
                               side_effect=RuntimeError("限频")):
            code, _, err, _ = run_main(["--refresh-names"])
        self.assertEqual(code, cycle_report.EXIT_USAGE)
        self.assertIn("限频", err)

    def test_refresh_names_empty_scan_returns_2(self):
        """横截面拿回来是空的（比如那天没取到数）→ 不能写一个空名称表。

        写空表的后果不是报错，而是之后所有 `--stock 贵州茅台` 都报
        「名称表中没有这只票」——症状看起来像"票不存在"。
        """
        fake_hub = mock.MagicMock()
        fake_hub.scan.return_value = []
        with mock.patch.object(fetch, "hub", return_value=fake_hub):
            code, _, err, _ = run_main(["--refresh-names"])
        self.assertEqual(code, cycle_report.EXIT_USAGE)

    def test_refresh_names_dedupes_by_code(self):
        """同一只票在横截面里出现两次（不同价格）→ 名称表只写一行。

        不算边缘情况：`scan` 是"一天一份全市场"，同一天出现重复行
        说明上游合并有问题；去重在这里兜住，不至于写出两行同名代码。
        """
        tmp = tempfile.mkdtemp(prefix="cycle_refresh_dup_")
        target = os.path.join(tmp, "stock_names.csv")
        fake_hub = mock.MagicMock()
        fake_hub.scan.return_value = [
            {"symbol": "sh600519", "name": "贵州茅台"},
            {"symbol": "sh600519", "name": "贵州茅台"},
        ]
        try:
            with mock.patch.object(config, "STOCK_NAMES_CSV", target), \
                    mock.patch.object(fetch, "hub", return_value=fake_hub):
                code, _, _, _ = run_main(["--refresh-names"], out_dir=tmp)
            self.assertEqual(code, cycle_report.EXIT_OK)
            with open(target, encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)          # 表头 + 一行
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_refresh_names_skips_rows_without_code(self):
        """横截面里有行没有 symbol（不该发生，但不该让整张表写歪）。"""
        tmp = tempfile.mkdtemp(prefix="cycle_refresh_miss_")
        target = os.path.join(tmp, "stock_names.csv")
        fake_hub = mock.MagicMock()
        fake_hub.scan.return_value = [
            {"symbol": "", "name": "无名氏"},
            {"symbol": "sz000001", "name": "平安银行"},
        ]
        try:
            with mock.patch.object(config, "STOCK_NAMES_CSV", target), \
                    mock.patch.object(fetch, "hub", return_value=fake_hub):
                code, _, _, _ = run_main(["--refresh-names"], out_dir=tmp)
            self.assertEqual(code, cycle_report.EXIT_OK)
            with open(target, encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("无名氏", text)
            self.assertIn("000001,平安银行", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # `_extract_stocklist()` 的三个用例已删除：那个函数解析的是 data_stocklist
    # 的任意嵌套 JSON，函数本身随取数层迁移一并删掉了（名称表现在直接读
    # `scan` 表的 symbol / name 两列）。测一个不存在的函数没有意义。


# ---------------------------------------------------------------------------
# 7. 输出文案
# ---------------------------------------------------------------------------


class TestPrintDecision(unittest.TestCase):

    def capture(self, fixture_name):
        stock = load(fixture_name)
        built = __import__("cycle.build", fromlist=["build"]).build(stock)
        m = __import__("cycle.metrics", fromlist=["metrics"]).compute_all(
            built["quarters"], config)
        d = __import__("cycle.decide", fromlist=["decide"]).decide(
            built["quarters"], m, built["industry"], config)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cycle_report._print_decision(built, d, m, "/tmp/x.html")
        return buf.getvalue()

    def test_contains_all_three_rows(self):
        text = self.capture("full_40q")
        for key in ("是不是周期股", "弹性", "周期位置"):
            self.assertIn(key, text)

    def test_contains_numbers(self):
        text = self.capture("full_40q")
        self.assertIn("pp", text)
        self.assertIn("%", text)

    def test_no_none_in_output(self):
        for fixture in ("financial", "balance_missing",
                        "flat_margin_equal_turnover"):
            with self.subTest(fixture=fixture):
                self.assertNotIn("None", self.capture(fixture))

    def test_report_path_printed(self):
        self.assertIn("/tmp/x.html", self.capture("full_40q"))

    def test_badges_printed(self):
        text = self.capture("full_40q")
        self.assertIn("标签", text)
        self.assertIn("周期股", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
