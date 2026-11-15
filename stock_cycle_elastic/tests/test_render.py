"""渲染层测试（README.md「HTML 报告设计」/「测试」）。

这一层最容易出**静默错误**：HTML 照样生成、页面照样能看，但
机器可读锚点缺一个、脚注少一条、把 "None" 写进了页面。
索引页与下游解析完全依赖这些锚点，所以逐字断言。

守的硬约束：
    * 9 个 data-* 恒定存在；值缺失时是**空串**，绝不是字符串 "None"
    * 全文不出现 None / nan / NaN / Infinity
    * 六个徽章文案；旧徽章（高风险 / 低位候选 / 降级 / 口径失真 / ★）绝迹
    * 脚注 5 条口径声明，缺一即失败
    * 全文不出现 DFL / 财务杠杆系数 / 利息保障（已从工具里彻底删除）
    * 合同负债**只作为原料出现一次**（剔除公司名后计数 == 1）
    * 3 张内联 SVG，各有 viewBox；无 JS、无 CDN
"""

import re
import unittest

import tests.helpers  # noqa: F401  —— 负责 sys.path 注入
from tests import helpers
from tests.fixtures import load

from cycle import build, decide, metrics, render

# ---------------------------------------------------------------------------
# 公共装配
# ---------------------------------------------------------------------------

#: 参与渲染矩阵的 fixture（覆盖 6 种组合：正/负/na × 三种判断）
MATRIX = (
    "full_40q",                    # 周期 + 强 + 低位，最完整
    "short_12q",                   # 非周期 + 弱 + 高位 + 历史不足
    "balance_missing",             # 缺资产负债表 → 判断③ na
    "flat_margin_equal_turnover",  # 弹性 na（比值 None）+ 位置 neutral
    "financial",                   # 银行 → 判断①③ 都 na，产能不适用
    "no_cl",                       # 缺合同负债字段
)

#: 15 个 data-* 锚点（顺序须与 render.DATA_ATTRS 一致）
#: 第 10 个 data-financial 用来区分"金融不适用"与"数据缺失"；
#: 第 11/12 个 data-gm-cv / data-cycle-channel 让索引页能判断"标签是哪条通道来的"
#: （判断① 双通道后，光看 data-gm-std 已推不出标签）
ATTRS = (
    "data-code",
    "data-name",
    "data-quarters",
    "data-gm-std",
    "data-cycle",
    "data-elastic",
    "data-elastic-ratio",
    "data-position",
    "data-position-pct",
    "data-financial",
    "data-gm-cv",
    "data-cycle-channel",
    "data-capacity-direction",
    "data-elastic-reason",
    "data-latest",
)

#: 旧版（已废弃）的徽章文案 —— 出现即回归
LEGACY_BADGES = ("高风险", "低位候选", "降级", "口径失真", "★", "四池", "诚信折扣")

#: 已从工具里删除的指标名
REMOVED_METRICS = ("DFL", "财务杠杆系数", "利息保障倍数", "利息保障")

#: 脚注 5 条口径声明（逐字取自 templates/report.html）
FOOTNOTE_CLAIMS = (
    "毛利率标准差为季度口径、5 年窗口",
    "周期性仅以毛利率波动一项判定",
    "产能周转率为固定资产+在建工程口径",
    "弹性为纯经营杠杆，不含财务杠杆",
    "所有标签为初步筛选，不构成买卖建议",
)


class Rendered:
    """一份渲染好的报告 + 它依赖的中间结果，供用例分段断言。"""

    def __init__(self, name, cfg=None):
        self.name = name
        self.cfg = cfg or helpers.config
        self.stock = build.build(load(name))
        self.metrics = metrics.compute_all(self.stock["quarters"], self.cfg)
        self.decision = decide.decide(
            self.stock["quarters"], self.metrics,
            self.stock.get("industry", ""), self.cfg,
        )
        self.charts = metrics.series_for_chart(self.stock["quarters"], self.cfg)
        self.html = render.render(self.stock, self.decision,
                                  self.metrics, self.charts,
                                  generated_at="2026-11-12 20:00")

    @property
    def attrs(self):
        return parse_attrs(self.html)


def render_named(name, cfg=None):
    return Rendered(name, cfg)


def parse_attrs(html):
    """把 body 上的 data-* 解成 dict（模拟索引页的解析方式）。"""
    match = re.search(r"<body([^>]*)>", html)
    assert match, "没找到 <body> 标签"
    raw = match.group(1)
    out = {}
    for key in ATTRS:
        found = re.search(rf'{key}="([^"]*)"', raw)
        out[key] = found.group(1) if found else None
    return out


def strip_identity(html, stock):
    """剔除公司代码/名称/行业，避免"无合同负债测试"这类名字污染全文断言。"""
    for token in (stock.get("name"), stock.get("code")):
        if token:
            html = html.replace(str(token), "")
    return html


def judge_html(html):
    """取出"三个判断"表 tbody 的 HTML。"""
    return html.split("<tbody>")[1].split("</tbody>")[0]


# ---------------------------------------------------------------------------
# 1. 9 个 data-* 锚点
# ---------------------------------------------------------------------------


class TestDataAttrs(unittest.TestCase):
    """§8.5：少一个锚点，索引页就会静默填空白。"""

    def test_all_ten_present_in_every_report(self):
        for name in MATRIX:
            with self.subTest(fixture=name):
                attrs = render_named(name).attrs
                for key in ATTRS:
                    self.assertIsNotNone(attrs[key], f"{name} 缺 {key}")

    def test_attr_order_is_fixed(self):
        """顺序固定，正则解析才稳。"""
        html = render_named("full_40q").html
        body = re.search(r"<body([^>]*)>", html).group(1)
        positions = [body.index(f'{key}="') for key in ATTRS]
        self.assertEqual(positions, sorted(positions))

    def test_data_attrs_matches_render_constant(self):
        self.assertEqual(tuple(render.DATA_ATTRS), ATTRS)
        self.assertEqual(len(render.DATA_ATTRS), 15)

    def test_code_and_name_are_verbatim(self):
        case = render_named("full_40q")
        self.assertEqual(case.attrs["data-code"], "601899")
        self.assertEqual(case.attrs["data-name"], "紫金矿业")

    def test_quarters_is_plain_integer(self):
        for name in MATRIX:
            with self.subTest(fixture=name):
                case = render_named(name)
                value = case.attrs["data-quarters"]
                self.assertRegex(value, r"^\d+$")
                self.assertEqual(int(value), len(case.stock["quarters"]))

    def test_cycle_is_enum_not_python_bool(self):
        """1 / 0 / na —— 绝不出现 True/False（§7.3 的理由）。"""
        for name in MATRIX:
            with self.subTest(fixture=name):
                value = render_named(name).attrs["data-cycle"]
                self.assertIn(value, ("1", "0", "na"))
                self.assertNotIn(value, ("True", "False"))

    def test_elastic_and_position_are_enum(self):
        for name in MATRIX:
            with self.subTest(fixture=name):
                attrs = render_named(name).attrs
                self.assertIn(attrs["data-elastic"],
                              ("strong", "weak", "unstable", "na"))
                self.assertIn(attrs["data-position"],
                              ("low", "neutral", "high", "na"))

    def test_missing_value_is_empty_string_not_none(self):
        """**最关键一条**：缺值 → 空串。写 "None" 会让索引页判成"有值"。"""
        # financial：判断① 的 gm_std 是 None
        attrs = render_named("financial").attrs
        self.assertEqual(attrs["data-gm-std"], "")
        self.assertEqual(attrs["data-cycle"], "na")

        # balance_missing：判断③ 的 pct 是 None
        attrs = render_named("balance_missing").attrs
        self.assertEqual(attrs["data-position-pct"], "")
        self.assertEqual(attrs["data-position"], "na")

        # flat_margin_equal_turnover：弹性比值算不出来
        attrs = render_named("flat_margin_equal_turnover").attrs
        self.assertEqual(attrs["data-elastic-ratio"], "")
        self.assertEqual(attrs["data-elastic"], "na")

    def test_attr_value_helper_never_emits_none(self):
        self.assertEqual(render.attr_value(None), "")
        self.assertEqual(render.attr_value(""), "")
        self.assertEqual(render.attr_value(0), "0")
        self.assertEqual(render.attr_value(0.0), "0")   # 0 不能被当成空

    def test_float_attr_is_trimmed(self):
        """6.13729968 → 6.1373（4 位足够，且不会太长）。"""
        self.assertEqual(render.attr_value(6.1372996805134195), "6.1373")
        self.assertEqual(render.attr_value(50.0), "50")
        self.assertEqual(render.attr_value(1.25), "1.25")

    def test_numeric_attrs_are_parseable(self):
        """非空时必须是能 float() 的数字，索引页会直接转。"""
        attrs = render_named("full_40q").attrs
        for key in ("data-gm-std", "data-elastic-ratio", "data-position-pct"):
            with self.subTest(attr=key):
                self.assertNotEqual(attrs[key], "")
                float(attrs[key])  # 不抛错即通过

    def test_attr_values_agree_with_decision(self):
        """锚点值必须与 decide() 的输出一致 —— 不能各说各话。"""
        for name in MATRIX:
            with self.subTest(fixture=name):
                case = render_named(name)
                d = case.decision
                attrs = case.attrs
                self.assertEqual(attrs["data-cycle"], str(d["cycle"]["label"]))
                self.assertEqual(attrs["data-elastic"], str(d["elastic"]["label"]))
                self.assertEqual(attrs["data-position"], str(d["position"]["label"]))

    def test_financial_flag_is_1_or_0(self):
        """`data-financial` 在报告里就两种值 —— 索引页靠它区分不适用与缺失。"""
        for name in MATRIX:
            with self.subTest(fixture=name):
                value = render_named(name).attrs["data-financial"]
                self.assertIn(value, ("1", "0"))

    def test_financial_flag_set_for_bank(self):
        """招商银行 → 1；紫金矿业 → 0。"""
        self.assertEqual(render_named("financial").attrs["data-financial"], "1")
        self.assertEqual(render_named("full_40q").attrs["data-financial"], "0")

    def test_financial_flag_never_empty(self):
        """这个锚点没有 None 状态，绝不能是空串（否则索引页判不出来）。"""
        for name in MATRIX:
            with self.subTest(fixture=name):
                self.assertNotEqual(render_named(name).attrs["data-financial"], "")


