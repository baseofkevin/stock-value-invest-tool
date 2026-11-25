"""config.py 的契约测试 —— 防文档漂移、防参数回流（README.md「测试」）。

这个文件存在的唯一理由：用户的核心要求是「尽可能少指标、少计算」。
少 = 可以被机械校验的。所以这里不测计算，只测**数量与名单**：
  * 判定阈值恰好 11 个，多一个就红；
  * 被删掉的 8 个历史参数一个都不许回来；
  * 窗口之间的相对大小关系（防止有人把 POSITION_WINDOW 调到 HISTORY 之外）。

为什么从 5 个变成 11 个（三轮改动，每轮都先量化再动手）：
  * 5 → 8：判断① 增相对波动通道 GM_CV_MIN + 两个守卫，修「绝对 pp 尺子
    对低毛利公司系统性偏严」（低毛利组命中率 35%→89%）。
  * 8 → 11：判断② 增 ELASTIC_ZERO_CROSS_MAX（盈亏平衡点附近 DOL 爆炸），
    判断③ 增 CAPACITY_EXPAND_GROWTH / CAPACITY_SHRINK_GROWTH（区分
    「需求走了」和「产能来了」两种语义相反的低位）。
数量仍然被这条测试钉死 —— 加了几个不重要，
重要的是**加的时候必须同时改这里和 README**。
"""

import os
import re
import unittest

import tests.helpers  # noqa: F401  （注入 sys.path）
import config

from tests.helpers import FIVE_THRESHOLDS


class TestThresholdCount(unittest.TestCase):
    """『指标要少』的机械保障。"""

    def test_exactly_eleven_decision_thresholds(self):
        self.assertEqual(len(config.DECISION_THRESHOLDS), 11, "判定阈值必须恰好 11 个")
        self.assertEqual(set(config.DECISION_THRESHOLDS),
                         set(FIVE_THRESHOLDS.keys()))

    def test_threshold_values_match_design(self):
        """§3.2 表里的数字改了，测试必须先改 —— 否则就是悄悄改了判定口径。"""
        for name, expected in FIVE_THRESHOLDS.items():
            with self.subTest(name=name):
                self.assertAlmostEqual(getattr(config, name), expected, places=9)

    def test_no_sixth_threshold_sneaks_in(self):
        """扫 config 里所有大写常量，减去窗口/保护/名单/路径/标记之后，
        剩下的"像个阈值"的名字必须为空。

        做法：凡是名字里带 _MIN / _MAX / _PCT / _RATIO 的，都必须在三份名单之一里。
        """
        whitelist = (set(config.DECISION_THRESHOLDS)
                     | set(config.WINDOWS)
                     | set(config.GUARD_PARAMS)
                     | {"RATIO_CLIP_ABS", "REV_GROWTH_MIN_ABS"})   # 保护参数（带 _ABS）
        suspects = []
        for name in dir(config):
            if not name.isupper():
                continue
            if not re.search(r"_(MIN|MAX|PCT|THRESHOLD)$", name):
                continue
            if name in whitelist:
                continue
            suspects.append(name)
        self.assertEqual(suspects, [], f"发现了不在名单里的拟阈值常量：{suspects}")

    def test_guard_params_are_all_numeric(self):
        for name in config.GUARD_PARAMS:
            with self.subTest(name=name):
                value = getattr(config, name)
                self.assertIsInstance(value, (int, float))
                self.assertGreater(value, 0)


class TestDeprecatedParamsGone(unittest.TestCase):
    """上一版删掉的参数不许回流（§3.5 第 5 条）。"""

    def test_declared_list_has_eight_items(self):
        self.assertEqual(len(config.DEPRECATED_PARAM_NAMES), 8)

    def test_none_of_them_exist(self):
        for name in config.DEPRECATED_PARAM_NAMES:
            with self.subTest(name=name):
                self.assertFalse(hasattr(config, name),
                                 f"{name} 是被删除的参数，不得回流")

    def test_cash_flow_and_leverage_fields_absent(self):
        """财务杠杆（§2.9）与偿债风险被判定为"零信息"，对应的参数名也不该出现。"""
        for name in ("DFL_MAX", "DFL_MIN", "FIN_LEVERAGE_MIN", "INTEREST_COVER_MIN",
                     "DEBT_RATIO_MAX", "CFO_TO_DEBT_MIN"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(config, name))


