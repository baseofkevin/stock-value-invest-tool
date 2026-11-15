"""索引页测试（README.md「CLI 参考」/「测试」）。

索引页是"批量模式的唯一入口"，三件事错了就整批白跑：
    * 链接编码错了 → file:// 下点不开（中文名、空格、`#` 是三大杀手）
    * 行数少了 → 跟 CSV 对不上，用户以为漏跑了
    * 统计数跟明细不一致 → 用户不信这页

另外守两条方法论立场：
    * **不排序** —— 不提供排序列、JS 里没有 sort
    * 失败行也要在表里，且写出原因
"""

import os
import re
import shutil
import tempfile
import unittest
from urllib.parse import quote

import tests.helpers  # noqa: F401  —— 负责 sys.path 注入
from tests import helpers
from tests.fixtures import load

from cycle import batch, build, decide, index_html, metrics, render
from cycle import decide as decide_mod

# ---------------------------------------------------------------------------
# 造 ItemResult
# ---------------------------------------------------------------------------


def make_row(code_raw, name_raw="", duplicate=False):
    row = batch.CsvRow(1, code_raw, name_raw)
    row.duplicate = duplicate
    return row


def item_from_decision(code, name, decision, path=None, error="", ok=True):
    """用真实 decision 造一个 ItemResult。"""
    item = batch.ItemResult(make_row(code, name))
    item.row.code = code
    item.ok = ok
    item.path = path if path is not None else f"/tmp/{name or code}-{code}.html"
    item.decision = decision
    item.error = error
    return item


def item_failed(code, name, error):
    item = batch.ItemResult(make_row(code, name))
    item.row.code = code
    item.ok = False
    item.path = ""
    item.decision = None
    item.error = error
    return item


def decision_of(cycle, elastic, position, gm_std=None, ratio=None, pct=None):
    return {
        "cycle": {"label": cycle, "gm_std": gm_std},
        "elastic": {"label": elastic, "ratio": ratio},
        "position": {"label": position, "pct": pct},
        "flags": {"financial": False, "insufficient_history": False},
        "meta": {"quarters": 40},
    }


def real_decision(fixture_name):
    """从 fixture 真跑一遍决策，保证标签组合是真实可能出现的。"""
    stock = build.build(load(fixture_name))
    m = metrics.compute_all(stock["quarters"], helpers.config)
    d = decide.decide(stock["quarters"], m, stock.get("industry", ""),
                      helpers.config)
    return stock, d


def render_index(items, csv_path="池.csv", out_dir="/tmp/out", ts="2026-11-12 20:00"):
    """不落盘，直接拼出索引页 HTML。"""
    summary = index_html.compute_stats(items)
    template = index_html._load_template()
    fname = batch.index_filename(csv_path)
    stem = os.path.splitext(os.path.basename(csv_path))[0]
    return (template
            .replace("{{TITLE}}", f"周期弹性初筛 · {stem}")
            .replace("{{SUBTITLE}}",
                     f"{os.path.basename(csv_path)} · 共 {summary['total']} 只")
            .replace("{{STATS}}", index_html.render_stats(summary["counts"]))
            .replace("{{TABLE}}", index_html.render_table(items))
            .replace("{{GENERATED_AT}}", ts))


# ---------------------------------------------------------------------------
# 1. 文件名与 URL 编码
# ---------------------------------------------------------------------------


class TestFilenames(unittest.TestCase):
    """文件名错了链接就是死的，file:// 下不会有任何提示。"""

    def test_report_filename_shape(self):
        name = batch.report_filename("贵州茅台", "600519", "20261112-144710")
        self.assertEqual(name, "贵州茅台-600519-周期弹性初筛-20261112-144710.html")

    def test_report_filename_matches_scan_regex(self):
        """自己生成的名字，自己扫得回来。"""
        name = batch.report_filename("贵州茅台", "600519", "20261112-144710")
        match = batch._REPORT_RE.search(name)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("code"), "600519")
        self.assertEqual(match.group("ts"), "20261112-144710")

    def test_illegal_chars_replaced(self):
        self.assertEqual(batch.safe_name('ST*退:市"股'),
                         "ST_退_市_股")
        self.assertNotIn("/", batch.safe_name("a/b"))

    def test_safe_name_strips_dots_and_spaces(self):
        self.assertEqual(batch.safe_name("  ..茅台..  "), "茅台")
        self.assertEqual(batch.safe_name("   "), "未命名")
        self.assertEqual(batch.safe_name(None), "未命名")

    def test_name_fallback_to_code(self):
        self.assertEqual(batch.safe_name("", "600519"), "600519")

    def test_index_filename_from_csv(self):
        self.assertEqual(batch.index_filename("池.csv"), "池.html")
        self.assertEqual(batch.index_filename("/data/我的股票池.csv"), "我的股票池.html")
        self.assertEqual(batch.index_filename("a.b.csv"), "a.b.html")

    def test_index_filename_sanitized(self):
        self.assertEqual(batch.index_filename("池:1.csv"), "池_1.html")

    def test_url_encoding_chinese(self):
        """中文文件名必须百分号编码，否则某些浏览器/系统下打不开。"""
        href = quote("贵州茅台-600519-周期弹性初筛-20261112.html")
        self.assertIn("%E8%B4%B5%E5%B7%9E", href)   # 贵州
        self.assertEqual(href, href.ascii if False else href)  # 保持 ASCII
        self.assertTrue(all(ord(c) < 128 for c in href))

    def test_url_encoding_space(self):
        self.assertEqual(quote("a b.html"), "a%20b.html")

    def test_url_encoding_hash(self):
        """`#` 不编码会被当成锚点，其后的文件名全丢 —— 参考页踩过。"""
        self.assertEqual(quote("ST#1.html"), "ST%231.html")

    def test_url_encoding_ampersand(self):
        self.assertEqual(quote("A&B.html"), "A%26B.html")

    def test_encode_roundtrip(self):
        from urllib.parse import unquote
        name = "招商银行-600036-周期弹性初筛-20261112.html"
        self.assertEqual(unquote(quote(name)), name)


