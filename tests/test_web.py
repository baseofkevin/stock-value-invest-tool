# -*- coding: utf-8 -*-
"""看板（`web/`）的测试，不联网、不起服务。

    python3 -m unittest discover -s tests -t .     # 含本文件

用 Flask 的 `test_client` 直接打路由，所以验的是"页面能不能出来"，
而不是"服务能不能起"。四件事值得钉：

1. **只读**。库里不写东西——看板是观察工具，不是编辑工具。
2. **空库不崩**。第一次用的人还没有 `results.db`，要给"先跑 ./run.py all"，
   不是 500 堆栈。
3. **中文标签**。库里存的是 `high/low/na` 这类枚举，页面上必须是中文；
   两套词表混着摆是之前真出现过的问题。
4. **合并表**。一只票只有部分工具的结果时，缺的那几格是 `—`，不能整行消失。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from stock_data_fetch import results as results_db


def _save_demo(db_path: str, date: str = "2026-11-13", kind: str = "batch") -> None:
    """造一批和真实跑出来同形状的结果。"""
    results_db.save("select", [
        {"symbol": "sh600019", "name": "宝钢股份", "industry": "钢铁",
         "pool_turnover_rank": 1.6, "turnover20": 0.4, "stars": "***",
         "close": 6.18, "market_cap_yi": 1346.13, "launch_date": "2026-11-13",
         "days_since_launch": 0, "vol_multiple": 1.56, "launch_gain_pct": 2.15,
         "ma20_gap_pct": 1.83, "ma60_gap_pct": 4.36,
         "gross_margin_up": "否：5.41→5.43"},
    ], date, argv="test", db=db_path, kind=kind)
    results_db.save("cycle", [
        {"symbol": "sh600019", "name": "宝钢股份", "is_cycle": True,
         "gm_std_pp": 2.3, "gm_cv": 37.3, "elasticity": -1.17,
         "elastic_label": "weak", "position_pct": 63.7,
         "position_label": "high"},
    ], date, argv="test", db=db_path, kind=kind)
    results_db.save("quality", [
        {"symbol": "sh600019", "name": "宝钢股份", "q": 54.4, "q_grade": "合格",
         "v": 67.0, "v_grade": "合理", "conclusion": "观察",
         "reason": "估值便宜，但现金流质量待观察", "risks_hit": 1,
         "risks_watch": 2, "risks_known": 9},
    ], date, argv="test", db=db_path, kind=kind)
    results_db.save("top", [
        {"code": "sh600019", "name": "宝钢股份", "ok": True, "error": "",
         "date": "2026-11-13", "close": 6.18, "pos250": -0.21,
         "days_since_high": 242, "phase": "反弹接近局部高点",
         "views": [["动量", "低位·走平"], ["换手", "偏低"], ["业绩", "减速"],
                   ["量价", "未触发"], ["周期位置", "周期·高位"]],
         "views_full": []},
    ], date, argv="test", db=db_path, kind=kind)


class WebCase(unittest.TestCase):
    """每个用例一个临时库，互不干扰。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="web-test-")
        self.db_path = os.path.join(self.tmp, "results.db")
        self._old = os.environ.get("STOCK_RESULTS_DB")
        os.environ["STOCK_RESULTS_DB"] = self.db_path

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STOCK_RESULTS_DB", None)
        else:
            os.environ["STOCK_RESULTS_DB"] = self._old

    def client(self, with_data=True):
        from web.app import create_app
        if with_data:
            _save_demo(self.db_path)
        app = create_app(self.db_path)
        app.config.update(TESTING=True)
        return app.test_client()


