# -*- coding: utf-8 -*-
"""渲染层测试：`screen/render.py` 与 `screen/summary.py`。

渲染层的特点决定了测试该盯什么：

1. **它只读结构，不重算数字** —— 所以测试不验证「Q 该是多少」，只验证
   「Q 是多少就渲染成多少」。算错了是 score 的测试该抓的。
2. **门槛必须来自 config** —— `_score_class` / `_val_band` / `_matrix_text`
   都从 config 取数，测试用「改 config 看输出跟着变」来证明没有写死。
   这一条最能防回流：以前矩阵在 score.py、说明文字在 render.py，改一处就
   「说一套、算一套」。
3. **降级路径同样重要** —— 真实数据里缺价格、缺估值序列、金融股不适用
   都是常态。渲染器在这些情况下要给出「— / 数据不足」而不是崩溃或印出 nan。
"""
from __future__ import annotations

import re
import unittest

from tests import helpers  # noqa: F401  （顺带把项目根塞进 sys.path）
import config
from screen import render, summary

# 渲染用的最小 data 在 helpers 里（CLI 测试也要用同一份）
render_data = helpers.render_data


# ---------------------------------------------------------------------------
# 渲染用的最小 data 见 `helpers.render_data()`（CLI 测试也用它，只维护一份）
# ---------------------------------------------------------------------------
render_data = helpers.render_data


class RenderCase(unittest.TestCase):
    """`_template_text()` 有 lru_cache；模板被替换时不至于串到别的用例。"""

    def setUp(self):
        cache_clear = getattr(render._template_text, "cache_clear", None)
        if cache_clear:
            cache_clear()
        self.addCleanup(lambda: getattr(render._template_text, "cache_clear", lambda: None)())


# ---------------------------------------------------------------------------
# 一、整体结构：报告能被 summary 解析回来
# ---------------------------------------------------------------------------
class TestRenderStructure(RenderCase):
    def test_returns_self_contained_html(self):
        """产出是一份完整 HTML：有 doctype、有 title、没有未替换的模板占位符。"""
        html = render.render_html(render_data())
        self.assertTrue(html.lstrip().lower().startswith("<!doctype html"), html[:60])
        self.assertIn("<title>", html.lower())
        leftover = re.findall(r"\{\{[A-Z]+\}\}", html)
        self.assertEqual(leftover, [], f"模板占位符没被替换：{leftover}")

    def test_no_external_resources(self):
        """报告是单文件：不引外链 CSS/JS/字体，否则离线打开就掉样式。"""
        html = render.render_html(render_data())
        self.assertNotIn("<script src=", html.lower())
        self.assertNotRegex(html.lower(), r'<link[^>]+href="https?://')
        self.assertNotRegex(html, r"@import\s+url\(")

    def test_title_contains_name_and_code(self):
        html = render.render_html(render_data())
        self.assertRegex(html, r"<title>[^<]*测试股份[^<]*600000")

    def test_summary_roundtrip(self):
        """渲染 → 解析闭环：批量模式就从这份 HTML 里读结论，两端口径必须一致。"""
        data = render_data()
        s = summary.extract_score_summary(render.render_html(data))
        self.assertEqual(s.quality, "72.3")
        self.assertEqual(s.valuation, "58.0")
        self.assertEqual(s.conclusion, data["decision"]["label"])

    def test_data_metric_anchors_present(self):
        """`data-metric` 是 summary 的稳定锚点，缺一个批量模式就全挂。"""
        html = render.render_html(render_data())
        for metric in ("q", "v", "conclusion"):
            with self.subTest(metric=metric):
                self.assertRegex(
                    html,
                    rf'<div class="v"[^>]*data-metric="{metric}"[^>]*>',
                    f"缺少 data-metric={metric} 锚点",
                )

    def test_all_five_sections_rendered(self):
        """报告固定五节，缺节说明某个 `_section_*` 提前返回了。"""
        html = render.render_html(render_data())
        for heading in ("一、", "二、", "三、", "四、", "五、"):
            with self.subTest(heading=heading):
                self.assertIn(heading, html)

    def test_disclaimer_carries_version(self):
        """免责声明里的版本号取自 config，不是手抄的。"""
        html = render.render_html(render_data())
        self.assertIn(config.VERSION, html)