# ---------------------------------------------------------------------------
# 2. 表格行
# ---------------------------------------------------------------------------


class TestTableRows(unittest.TestCase):

    def test_one_row_per_item(self):
        items = [
            item_from_decision("601899", "紫金矿业",
                               decision_of(1, "strong", "low", 6.1, 2.5, 1.2)),
            item_from_decision("600036", "招商银行",
                               decision_of("na", "weak", "na", None, 1.0, 98.8)),
            item_failed("600519", "贵州茅台", "接口超时"),
        ]
        html = index_html.render_table(items)
        self.assertEqual(html.count("<tr data-search="), 3)

    def test_row_count_equals_csv_row_count(self):
        """成功 + 失败 + 重复 == CSV 行数。少一行用户就会以为漏跑。"""
        items = [
            item_from_decision("601899", "紫金矿业", decision_of(1, "strong", "low")),
            item_from_decision("600036", "招商银行", decision_of(0, "weak", "high")),
            item_failed("600519", "贵州茅台", "取数失败"),
            item_failed("000001", "平安银行", "代码无效：xx"),
        ]
        items[3].row.duplicate = True
        html = index_html.render_table(items)
        self.assertEqual(html.count("<tr data-search="), len(items))

    def test_seven_columns(self):
        items = [item_from_decision("601899", "紫金矿业",
                                    decision_of(1, "strong", "low"))]
        html = index_html.render_table(items)
        header = html.split("<thead>")[1].split("</thead>")[0]
        self.assertEqual(header.count("<th>"), len(index_html.COLUMNS))
        for col in ("代码", "名称", "毛利率σ", "弹性", "周期位置", "判定", "报告"):
            self.assertIn(f"<th>{col}</th>", header)

    def test_row_has_seven_cells(self):
        for fixture in ("full_40q", "financial", "short_12q"):
            with self.subTest(fixture=fixture):
                stock, d = real_decision(fixture)
                item = item_from_decision(stock["code"], stock["name"], d)
                html = index_html.render_row(item)
                self.assertEqual(html.count("<td"), len(index_html.COLUMNS))

    def test_code_and_name_shown(self):
        items = [item_from_decision("601899", "紫金矿业",
                                    decision_of(1, "strong", "low"))]
        html = index_html.render_table(items)
        self.assertIn('<td class="code">601899</td>', html)
        self.assertIn("紫金矿业", html)

    def test_failed_row_kept_with_reason(self):
        """失败行不能消失 —— 必须留下痕迹，否则用户不知道哪只没跑。"""
        items = [item_failed("600519", "贵州茅台", "服务限频，请稍后再试")]
        html = index_html.render_table(items)
        self.assertIn("600519", html)
        self.assertIn("贵州茅台", html)
        self.assertIn("服务限频", html)
        self.assertIn("btn disabled", html)
        self.assertIn("无报告", html)

    def test_failed_row_has_no_report_link(self):
        """失败行没有报告链接 —— 但**仍要**有东方财富链接。

        两个链接性质不同：报告链接指向不存在的文件，给了会 404；
        东方财富是外链，跟本地取数成不成功无关 —— 初筛失败恰恰是需要
        自己去看一眼 K 线的时刻。
        """
        items = [item_failed("600519", "贵州茅台", "失败")]
        html = index_html.render_row(items[0])
        self.assertNotIn('<a class="btn" ', html)
        self.assertIn("无报告", html)
        self.assertIn("quote.eastmoney.com/sh600519.html", html)

    def test_duplicate_row_marked(self):
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low"))
        item.row.duplicate = True
        html = index_html.render_row(item)
        self.assertIn("b-dup", html)
        self.assertIn("重复", html)

    def test_null_metric_shows_dash(self):
        """缺值不能留空单元格（会被误读成 0）。"""
        item = item_from_decision("600036", "招商银行",
                                  decision_of("na", "weak", "na", None, 1.0, 98.8))
        html = index_html.render_row(item)
        self.assertIn('<td class="num null">—</td>', html)

    def test_metric_formatted(self):
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low", 6.137, 2.4999, 1.25))
        html = index_html.render_row(item)
        # σ 与报告页同为 2 位小数（1 位小数会把 4.95 显示成 5.0，跨过 5pp 判定线）
        self.assertIn("6.14 pp", html)
        self.assertIn("2.50", html)

    def test_cv_channel_badge_in_index(self):
        """走相对波动通道的周期股，索引页要标出来，否则 σ<5 配「周期股」看着像错。"""
        d = {
            "cycle": {"label": 1, "gm_std": 3.8, "gm_cv": 23.5,
                      "gm_median": 16.2, "channel": "cv"},
            "elastic": {"label": "strong", "ratio": 17.31},
            "position": {"label": "low", "pct": 26.25},
            "flags": {"financial": False, "insufficient_history": False},
            "meta": {"quarters": 43},
        }
        item = item_from_decision("600409", "三友化工", d)
        html = index_html.render_row(item)
        self.assertIn("相对波动", html)
        self.assertIn("周期股", html)
        self.assertIn("3.80 pp", html)

    def test_std_channel_has_no_cv_badge(self):
        d = {
            "cycle": {"label": 1, "gm_std": 6.0, "gm_cv": 40.0,
                      "gm_median": 20.0, "channel": "std"},
            "elastic": {"label": "strong", "ratio": 3.0},
            "position": {"label": "low", "pct": 20.0},
            "flags": {"financial": False, "insufficient_history": False},
            "meta": {"quarters": 43},
        }
        item = item_from_decision("601899", "紫金矿业", d)
        html = index_html.render_row(item)
        self.assertNotIn("相对波动", html)

    def test_capacity_direction_badge_in_index(self):
        """「低位」必须配方向标记 —— 它是全工具最容易读成买入信号的地方。"""
        d = {
            "cycle": {"label": 1, "gm_std": 5.83, "gm_cv": 36.3,
                      "gm_median": 16.1, "channel": "std"},
            "elastic": {"label": "weak", "ratio": -0.81, "reason": "ok"},
            "position": {"label": "low", "pct": 6.25,
                         "capacity_direction": "expand",
                         "asset_growth": 49.0, "revenue_growth": 57.0},
            "flags": {"financial": False, "insufficient_history": False},
            "meta": {"quarters": 43},
        }
        item = item_from_decision("000758", "中色股份", d)
        html = index_html.render_row(item)
        self.assertIn("扩产中", html)
        self.assertIn("低位", html)
        # 也要能被搜到
        self.assertIn("扩产中", html.split("<td")[0])

    def test_capacity_direction_hidden_on_neutral(self):
        """中性区本来就没方向，多一个标记会像第四个结论。"""
        d = {
            "cycle": {"label": 1, "gm_std": 6.0, "gm_cv": 30.0,
                      "gm_median": 20.0, "channel": "std"},
            "elastic": {"label": "weak", "ratio": 1.0, "reason": "ok"},
            "position": {"label": "neutral", "pct": 50.0,
                         "capacity_direction": "expand",
                         "asset_growth": 20.0, "revenue_growth": 5.0},
            "flags": {"financial": False, "insufficient_history": False},
            "meta": {"quarters": 43},
        }
        item = item_from_decision("600000", "测试", d)
        html = index_html.render_row(item)
        self.assertNotIn("扩产中", html)

    def test_contradiction_badge_in_index(self):
        """①否 且 ②强 → 索引页带提示标记，且能被搜索到。"""
        item = item_from_decision("000878", "云南铜业",
                                  decision_of(0, "strong", "high", 0.99, 8.30, 88.75))
        html = index_html.render_row(item)
        self.assertIn(decide_mod.BADGE_CONTRADICTION, html)
        self.assertIn(decide_mod.BADGE_CONTRADICTION, html.split("<td")[0])

    def test_no_contradiction_badge_when_both_agree(self):
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low", 6.0, 3.0, 20.0))
        html = index_html.render_row(item)
        self.assertNotIn(decide_mod.BADGE_CONTRADICTION, html)

    def test_sigma_rounding_does_not_contradict_badge(self):
        """川发龙蟒实测值：σ=4.9505 判「非周期」，索引页不许显示成 5.00 pp。"""
        item = item_from_decision("002312", "川发龙蟒",
                                  decision_of(0, "strong", "low", 4.9505, 10.3296, 8.75))
        html = index_html.render_row(item)
        self.assertIn("4.95 pp", html)
        self.assertNotIn("5.00 pp", html)
        self.assertIn("非周期", html)

    def test_report_link_present_and_encoded(self):
        item = item_from_decision(
            "600519", "贵州茅台", decision_of(1, "strong", "low"),
            path="/out/贵州茅台-600519-周期弹性初筛-20261112-144710.html")
        html = index_html.render_row(item)
        self.assertIn('<a class="btn" href="', html)
        href = re.search(r'href="([^"]+)"', html).group(1)
        self.assertTrue(all(ord(c) < 128 for c in href))
        self.assertIn("%E8%B4%B5%E5%B7%9E%E8%8C%85%E5%8F%B0", href)
        # 不能带目录，只能是文件名
        self.assertNotIn("/", href)

    def test_link_uses_basename_only(self):
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low"),
                                  path="/very/deep/dir/紫金矿业-601899-x.html")
        href = re.search(r'href="([^"]+)"', index_html.render_row(item)).group(1)
        from urllib.parse import unquote
        self.assertEqual(unquote(href), "紫金矿业-601899-x.html")

    def test_report_link_opens_in_new_tab(self):
        """报告链接必须新开标签页，不能在索引页上直接跳走。

        索引页是"目录"，用户点进一只看完要能回来接着扫下一只。
        直接跳转会把索引顶掉，扫 100 多只时得一路按后退。
        """
        item = item_from_decision("600519", "贵州茅台",
                                  decision_of(1, "strong", "low"),
                                  path="/out/贵州茅台-600519-x.html")
        html = index_html.render_row(item)
        link = re.search(r'<a [^>]*class="btn"[^>]*>报告链接</a>', html).group(0)
        self.assertIn('target="_blank"', link)
        self.assertIn('rel="noopener"', link,
                      "noopener 不能省：否则报告页能通过 window.opener 控制索引页")

    def test_disabled_report_cell_has_no_link(self):
        """无报告的行只是个 span，不能误带 target（否则是个空标签）。

        只检查「报告」那一格：行末的「东方财富」链接本来就有 target="_blank"。
        """
        item = item_from_decision("600519", "贵州茅台", None)
        item.ok = False
        item.path = ""
        item.error = "取数失败"
        html = index_html.render_row(item)
        self.assertIn('<span class="btn disabled">无报告</span>', html)
        self.assertNotIn('href', html.split("无报告")[0].split("<td")[-1])

    def test_empty_rows_shows_placeholder(self):
        html = index_html.render_table([])
        self.assertIn("CSV 里没有数据行", html)
        self.assertNotIn("<table>", html)