# ---------------------------------------------------------------------------
# 2. 全文不得出现的脏东西
# ---------------------------------------------------------------------------


class TestNoGarbage(unittest.TestCase):
    """HTML 里出现 "None" 是最典型的模板 bug，肉眼极难发现。"""

    def test_no_none_or_nan(self):
        """页面上出现 "None" 是最典型的模板 bug，肉眼极难发现。

        "nan" 不能当裸子串查 —— `data-financial` 里就含 `nan` 这三个字母。
        用词边界正则，只抓真正当作数值打印出来的 NaN / inf。
        """
        bad_none = re.compile(r"\bNone\b")
        bad_nan = re.compile(r"\b(nan|NaN|inf|Infinity)\b")
        for name in MATRIX:
            with self.subTest(fixture=name):
                html = render_named(name).html
                self.assertIsNone(bad_none.search(html), f"{name} 出现了 None")
                self.assertIsNone(bad_nan.search(html), f"{name} 出现了 nan/inf")

    def test_no_python_repr_leftovers(self):
        """模板漏替换会留下 {{KEY}}，或留下 dict/list 的 repr。"""
        for name in MATRIX:
            with self.subTest(fixture=name):
                html = render_named(name).html
                self.assertNotIn("{{", html)
                self.assertNotIn("}}", html)
                self.assertNotIn("{'", html)
                self.assertNotIn("[(", html)

    def test_no_legacy_badges(self):
        for name in MATRIX:
            with self.subTest(fixture=name):
                body = strip_identity(render_named(name).html, load(name))
                for token in LEGACY_BADGES:
                    self.assertNotIn(token, body, f"{name} 残留旧徽章 {token}")

    def test_no_removed_metrics(self):
        """财务杠杆已论证为"零信息"，连展示都不做（§6.3）。"""
        for name in MATRIX:
            with self.subTest(fixture=name):
                html = render_named(name).html
                for token in REMOVED_METRICS:
                    self.assertNotIn(token, html, f"{name} 残留 {token}")

    def test_no_contradictory_footnote(self):
        """旧版脚注说"弹性含财务杠杆"，与现在的口径完全相反。"""
        html = render_named("full_40q").html
        self.assertNotIn("含财务杠杆", html.replace("不含财务杠杆", ""))

    def test_no_js_no_cdn(self):
        """单文件、离线可看：不许引外链、不许带脚本。"""
        for name in MATRIX:
            with self.subTest(fixture=name):
                html = render_named(name).html
                self.assertNotIn("<script", html)
                self.assertNotIn("http://", html)
                self.assertNotIn("https://", html)
                self.assertNotIn("cdn.", html)
                self.assertNotIn("@import", html)


# ---------------------------------------------------------------------------
# 3. 徽章（§8.3）
# ---------------------------------------------------------------------------


class TestBadges(unittest.TestCase):
    """徽章是页面上唯一"给结论"的地方，文案必须与 decide 严格对应。"""

    def badges_of(self, name):
        html = render_named(name).html
        return re.findall(r'<span class="badge ([a-z-]+)">([^<]*)</span>', html)

    def test_full_report_has_three_badges(self):
        """三个主判断三个徽章；产能方向若判定为 expand/shrink 会再多一个限定语。"""
        pairs = self.badges_of("full_40q")
        self.assertEqual(pairs[:3],
                         [("badge-cycle-yes", "周期股"),
                          ("badge-elastic-strong", "弹性强"),
                          ("badge-low", "低位")])
        for cls, text in pairs[3:]:
            self.assertEqual(cls, "badge-capacity")
            self.assertIn(text, ("扩产中", "产能在退"))

    def test_short_history_adds_insufficient_badge(self):
        pairs = self.badges_of("short_12q")
        texts = [t for _, t in pairs]
        self.assertEqual(texts[0], "非周期")
        self.assertEqual(texts[1], "弹性弱")
        self.assertEqual(texts[2], "高位")
        self.assertEqual(texts[-1], "历史不足")

    def test_unstable_replaces_strong_weak(self):
        """`unstable` 与 strong/weak 三者互斥，不能同时出现。"""
        case = render_named("neg_profit")
        self.assertEqual(case.decision["elastic"]["label"], "unstable")
        texts = [t for _, t in re.findall(
            r'<span class="badge ([a-z-]+)">([^<]*)</span>', case.html)]
        self.assertIn("比值不稳定", texts)
        self.assertNotIn("弹性强", texts)
        self.assertNotIn("弹性弱", texts)

    def test_financial_uses_muted_badge_no_position(self):
        """银行：判断③ 无意义 → "产能不适用"，且不出低位/高位。"""
        pairs = self.badges_of("financial")
        texts = [t for _, t in pairs]
        self.assertIn("产能不适用", texts)
        self.assertNotIn("低位", texts)
        self.assertNotIn("高位", texts)

    def test_na_position_produces_no_position_badge(self):
        """balance_missing：pct 缺失 → 位置 na → 不出位置徽章。"""
        texts = [t for _, t in self.badges_of("balance_missing")]
        self.assertNotIn("低位", texts)
        self.assertNotIn("高位", texts)
        self.assertEqual(texts, ["周期股", "弹性强"])

    def test_neutral_position_produces_no_badge(self):
        """中间地带不给标签（§7.3）—— 也不给徽章。"""
        texts = [t for _, t in self.badges_of("flat_margin_equal_turnover")]
        self.assertEqual(texts, ["非周期"])

    def test_badge_order_is_cycle_then_elastic_then_position(self):
        pairs = self.badges_of("short_12q")
        classes = [c for c, _ in pairs]
        self.assertTrue(classes[0].startswith("badge-cycle"))
        self.assertTrue(classes[1].startswith("badge-elastic"))
        self.assertTrue(classes[2].startswith("badge-"))

    def test_only_known_badge_texts(self):
        """文案白名单 —— 防止有人随手加新徽章。"""
        allowed = {"周期股", "非周期", "弹性强", "弹性弱",
                   "比值不稳定", "低位", "高位", "产能不适用",
                   "历史不足", "数据不足",
                   "相对波动", "①否②强·存疑", "扩产中", "产能在退"}
        for name in MATRIX:
            with self.subTest(fixture=name):
                for _, text in self.badges_of(name):
                    self.assertIn(text, allowed)

    def test_empty_badges_fall_back_to_muted(self):
        """一个徽章都出不来时给"数据不足"，不能是空白 div。"""
        decision = {
            "cycle": {"label": decide.NA, "gm_std": None},
            "elastic": {"label": decide.NA, "ratio": None},
            "position": {"label": decide.NA, "pct": None},
            "flags": {},
            "meta": {"quarters": 0},
        }
        html = render.render_badges(decision)
        self.assertIn("数据不足", html)
        self.assertIn("badge-muted", html)


# ---------------------------------------------------------------------------
# 4. 四块结构 + 脚注
# ---------------------------------------------------------------------------