# ---------------------------------------------------------------------------
# 二、分数条配色：门槛必须来自 config
# ---------------------------------------------------------------------------
class TestScoreClass(unittest.TestCase):
    def test_thresholds_come_from_config(self):
        good, bad = config.SCORE_BAR_GOOD, config.SCORE_BAR_BAD
        self.assertEqual(render._score_class(good), "good")
        self.assertEqual(render._score_class(good - 0.1), "")
        self.assertEqual(render._score_class(bad - 0.1), "bad")
        self.assertEqual(render._score_class(bad), "")

    def test_none_is_unstyled(self):
        """算不出来就不上色：空白格比一段绿条诚实。"""
        self.assertEqual(render._score_class(None), "")

    def test_follows_config_change(self):
        """把门槛改掉，配色跟着变 —— 证明不是写死的 65/45。"""
        original = config.SCORE_BAR_GOOD
        try:
            config.SCORE_BAR_GOOD = 50.0
            self.assertEqual(render._score_class(55.0), "good")
        finally:
            config.SCORE_BAR_GOOD = original

    def test_bar_clamps_to_0_100(self):
        self.assertIn("width:100.0%", render._bar(150))
        self.assertIn("width:0.0%", render._bar(-20))
        self.assertIn("width:0.0%", render._bar(None))


# ---------------------------------------------------------------------------
# 三、估值分档：七档首尾相接，边界含下端不含上端
# ---------------------------------------------------------------------------
class TestValBand(unittest.TestCase):
    def test_boundaries_are_half_open(self):
        """`[low, high)`：10.0 归「下三分之一」，不归「历史底部」。"""
        self.assertEqual(render._val_band(0.0)[0], "历史底部")
        self.assertEqual(render._val_band(9.99)[0], "历史底部")
        self.assertEqual(render._val_band(10.0)[0], "下三分之一")
        self.assertEqual(render._val_band(100.0)[0], "历史顶部")

    def test_every_band_is_reachable(self):
        reached = set()
        for band in config.VAL_BANDS:
            reached.add(render._val_band(band[0])[0])
        self.assertEqual(reached, {b[2] for b in config.VAL_BANDS})

    def test_out_of_range_clamps(self):
        """越界夹到端点，不抛异常也不返回空档名。"""
        self.assertEqual(render._val_band(-5.0)[0], config.VAL_BANDS[0][2])
        self.assertEqual(render._val_band(999.0)[0], config.VAL_BANDS[-1][2])

    def test_verdict_is_paired_with_band(self):
        """档名和结论词必须同属一条配置，不能错位配对。"""
        for low, _high, name, verdict in config.VAL_BANDS:
            with self.subTest(name=name):
                got_name, got_verdict = render._val_band(low)
                self.assertEqual((got_name, got_verdict), (name, verdict))


class TestValItems(unittest.TestCase):
    """`_val_items()`：PE/PB/PS 分位 → 展示条目。"""

    def metrics(self, **kw):
        return {k: {"percentile": v} for k, v in kw.items()}

    def test_order_is_pe_pb_ps(self):
        items = render._val_items(self.metrics(ps=50, pb=20, pe=10))
        self.assertEqual([i["name"] for i in items], ["PE", "PB", "PS"])

    def test_missing_percentile_is_skipped(self):
        items = render._val_items({"pe": {"percentile": None}, "pb": {"percentile": 30}})
        self.assertEqual([i["name"] for i in items], ["PB"])

    def test_out_of_range_is_skipped_not_clamped(self):
        """-5% 不能读成「历史底部」——夹取比跳过更误导。"""
        items = render._val_items(self.metrics(pe=-5, pb=150, ps=50))
        self.assertEqual([i["name"] for i in items], ["PS"])

    def test_nan_string_is_skipped(self):
        self.assertEqual(render._val_items({"pe": {"percentile": "nan"}}), [])

    def test_garbage_is_skipped(self):
        self.assertEqual(render._val_items({"pe": {"percentile": "未披露"}}), [])

    def test_empty_metrics(self):
        self.assertEqual(render._val_items({}), [])
        self.assertEqual(render._val_items(None or {}), [])


class TestSampleShort(unittest.TestCase):
    def test_short_sample_detected(self):
        self.assertTrue(render._sample_short({"points": 10}))
        self.assertFalse(render._sample_short({"points": config.VAL_MIN_SAMPLE_WEEKS}))
        self.assertFalse(render._sample_short({"points": 200}))

    def test_zero_points_is_not_short(self):
        """0 表示「没算过样本」，不是「样本很少」：两者的处理不同。"""
        self.assertFalse(render._sample_short({"points": 0}))
        self.assertFalse(render._sample_short(None))

    def test_garbage_points(self):
        self.assertEqual(render._sample_points({"points": "abc"}), 0)
        self.assertEqual(render._sample_points({"points": None}), 0)