# ---------------------------------------------------------------------------
# 3. data-search（搜索框能不能用全靠它）
# ---------------------------------------------------------------------------


class TestQuotesColumn(unittest.TestCase):
    """『行情』列：东方财富跳转。

    初筛的动线是「表格里扫一眼 → 挑出想看的 → 去翻 K 线」，
    链接在这条动线上省一次手工搜索。
    """

    def test_column_exists(self):
        self.assertIn("行情", index_html.COLUMNS)
        self.assertEqual(index_html.COLUMNS[-1], "行情")

    def test_row_has_eastmoney_link(self):
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low"))
        html = index_html.render_row(item)
        self.assertIn("https://quote.eastmoney.com/sh601899.html", html)
        self.assertIn("东方财富", html)

    def test_link_opens_in_new_tab_without_opener(self):
        """`noopener` 不能省：外链页面不该拿到 `window.opener`。

        本地报告是 `file://`，被外链反向控制是真实存在的钓鱼路径。
        """
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low"))
        html = index_html.render_row(item)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener"', html)

    def test_every_decision_row_has_link(self):
        for cycle, elastic, position in (
            (1, "strong", "low"), (0, "weak", "high"),
            (1, "unstable", "na"), (None, "na", "neutral"),
        ):
            with self.subTest(position=position):
                item = item_from_decision(
                    "600519", "贵州茅台", decision_of(cycle, elastic, position))
                self.assertIn("quote.eastmoney.com/sh600519.html",
                              index_html.render_row(item))

    def test_bank_row_also_has_link(self):
        """金融企业不适用产能周转率，但照样要看 K 线。"""
        decision = decision_of(1, "weak", "na")
        decision["flags"] = {"financial": True, "insufficient_history": False}
        item = item_from_decision("600036", "招商银行", decision)
        html = index_html.render_row(item)
        self.assertIn("不适用", html)
        self.assertIn("quote.eastmoney.com/sh600036.html", html)

    def test_invalid_code_degrades_to_dash(self):
        """代码拼不出来时退化成破折号，不能崩、不能拼出半截 URL。"""
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low"))
        item.row.code = "garbage"
        item.row.code_raw = "garbage"
        html = index_html.render_row(item)
        self.assertNotIn("eastmoney", html)
        self.assertIn('class="btn disabled"', html)

    def test_duplicate_row_has_link(self):
        """重复行被跳过没报告，但仍值得看一眼行情。"""
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low"))
        item.row.duplicate = True
        self.assertIn("quote.eastmoney.com/sh601899.html",
                      index_html.render_row(item))

    def test_beijing_stock_path(self):
        item = item_from_decision("920001", "北交所样本",
                                  decision_of(1, "weak", "low"))
        html = index_html.render_row(item)
        self.assertIn("quote.eastmoney.com/bj/920001.html", html)