class TestStructure(unittest.TestCase):

    def test_four_sections_present(self):
        """页面骨架：Hero + 两个 section（第三栏「原始数据」已删）。"""
        html = render_named("full_40q").html
        for heading in ("三个判断", "指标历史"):
            self.assertIn(f"<h2>{heading}", html)
        self.assertIn('class="hero"', html)

    def test_exactly_three_judge_rows(self):
        """一个判断一行，不多不少。"""
        html = render_named("full_40q").html
        tbody = html.split("<tbody>")[1].split("</tbody>")[0]
        self.assertEqual(tbody.count("<tr>"), 3)
        for name in ("是不是周期股", "弹性", "周期位置"):
            self.assertIn(f"<td>{name}</td>", tbody)

    def test_five_footnote_claims(self):
        """§8.6 五条口径声明，少一条就打回去。"""
        for name in MATRIX:
            with self.subTest(fixture=name):
                html = render_named(name).html
                for claim in FOOTNOTE_CLAIMS:
                    self.assertIn(claim, html, f"{name} 缺脚注：{claim}")

    def test_footnote_ordered_list_has_six_items(self):
        """六条：5 条原有口径 + 新增的「周期位置只看供给端」。"""
        html = render_named("full_40q").html
        foot = html.split('<div class="foot">')[1]
        ordered = foot.split("<ol>")[1].split("</ol>")[0]
        self.assertEqual(ordered.count("<li>"), 6)

    def test_gm_window_number_is_interpolated(self):
        """脚注里的窗口数字来自 config，不能硬编码成别的值。"""
        html = render_named("full_40q").html
        self.assertIn(f"（{helpers.config.WAVE_WINDOW_QUARTERS} 季）", html)

    def test_title_contains_name_code_and_tag(self):
        html = render_named("full_40q").html
        title = re.search(r"<title>([^<]*)</title>", html).group(1)
        self.assertIn("紫金矿业", title)
        self.assertIn("601899", title)
        self.assertIn(helpers.config.REPORT_NAME_TAG, title)

    def test_data_range_shown(self):
        case = render_named("full_40q")
        html = case.html
        self.assertIn(case.stock["quarters"][0]["date"], html)
        self.assertIn(case.stock["quarters"][-1]["date"], html)

    def test_data_gaps_listed_when_present(self):
        """有 data_gaps 就列出；没有就整块不出现。"""
        stock = {
            "code": "600000", "name": "测试", "industry": "",
            "quarters": [],
            "meta": {"data_gaps": ["2024-03-31 缺资产负债表"]},
        }
        decision = {
            "cycle": {"label": decide.NA, "gm_std": None},
            "elastic": {"label": decide.NA, "ratio": None},
            "position": {"label": decide.NA, "pct": None},
            "flags": {}, "meta": {"quarters": 0},
        }
        m = helpers.metrics_of()
        html = render.render(stock, decision, m, {}, generated_at="X")
        self.assertIn("数据说明", html)
        self.assertIn("2024-03-31 缺资产负债表", html)

    def test_no_gaps_block_when_clean(self):
        html = render_named("full_40q").html
        self.assertNotIn("数据说明", html)


# ---------------------------------------------------------------------------
# 5. 「原始数据」区块已删除（用户要求）
# ---------------------------------------------------------------------------


class TestRawDataRemoved(unittest.TestCase):
    """原「原始数据」栏（合同负债 / 单季营收）已整块删除。

    删除理由有两层：
      1. 用户的明确要求 —— 报告只回答那三个判断，展示型数据去原始报表看；
      2. 与工具定位一致 —— 合同负债曾是候选指标，**实测后决定不进判定**
         （§6.2：覆盖率 16/16 但领先性只 3/7 稳定）。既不判断、又要占一栏，
         就是纯粹的视觉噪音。

    这组测试的存在意义是**防止回流**：以后有人觉得"顺手展示一下也不亏"，
    会立刻红。
    """

    def test_section_heading_is_gone(self):
        for name in MATRIX:
            with self.subTest(fixture=name):
                self.assertNotIn("原始数据", render_named(name).html)

    def test_rawdata_div_is_gone(self):
        for name in MATRIX:
            with self.subTest(fixture=name):
                self.assertNotIn('class="rawdata"', render_named(name).html)

    def test_contract_liability_is_gone_entirely(self):
        """合同负债这个词在整页里一次都不该出现。

        `no_cl` 这个 fixture 的股票名恰好叫「无合同负债测试」，所以要先把
        名称/代码剥掉再断言 —— 不然测的是测试数据自己。
        """
        for name in MATRIX:
            with self.subTest(fixture=name):
                case = render_named(name)
                self.assertNotIn("合同负债", strip_identity(case.html, case.stock))

    def test_renderer_function_is_gone(self):
        self.assertFalse(hasattr(render, "render_raw_data"))

    def test_placeholder_is_unknown(self):
        """模板里不能再有 {{RAW_DATA}}（否则会原样漏到页面上）。"""
        self.assertNotIn("{{RAW_DATA}}", render._load_template())

    def test_page_still_has_three_sections(self):
        """删掉一栏之后，剩下的结构不能少。"""
        html = render_named("full_40q").html
        self.assertIn("<h2>三个判断", html)
        self.assertIn("<h2>指标历史", html)
        self.assertIn('class="hero"', html)

    def test_zero_revenue_does_not_divide(self):
        """营收为 0 时占比算不出来，不能炸也不能写 inf。

        fixture 里"零营收"不在最后一季（build 层的 capacity_turnover 已经是
        None），所以这里手工把末季营收改成 0，直接打渲染层的分支。
        「原始数据」区块删除后，这条断言改走整页渲染 —— 只要页面里不出现
        inf / ZeroDivision 就说明分母为零的分支被兜住了。
        """
        stock = {
            "code": "600001", "name": "零营收末季", "industry": "",
            "quarters": [
                {"date": "2026-06-30", "revenue_q": 1e8, "cost_q": 7e7,
                 "op_profit_q": 3e7, "fixed_asset": 5e8, "cip": 0.0,
                 "contract_liab": 1e8, "gross_margin": 0.30,
                 "capacity_turnover": 0.8},
                {"date": "2026-09-30", "revenue_q": 0.0, "cost_q": 0.0,
                 "op_profit_q": -1e7, "fixed_asset": 5e8, "cip": 0.0,
                 "contract_liab": 1e8, "gross_margin": None,
                 "capacity_turnover": None},
            ],
            "meta": {},
        }
        html = render.render(stock, {}, {}, {})
        self.assertIsNone(re.search(r"\b(inf|nan)\b", html))
        self.assertNotIn("ZeroDivision", html)
        self.assertIn("—", html)

    def test_zero_revenue_fixture_still_renders(self):
        """fixture 的零营收季不能把整页带崩。"""
        html = render_named("zero_revenue").html
        self.assertNotIn("inf", html)
        self.assertNotIn("None", html)
        self.assertEqual(html.count("<svg"), 3)


# ---------------------------------------------------------------------------
# 6. 图表（内联 SVG）
# ---------------------------------------------------------------------------


class TestCharts(unittest.TestCase):

    def test_three_svgs_with_viewbox(self):
        for name in MATRIX:
            with self.subTest(fixture=name):
                html = render_named(name).html
                self.assertEqual(html.count("<svg"), 3)
                for svg in re.findall(r"<svg[^>]*>", html):
                    self.assertIn("viewBox", svg)
                    self.assertIn("role=\"img\"", svg)

    def test_chart_titles(self):
        html = render_named("full_40q").html
        for title in ("毛利率", "弹性比值", "产能周转率"):
            self.assertIn(f"<h3>{title}", html)

    def test_empty_series_renders_placeholder(self):
        """全 None → 画"无可用数据"，而不是抛异常或空白 svg。"""
        svg = render._line_chart([("26Q1", None), ("26Q2", None)], "测试")
        self.assertIn("无可用数据", svg)
        self.assertIn("<svg", svg)

    def test_empty_points_renders_placeholder(self):
        self.assertIn("无可用数据", render._line_chart([], "测试"))
        self.assertIn("无可用数据", render._bar_chart([], "测试"))

    def test_single_point_does_not_crash(self):
        """一个点连不成线，但也不该抛错。"""
        svg = render._line_chart([("26Q1", 12.0)], "测试")
        self.assertIn("<svg", svg)
        self.assertIn("<circle", svg)   # 端点圆点还在

    def test_flat_series_does_not_divide_by_zero(self):
        """所有值相等 → 纵轴跨度 0，不能除零。"""
        svg = render._line_chart([("a", 5.0), ("b", 5.0), ("c", 5.0)], "测试")
        self.assertIn("<svg", svg)
        self.assertNotIn("inf", svg)
        self.assertNotIn("nan", svg)

    def test_bar_chart_has_zero_axis_and_both_colors(self):
        """正负值都要能画（比值有负）。"""
        svg = render._bar_chart([("a", 2.0), ("b", -1.0)], "测试", threshold=2.0)
        self.assertIn('class="axis"', svg)
        self.assertIn('class="threshold"', svg)
        self.assertIn("#2f6152", svg)   # 正：墨绿
        self.assertIn("#b3261e", svg)   # 负：红

    def test_threshold_outside_data_range_still_inside_viewbox(self):
        """参考线不能被裁到图外（值域要把 extras 算进去）。"""
        svg = render._line_chart([("a", 30.0), ("b", 31.0)], "测试", threshold=5.0)
        ys = [float(m) for m in re.findall(r'\bcy="(-?\d+\.?\d*)"', svg)]
        self.assertTrue(all(0 <= y <= render.SVG_H for y in ys))

    def test_gaps_break_the_line(self):
        """中间 None → 折线断开，而不是连成一条假的直线。"""
        svg = render._line_chart(
            [("a", 1.0), ("b", 2.0), ("c", None), ("d", 3.0), ("e", 4.0)], "测试")
        self.assertEqual(svg.count("<path"), 2)

    def test_render_charts_accepts_empty_dict(self):
        """三块图都在，且都是"无可用数据"占位。"""
        html = render.render_charts({})
        self.assertEqual(html.count("<svg"), 3)
        # 每张空图里文案出现 2 次：aria-label 一次 + <text> 一次
        self.assertEqual(html.count("无可用数据"), 6)
        self.assertEqual(html.count('<text class="empty"'), 3)


# ---------------------------------------------------------------------------
# 6b. 横轴季度刻度
# ---------------------------------------------------------------------------