# ---------------------------------------------------------------------------
# 四、说明文字由 config 生成（防「说一套、算一套」）
# ---------------------------------------------------------------------------
class TestMatrixText(unittest.TestCase):
    """报告的「决策规则」说明读 `config.DECISION_MATRIX`，与 `_decision()` 同一张表。"""

    def test_every_tier_appears(self):
        text = render._matrix_text()
        for tier in config.DECISION_MATRIX:
            with self.subTest(q_min=tier["q_min"]):
                self.assertIn(f"{tier['q_min']:g}", text)

    def test_every_verdict_word_appears(self):
        text = render._matrix_text()
        for tier in config.DECISION_MATRIX:
            for rule in tier["rules"]:
                with self.subTest(text=rule["text"]):
                    self.assertIn(rule["text"], text)

    def test_mentions_hard_floor(self):
        self.assertIn(f"{config.QUALITY_HARD_FLOOR:g}", render._matrix_text())

    def test_ranges_are_derived_from_adjacent_vmin(self):
        """V 区间由相邻 `v_min` 相减得来，手抄的数字对不上就会被抓出来。"""
        text = render._matrix_text()
        for tier in config.DECISION_MATRIX:
            rules = [r for r in tier["rules"] if r["v_min"] is not None]
            for upper, lower in zip(rules, rules[1:]):
                with self.subTest(lower=lower["v_min"], upper=upper["v_min"]):
                    self.assertIn(f"{lower['v_min']:g}~{upper['v_min']:g}", text)

    def test_follows_config_change(self):
        """改矩阵 → 说明文字跟着变。这条抓的是「有没有偷偷硬编码」。"""
        original = config.DECISION_MATRIX
        try:
            config.DECISION_MATRIX = ({
                "q_min": 88.0,
                "rules": ({"v_min": 66.0, "key": "buy", "label": "买入",
                           "reason": "x", "text": "独特措辞ZZZ"},),
            },)
            text = render._matrix_text()
            self.assertIn("88", text)
            self.assertIn("66", text)
            self.assertIn("独特措辞ZZZ", text)
        finally:
            config.DECISION_MATRIX = original


class TestGradeAndWeightText(unittest.TestCase):
    def test_grade_text_covers_all_grades(self):
        text = render._grade_text()
        for _cut, label in config.QUALITY_GRADES:
            with self.subTest(label=label):
                self.assertIn(label, text)

    def test_weight_text_sums_to_100(self):
        """权重说明里印出来的数字，加起来必须是 100。"""
        text = render._weight_text(config.QUALITY_WEIGHTS, config.QUALITY_GROUP_LABELS)
        nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)", text)]
        self.assertAlmostEqual(sum(nums), 100.0, places=6, msg=text)

    def test_v_formula_weights_sum_to_100(self):
        text = render._v_formula_text()
        nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)%", text)]
        self.assertAlmostEqual(sum(nums), 100.0, places=6, msg=text)


# ---------------------------------------------------------------------------
# 五、降级路径：缺数据的报告不能崩，也不能印 nan
# ---------------------------------------------------------------------------
class TestDegradedRendering(RenderCase):
    def test_missing_price_says_so(self):
        data = render_data()
        data["meta"] = dict(data["meta"], price=None, price_date=None)
        html = render.render_html(data)
        self.assertIn("价格未取得", html)
        self.assertIn("无估值序列", html)

    def test_no_replacement_placeholder_leaks(self):
        """各种字段缺失时都不该出现 `None` / `nan` / `inf` 这类 Python 字面量。

        用词边界而不是子串：`"financials"` 里含 `nan`，子串匹配会假阳性。
        """
        data = render_data()
        data["meta"] = {"code": "600000", "name": "空壳"}
        data["valuation"] = {}
        data["tracking"] = {}
        html = render.render_html(data)
        self.assertNotIn("None", html)
        for bad in (r"\bnan\b", r"\binf\b", r"\bNaN\b", r"\bInfinity\b"):
            with self.subTest(bad=bad):
                self.assertNotRegex(html, bad)

    def test_html_is_escaped(self):
        """公司名来自上游，带尖括号时必须转义，否则报告结构会被打散。"""
        html = render.render_html(render_data(
            meta={"code": "1", "name": "<script>x</script>", "industry": "a&b"}))
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_financial_stock_structure_text(self):
        """金融股的货款占用是「不适用」，不是「数据不足」——两者含义不同。"""
        line = render._structure_line({}, financial=True)
        self.assertIn("不适用", line)
        self.assertNotIn("数据不足", line)

    def test_missing_structure_is_insufficient(self):
        self.assertIn("数据不足", render._structure_line({}))

    def test_bargain_verdict_boundaries(self):
        strong, weak = config.PAYABLE_SLIP_STRONG, config.PAYABLE_SLIP_WEAK
        self.assertEqual(render._bargain_verdict(strong, None), "两头通吃")
        self.assertEqual(render._bargain_verdict(weak, None), "正常占款")
        self.assertEqual(render._bargain_verdict(weak - 0.01, config.CL_RATIO_STRONG),
                         "预收型强势")
        self.assertEqual(render._bargain_verdict(0.1, 0.1), "几乎不占供应商款")
        self.assertEqual(render._bargain_verdict(None, None), "")