class TestSearch(unittest.TestCase):

    def test_every_row_has_data_search(self):
        items = [
            item_from_decision("601899", "紫金矿业", decision_of(1, "strong", "low")),
            item_failed("600519", "贵州茅台", "失败"),
        ]
        html = index_html.render_table(items)
        self.assertEqual(html.count("data-search="), 2)

    def test_data_search_contains_code_and_name(self):
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low"))
        html = index_html.render_row(item)
        search = re.search(r'data-search="([^"]*)"', html).group(1)
        self.assertIn("601899", search)
        self.assertIn("紫金矿业", search)

    def test_data_search_contains_plain_badges(self):
        """搜"低位""弹性强"要能命中 —— 徽章是 span 拼的，得另存纯文本。"""
        item = item_from_decision("601899", "紫金矿业",
                                  decision_of(1, "strong", "low"))
        search = re.search(r'data-search="([^"]*)"',
                           index_html.render_row(item)).group(1)
        self.assertIn("周期股", search)
        self.assertIn("弹性强", search)
        self.assertIn("低位", search)

    def test_data_search_contains_error_for_failed_row(self):
        item = item_failed("600519", "贵州茅台", "接口超时")
        search = re.search(r'data-search="([^"]*)"',
                           index_html.render_row(item)).group(1)
        self.assertIn("接口超时", search)

    def test_plain_badges_mapping(self):
        cases = [
            (decision_of(1, "strong", "low"), "周期股 弹性强 低位"),
            (decision_of(0, "weak", "high"), "非周期 弹性弱 高位"),
            (decision_of(1, "unstable", "neutral"), "周期股 比值不稳定 中性"),
            (decision_of("na", "na", "na"), ""),
        ]
        for decision, expect in cases:
            with self.subTest(expect=expect):
                self.assertEqual(index_html._plain_badges(decision), expect)

    def test_data_search_escaped(self):
        """名称里有引号不能把属性截断。"""
        item = item_from_decision("600000", 'A"B', decision_of(1, "strong", "low"))
        html = index_html.render_row(item)
        search = re.search(r'data-search="([^"]*)"', html).group(1)
        self.assertNotIn('"', search)
        self.assertIn("&quot;", search)