class TestXAxisTicks(unittest.TestCase):
    """横轴按季度标时间。

    这张图是给"人眼连点成趋势"用的，没有时间刻度就只能看到形状看不出节奏 ——
    看不出哪个季度是拐点，也看不出现在离上一次拐点隔了几季。
    """

    def ticks(self, svg):
        return re.findall(r'<text class="xtick"[^>]*>([^<]*)</text>', svg)

    def test_quarter_label_from_raw_date(self):
        self.assertEqual(render._quarter_label("2026-09-30"), "26Q3")
        self.assertEqual(render._quarter_label("2026-01-31"), "26Q1")
        self.assertEqual(render._quarter_label("2025-12-31"), "25Q4")
        self.assertEqual(render._quarter_label("2024-06-30"), "24Q2")

    def test_quarter_label_accepts_preformatted(self):
        """`points` 传进来的是 `_short_date` 处理过的 '26Q3'。

        早期版本只认原始日期串，接进图表时所有刻度都判空、一个也画不出来。
        """
        for text in ("26Q3", "24Q1", "99Q4"):
            with self.subTest(label=text):
                self.assertEqual(render._quarter_label(text), text)

    def test_quarter_label_rejects_garbage(self):
        """认不出就返回空串 —— 宁可不标，不标错。"""
        for bad in ("", None, "2026", "2026-13-45", "2026-00-01", "abc",
                    "26Q5", "26Q0"):
            with self.subTest(label=bad):
                self.assertEqual(render._quarter_label(bad), "")

    def test_line_chart_has_baseline_and_ticks(self):
        pts = [(f"{20 + i // 4}Q{i % 4 + 1}", float(i)) for i in range(12)]
        svg = render._line_chart(pts, "测试")
        self.assertIn('class="xaxis"', svg)
        got = self.ticks(svg)
        self.assertTrue(got, "折线图横轴一个刻度都没画")
        # 最新一季必须有刻度：看报告时最想知道"现在到哪了"
        self.assertIn(pts[-1][0], got)

    def test_bar_chart_has_baseline_and_ticks(self):
        pts = [(f"25Q{i + 1}", float(i + 1)) for i in range(4)]
        svg = render._bar_chart(pts, "测试")
        self.assertIn('class="xaxis"', svg)
        got = self.ticks(svg)
        self.assertEqual(got, ["25Q1", "25Q2", "25Q3", "25Q4"])

    def test_ticks_do_not_overlap_in_dense_series(self):
        """40 季挤在 582px 里，每格 14.9px，装不下 38px 的"22Q1"。

        这时必须**跳着标**，否则文字叠成一团黑。断言：相邻刻度的像素间距
        不小于标签宽度。
        """
        pts = [(f"{16 + i // 4}Q{i % 4 + 1}", float(i)) for i in range(40)]
        svg = render._line_chart(pts, "测试")
        xs = sorted(float(m) for m in
                    re.findall(r'<text class="xtick" x="(-?\d+\.?\d*)"', svg))
        self.assertGreater(len(xs), 1)
        for a, b in zip(xs, xs[1:]):
            self.assertGreaterEqual(b - a, render.TICK_LABEL_PX - 3,
                                    f"刻度太挤：{a} → {b}")

    def test_20_quarter_chart_skips_ticks(self):
        """20 季时每格 30.6px < 38px —— 必须隔一个标一个。

        这是实机截图发现的 bug：label_px 定成 30.0 时刚好"刚好不挤"，
        于是 20 个刻度全画出来，渲染成 `21Q422Q122Q2...` 连成一串。
        """
        pts = [(f"{21 + i // 4}Q{i % 4 + 1}", float(i)) for i in range(20)]
        svg = render._line_chart(pts, "测试")
        got = self.ticks(svg)
        self.assertLess(len(got), 20, "20 季不该标满 20 个刻度")
        self.assertIn(pts[-1][0], got, "最新一季必须有刻度")

    def test_tick_label_width_is_conservative_enough(self):
        """`22Q1` 四个字符在 10px 字号下约 34px，留到 38 才算安全。"""
        self.assertGreaterEqual(render.TICK_LABEL_PX, 34.0)

    def test_last_tick_always_present(self):
        """从最右往左挑刻度，保证最新一季永远有标。"""
        for n in (1, 2, 3, 7, 8, 20, 40, 43):
            with self.subTest(n=n):
                picks = render._pick_ticks(n, 582.0 / max(n - 1, 1))
                self.assertIn(n - 1, picks)
                picks_bar = render._bar_ticks(n, 582.0 / n)
                self.assertIn(n - 1, picks_bar)

    def test_tick_labels_stay_inside_viewbox(self):
        """首个刻度贴左边、末个贴右边，文字不能被裁掉。"""
        pts = [("22Q1", 1.0), ("22Q2", 2.0), ("22Q3", 3.0)]
        svg = render._line_chart(pts, "测试")
        self.assertIn('text-anchor="start"', svg)
        self.assertIn('text-anchor="end"', svg)
        for x in re.findall(r'<text class="xtick" x="(-?\d+\.?\d*)"', svg):
            self.assertGreaterEqual(float(x), render.PAD_L)
            self.assertLessEqual(float(x), render.SVG_W - render.PAD_R)

    def test_ticks_sit_below_plot_area(self):
        """刻度文字要落在绘图区底边之下，不能压到折线上。"""
        pts = [(f"24Q{i + 1}", float(i)) for i in range(4)]
        svg = render._line_chart(pts, "测试")
        base_y = render.SVG_H - render.PAD_B
        for y in re.findall(r'<text class="xtick"[^>]*y="(-?\d+\.?\d*)"', svg):
            self.assertGreater(float(y), base_y - 1)
            self.assertLessEqual(float(y), render.SVG_H)

    def test_no_ticks_when_labels_unparseable(self):
        """标签认不出就一条刻度都不画，而不是画一堆空 <text>。"""
        svg = render._line_chart([("x", 1.0), ("y", 2.0)], "测试")
        self.assertIn('class="xaxis"', svg)   # 基线还是要的
        self.assertEqual(self.ticks(svg), [])

    def test_empty_chart_has_no_axis(self):
        svg = render._line_chart([], "测试")
        self.assertNotIn('class="xaxis"', svg)
        self.assertIn("无可用数据", svg)


# ---------------------------------------------------------------------------
# 6c. 周期位置三档含义（用户明确要求写进 HTML）
# ---------------------------------------------------------------------------


class TestPositionMeaning(unittest.TestCase):
    """「低位」到底指什么，必须在报告里写清楚，不能让人自己猜。

    这个区块存在的理由：`低位` 两个字歧义极大 —— 既能读成"营收在低位"，
    也能读成"股价在低位"，也能读成"产能过剩、供给端在出清"。三者含义
    完全不同。工具只做第三种的判断，就必须把另两种明确排除掉。
    """

    def block(self, decision):
        return render.render_position_meaning(decision)

    def test_all_three_tiers_present_regardless_of_current(self):
        """三档都要摊开写 —— 只写当前那一档，用户就不知道"中位"是什么。"""
        for label in (decide.POSITION_LOW, decide.POSITION_NEUTRAL,
                      decide.POSITION_HIGH):
            with self.subTest(label=label):
                html = self.block({"position": {"label": label}, "flags": {}})
                for tier in ("低位", "中性", "高位"):
                    self.assertIn(f"<b>{tier}</b>", html)

    def test_only_current_tier_is_highlighted(self):
        html = self.block({"position": {"label": decide.POSITION_LOW},
                           "flags": {}})
        self.assertEqual(html.count("pm-active"), 1)
        self.assertEqual(html.count('class="pm-now"'), 1)
        self.assertIn('class="pm-low pm-active"', html)

    def test_low_highlight_moves_with_label(self):
        html = self.block({"position": {"label": decide.POSITION_HIGH},
                           "flags": {}})
        self.assertIn('class="pm-high pm-active"', html)
        self.assertNotIn('class="pm-low pm-active"', html)

    def test_low_explicitly_disclaims_revenue_and_demand(self):
        """低位 ≠ 营收低位 ≠ 需求回暖。这两句是整段最重要的。"""
        html = self.block({"position": {"label": decide.POSITION_LOW},
                           "flags": {}})
        self.assertIn("供给端可能已经出清", html)
        # "**不代表**" 会被 _emph 转成 <b>，所以夹在标签之间
        self.assertIn("<b>不代表</b>营收处在低位", html)
        self.assertIn("<b>不代表</b>需求正在回暖", html)

    def test_high_explicitly_disclaims_peak_earnings(self):
        """高位 ≠ 业绩见顶。紧张也可能因为需求旺。"""
        html = self.block({"position": {"label": decide.POSITION_HIGH},
                           "flags": {}})
        self.assertIn("<b>不代表</b>业绩见顶", html)
        self.assertIn("在建工程", html)

    def test_neutral_has_no_direction(self):
        html = self.block({"position": {"label": decide.POSITION_NEUTRAL},
                           "flags": {}})
        self.assertIn("中间", html)

    def test_lead_says_supply_side_not_revenue(self):
        html = self.block({"position": {"label": decide.POSITION_LOW},
                           "flags": {}})
        self.assertIn("看的是供给端", html)
        self.assertIn("产能周转率", html)

    def test_thresholds_match_config(self):
        """阈值写在正文里就必须跟 config 一致，不能写死成别的数。"""
        html = self.block({"position": {"label": decide.POSITION_LOW},
                           "flags": {}})
        self.assertIn(f"{helpers.config.POSITION_LOW_PCT:g}%", html)
        self.assertIn(f"{helpers.config.POSITION_HIGH_PCT:g}%", html)
        years = helpers.config.POSITION_WINDOW_QUARTERS // 4
        self.assertIn(f"近 {years} 年", html)

    def test_markdown_stars_never_leak(self):
        """文案里不能留 Markdown 的 `**`。

        这是实机截图抓到的 bug：原文写成 `**不代表**营收...`，
        渲染到 HTML 里星号原样显示，成了"**不代表**营收"。
        这是 HTML 不是 Markdown，强调要用标签。
        """
        for label in (decide.POSITION_LOW, decide.POSITION_NEUTRAL,
                      decide.POSITION_HIGH, decide.NA):
            with self.subTest(label=label):
                html = self.block({"position": {"label": label}, "flags": {}})
                self.assertNotIn("**", html)

    def test_financial_stock_shows_na_highlight(self):
        """金融企业不适用：三档照写，但"当前"落在不适用上。"""
        html = self.block({"position": {"label": decide.POSITION_LOW},
                           "flags": {"financial": True}})
        # 金融企业：三档照写，但没有任何一档被标成"当前"
        self.assertNotIn("pm-active", html)
        self.assertNotIn("pm-now", html)
        self.assertEqual(html.count("<b>低位</b>"), 1)

    def test_no_highlight_when_position_unknown(self):
        html = self.block({"position": {"label": decide.NA}, "flags": {}})
        self.assertNotIn("pm-active", html)
        self.assertNotIn('class="pm-now"', html)

    def test_survives_empty_decision(self):
        """decision 是空 dict 也不能抛异常（失败路径会走到）。"""
        for decision in ({}, {"position": {}}, {"position": None}):
            with self.subTest(decision=decision):
                self.assertIn("低位", self.block(decision))

    def test_appears_in_full_page(self):
        html = render_named("full_40q").html
        self.assertIn('class="posmean"', html)
        self.assertIn('class="pm-list"', html)