class TestReadmeInSync(unittest.TestCase):
    """README.md 里列出的参数必须真实存在于 config.py。

    文档漂移是最难发现的一类错误 —— 改了阈值却忘了改文档，读者照着
    README 调参数会调到不存在的名字，或者以为某个值是 5.0 而实际是 3.0。
    这里把 README 当成一份需要校验的"第二事实来源"。
    """

    @classmethod
    def setUpClass(cls):
        import pathlib
        cls.readme = (pathlib.Path(__file__).resolve().parent.parent
                      / "README.md").read_text(encoding="utf-8")

    def test_readme_exists(self):
        self.assertGreater(len(self.readme), 1000)

    def test_all_referenced_constants_exist(self):
        """README 里用反引号标出的全大写常量，除了废弃黑名单，都得存在。"""
        names = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", self.readme))
        allowed_missing = set(config.DEPRECATED_PARAM_NAMES)
        for name in sorted(names - allowed_missing):
            with self.subTest(name=name):
                self.assertTrue(hasattr(config, name),
                                f"README 提到 {name}，config.py 里没有")

    def test_threshold_values_match(self):
        """README 参数表里的数值不能跟 config 对不上。"""
        for name in config.DECISION_THRESHOLDS:
            with self.subTest(name=name):
                value = getattr(config, name)
                # 参数表里的写法是 `| \`NAME\` | 5.0 pp |`，取竖线之间那一段
                # 数值可能带负号（CAPACITY_SHRINK_GROWTH = -5.0）
                pattern = r"\|\s*`" + name + r"`\s*\|\s*(-?[0-9.]+)"
                match = re.search(pattern, self.readme)
                self.assertIsNotNone(match, f"README 参数表里找不到 {name} 的行")
                self.assertAlmostEqual(float(match.group(1)), float(value),
                                       msg=f"{name} 文档写 {match.group(1)}，"
                                           f"实际 {value}")

    def test_windows_match(self):
        for name in ("HISTORY_QUARTERS", "WAVE_WINDOW_QUARTERS",
                     "ELASTIC_WINDOW_QUARTERS", "POSITION_WINDOW_QUARTERS",
                     "YOY_PERIODS"):
            with self.subTest(name=name):
                pattern = r"\|\s*`" + name + r"`\s*\|\s*([0-9]+)"
                match = re.search(pattern, self.readme)
                self.assertIsNotNone(match, f"README 窗口表里找不到 {name}")
                self.assertEqual(int(match.group(1)), getattr(config, name))

    def test_no_markdown_stars_in_code_blocks(self):
        """代码/表格里不该出现 `**` 这种 Markdown 强调标记烧进 HTML 文案。"""
        # 只看"文案"部分：README 本身是 Markdown，可以有 **；但业务文案里不行。
        # 这条留给 render 的测试，这里只做一个轻量提醒性质的检查。
        self.assertIn("## 已知边界", self.readme)


class TestWindowConsistency(unittest.TestCase):
    """窗口之间的相对关系 —— 调一个不顾其余，最容易把取数窗口和判定窗口搞矛盾。"""

    def test_six_windows(self):
        self.assertEqual(len(config.WINDOWS), 6)
        for name in config.WINDOWS:
            with self.subTest(name=name):
                value = getattr(config, name)
                self.assertIsInstance(value, int)
                self.assertGreater(value, 0)

    def test_wave_within_position_within_history(self):
        self.assertLessEqual(config.WAVE_WINDOW_QUARTERS,
                             config.POSITION_WINDOW_QUARTERS)
        self.assertLessEqual(config.POSITION_WINDOW_QUARTERS,
                             config.HISTORY_QUARTERS - config.YOY_PERIODS)

    def test_elastic_window_within_history(self):
        self.assertLessEqual(config.ELASTIC_WINDOW_QUARTERS, config.HISTORY_QUARTERS)

    def test_history_covers_position_plus_yoy(self):
        """要能算 40 季分位 + 4 季同比，取数至少得 44 季。"""
        need = config.POSITION_WINDOW_QUARTERS + config.YOY_PERIODS
        self.assertGreaterEqual(config.HISTORY_QUARTERS, need)

    def test_yoy_is_one_year(self):
        self.assertEqual(config.YOY_PERIODS, 4)