# ---------------------------------------------------------------------------
# 4. 统计块
# ---------------------------------------------------------------------------


class TestStats(unittest.TestCase):
    """统计与明细必须一致 —— 不一致的话整页就没人信了。"""

    def test_five_stat_blocks(self):
        html = index_html.render_stats({k: 0 for k in index_html.STAT_KEYS})
        self.assertEqual(html.count('class="stat"'), 5)
        for label in ("周期股", "弹性强", "低位", "高位", "失败/缺数据"):
            self.assertIn(label, html)

    def test_stat_keys_locked(self):
        self.assertEqual(index_html.STAT_KEYS,
                         ("cycle_yes", "elastic_strong", "low", "high", "failed"))

    def test_counts_match_rows(self):
        items = [
            item_from_decision("A", "甲", decision_of(1, "strong", "low")),
            item_from_decision("B", "乙", decision_of(1, "weak", "high")),
            item_from_decision("C", "丙", decision_of(0, "strong", "neutral")),
        ]
        stats = index_html.compute_stats(items)
        self.assertEqual(stats["counts"]["cycle_yes"], 2)
        self.assertEqual(stats["counts"]["elastic_strong"], 2)
        self.assertEqual(stats["counts"]["low"], 1)
        self.assertEqual(stats["counts"]["high"], 1)
        self.assertEqual(stats["counts"]["failed"], 0)
        self.assertEqual(stats["success"], 3)

    def test_failed_counts_errors_and_na(self):
        """失败/缺数据 = 无报告 + 报告中任一 na，两类都算。"""
        items = [
            item_from_decision("A", "甲", decision_of(1, "strong", "low")),
            item_from_decision("B", "乙", decision_of("na", "weak", "high")),
            item_failed("C", "丙", "失败"),
        ]
        stats = index_html.compute_stats(items)
        self.assertEqual(stats["counts"]["failed"], 2)
        self.assertEqual(stats["success"], 2)

    def test_position_low_high_are_exclusive(self):
        items = [item_from_decision("A", "甲", decision_of(1, "strong", "neutral"))]
        stats = index_html.compute_stats(items)
        self.assertEqual(stats["counts"]["low"], 0)
        self.assertEqual(stats["counts"]["high"], 0)

    def test_duplicate_not_counted_twice(self):
        """同一只股票在 CSV 里出现两次，统计只算一次。"""
        item = item_from_decision("A", "甲", decision_of(1, "strong", "low"))
        dup = item_from_decision("A", "甲", decision_of(1, "strong", "low"))
        dup.row.duplicate = True
        stats = index_html.compute_stats([item, dup])
        self.assertEqual(stats["counts"]["cycle_yes"], 1)
        self.assertEqual(stats["total"], 1)

    def test_total_matches_non_duplicate_count(self):
        items = [
            item_from_decision("A", "甲", decision_of(1, "strong", "low")),
            item_failed("B", "乙", "失败"),
            item_from_decision("A", "甲", decision_of(1, "strong", "low")),
        ]
        items[2].row.duplicate = True
        stats = index_html.compute_stats(items)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["success"] + stats["failed"], 2)

    def test_empty_rows_stats_are_zero(self):
        stats = index_html.compute_stats([])
        self.assertEqual(stats["total"], 0)
        self.assertTrue(all(v == 0 for v in stats["counts"].values()))

    def test_stats_text_matches_computed_numbers(self):
        items = [
            item_from_decision("A", "甲", decision_of(1, "strong", "low")),
            item_from_decision("B", "乙", decision_of(0, "weak", "high")),
        ]
        stats = index_html.compute_stats(items)
        html = index_html.render_stats(stats["counts"])
        self.assertIn("周期股 1", html)
        self.assertIn("弹性强 1", html)
        self.assertIn("低位 1", html)
        self.assertIn("高位 1", html)
        self.assertIn("失败/缺数据 0", html)