# ---------------------------------------------------------------------------
# 6c-2. 周期股两档含义 + 标准差算法（用户明确要求，与位置/弹性同规格）
# ---------------------------------------------------------------------------


class TestCycleMeaning(unittest.TestCase):
    """「是不是周期股」怎么算出来、算出来该怎么读，必须写在报告里。

    这一列比②③ 更容易被当成结论直接采信：索引页的徽章就叫「周期股」，
    扫一眼就走了。所以两件事必须在报告上钉住 ——
      * σ 是**样本**标准差、**单季**口径、缺失不补 0（可复核）
      * 「是」只过了三条判据中的一条，「否」也只否掉了毛利率这一项
    """

    def block(self, label, gm_std=None):
        return render.render_cycle_meaning(
            {"cycle": {"label": label, "gm_std": gm_std}})

    # ---- 版式 ----

    def test_both_tiers_present_regardless_of_current(self):
        """两档都要摊开写 —— 只写当前那档，用户不知道另一档意味着什么。"""
        for label in (decide.CYCLE_YES, decide.CYCLE_NO):
            with self.subTest(label=label):
                html = self.block(label)
                self.assertIn("<b>是</b>", html)
                self.assertIn("<b>否</b>", html)

    def test_only_current_tier_highlighted(self):
        html = self.block(decide.CYCLE_YES)
        self.assertEqual(html.count("pm-active"), 1)
        self.assertEqual(html.count('class="pm-now"'), 1)
        self.assertIn('class="pm-strong pm-active"', html)

    def test_highlight_moves_with_label(self):
        html = self.block(decide.CYCLE_NO)
        self.assertIn('class="pm-weak pm-active"', html)
        self.assertNotIn('class="pm-strong pm-active"', html)

    def test_no_highlight_when_unknown(self):
        """算不出来时高亮落在「算不出」那张卡上，而不是落在「是」或「否」上。

        为什么不是完全不标：那个破折号如果不解释，用户会以为数据丢了。
        标在「算不出」上，是告诉他「这不是缺失，是样本不够」。
        """
        html = self.block(decide.NA)
        self.assertNotIn('class="pm-strong pm-active"', html)
        self.assertNotIn('class="pm-weak pm-active"', html)
        self.assertIn('class="pm-na pm-active"', html)
        self.assertEqual(html.count('class="pm-now"'), 1)

    # ---- 算法必须写清楚 ----

    def test_lead_states_window_and_threshold_from_config(self):
        """窗口与阈值写在正文里，就必须跟 config 一致，不能写死。"""
        html = self.block(decide.CYCLE_YES)
        cfg = helpers.config
        self.assertIn(f"近 {cfg.WAVE_WINDOW_QUARTERS} 季", html)
        self.assertIn(f"{cfg.WAVE_WINDOW_QUARTERS // 4} 年", html)
        self.assertIn(f"{cfg.GM_STD_CYCLE:g}pp", html)

    def test_formula_is_explicit(self):
        """σ 的算法要能照着复核：公式、单季口径、剔缺失。"""
        html = self.block(decide.CYCLE_YES)
        self.assertIn("收入", html)
        self.assertIn("成本", html)
        self.assertIn("样本标准差", html)
        self.assertIn("n−1", html)
        self.assertIn("百分点", html)

    def test_formula_warns_against_cumulative_values(self):
        """必须说明为什么不能用累计口径 —— 这是最容易踩的坑。"""
        html = self.block(decide.CYCLE_YES)
        self.assertIn("单季", html)
        self.assertIn("累计", html)
        self.assertIn("抹平波动", html)

    def test_formula_warns_against_filling_zero(self):
        """缺失季不能补 0 —— 会凭空造出「毛利率归零」的极端值。"""
        html = self.block(decide.CYCLE_NO)
        self.assertIn("不补 0", html)
        self.assertIn("极端值", html)

    def test_formula_states_min_samples(self):
        html = self.block(decide.NA)
        self.assertIn(f"{helpers.config.GM_MIN_SAMPLES} 季", html)

    # ---- 含义边界 ----

    def test_yes_does_not_mean_confirmed_cycle(self):
        """「是」= 只过了三条判据之一，不等于确定是周期股。"""
        html = self.block(decide.CYCLE_YES)
        self.assertIn("<b>「是」不等于「确定是周期股」</b>", html)
        self.assertIn("第一条", html)

    def test_yes_flags_quarterly_calibration_direction(self):
        """季度口径对 5pp 线**偏松** —— 这个方向必须写明，否则会误判严格程度。"""
        html = self.block(decide.CYCLE_YES)
        self.assertIn("偏松", html)
        self.assertIn("季节性", html)

    def test_yes_explains_the_margin_gap_story(self):
        """毛利率 = 卖价与原料成本之间那道缝。"""
        html = self.block(decide.CYCLE_YES)
        self.assertIn("那道缝", html)
        self.assertIn("张开又收窄", html)

    def test_no_explicitly_does_not_mean_not_cycle(self):
        """「否」只否掉毛利率这一项，不能读成"不是周期股"。"""
        html = self.block(decide.CYCLE_NO)
        self.assertIn("<b>不代表</b>这家公司不是周期股", html)

    def test_no_lists_both_false_negative_paths(self):
        """两种把周期股判成「否」的常见情形都要写。"""
        html = self.block(decide.CYCLE_NO)
        self.assertIn("同步联动", html)
        self.assertIn("长协", html)

    def test_no_does_not_mean_bad_company(self):
        """平滑的毛利率是护城河，不是缺点。"""
        html = self.block(decide.CYCLE_NO)
        self.assertIn("护城河", html)
        self.assertIn("不代表公司差", html)

    def test_na_explains_why_no_conclusion(self):
        """样本不够时不给「否」，而是不给结论。"""
        html = self.block(decide.NA)
        self.assertIn("不足", html)
        self.assertIn("不给结论", html)

    # ---- 卫生 ----

    def test_markdown_stars_never_leak(self):
        for label in (decide.CYCLE_YES, decide.CYCLE_NO, decide.NA):
            with self.subTest(label=label):
                self.assertNotIn("**", self.block(label))

    def test_survives_empty_decision(self):
        for decision in ({}, {"cycle": {}}, {"cycle": None}):
            with self.subTest(decision=decision):
                self.assertIn("是", render.render_cycle_meaning(decision))

    def test_appears_in_full_page(self):
        """整页级别：三组说明块都要在，且板块顺序是 周期 → 弹性 → 位置。"""
        html = render_named("full_40q").html
        for placeholder in ("{{CYCLE_MEANING}}", "{{ELASTIC_MEANING}}",
                            "{{POSITION_MEANING}}"):
            self.assertNotIn(placeholder, html)
        # 顺序：周期股的说明先于弹性的，弹性的先于位置的
        i_cycle = html.find("看的是毛利率波动")
        i_elastic = html.find("「弹性」是经营杠杆")
        i_pos = html.find("「周期位置」看的是供给端")
        self.assertTrue(0 < i_cycle < i_elastic < i_pos,
                        f"顺序不对：{i_cycle=} {i_elastic=} {i_pos=}")

    def test_full_page_shows_formula_note(self):
        html = render_named("full_40q").html
        self.assertIn('class="pm-formula"', html)

    def test_financial_stock_still_shows_cycle_block(self):
        """金融企业：周期判断照样成立（不看产能），该区块不能被吞掉。"""
        html = render.render_cycle_meaning(
            {"cycle": {"label": decide.CYCLE_YES, "gm_std": 8.0}})
        self.assertIn('class="pm-strong pm-active"', html)