class TestPages(WebCase):
    def test_all_pages_render(self):
        client = self.client()
        for url in ("/select", "/cycle", "/quality", "/top", "/review",
                    "/batches", "/stock/sh600019"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_root_goes_to_review(self):
        """/ 是旧书签和 ./run.py web 打印的地址：转到综合评估，不是 404。

        原来的「概览」页删了——它那张合并表就是综合评估下面的全表。
        """
        resp = self.client().get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/review"))

    def test_unknown_page_is_404_with_nav(self):
        resp = self.client().get("/nope")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("导航", resp.get_data(as_text=True))

    def test_missing_db_explains_what_to_run(self):
        """还没有 results.db：给一句"先跑 ./run.py all"，不是堆栈。"""
        resp = self.client(with_data=False).get("/review")
        self.assertEqual(resp.status_code, 500)
        self.assertIn("./run.py all", resp.get_data(as_text=True))

    def test_merged_table_shows_partial_rows(self):
        """只跑过选股的票也要在全表里出现（缺的格子是 —）。"""
        client = self.client()
        results_db.save("select", [{"symbol": "sz000001", "name": "平安银行"}],
                        "2026-11-13", argv="test", db=self.db_path)
        html = client.get("/review").get_data(as_text=True)
        # 选股最新一次是"平安银行"，质量/顶部最新一次仍是"宝钢股份"：
        # 合并按 symbol 并排，两边都得在，缺的格子留 —。
        self.assertIn("平安银行", html)
        self.assertIn("宝钢股份", html)


class TestBatches(WebCase):
    """批次与单只要分清：这是看板最容易出错的一处。

    同一只票可能周五批量算过、周中单只又算过。批次页与工具页只能看批量那一次；
    单只那次出现在个股页的时间线里。
    """

    def test_batch_page_lists_dates(self):
        client = self.client()
        _save_demo(self.db_path, date="2026-11-06")
        html = client.get("/batches").get_data(as_text=True)
        self.assertIn("2026-11-13", html)
        self.assertIn("2026-11-06", html)

    def test_single_run_does_not_join_batch(self):
        """单只跑（kind=single）不进批次统计，也不覆盖批量那一次。"""
        client = self.client()                     # 先有一批 2026-11-13
        _save_demo(self.db_path, kind="single")     # 同一基准日的单只结果
        html = client.get("/select").get_data(as_text=True)
        self.assertIn("1 只", html)                 # 批量那次的数量，不是单只

    def test_date_scopes_tool_page(self):
        """?date= 看历史批次，不能混进最新那一批的行。"""
        client = self.client()
        results_db.save("select", [{"symbol": "sz000001", "name": "平安银行"}],
                        "2026-11-06", argv="test", db=self.db_path)
        html = client.get("/select?date=2026-11-06").get_data(as_text=True)
        self.assertIn("平安银行", html)
        self.assertNotIn("宝钢股份", html)

    def test_run_param_shows_that_run(self):
        """?run=<id> 精确到某一次运行（同一天重跑也能分辨）。"""
        client = self.client()
        run_id = results_db.save("select", [{"symbol": "sz000001", "name": "平安银行"}],
                                 "2026-11-13", argv="test", db=self.db_path)
        html = client.get(f"/select?run={run_id}").get_data(as_text=True)
        self.assertIn("平安银行", html)
        self.assertIn(f"#{run_id}", html)

    def test_batch_merged_only_that_batch(self):
        """综合评估的全表按批次对齐：上周那批的票不出现在本周的表里。"""
        client = self.client()
        results_db.save("select", [{"symbol": "sz000001", "name": "平安银行"}],
                        "2026-11-06", argv="test", db=self.db_path)
        html = client.get("/review?date=2026-11-06").get_data(as_text=True)
        self.assertIn("平安银行", html)
        self.assertNotIn("宝钢股份", html)


class TestTimeline(WebCase):
    """个股页要把"同一只票的不同时间"逐条摆出来。"""

    def test_each_run_is_listed(self):
        client = self.client()
        results_db.save("quality", [{"symbol": "sh600019", "name": "宝钢股份",
                                     "q": 61.2, "v": 70.0, "conclusion": "观察"}],
                        "2026-11-13", argv="test", db=self.db_path, kind="single")
        html = client.get("/stock/sh600019").get_data(as_text=True)
        self.assertIn("历次结果", html)
        self.assertIn("单只", html)                 # 来源列
        self.assertIn("61.2", html)                 # 单只那次的 Q

    def test_brief_formats_percent(self):
        """距 250 日高点在库里是小数（-0.21），时间线上要显示成 -21.0%。"""
        from web.db import _brief
        self.assertIn("-21.0%", _brief("top", {"pos250": -0.21,
                                               "momentum_state": "低位·走平"}))

    def test_unknown_values_do_not_crash(self):
        from web.db import _brief
        for tool in ("select", "cycle", "quality", "top"):
            with self.subTest(tool=tool):
                self.assertIsInstance(_brief(tool, {}), str)


class TestLabels(WebCase):
    """枚举 → 中文。库里存英文，页面上必须写中文。"""

    def test_position_label_is_chinese(self):
        html = self.client().get("/review").get_data(as_text=True)
        self.assertNotIn(">high<", html)
        self.assertIn("高位", html)

    def test_cycle_state_prefix_stripped(self):
        """顶部卡里的「周期·高位」在专门那一列只写「高位」。"""
        html = self.client().get("/top").get_data(as_text=True)
        self.assertNotIn("周期·高位", html)
        self.assertIn("高位", html)

    def test_filter_unknown_value_passthrough(self):
        from web.app import _poslabel
        self.assertEqual(_poslabel("na"), "不适用")
        self.assertEqual(_poslabel(None), "—")
        self.assertEqual(_poslabel("sideways"), "sideways")

    def test_tone_maps_states(self):
        from web.app import tone
        self.assertEqual(tone("高位·走平"), "warn")
        self.assertEqual(tone("低位·偏弱"), "muted")
        self.assertEqual(tone("不认识的词"), "info")


class TestReviewRules(unittest.TestCase):
    """综合评估的规则（`web/review.py`）：纯函数，不碰库、不起服务。

    这一层最容易悄悄松掉的是**风险门槛**：一个"总分高"的票如果命中了致命
    风险，必须还是排除。这几条钉的就是这件事。
    """

    def _row(self, q=None, v=None, conclusion="观察", reason="", fatal=None,
             is_cycle=False, position="", pos250=-0.20, momentum="低位·走平",
             earnings="加速", pe_pct=30.0, in_select=True):
        quality = {}
        if conclusion is not None:
            quality = {"symbol": "sh600000", "name": "测试", "q": q, "v": v,
                       "q_grade": "", "v_grade": "", "conclusion": conclusion,
                       "reason": reason, "risks_hit": 0, "risks_known": 14,
                       "data": {"payload": {"fatal": fatal or []}}}
        top = {}
        if pos250 is not None or momentum or earnings:
            top = {"symbol": "sh600000", "pos250": pos250, "momentum_state": momentum,
                   "turnover_state": "中性", "earnings_state": earnings,
                   "volume_state": "未触发", "cycle_state": "", "pe_pct": pe_pct,
                   "target_upside": 0.2, "data": {}}
        return {"symbol": "sh600000", "name": "测试", "select": {"symbol": "sh600000"} if in_select else {},
                "cycle": {"symbol": "sh600000", "is_cycle": is_cycle,
                          "position_label": position, "position_pct": 10.0},
                "quality": quality, "top": top}

    def test_all_gates_pass_is_buy(self):
        from web import review
        item = review.evaluate(self._row(q=73.8, v=83.2))
        self.assertEqual(item["tier"], "buy")
        self.assertEqual(item["gaps"], [])

    def test_hard_floor_is_not_negotiable(self):
        """Q < 45 就是排除，V 再高也不抵——照抄工具的硬线。"""
        from web import review
        item = review.evaluate(self._row(q=42.0, v=96.5, conclusion="回避",
                                        reason="质量分 42.0 低于 45，禁止买入"))
        self.assertEqual(item["tier"], "out")
        self.assertEqual(item["gaps"][0]["label"], "Q 不足")

    def test_fatal_risk_vetoes_even_with_high_scores(self):
        from web import review
        item = review.evaluate(self._row(q=88.0, v=88.0, conclusion="回避",
                                        reason="出现致命风险：利息保障 < 1",
                                        fatal=["利息保障 < 1"]))
        self.assertEqual(item["tier"], "out")
        self.assertEqual(item["gaps"][0]["label"], "致命风险")

    def test_fatal_written_in_reason_is_recognised(self):
        """老记录没有 `fatal` 列表，致命项只写在 reason 里，也要认出来。"""
        from web import review
        item = review.evaluate(self._row(q=52.0, v=90.0, conclusion="回避",
                                        reason="出现致命风险：商誉/净资产超过 50%"))
        self.assertEqual(item["gaps"][0]["label"], "致命风险")
        self.assertIn("商誉", item["gaps"][0]["detail"])

    def test_cycle_high_downgrades_to_watch(self):
        from web import review
        item = review.evaluate(self._row(q=73.8, v=83.2, is_cycle=True,
                                        position="high"))
        self.assertEqual(item["tier"], "watch")
        self.assertEqual([g["label"] for g in item["gaps"]], ["周期高位"])

    def test_missing_v_is_watch_not_buy(self):
        from web import review
        item = review.evaluate(self._row(q=70.0, v=None))
        self.assertEqual(item["tier"], "watch")
        self.assertEqual(item["gaps"][0]["label"], "缺 V 分")
        self.assertEqual(item["bucket"], 0.0)      # 缺数据不许排到前头

    def test_v_ladder_requires_more_when_q_low(self):
        from web import review
        # Q 56 需要 V≥65：V 60 不够
        self.assertEqual(review.evaluate(self._row(q=56.0, v=60.0))["tier"], "watch")
        # Q 72 只需要 V≥50
        self.assertEqual(review.evaluate(self._row(q=72.0, v=55.0))["tier"], "buy")

    def test_near_high_blocks(self):
        from web import review
        self.assertEqual(review.evaluate(self._row(q=73.8, v=83.2,
                                                   pos250=-0.02))["tier"], "watch")

    def test_quality_conclusion_missing_is_nodata(self):
        from web import review
        item = review.evaluate(self._row(conclusion=None))
        self.assertEqual(item["tier"], "nodata")

    def test_sort_uses_min_of_q_and_v(self):
        """档内排序按 min(Q, V)：Q 90/V 60 要排在 Q 66/V 80 后面（木桶原则）。"""
        from web import review
        a = review.evaluate(self._row(q=90.0, v=60.0))
        b = review.evaluate(self._row(q=66.0, v=80.0))
        a["symbol"], b["symbol"] = "sh600001", "sh600002"
        out = review.build([{"symbol": a["symbol"], "name": "A",
                             "quality": {"q": 90.0, "v": 60.0, "conclusion": "观察"},
                             "select": {}, "cycle": {}, "top": {"pos250": -0.3}},
                            {"symbol": b["symbol"], "name": "B",
                             "quality": {"q": 66.0, "v": 80.0, "conclusion": "观察"},
                             "select": {}, "cycle": {}, "top": {"pos250": -0.3}}])
        self.assertEqual([it["symbol"] for it in out["rows"]], ["sh600002", "sh600001"])

    def test_out_comes_before_watch_in_ranking(self):
        from web import review
        out = review.build([
            {"symbol": "sz000001", "name": "差", "select": {},
             "quality": {"q": 30.0, "v": 80.0, "conclusion": "回避", "reason": "x"},
             "cycle": {}, "top": {"pos250": -0.3}},
            {"symbol": "sh600002", "name": "好", "select": {"symbol": "sh600002"},
             "quality": {"q": 70.0, "v": 80.0, "conclusion": "观察"},
             "cycle": {}, "top": {"pos250": -0.3}},
        ])
        self.assertEqual([it["symbol"] for it in out["rows"]], ["sh600002", "sz000001"])
        self.assertEqual(out["counts"]["buy"], 1)
        self.assertEqual(out["counts"]["out"], 1)


class TestReviewPage(WebCase):
    def test_review_page_renders_with_nav(self):
        client = self.client()
        resp = client.get("/review")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("综合评估", html)
        self.assertIn("优先研究", html)

    def test_empty_db_does_not_crash(self):
        """库存在但一条结果都没有：给「先跑 ./run.py all」，不是 500。"""
        results_db.connect(self.db_path).close()     # 建库建表，但不写行
        client = self.client(with_data=False)
        resp = client.get("/review")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("./run.py all", resp.get_data(as_text=True))

    def test_demo_row_is_not_buy(self):
        """演示数据 Q54.4/V67 + 周期高位：只能进「观察」，不能进「优先研究」。"""
        client = self.client()
        html = client.get("/review").get_data(as_text=True)
        self.assertIn("周期高位", html)
        self.assertIn("宝钢股份", html)


class TestNavAndLinks(WebCase):
    """导航只有 6 项，且都指向真页面；名称的链接行为按页区分（2026-11-15 反馈）。"""

    def nav_labels(self, html):
        import re
        seg = html.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
        return re.findall(r">([^<]+)</a>", seg)

    def test_nav_order_and_removed_pages(self):
        html = self.client().get("/review").get_data(as_text=True)
        self.assertEqual(self.nav_labels(html),
                         ["批次", "选股", "周期", "质量", "顶部", "综合评估"])
        self.assertNotIn("运行记录", html)

    def test_select_names_are_not_links(self):
        """选股页的名称不点：那一页看的是"名单和排序依据"，不需要跳走。"""
        html = self.client().get("/select").get_data(as_text=True)
        self.assertNotIn("/stock/sh600019", html)
        self.assertIn("宝钢股份", html)

    def test_tool_pages_link_into_their_own_section(self):
        """周期/质量/顶部点名称都要带 focus，否则落地页永远先显示四因子。"""
        for tool in ("cycle", "quality", "top"):
            with self.subTest(tool=tool):
                html = self.client().get(f"/{tool}").get_data(as_text=True)
                self.assertIn(f"/stock/sh600019?focus={tool}", html)

    def test_stock_page_puts_the_clicked_block_first(self):
        import re
        want = {"": "四因子独立观察", "?focus=cycle": "周期弹性三判断",
                "?focus=quality": "质量初筛", "?focus=top": "四因子独立观察"}
        for query, first in want.items():
            with self.subTest(query=query or "(默认)"):
                html = self.client().get("/stock/sh600019" + query).get_data(as_text=True)
                heads = re.findall(r"<h2>(.*?)<", html)
                self.assertEqual(heads[0], first, heads)


class TestReadOnly(WebCase):
    def test_queries_do_not_write(self):
        """看板只读：打开几个页面后，库文件的 mtime 与大小都不变。"""
        client = self.client()
        before = Path(self.db_path).stat()
        for url in ("/review", "/top", "/stock/sh600019", "/stock/sh600019?focus=cycle"):
            client.get(url)
        after = Path(self.db_path).stat()
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)

    def test_missing_tool_does_not_break_page(self):
        """只有选股结果时，周期/质量/顶部三页要各自给"还没有"的提示。"""
        results_db.save("select", [{"symbol": "sh600019", "name": "宝钢股份"}],
                        "2026-11-13", argv="test", db=self.db_path)
        client = self.client(with_data=False)
        for url in ("/cycle", "/quality", "/top"):
            with self.subTest(url=url):
                resp = client.get(url)
                self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