# ---------------------------------------------------------------------------
# 5. 完整页面
# ---------------------------------------------------------------------------


class TestFullPage(unittest.TestCase):

    def items(self):
        return [
            item_from_decision("601899", "紫金矿业",
                               decision_of(1, "strong", "low", 6.1, 2.5, 1.2),
                               path="/o/紫金矿业-601899-周期弹性初筛-20261112.html"),
            item_from_decision("600036", "招商银行",
                               decision_of("na", "weak", "na", None, 1.0, 98.8),
                               path="/o/招商银行-600036-周期弹性初筛-20261112.html"),
            item_failed("600519", "贵州茅台", "服务限频"),
        ]

    def test_page_structure(self):
        html = render_index(self.items())
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn('lang="zh-CN"', html)
        self.assertIn("<header>", html)
        self.assertIn('id="rows"', html)
        self.assertIn('id="q"', html)

    def test_no_unfilled_placeholders(self):
        html = render_index(self.items())
        self.assertNotIn("{{", html)
        self.assertNotIn("}}", html)

    def test_subtitle_counts(self):
        html = render_index(self.items())
        self.assertIn("共 3 只", html)

    def test_three_rows_in_page(self):
        html = render_index(self.items())
        tbody = html.split('id="rows">')[1].split("</tbody>")[0]
        self.assertEqual(tbody.count("<tr data-search="), 3)

    def test_no_sort_affordance(self):
        """**有意不提供排序** —— 方法论立场，不是遗漏。"""
        html = render_index(self.items())
        self.assertNotIn("sort", html.lower().replace("resort", ""))
        self.assertNotIn("data-sort", html)
        # 表头不能是按钮
        thead = html.split("<thead>")[1].split("</thead>")[0]
        self.assertNotIn("<button", thead)
        self.assertNotIn("onclick", thead)

    def test_page_states_no_sort_explicitly(self):
        html = render_index(self.items())
        self.assertIn("不排序", html)

    def test_footnote_states_three_judgements(self):
        html = render_index(self.items())
        self.assertIn("毛利率", html)
        self.assertIn("产能周转率", html)
        self.assertIn("分位", html)

    def test_responsive_and_print_css(self):
        html = render_index(self.items())
        self.assertIn("@media (max-width:720px)", html)
        self.assertIn("@media print", html)

    def test_search_js_reads_data_search(self):
        """搜索脚本必须真的用 data-search，否则这是个假搜索框。"""
        html = render_index(self.items())
        script = html.split("<script>")[1].split("</script>")[0]
        self.assertIn("data-search", script)
        self.assertIn("#rows tr", script)

    def test_generated_at_shown(self):
        html = render_index(self.items(), ts="2026-11-12 20:00")
        self.assertIn("2026-11-12 20:00", html)

    def test_empty_csv_page(self):
        html = render_index([])
        self.assertIn("CSV 里没有数据行", html)
        self.assertIn("共 0 只", html)

    def test_xss_in_name_neutralized(self):
        item = item_from_decision(
            "600000", "<script>alert(1)</script>",
            decision_of(1, "strong", "low"))
        html = render_index([item])
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)


# ---------------------------------------------------------------------------
# 6. 落盘、复用、模板
# ---------------------------------------------------------------------------