# ---------------------------------------------------------------------------
# 6d. 弹性三档含义（用户明确要求写进 HTML，与周期位置同规格）
# ---------------------------------------------------------------------------


class TestElasticMeaning(unittest.TestCase):
    """「弹性强/弱」到底指什么，必须写清楚。

    `弱` 是最容易被误读成"公司差"的词 —— 而弹性衡量的是放大倍数，不是好坏。
    茅台弹性弱，恰恰因为毛利率稳定（护城河的证据）。
    """

    def block(self, label, ratio=None):
        return render.render_elastic_meaning(
            {"elastic": {"label": label, "ratio": ratio}})

    def test_all_three_tiers_present(self):
        for label in (decide.ELASTIC_STRONG, decide.ELASTIC_WEAK,
                      decide.ELASTIC_UNSTABLE):
            with self.subTest(label=label):
                html = self.block(label)
                for tier in ("强", "弱", "不稳定"):
                    self.assertIn(f"<b>{tier}</b>", html)

    def test_only_current_tier_highlighted(self):
        html = self.block(decide.ELASTIC_STRONG)
        self.assertEqual(html.count("pm-active"), 1)
        self.assertEqual(html.count('class="pm-now"'), 1)
        self.assertIn('class="pm-strong pm-active"', html)

    def test_highlight_moves_with_label(self):
        html = self.block(decide.ELASTIC_WEAK)
        self.assertIn('class="pm-weak pm-active"', html)
        self.assertNotIn('class="pm-strong pm-active"', html)

    def test_strong_says_symmetric_not_free_money(self):
        """强 = 放大倍数大，且**涨跌对称**。不是只赚不赔。"""
        html = self.block(decide.ELASTIC_STRONG)
        self.assertIn("2 倍以上", html)
        self.assertIn("涨跌对称", html)

    def test_strong_warns_dol_blows_up_near_breakeven(self):
        """DOL 在盈亏平衡点附近天然爆表 —— 强不等于质地好。"""
        html = self.block(decide.ELASTIC_STRONG)
        self.assertIn("盈亏平衡", html)
        self.assertIn("质地", html)

    def test_weak_explicitly_does_not_mean_bad_company(self):
        """弱 ≠ 公司差。这是整段最重要的排除项 —— 正负两种情形都要排除。"""
        html = self.block(decide.ELASTIC_WEAK)
        self.assertIn("<b>两种情况都不代表</b>公司差", html)
        self.assertIn("护城河", html)

    def test_unstable_explains_why_untrustworthy(self):
        html = self.block(decide.ELASTIC_UNSTABLE)
        self.assertIn("3%", html)
        self.assertIn("噪声", html)

    def test_lead_says_leverage_not_quality(self):
        html = self.block(decide.ELASTIC_STRONG)
        self.assertIn("经营杠杆", html)
        self.assertIn("不衡量公司好坏", html)
        self.assertIn("营业利润增速 ÷ 营业收入增速", html)

    def test_formula_text_matches_implementation(self):
        """公式文案必须与代码一致（2026-11-13 修复过两处错文案）。

        弹性：代码用 **营业利润** 的环比增速（metrics.elasticity_ratio 取
        op_profit_q），文案曾写成「利润增速」——读者按「净利润」复核会对不上。
        周转率：代码用 **TTM 四季和**（build._fill_capacity_turnover），
        文案曾写成「单季营收×4」，两者分位差异中位 5pp、最大 40pp。
        """
        html = self.block(decide.ELASTIC_STRONG)
        self.assertIn("营业利润增速", html)
        self.assertNotIn("单季营收×4", html)
        self.assertNotIn("利润增速 ÷ 收入增速", html)

    def test_thresholds_match_config(self):
        html = self.block(decide.ELASTIC_STRONG)
        self.assertIn(f"{helpers.config.ELASTIC_RATIO_MIN:g}", html)
        self.assertIn(f"近 {helpers.config.ELASTIC_WINDOW_QUARTERS} 季", html)

    def test_markdown_stars_never_leak(self):
        for label in (decide.ELASTIC_STRONG, decide.ELASTIC_WEAK,
                      decide.ELASTIC_UNSTABLE, decide.NA):
            with self.subTest(label=label):
                self.assertNotIn("**", self.block(label))

    def test_no_highlight_when_na(self):
        html = self.block(decide.NA)
        self.assertNotIn("pm-active", html)
        self.assertNotIn("pm-now", html)

    def test_survives_empty_decision(self):
        for decision in ({}, {"elastic": {}}, {"elastic": None}):
            with self.subTest(decision=decision):
                self.assertIn("强", render.render_elastic_meaning(decision))

    def test_both_meaning_blocks_in_full_page(self):
        """三组含义区块共存，顺序对应判断①②③：周期 → 弹性 → 位置。"""
        html = render_named("full_40q").html
        self.assertEqual(html.count('class="posmean"'), 3)
        cycle_at = html.find("看的是毛利率波动")
        elastic_at = html.find("经营杠杆")
        position_at = html.find("看的是供给端")
        self.assertGreater(cycle_at, 0)
        self.assertGreater(elastic_at, cycle_at)
        self.assertGreater(position_at, elastic_at)


class TestElasticNegativeRatio(unittest.TestCase):
    """比值为负时，「弱」要讲成另一件事。

    实测 113 只自选股里，65 只 weak 有 **28 只比值为负**（43%）。
    负中位数不是"放大得少"，而是"过半季度营收与利润方向相反"。
    一句"成本随收入同比例变动"套上去就是错的。
    """

    def block(self, label, ratio):
        return render.render_elastic_meaning(
            {"elastic": {"label": label, "ratio": ratio}})

    def test_negative_ratio_uses_negative_wording(self):
        html = self.block(decide.ELASTIC_WEAK, -4.12)
        self.assertIn("方向相反", html)
        self.assertIn("没在放大", html)
        # 不该出现"放大效应有限"那句——那是正值分支的说法
        self.assertNotIn("放大效应有限", html)

    def test_positive_ratio_keeps_normal_wording(self):
        for ratio in (0.0, 0.8, 1.99):
            with self.subTest(ratio=ratio):
                html = self.block(decide.ELASTIC_WEAK, ratio)
                self.assertIn("放大效应有限", html)
                self.assertNotIn("这 8 季里过半季度营收与利润方向相反", html)

    def test_negative_highlights_weak_tier(self):
        """负值也是 weak，高亮必须落在「弱」上。"""
        html = self.block(decide.ELASTIC_WEAK, -4.12)
        self.assertIn('class="pm-weak pm-active"', html)

    def test_negative_warns_against_extrapolating(self):
        """负弹性不能拿来做"周期来了放大几倍"的推演 —— 会得到反的结论。"""
        html = self.block(decide.ELASTIC_WEAK, -4.12)
        self.assertIn("反的结论", html)

    def test_negative_does_not_use_positive_only_metaphor(self):
        """「赚了吆喝不赚钱」是正值弱的说法，负值分支不该照搬。

        负值确实也"不赚钱"，但原因是价差收紧而非固定成本低，
        混用会让人误以为是成本结构问题。
        """
        html = self.block(decide.ELASTIC_WEAK, -4.12)
        self.assertNotIn("赚了吆喝不赚钱", html)

    def test_no_ratio_falls_back_to_generic(self):
        """比值缺失时用通用说法，不能崩、也不能瞎猜正负。"""
        for ratio in (None, None):
            with self.subTest(ratio=ratio):
                html = self.block(decide.ELASTIC_WEAK, ratio)
                self.assertIn("弱", html)

    def test_strong_ignores_negative_branch(self):
        """strong 一定是正数（阈值 2.0 > 0），不该被负值分支影响。"""
        html = self.block(decide.ELASTIC_STRONG, 2.5)
        self.assertIn("2 倍以上", html)

    def test_negative_wording_has_no_markdown_stars(self):
        html = self.block(decide.ELASTIC_WEAK, -4.12)
        self.assertNotIn("**", html)

    def test_full_page_negative_stock(self):
        """整页级别：构造一只负弹性的股票，页面里要出现负值说明。"""
        stock = {
            "code": "600230", "name": "负弹性样本", "industry": "基础化工",
            "quarters": [
                {"date": "2025-06-30", "revenue_q": 1e9, "cost_q": 8e8,
                 "op_profit_q": 1e8, "fixed_asset": 2e9, "cip": 0.0,
                 "contract_liab": None, "gross_margin": 0.20,
                 "capacity_turnover": 2.0},
                {"date": "2025-09-30", "revenue_q": 1.1e9, "cost_q": 9.2e8,
                 "op_profit_q": 8e7, "fixed_asset": 2e9, "cip": 0.0,
                 "contract_liab": None, "gross_margin": 0.16,
                 "capacity_turnover": 2.2},
            ],
            "meta": {},
        }
        decision = {
            "cycle": {"label": decide.CYCLE_YES, "gm_std": 7.0},
            "elastic": {"label": decide.ELASTIC_WEAK, "ratio": -4.12},
            "position": {"label": decide.POSITION_HIGH, "pct": 76.2},
            "flags": {"financial": False, "insufficient_history": False},
            "meta": {"quarters": 2},
        }
        html = render.render(stock, decision, {}, {}, generated_at="X")
        self.assertIn("方向相反", html)
        self.assertIn("-4.12", html)