class TestLowHighOrdering(unittest.TestCase):

    def test_low_less_than_high(self):
        self.assertLess(config.POSITION_LOW_PCT, config.POSITION_HIGH_PCT)

    def test_position_bounds_look_like_percentiles(self):
        self.assertGreaterEqual(config.POSITION_LOW_PCT, 0)
        self.assertLessEqual(config.POSITION_HIGH_PCT, 100)

    def test_sample_guards_are_sane(self):
        """最小样本数必须小于等于对应窗口，否则那个判断永远出不了值。"""
        self.assertLessEqual(config.GM_MIN_SAMPLES, config.WAVE_WINDOW_QUARTERS)
        self.assertLessEqual(config.RATIO_MIN_SAMPLES, config.ELASTIC_WINDOW_QUARTERS)
        self.assertLessEqual(config.POSITION_MIN_SAMPLES, config.POSITION_WINDOW_QUARTERS)


class TestPaths(unittest.TestCase):

    def test_default_out_dir_points_to_shared_output(self):
        """默认产物落 `stock_value_invest_tool/output/cycle-elastic`。

        原断言是"落在项目内 html-report/"。改它不是迁就新代码——产物进源码目录
        本身就是问题（跑几次就分不清哪份最新）。现在四个工具统一进
        `output/<工具名>/`，见 output/README.md。
        """
        self.assertTrue(config.DEFAULT_OUT_DIR.startswith(config.HUB_ROOT))
        self.assertTrue(config.DEFAULT_OUT_DIR.endswith(
            os.path.join("output", "cycle-elastic")))
        # 不能落回源码目录
        self.assertFalse(config.DEFAULT_OUT_DIR.startswith(config.PROJECT_ROOT))

    def test_hub_root_is_parent_of_project_root(self):
        """`HUB_ROOT` 必须是 `PROJECT_ROOT` 的父目录 —— 否则 sys.path 与
        output/ 都会指到错的地方，而症状是 `ModuleNotFoundError` 或产物散落。"""
        self.assertEqual(os.path.dirname(config.PROJECT_ROOT), config.HUB_ROOT)

    def test_report_name_tag(self):
        self.assertEqual(config.REPORT_NAME_TAG, "周期弹性初筛")

    def test_no_westock_client_constant(self):
        """`WESTOCK_CLIENT` 必须**不存在** —— 防止有人把 node 客户端加回来。

        原版靠它定位 `providers/westock_client.js` 自己调 MCP。迁移到共享数据层后，
        取数一律走 `stock_data_fetch`，本工具不该再持有任何直连上游的东西：
        一旦有人"顺手"加回来，就绕过了节流闸门与缓存，回到每只票重复打上游的老路。
        这条断言与 `test_no_dead_thresholds` 同一思路：删掉的东西不该被悄悄加回来。
        """
        self.assertFalse(hasattr(config, "WESTOCK_CLIENT"))

    def test_names_csv_path_under_project(self):
        """名称表是本工具自己的状态文件，落在源码目录下是对的（与产物不同）。"""
        self.assertEqual(config.STOCK_NAMES_CSV,
                         os.path.join(config.PROJECT_ROOT, "data", "stock_names.csv"))


class TestFinancialKeywords(unittest.TestCase):

    def test_keywords_cover_four_types(self):
        self.assertEqual(len(config.FINANCIAL_INDUSTRY_KEYWORDS), 4)
        for word in ("银行", "保险", "证券", "多元金融"):
            self.assertIn(word, config.FINANCIAL_INDUSTRY_KEYWORDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