class TestLineChart(unittest.TestCase):
    """SVG 折线图：无 JS、无外部资源、缺失值断线。

    series 每项是 `{"key", "label", "color"[, "dash"]}`：
    - `key` 是取值字段名，`color` 画线，`label` 是图例文字（不是 `name`）。
    - `color` / `label` 都是必填的，渲染器直接下标取用；配色由调用方按语义决定，
      不在渲染器里写死。
    """

    SERIES = [{"key": "a", "label": "营收", "color": "#1f5f8b"}]

    def test_empty_points(self):
        self.assertIn("暂无图表数据", render._line_chart([], self.SERIES))

    def test_all_values_missing(self):
        pts = [{"x": "2023", "a": None}, {"x": "2024", "a": None}]
        self.assertIn("暂无可绘制", render._line_chart(pts, self.SERIES))

    def test_renders_svg_path(self):
        pts = [{"x": "2023", "a": 1.0}, {"x": "2024", "a": 2.0}]
        svg = render._line_chart(pts, self.SERIES)
        self.assertIn("<svg", svg)
        self.assertIn("<path", svg)
        self.assertIn(self.SERIES[0]["color"], svg)
        self.assertNotIn("<script", svg)

    def test_flat_series_does_not_crash(self):
        """所有值相同 → span=0，必须补一个假区间，不能除零。"""
        pts = [{"x": "2023", "a": 5.0}, {"x": "2024", "a": 5.0}]
        self.assertIn("<svg", render._line_chart(pts, self.SERIES))

    def test_missing_value_breaks_the_line(self):
        """缺失点必须断线（重新 M），不能直连造成「数据连续」的错觉。"""
        pts = [{"x": "2022", "a": 1.0}, {"x": "2023", "a": None},
               {"x": "2024", "a": 3.0}]
        svg = render._line_chart(pts, self.SERIES)
        d = re.search(r'<path d="([^"]+)"', svg).group(1)
        self.assertEqual(d.count("M"), 2, f"缺失点没有断线：{d}")

    def test_single_point(self):
        """只有 1 个点时 `last` 为 0，x() 不能除零。"""
        self.assertIn("<svg", render._line_chart([{"x": "2024", "a": 5.0}], self.SERIES))