# ---------------------------------------------------------------------------
# 7. 转义与格式化
# ---------------------------------------------------------------------------


class TestEscapeAndFormat(unittest.TestCase):

    def test_ampersand_first(self):
        """顺序错了会把 &amp; 再转成 &amp;amp;。"""
        self.assertEqual(render.esc("a & b"), "a &amp; b")
        self.assertEqual(render.esc("<a>"), "&lt;a&gt;")
        self.assertEqual(render.esc('"q"'), "&quot;q&quot;")
        self.assertEqual(render.esc("it's"), "it&#39;s")

    def test_none_becomes_empty_string(self):
        self.assertEqual(render.esc(None), "")

    def test_double_escape_not_applied(self):
        self.assertEqual(render.esc("&lt;"), "&amp;lt;")

    def test_xss_style_name_is_neutralized(self):
        stock = {
            "code": "600000", "name": '<img src=x onerror="alert(1)">',
            "industry": "测试", "quarters": [], "meta": {},
        }
        decision = {
            "cycle": {"label": decide.NA, "gm_std": None},
            "elastic": {"label": decide.NA, "ratio": None},
            "position": {"label": decide.NA, "pct": None},
            "flags": {}, "meta": {"quarters": 0},
        }
        html = render.render(stock, decision, helpers.metrics_of(), {},
                             generated_at="X")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_nan_in_input_does_not_break_page(self):
        """上游若混进 nan / inf，页面必须显示"缺失"，而不是把 nan 打出来。

        这条曾经被写成"当前行为就是输出 nan"——那是把 bug 当规范。
        现在三个格式化函数和 attr_value 都要挡掉非有限数。
        """
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                self.assertEqual(render.fmt_num(bad), render.DASH)
                self.assertEqual(render.fmt_yi(bad), render.DASH)
                self.assertEqual(render.fmt_pp(bad), render.DASH)
                self.assertEqual(render.fmt_pct(bad), render.DASH)
                self.assertEqual(render.attr_value(bad), "")

    def test_nan_metric_renders_dash_html(self):
        """整页级别：把 gm_std 换成 nan，页面里不能出现 nan。"""
        case = render_named("full_40q")
        decision = {
            "cycle": {"label": decide.CYCLE_YES, "gm_std": float("nan")},
            "elastic": {"label": "strong", "ratio": float("nan")},
            "position": {"label": "low", "pct": float("nan")},
            "flags": {"financial": False, "insufficient_history": False},
            "meta": {"quarters": len(case.stock["quarters"])},
        }
        html = render.render(case.stock, decision, case.metrics, case.charts,
                             generated_at="X")
        self.assertIsNone(re.search(r"\b(nan|inf)\b", html))
        self.assertIn(render.DASH, html)

    def test_fmt_helpers_return_dash_for_none(self):
        for fn in (render.fmt_num, render.fmt_yi, render.fmt_pp, render.fmt_pct):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(fn(None), render.DASH)

    def test_fmt_yi_converts_to_yi(self):
        self.assertEqual(render.fmt_yi(1.5e8), "1.5 亿")
        self.assertEqual(render.fmt_yi(0.0), "0.0 亿")

    def test_fmt_pp_and_pct(self):
        # σ 默认 2 位小数：5pp 这条线上 1 位小数会把 4.95 显示成 5.0
        self.assertEqual(render.fmt_pp(6.137), "6.14 pp")
        self.assertEqual(render.fmt_pp(6.137, 1), "6.1 pp")
        self.assertEqual(render.fmt_pct(1.25), "1.2%")

    def test_fmt_pp_never_crosses_lower_threshold(self):
        """4.9505 判「非周期」，显示值不能是 5.00 —— 这是真实踩过的坑。"""
        text = render.fmt_pp(4.9505, lower=5.0)
        self.assertEqual(text, "4.95 pp")
        # 线下方任意贴近的值都不许冒到线上
        for value in (4.999, 4.9999, 4.995):
            shown = float(render.fmt_pp(value, lower=5.0).split()[0])
            self.assertLess(shown, 5.0, f"{value} 舍入后跨线了")

    def test_fmt_pp_never_crosses_upper_threshold(self):
        """线上方的值也不许被舍入到线下方（阈值相等即「是」，两个方向都要守）。"""
        for value in (5.001, 5.0049):
            shown = float(render.fmt_pp(value, lower=5.0).split()[0])
            self.assertGreaterEqual(shown, 5.0, f"{value} 舍入后跨线了")

    def test_fmt_pct_respects_both_position_bounds(self):
        """分位 39.99% 是低位 → 不许显示成 40.0%（40.0 本身属中性）。

        边界另一侧放宽：40.01% 显示成 40.0% 没问题，因为 40.0 也是「中性」，
        显示值与真实值落在同一档。真正的不变量是**档位不变**，不是数值不准。
        """
        self.assertLess(float(render.fmt_pct(39.99, lower=40.0).rstrip("%")), 40.0)
        self.assertGreater(float(render.fmt_pct(60.01, upper=60.0).rstrip("%")), 60.0)
        self.assertEqual(render.fmt_pct(40.01, lower=40.0), "40.0%")

    def test_display_never_flips_the_label(self):
        """跨档不变量：显示值重新套一遍阈值，得到的标签必须与真实值一致。

        这条比逐个 case 断言更结实 —— 以后精度、阈值怎么调都仍然成立。
        """
        cases = []
        # 判断①：5pp 边界两侧
        for v in (4.94, 4.949, 4.9505, 4.995, 4.999, 5.0, 5.001, 5.0049, 5.01, 0.0, 20.9):
            cases.append((render.fmt_pp(v, lower=5.0), v, lambda x: x >= 5.0))
        # 判断②：2.0 边界两侧（负值也测，-2.0 是边界）
        for v in (1.994, 1.999, 2.0, 2.001, 2.0049, -1.999, -2.0, -2.001, 17.31):
            cases.append((render.fmt_num(v, 2, lower=2.0), v,
                          lambda x: x >= 2.0))
        # 判断③：40 / 60 两条边界
        for v in (39.94, 39.96, 39.99, 40.0, 40.01, 59.99, 60.0, 60.01, 60.04):
            cases.append((render.fmt_pct(v, lower=40.0, upper=60.0), v,
                          lambda x: "low" if x < 40.0 else ("high" if x > 60.0 else "neutral")))
        for text, real, classify in cases:
            shown = float(text.split()[0].rstrip("%"))
            self.assertEqual(classify(shown), classify(real),
                             f"{real} 显示成 {text}，档位从 "
                             f"{classify(real)} 变成了 {classify(shown)}")

    def test_fmt_num_boundary_guard(self):
        self.assertEqual(render.fmt_num(1.999, 2, lower=2.0), "1.99")

    def test_fmt_num_digits(self):
        self.assertEqual(render.fmt_num(2.4999, 2), "2.50")
        self.assertEqual(render.fmt_num(0, 2), "0.00")

    def test_short_date(self):
        self.assertEqual(render._short_date("2026-09-30"), "26Q3")
        self.assertEqual(render._short_date("2026-03-31"), "26Q1")
        self.assertEqual(render._short_date("2025-12-31"), "25Q4")

    def test_short_date_degenerate_input(self):
        self.assertEqual(render._short_date(""), "")
        self.assertEqual(render._short_date(None), "")
        self.assertEqual(render._short_date("2026"), "2026")


# ---------------------------------------------------------------------------
# 8. 判断表里的说明文字（"为什么是 na"）
# ---------------------------------------------------------------------------