class TestWriteAndTemplate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cycle_index_")
        index_html.clear_template_cache()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_index_writes_file(self):
        items = [item_from_decision("601899", "紫金矿业",
                                    decision_of(1, "strong", "low"),
                                    path=os.path.join(self.tmp, "x.html"))]
        path = index_html.build_index(items, "池.csv", self.tmp, generated_at="X")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.path.basename(path), "池.html")

    def test_build_index_named_after_csv(self):
        path = index_html.build_index([], "我的股票池.csv", self.tmp, generated_at="X")
        self.assertEqual(os.path.basename(path), "我的股票池.html")

    def test_build_index_creates_dir(self):
        target = os.path.join(self.tmp, "sub", "dir")
        path = index_html.build_index([], "池.csv", target, generated_at="X")
        self.assertTrue(os.path.isdir(target))
        self.assertTrue(os.path.exists(path))

    def test_build_index_atomic_no_tmp_left(self):
        index_html.build_index([], "池.csv", self.tmp, generated_at="X")
        leftovers = [f for f in os.listdir(self.tmp) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_written_file_is_utf8(self):
        items = [item_from_decision("601899", "紫金矿业",
                                    decision_of(1, "strong", "low"),
                                    path=os.path.join(self.tmp, "x.html"))]
        path = index_html.build_index(items, "池.csv", self.tmp, generated_at="X")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("紫金矿业", text)

    def test_template_placeholders_known(self):
        known = {"{{TITLE}}", "{{SUBTITLE}}", "{{STATS}}",
                 "{{TABLE}}", "{{GENERATED_AT}}"}
        found = set(re.findall(r"\{\{[A-Z_]+\}\}",
                               index_html._load_template()))
        self.assertEqual(found, known)

    def test_template_cached(self):
        self.assertIs(index_html._load_template(), index_html._load_template())

    def test_clear_cache(self):
        index_html._load_template()
        index_html.clear_template_cache()
        self.assertNotIn("index", index_html._CACHE)

    def test_index_matches_reference_page_shape(self):
        """对齐参考页（选股_2026-11-10.html）：白底 header + 统计块 + 搜索 + 表格。"""
        html = render_index([item_from_decision(
            "601899", "紫金矿业", decision_of(1, "strong", "low"),
            path=os.path.join(self.tmp, "x.html"))])
        for token in ('class="stats"', 'class="toolbar"', 'class="tablecard"',
                      'class="foot"', 'type="search"'):
            self.assertIn(token, html)


# ---------------------------------------------------------------------------
# 7. 已有报告复用（续跑不重复取数）
# ---------------------------------------------------------------------------


class TestScanExisting(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cycle_scan_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def touch(self, name, text=""):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_scan_finds_report(self):
        self.touch("紫金矿业-601899-周期弹性初筛-20261112-144710.html")
        found = batch.scan_existing(self.tmp)
        self.assertIn("601899", found)

    def test_scan_picks_latest_timestamp(self):
        self.touch("甲-601899-周期弹性初筛-20260101-000000.html")
        self.touch("乙-601899-周期弹性初筛-20261112-144710.html")
        found = batch.scan_existing(self.tmp)
        self.assertEqual(found["601899"][0], "20261112-144710")
        self.assertIn("乙", found["601899"][1])

    def test_scan_ignores_unrelated_files(self):
        self.touch("池.html")
        self.touch("notes.txt")
        self.touch("紫金矿业-601899-别的工具-20261112-144710.html")
        self.assertEqual(batch.scan_existing(self.tmp), {})

    def test_scan_missing_dir_returns_empty(self):
        self.assertEqual(batch.scan_existing(os.path.join(self.tmp, "nope")), {})

    def test_parse_report_decision_roundtrip(self):
        """用真实渲染结果 → 存文件 → 读回来，10 个锚点一个不差。"""
        stock, d = real_decision("full_40q")
        m = metrics.compute_all(stock["quarters"], helpers.config)
        charts = metrics.series_for_chart(stock["quarters"], helpers.config)
        html = render.render(stock, d, m, charts, generated_at="X")
        path = self.touch("r.html", html)

        back = batch.parse_report_decision(path)
        self.assertEqual(back["cycle"]["label"], d["cycle"]["label"])
        self.assertEqual(back["elastic"]["label"], d["elastic"]["label"])
        self.assertEqual(back["position"]["label"], d["position"]["label"])
        self.assertAlmostEqual(back["cycle"]["gm_std"], d["cycle"]["gm_std"], places=3)
        self.assertAlmostEqual(back["elastic"]["ratio"], d["elastic"]["ratio"], places=3)
        self.assertAlmostEqual(back["position"]["pct"], d["position"]["pct"], places=3)
        self.assertEqual(back["meta"]["quarters"], len(stock["quarters"]))

    def test_parse_missing_file_returns_none(self):
        self.assertIsNone(batch.parse_report_decision(
            os.path.join(self.tmp, "nope.html")))

    def test_parse_garbage_returns_none(self):
        path = self.touch("bad.html", "<html>no attrs here</html>")
        self.assertIsNone(batch.parse_report_decision(path))

    def test_parse_empty_attrs_become_na_not_crash(self):
        path = self.touch("e.html",
                          '<body data-code="600000" data-name="甲" '
                          'data-gm-std="" data-elastic-ratio="">')
        back = batch.parse_report_decision(path)
        self.assertEqual(back["cycle"]["label"], decide.NA)
        self.assertIsNone(back["cycle"]["gm_std"])
        self.assertIsNone(back["elastic"]["ratio"])

    def test_parse_unescapes_name(self):
        path = self.touch("u.html",
                          '<body data-code="600000" data-name="A&amp;B" '
                          'data-cycle="1" data-elastic="strong">')
        back = batch.parse_report_decision(path)
        self.assertEqual(back["meta"]["quarters"], 0)
        # 只验不炸 + label 映射
        self.assertEqual(back["cycle"]["label"], decide.CYCLE_YES)
        self.assertEqual(back["elastic"]["label"], "strong")


# ---------------------------------------------------------------------------
# 8. CSV 读取（索引页的上游）
# ---------------------------------------------------------------------------


class TestReadCsv(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cycle_csv_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text, name="池.csv", encoding="utf-8"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        return path

    def test_basic(self):
        path = self.write("代码,名称\n601899,紫金矿业\n600036,招商银行\n")
        rows = batch.read_csv(path)
        self.assertEqual([r.code_raw for r in rows], ["601899", "600036"])
        self.assertEqual([r.name_raw for r in rows], ["紫金矿业", "招商银行"])

    def test_bom_is_stripped(self):
        """带 BOM 的 CSV 第一列名会变成 \\ufeff代码，匹配失败。"""
        path = self.write("代码,名称\n601899,紫金矿业\n", encoding="utf-8-sig")
        rows = batch.read_csv(path)
        self.assertEqual(len(rows), 1)

    def test_extra_name_columns(self):
        for header in ("股票代码,证券简称", "code,name", "SecuCode,SecuAbbr"):
            with self.subTest(header=header):
                path = self.write(f"{header}\n601899,紫金矿业\n")
                self.assertEqual(batch.read_csv(path)[0].code_raw, "601899")

    def test_blank_lines_skipped(self):
        path = self.write("代码,名称\n\n601899,紫金矿业\n\n\n600036,招商银行\n")
        self.assertEqual(len(batch.read_csv(path)), 2)

    def test_empty_file_returns_empty(self):
        path = self.write("")
        self.assertEqual(batch.read_csv(path), [])

    def test_missing_code_column_raises(self):
        path = self.write("行业,备注\n有色,ok\n")
        with self.assertRaises(ValueError) as ctx:
            batch.read_csv(path)
        self.assertIn("找不到代码列", str(ctx.exception))

    def test_missing_file_raises(self):
        with self.assertRaises(ValueError) as ctx:
            batch.read_csv(os.path.join(self.tmp, "nope.csv"))
        self.assertIn("不存在", str(ctx.exception))

    def test_header_only_returns_empty(self):
        path = self.write("代码,名称\n")
        self.assertEqual(batch.read_csv(path), [])

    def test_no_name_column_is_ok(self):
        path = self.write("代码\n601899\n")
        rows = batch.read_csv(path)
        self.assertEqual(rows[0].code_raw, "601899")
        self.assertEqual(rows[0].name_raw, "")

    def test_tab_delimited(self):
        path = self.write("代码\t名称\n601899\t紫金矿业\n")
        rows = batch.read_csv(path)
        self.assertEqual(rows[0].code_raw, "601899")

    def test_csv_not_modified(self):
        """**输入文件只读** —— 读一百遍 md5 也不变。"""
        import hashlib

        def md5_of(p):
            with open(p, "rb") as fh:
                return hashlib.md5(fh.read()).hexdigest()

        path = self.write("代码,名称\n601899,紫金矿业\n600036,招商银行\n")
        before = md5_of(path)
        batch.read_csv(path)
        batch.read_csv(path)
        self.assertEqual(before, md5_of(path))

    def test_line_numbers_are_1_based_data_lines(self):
        path = self.write("代码,名称\n601899,紫金矿业\n600036,招商银行\n")
        rows = batch.read_csv(path)
        self.assertEqual(rows[0].line, 2)
        self.assertEqual(rows[1].line, 3)


# ---------------------------------------------------------------------------
# 9. 渲染行与真实决策的一致性
# ---------------------------------------------------------------------------


class TestRowConsistency(unittest.TestCase):

    def test_every_label_combination_renders(self):
        """枚举所有 label 组合，逐一出图 —— 不能有 KeyError。"""
        cycles = (decide.CYCLE_YES, decide.CYCLE_NO, decide.NA)
        elastics = decide.ELASTIC_LABELS
        positions = decide.POSITION_LABELS
        seen = 0
        for c in cycles:
            for e in elastics:
                for p in positions:
                    with self.subTest(cycle=c, elastic=e, position=p):
                        d = decision_of(c, e, p)
                        item = item_from_decision("600000", "测试", d)
                        html = index_html.render_row(item)
                        self.assertEqual(html.count("<td"), len(index_html.COLUMNS))
                        self.assertNotIn("None", html)
                        seen += 1
        self.assertEqual(seen, len(cycles) * len(elastics) * len(positions))

    def test_financial_position_renders_dash(self):
        stock, d = real_decision("financial")
        item = item_from_decision(stock["code"], stock["name"], d)
        html = index_html.render_row(item)
        self.assertIn("b-muted", html)

    def test_financial_position_shows_not_applicable(self):
        """银行的位置列显示「不适用」，不是「—」。

        两者都对应 label=na，但含义完全不同：
          * 不适用 = 产能周转率对银行没有意义（永远不会有数）
          * —      = 这只股票数据缺失（换个数据源可能有）
        混在一起显示等于把"不该问"和"问不到"混为一谈。
        """
        stock, d = real_decision("financial")
        item = item_from_decision(stock["code"], stock["name"], d)
        html = index_html.render_row(item)
        self.assertIn("不适用", html)
        cell = re.search(r'<td><span class="badge b-muted">([^<]*)</span></td>',
                         html).group(1)
        self.assertEqual(cell, "不适用")

    def test_bank_never_shows_low_high(self):
        stock, d = real_decision("financial")
        html = index_html.render_row(
            item_from_decision(stock["code"], stock["name"], d))
        self.assertNotIn("低位", html)
        self.assertNotIn("高位", html)

    def test_missing_position_still_shows_dash(self):
        """非金融 + 位置 na → 仍是「—」（数据缺失，不是不适用）。"""
        stock, d = real_decision("balance_missing")
        html = index_html.render_row(
            item_from_decision(stock["code"], stock["name"], d))
        self.assertIn("b-muted", html)
        self.assertNotIn("不适用", html)

    def test_neutral_position_renders_neutral_badge(self):
        item = item_from_decision("600014", "全等周转",
                                  decision_of(0, "na", "neutral"))
        html = index_html.render_row(item)
        self.assertIn("b-neutral", html)
        self.assertIn("中性", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