# ---------------------------------------------------------------------------
# 六、summary 解析：标记位优先，老报告回退
# ---------------------------------------------------------------------------
# 这块单独测，因为它是**批量模式的唯一数据源**：批量不重算分数，只从已落盘的
# HTML 里读结论。解析一坏，整批导航页全变「评分提取失败」，而且不会报错。
#
# 回退那条路（`_legacy_metrics`）是给 0.9.7 及更早的报告留的：批量会复用已有
# 报告，改字段名不该让历史报告全部失效。
class TestSummaryMarkers(unittest.TestCase):
    def test_prefers_markers_over_headings(self):
        """两套锚点同时存在时以标记位为准：标题文字可能被改，属性不会。"""
        html = (
            '<h2>四、综合评分</h2>'
            '<div class="k">质量分 Q</div><div class="v">11.1</div>'
            '<div class="k">估值分 V</div><div class="v">22.2</div>'
            '<div class="k">结论</div><div class="v">旧结论</div>'
            '<div class="v" data-metric="q">72.3</div>'
            '<div class="v" data-metric="v">58.0</div>'
            '<div class="v" data-metric="conclusion">新结论</div>'
        )
        s = summary.extract_score_summary(html)
        self.assertEqual((s.quality, s.valuation, s.conclusion),
                         ("72.3", "58.0", "新结论"))

    def test_markers_do_not_need_the_section_heading(self):
        """标记位路线不依赖「四、综合评分」标题——结构改了也能解析。"""
        html = ('<div class="v" data-metric="q">80.0</div>'
                '<div class="v" data-metric="v">60.0</div>'
                '<div class="v" data-metric="conclusion">可买</div>')
        s = summary.extract_score_summary(html)
        self.assertEqual(s.conclusion, "可买")

    def test_markers_tolerate_extra_attributes(self):
        """真实报告里 `data-metric` 上还有 `style`、`class` 等，顺序也不固定。"""
        html = ('<div class="v" style="font-size:19px" data-metric="conclusion" '
                'id="x">推荐买入</div>')
        self.assertEqual(summary.extract_score_summary(
            html + '<div class="v" data-metric="q">1</div>'
                   '<div class="v" data-metric="v">2</div>').conclusion,
            "推荐买入")

    def test_entities_are_unescaped(self):
        html = '<div class="v" data-metric="conclusion">回避 &amp; 观察</div>'
        s = summary.extract_score_summary(
            html + '<div class="v" data-metric="q">1</div>'
                   '<div class="v" data-metric="v">2</div>')
        self.assertEqual(s.conclusion, "回避 & 观察")

    def test_inner_tags_are_stripped(self):
        """值里带 <b> 之类的内联标签时，只留文本。"""
        html = ('<div class="v" data-metric="q"><b>72.3</b></div>'
                '<div class="v" data-metric="v">58.0</div>'
                '<div class="v" data-metric="conclusion">观察</div>')
        self.assertEqual(summary.extract_score_summary(html).quality, "72.3")


class TestSummaryLegacy(unittest.TestCase):
    """0.9.7 及更早的报告没有标记位，得靠标题文字回退。"""

    LEGACY = """
    <section>
      <h2>四、综合评分</h2>
      <div class="grid">
        <div class="metric"><div class="k">质量分 Q</div><div class="v">62.7</div>
          <div class="note">良好</div></div>
        <div class="metric"><div class="k">估值分 V</div><div class="v">41.0</div></div>
        <div class="metric"><div class="k">结论</div><div class="v">观察</div></div>
      </div>
    </section>
    """

    def test_reads_legacy_layout(self):
        s = summary.extract_score_summary(self.LEGACY)
        self.assertEqual((s.quality, s.valuation, s.conclusion),
                         ("62.7", "41.0", "观察"))

    def test_missing_section_raises(self):
        with self.assertRaises(summary.SummaryError) as cm:
            summary.extract_score_summary("<html><body>无评分区</body></html>")
        self.assertIn("四、综合评分", str(cm.exception))

    def test_missing_quality_raises(self):
        html = ('<section><h2>四、综合评分</h2>'
                '<div class="k">估值分 V</div><div class="v">41.0</div>'
                '<div class="k">结论</div><div class="v">观察</div></section>')
        with self.assertRaises(summary.SummaryError) as cm:
            summary.extract_score_summary(html)
        self.assertIn("质量分 Q", str(cm.exception))

    def test_missing_valuation_raises(self):
        html = ('<section><h2>四、综合评分</h2>'
                '<div class="k">质量分 Q</div><div class="v">62.7</div>'
                '<div class="k">结论</div><div class="v">观察</div></section>')
        with self.assertRaises(summary.SummaryError) as cm:
            summary.extract_score_summary(html)
        self.assertIn("估值分 V", str(cm.exception))

    def test_missing_conclusion_raises(self):
        html = ('<section><h2>四、综合评分</h2>'
                '<div class="k">质量分 Q</div><div class="v">62.7</div>'
                '<div class="k">估值分 V</div><div class="v">41.0</div></section>')
        with self.assertRaises(summary.SummaryError) as cm:
            summary.extract_score_summary(html)
        self.assertIn("结论", str(cm.exception))

    def test_empty_input_raises(self):
        with self.assertRaises(summary.SummaryError):
            summary.extract_score_summary("")

    def test_partial_markers_fail_loudly(self):
        """命中标记位就**不再回退**，缺的那项直接报错。

        `values = marked if marked else _legacy_metrics(text)`：只要认到任意一个
        标记，就整份按新格式读。剩下的项在标记里找不到 → SummaryError。
        这比「一半新一半旧」混着读安全：混读会给出半对半错的结论，
        而报错会让人去看那份报告到底出了什么问题。
        """
        html = self.LEGACY + '<div class="v" data-metric="q">99.9</div>'
        with self.assertRaises(summary.SummaryError) as cm:
            summary.extract_score_summary(html)
        self.assertIn("估值分 V", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