class TestJudgeNotes(unittest.TestCase):

    def judge_html(self, name):
        return judge_html(render_named(name).html)

    def test_na_cycle_explains_sample_count(self):
        body = self.judge_html("financial")
        self.assertIn(f"需 {helpers.config.GM_MIN_SAMPLES} 季毛利率", body)

    def test_na_elastic_explains_sample_count(self):
        body = self.judge_html("flat_margin_equal_turnover")
        self.assertIn(f"需 ≥ {helpers.config.RATIO_MIN_SAMPLES}", body)

    def test_financial_position_note(self):
        body = self.judge_html("financial")
        self.assertIn("金融企业", body)
        self.assertIn("产能周转率无意义", body)

    def test_threshold_numbers_come_from_config(self):
        """阈值改配置，页面得跟着变（不硬编码）。"""
        cfg = helpers.Cfg(GM_STD_CYCLE=7.5, ELASTIC_RATIO_MIN=3.5)
        case = render_named("full_40q", cfg)
        # render_judge_rows 用的是传入的 cfg，渲染时用的是模块级 config
        body = render.render_judge_rows(case.stock, case.decision,
                                        case.metrics, cfg)
        self.assertIn("7.5 pp", body)
        self.assertIn("3.5", body)

    def test_cv_channel_note_explains_why_yes(self):
        """走通道 B 时必须写清"σ 不到线但相对波动够" —— 否则读起来像算错了。"""
        d = {
            "cycle": {"label": decide.CYCLE_YES, "gm_std": 3.8,
                      "gm_cv": 23.5, "gm_median": 16.2, "channel": "cv"},
            "elastic": {"label": decide.NA, "ratio": None},
            "position": {"label": decide.NA, "pct": None},
            "flags": {"financial": False, "insufficient_history": False},
            "meta": {"quarters": 43},
        }
        stock = render_named("full_40q").stock
        body = render.render_judge_rows(stock, d, {}, helpers.config)
        first = body.split("</tr>")[0]
        self.assertIn("相对波动 24%", first)   # 23.5 → 24%
        self.assertIn(f"{helpers.config.GM_CV_MIN:g}%", first)
        self.assertIn("16.2%", first)
        self.assertIn("3.80 pp", first)

    def test_no_cycle_note_mentions_cv_when_below(self):
        """两条通道都没过时，把 CV 也报出来 —— 让用户看到"比值"而非只看到"否"。"""
        d = {
            "cycle": {"label": decide.CYCLE_NO, "gm_std": 2.58,
                      "gm_cv": 15.2, "gm_median": 16.9, "channel": None},
            "elastic": {"label": decide.NA, "ratio": None},
            "position": {"label": decide.NA, "pct": None},
            "flags": {"financial": False, "insufficient_history": False},
            "meta": {"quarters": 43},
        }
        stock = render_named("full_40q").stock
        body = render.render_judge_rows(stock, d, {}, helpers.config)
        first = body.split("</tr>")[0]
        self.assertIn("相对波动 15%", first)

    def test_position_note_shows_band(self):
        body = self.judge_html("flat_margin_equal_turnover")
        self.assertIn(f"{helpers.config.POSITION_LOW_PCT:g}%", body)
        self.assertIn(f"{helpers.config.POSITION_HIGH_PCT:g}%", body)
        self.assertIn("中间地带不给标签", body)

    def test_no_row_has_broken_cells(self):
        """每行恰好 4 个 <td>。"""
        for name in MATRIX:
            with self.subTest(fixture=name):
                body = self.judge_html(name)
                for row in re.findall(r"<tr>.*?</tr>", body):
                    self.assertEqual(row.count("<td"), 4, row)


# ---------------------------------------------------------------------------
# 9. 模板
# ---------------------------------------------------------------------------


class TestTemplate(unittest.TestCase):

    def setUp(self):
        render.clear_template_cache()

    def test_template_file_exists(self):
        text = render._load_template()
        self.assertIn("<!doctype html>", text.lower())
        self.assertIn('lang="zh-CN"', text)

    def test_all_placeholders_are_known(self):
        """模板里的占位符必须都在渲染器里有值，否则会漏出 {{XXX}}。"""
        known = {
            "{{TITLE}}", "{{ATTRS}}", "{{NAME}}", "{{CODE}}",
            "{{INDUSTRY_LINE}}", "{{HEADLINE}}", "{{BADGES}}",
            "{{JUDGE_ROWS}}", "{{CHARTS}}",
            "{{CYCLE_MEANING}}",
            "{{POSITION_MEANING}}", "{{ELASTIC_MEANING}}",
            "{{GM_WINDOW}}", "{{QUARTERS}}", "{{DATE_FROM}}",
            "{{DATE_TO}}", "{{GENERATED_AT}}", "{{GAPS_BLOCK}}",
        }
        found = set(re.findall(r"\{\{[A-Z_]+\}\}", render._load_template()))
        self.assertEqual(found, known)

    def test_template_cache_used(self):
        first = render._load_template()
        second = render._load_template()
        self.assertIs(first, second)

    def test_cache_clear_forces_reload(self):
        render._load_template()
        render.clear_template_cache()
        self.assertNotIn("report", render._TEMPLATE_CACHE)
        render._load_template()
        self.assertIn("report", render._TEMPLATE_CACHE)

    def test_responsive_and_print_css_present(self):
        text = render._load_template()
        self.assertIn("@media (max-width:640px)", text)
        self.assertIn("@media print", text)

    def test_empty_stock_still_renders(self):
        """0 季数据也不能抛错（新建 stock 时就会遇到）。"""
        stock = {"code": "600000", "name": "", "industry": "",
                 "quarters": [], "meta": {}}
        decision = {
            "cycle": {"label": decide.NA, "gm_std": None},
            "elastic": {"label": decide.NA, "ratio": None},
            "position": {"label": decide.NA, "pct": None},
            "flags": {"insufficient_history": True}, "meta": {"quarters": 0},
        }
        html = render.render(stock, decision, helpers.metrics_of(), {},
                             generated_at="X")
        self.assertIn("<body", html)
        self.assertIn('data-code="600000"', html)
        self.assertIn("历史不足", html)
        self.assertNotIn("None", html)

    def test_generated_at_default_is_timestamp(self):
        case = render_named("full_40q")
        html = render.render(case.stock, case.decision, case.metrics, case.charts)
        self.assertRegex(html, r"生成于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}")


# ---------------------------------------------------------------------------
# 10. 端到端：决定放什么，页面就显示什么（一致性）
# ---------------------------------------------------------------------------


class TestConsistency(unittest.TestCase):
    """渲染层不做任何判断 —— 它只是把 decide() 的结果摆出来。"""

    def test_rendered_result_column_matches_decision(self):
        expect = {
            decide.CYCLE_YES: "是", decide.CYCLE_NO: "否", decide.NA: render.DASH,
        }
        for name in MATRIX:
            with self.subTest(fixture=name):
                case = render_named(name)
                html = case.html.split("<tbody>")[1].split("</tbody>")[0]
                first_row = re.findall(r"<tr>.*?</tr>", html)[0]
                cells = re.findall(r"<td[^>]*>(.*?)</td>", first_row)
                self.assertEqual(cells[1], expect[case.decision["cycle"]["label"]])

    def test_elastic_text_matches(self):
        expect = {
            decide.ELASTIC_STRONG: "强",
            decide.ELASTIC_WEAK: "弱",
            decide.ELASTIC_UNSTABLE: "不稳定",
            decide.NA: render.DASH,
        }
        for name in MATRIX:
            with self.subTest(fixture=name):
                case = render_named(name)
                html = case.html.split("<tbody>")[1].split("</tbody>")[0]
                row = re.findall(r"<tr>.*?</tr>", html)[1]
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row)
                self.assertEqual(cells[1], expect[case.decision["elastic"]["label"]])

    def test_position_text_matches(self):
        expect = {
            decide.POSITION_LOW: "低位",
            decide.POSITION_NEUTRAL: "中性",
            decide.POSITION_HIGH: "高位",
            decide.NA: render.DASH,
        }
        for name in MATRIX:
            with self.subTest(fixture=name):
                case = render_named(name)
                html = case.html.split("<tbody>")[1].split("</tbody>")[0]
                row = re.findall(r"<tr>.*?</tr>", html)[2]
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row)
                self.assertEqual(cells[1], expect[case.decision["position"]["label"]])

    def test_headline_contains_three_numbers(self):
        html = render_named("full_40q").html
        headline = html.split('class="headline">')[1].split("</div>")[0]
        self.assertIn("毛利率σ", headline)
        self.assertIn("弹性", headline)
        self.assertIn("产能分位", headline)
        self.assertEqual(headline.count("<b>"), 3)

    def test_headline_handles_all_none(self):
        decision = {
            "cycle": {"label": decide.NA, "gm_std": None},
            "elastic": {"label": decide.NA, "ratio": None},
            "position": {"label": decide.NA, "pct": None},
            "flags": {}, "meta": {"quarters": 0},
        }
        text = render.build_headline(decision)
        self.assertNotIn("None", text)
        self.assertEqual(text.count(render.DASH), 3)

    def test_headline_warns_when_any_na(self):
        """任一项算不出来 → 页头要写「数据不完整，结论仅供参考」。

        否则用户只看到某个破折号，容易以为那一项不重要。
        次新股缺 10 年数据是这种情况的常客。
        """
        partial = {
            "cycle": {"label": decide.CYCLE_YES, "gm_std": 7.2},
            "elastic": {"label": decide.ELASTIC_WEAK, "ratio": -1.5},
            "position": {"label": decide.NA, "pct": None},
            "flags": {}, "meta": {"quarters": 40},
        }
        self.assertIn("数据不完整", render.build_headline(partial))

    def test_headline_no_warning_when_all_decided(self):
        full = {
            "cycle": {"label": decide.CYCLE_YES, "gm_std": 7.2},
            "elastic": {"label": decide.ELASTIC_WEAK, "ratio": -1.5},
            "position": {"label": decide.POSITION_LOW, "pct": 22.0},
            "flags": {}, "meta": {"quarters": 40},
        }
        self.assertNotIn("数据不完整", render.build_headline(full))

    def test_render_is_pure(self):
        """同样的输入渲两次，输出逐字相同（无隐藏随机/时间依赖）。"""
        case = render_named("full_40q")
        a = render.render(case.stock, case.decision, case.metrics,
                          case.charts, generated_at="FIXED")
        b = render.render(case.stock, case.decision, case.metrics,
                          case.charts, generated_at="FIXED")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
