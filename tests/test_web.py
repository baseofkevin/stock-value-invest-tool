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

import copy
import os
import re
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
         "position_label": "high",
         "payload": {"detail": {
             "industry": "钢铁",
             "judge": [
                 {"name": "判断① 是不是周期股", "result": "是", "metric": "毛利率标准差 σ",
                  "value": "2.30 pp", "detail": "CV 37.3% · 中位 6.2%", "tone": "warn"},
                 {"name": "判断② 弹性", "result": "弱", "metric": "增速比值",
                  "value": "-1.17", "detail": "", "tone": "muted"},
                 {"name": "判断③ 周期位置", "result": "高位", "metric": "产能周转率分位",
                  "value": "63.7%", "detail": "", "tone": "warn"},
             ],
             "notes": ["判断① 走相对波动通道：σ 2.30pp 未到 5pp，但 CV 37% ≥ 20%"],
             "badges": ["周期股", "相对波动", "弹性弱", "高位"],
             "series": {"gross_margin": [{"date": "2026-06-30", "value": 5.41},
                                         {"date": "2026-09-30", "value": 5.43}],
                        "ratio": [{"date": "2026-09-30", "value": -1.17}],
                        "turnover": [{"date": "2026-09-30", "value": 1.02}]},
             "meta": {"quarters": 43, "latest": "2026-09-30"},
         }}},
    ], date, argv="test", db=db_path, kind=kind)
    results_db.save("quality", [
        {"symbol": "sh600019", "name": "宝钢股份", "q": 54.4, "q_grade": "合格",
         "v": 67.0, "v_grade": "合理", "conclusion": "观察",
         "reason": "估值便宜，但现金流质量待观察", "risks_hit": 1,
         "risks_watch": 2, "risks_known": 9,
         "payload": {"detail": {
             "meta": {"industry": "钢铁", "business": "钢铁业、贸易。",
                      "price": 6.18, "price_basis": "2026-11-13 收盘", "mcap_yi": 1346},
             "q": {"score": 54.4, "grade": "合格", "coverage": 100,
                   "groups": [{"label": "资本回报", "score": 20.0, "max": 30,
                               "coverage": 100,
                               "subs": [{"label": "ROE 中位数", "score": 6.0,
                                          "max": 6, "raw": "6.2%"}]}]},
             "v": {"score": 67.0, "grade": "合理", "coverage": 100,
                   "parts": [{"label": "机构目标价空间", "score": 26.8, "max": 40,
                              "raw": "目标价 7.47 元（+20.9%）"}]},
             "risks": [{"group": "安全", "name": "现金短债比", "status": "watch",
                        "status_label": "提示", "severity": "中",
                        "detail": "0.62（<0.8）"},
                       {"group": "真伪", "name": "现金流质量", "status": "clear",
                        "status_label": "通过", "severity": "高",
                        "detail": "净现比(TTM) 3.25（优秀）"}],
             "risk_counts": {"hit": 0, "watch": 1, "clear": 1},
             "fatal": [],
             "valuation": {"rows": [{"label": "PE(TTM)", "current": 14.75,
                                     "percentile": 79, "min": 5.33, "max": 20.68}],
                           "sample": "2018-01-05 至 2026-11-13 · 454 个周点"},
             "forecast": {"available": True, "target_price": 7.47,
                          "rows": [{"year": 2026, "eps": 0.44, "pe": 13.92,
                                    "pb": 0.65, "profit_yi": 96.7,
                                    "profit_yoy": -6.5}]},
             "tracking": [{"metric": "CFO/净利润(TTM)", "current": "3.25",
                           "threshold": "≥ 0.7", "frequency": "每季"}],
             "data_quality": {"risks_known": 9, "risks_total": 9, "coverage": 100,
                              "institution": 1, "blockers": []},
             "decision": {"label": "观察", "reason": "估值便宜，但现金流质量待观察"},
         }}},
    ], date, argv="test", db=db_path, kind=kind)
    results_db.save("top", [
        {"code": "sh600019", "name": "宝钢股份", "ok": True, "error": "",
         "date": "2026-11-13", "close": 6.18, "pos250": -0.21,
         "days_since_high": 242, "phase": "反弹接近局部高点",
         "views": [["动量", "低位·走平"], ["换手", "偏低"], ["业绩", "减速"],
                   ["量价", "未触发"]],
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


class TestRegimeBanner(WebCase):
    """`/batches` 顶部那块大势横幅：有数据摆三张卡，没数据说人话。"""

    RESULT = {
        "trend": {"state": "空头确认", "score": 2, "date": "2026-11-20",
                  "close": 3906.1, "ma20": 4402.7, "ma60": 4589.9,
                  "ma60_slope": -0.73, "weeks_below_ma20": 19,
                  "weeks_below_ma60": 12},
        "valuation": {"state": "便宜", "date": "2026-11-19", "pe_ttm": 13.09,
                      "bond10y": 1.58, "erp": 6.06, "pct": 84, "mean": 5.4,
                      "n": 2400},
        "sentiment": {"state": "恐慌", "date": "2026-11-19", "below_na": 10.95,
                      "below_na_pct": 94, "below_count": 572, "total_count": 5224,
                      "turnover": 2.33, "turnover_q10": 1.46, "volume_dry": False},
        "data_asof": "2026-11-19",
        "summary": "趋势 空头确认 · 估值 便宜 84% · 情绪 恐慌",
    }

    def _seed(self, day: str = "2026-11-20") -> None:
        from stock_market_trend import store
        conn = store.connect(self.db_path)
        try:
            store.save(conn, day, self.RESULT)
        finally:
            conn.close()

    def test_three_cards(self):
        self._seed()
        html = self.client().get("/batches").get_data(as_text=True)
        for text in ("大势 · A股整体", "空头确认", "6.06%", "10.95%", "未地量"):
            with self.subTest(text=text):
                self.assertIn(text, html)

    def test_cells_and_headers_carry_details(self):
        """历史表的格子挂明细、表头挂阈值——两处都得在，缺一个就白做了。"""
        self._seed()
        html = self.client().get("/batches").get_data(as_text=True)
        self.assertIn("已 19 周收在 MA20 下方", html)      # 格子的明细
        self.assertIn("≥80 便宜", html)                    # 表头的阈值
        self.assertIn("≥90 恐慌", html)

    def test_no_table_degrades(self):
        """从没跑过这个功能：不 500，给一句怎么跑。"""
        html = self.client().get("/batches").get_data(as_text=True)
        self.assertIn("还没有大势记录", html)

    def test_header_leads_with_base_date(self):
        """横幅右上角主词是**基准日**（跑的那天），不是 `data_asof`。

        `data_asof` 取三路里最旧的一天，常年比基准日早一到几天。摆在主位时
        「跑在 11-23、数据截至 11-20」并排，会让人以为面板停在上一行没更新。
        这里的 RESULT 正好是基准日 11-20 / 数据截至 11-19。
        """
        self._seed()
        html = self.client().get("/batches").get_data(as_text=True)
        m = re.search(r'<div class="regime-when">(.*?)</div>', html, re.S)
        self.assertIsNotNone(m, "横幅头部那块日期必须还在")
        head = re.sub(r"\s+", " ", m.group(1))
        self.assertIn("基准日 <b>2026-11-20</b>", head)
        self.assertIn("数据截至 2026-11-19", head)

    def test_rerun_same_day_overwrites(self):
        self._seed()
        self._seed()          # 同一周五跑两次
        conn = __import__("sqlite3").connect(self.db_path)
        try:
            n = conn.execute("select count(*) from market_regime").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1, "一周一行，重跑覆盖而不是追加")


class TestRegimeHistory(WebCase):
    """`/trend`：每周的大势要按基准日留下来，并且能按日期查。"""

    def _seed(self, day: str) -> None:
        from stock_market_trend import store
        conn = store.connect(self.db_path)
        try:
            store.save(conn, day, TestRegimeBanner.RESULT)
        finally:
            conn.close()

    def test_weeks_are_stored_and_filterable(self):
        self._seed("2026-11-13")
        self._seed("2026-11-20")

        conn = __import__("sqlite3").connect(self.db_path)
        try:
            n = conn.execute("select count(*) from market_regime").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 2, "跨周追加，不覆盖上一周")

        client = self.client()
        html = client.get("/trend").get_data(as_text=True)
        self.assertIn("2026-11-13", html)
        self.assertIn("2026-11-20", html)

        filtered = client.get("/trend?start=2026-11-20").get_data(as_text=True)
        self.assertIn("2026-11-20", filtered)
        # 只看表：页面上方那张量能图是固定的 5 年背景，不受日期筛选影响
        # （筛选筛的是"周记"，而图是"量能的参照系"），所以断言得落在表格里，
        # 不能拿整页 HTML 判"这个日期不出现"。
        table = filtered.split('class="grid regime-history"', 1)[1].split("</table>", 1)[0]
        self.assertNotIn("2026-11-13", table, "start 之后的旧周不该出现在表里")

    def test_home_links_to_full_history(self):
        self._seed("2026-11-20")
        html = self.client().get("/batches").get_data(as_text=True)
        self.assertIn("查看全部历史", html)
        self.assertIn("/trend", html)

    def test_every_metric_cell_has_a_hover_tip(self):
        """六个指标列每格都要有悬停解读，PE / 国债也不例外。

        曾有四列（趋势/ERP/破净/量能）有 title、PE 和国债没有——
        那两格恰恰最需要解释：单看 13 倍 PE 不知道贵不贵，得扣掉国债的机会成本。
        这条同时钉住 `_regime_tips` 的六个键（少一个 Jinja 渲染成空 title，
        页面上就是"鼠标放上去没反应"，很难被测试之外的手段发现）。
        """
        from web import db as webdb
        res = copy.deepcopy(TestRegimeBanner.RESULT)
        w = {"pe_ttm": 13.02, "bond10y": 1.59, "erp": 6.09, "erp_pct": 85,
             "erp_mean": 4.82, "trend_state": "空头确认", "trend_score": 2,
             "trend_close": 3958.29, "ma20": 4363.97, "ma60": 4580.87,
             "weeks_below_ma20": 20, "ma60_slope": -0.74,
             "below_na": 12.17, "below_na_pct": 96, "turnover": 2.7,
             "volume_state": "平稳", "volume_dry": False}
        tips = webdb._regime_tips(w)
        for key in ("trend", "erp", "pe", "bond", "below", "volume"):
            self.assertTrue(tips.get(key), f"tips.{key} 不能为空")
        self.assertIn("100/PE", tips["pe"])
        self.assertIn("机会成本", tips["bond"])

        self._seed("2026-11-23")
        html = self.client().get("/trend").get_data(as_text=True)
        table = html.split('class="grid regime-history"', 1)[1].split("</table>", 1)[0]
        self.assertRegex(table, r'<td class="n tip" title="PE\(TTM\)')
        self.assertRegex(table, r'<td class="n tip" title="10 年期国债收益率')
        self.assertNotIn('title="">', table, "不许出现空 title（渲染成空格也没提示）")

    def test_partial_week_badge_sits_on_the_base_date(self):
        """「周内跑」挂在**基准日**，不挂量能格。

        半根周线同时影响两格：趋势拿它的收盘判 MA，量能跳过它退回上一完整周。
        标记塞在量能格里会被读成「平稳·未走完」（同一档位的两个限定），
        而且暗示只有量能受影响——所以它属于整行，落在基准日。
        """
        from stock_market_trend import store
        res = copy.deepcopy(TestRegimeBanner.RESULT)
        res["sentiment"]["volume_state"] = "平稳"
        res["sentiment"]["partial_week"] = {"date": "2026-11-23", "turnover": 0.61,
                                            "close": 3958.29, "prev_date": "2026-11-20"}
        conn = store.connect(self.db_path)
        try:
            store.save(conn, "2026-11-23", res)
        finally:
            conn.close()

        html = self.client().get("/trend").get_data(as_text=True)
        table = html.split('class="grid regime-history"', 1)[1].split("</table>", 1)[0]

        self.assertEqual(table.count('class="chip partial"'), 1, "整行只该有一个标记")
        self.assertRegex(
            table,
            r'<td class="name">\s*2026-11-23<small>[^<]*</small>'
            r'<span class="chip partial"[^>]*>周内跑</span>')
        # 量能格只剩档位一个 chip；退回哪一周写在 title 里
        vol = re.search(r'<td class="tip" title="量能：.*?">\s*(.*?)</td>', table, re.S)
        self.assertIsNotNone(vol, "量能格的 title 必须以「量能：」开头")
        self.assertEqual(vol.group(1).count("chip"), 1)
        self.assertNotIn("周内跑", vol.group(1))


class TestPages(WebCase):
    def test_all_pages_render(self):
        client = self.client()
        for url in ("/select", "/cycle", "/quality", "/top", "/review",
                    "/batches", "/trend", "/stock/sh600019"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_cycle_has_no_standalone_contract_liability_column(self):
        """合同负债**不单独占一列**：它融进「位置」当限定语（2026-11-20 改）。

        单独一列没人看、也读不出结论；并入「低位 · 订单回升」才有意义。
        """
        html = self.client().get("/cycle").get_data(as_text=True)
        self.assertNotIn("合同负债</th>", html)
        self.assertIn("需求端", html)

    def test_cycle_charts_filter_the_detail_table(self):
        """点散点圆点 / 产能横条，明细只留这一只（2026-11-23 加）。

        两张图画的是同一批票，点一处就该只留一只；取消入口（提示条 + 显示全部）
        得在同一屏里，否则图表看着像"不可点"。这里钉住接线，免得以后改模板
        把联动悄悄拆掉。
        """
        html = self.client().get("/cycle").get_data(as_text=True)
        self.assertIn('id="cycle-count"', html)          # 计数跟着筛选变
        self.assertIn('id="cycle-filter"', html)         # 筛选提示条
        self.assertIn('id="cycle-filter-clear"', html)   # 显示全部
        self.assertIn('id="cycle-empty"', html)          # 命不中时的空态
        self.assertIn('class="stock-row" data-symbol="sh600019"', html)
        self.assertIn('data-name="宝钢股份"', html)       # 提示条写名称，不只写代码
        # 两张图都能点：散点 + 横条各一个 onClick
        self.assertEqual(html.count("onClick:"), 2)
        # 筛表那层走共用脚本，和 /quality /top /review 是同一份，别再本地重写一套
        self.assertIn("createChartFilter({", html)
        self.assertIn("filter.apply(", html)
        for fn in ("function toggleFilter", "function paintBar",
                   "function paintScatter"):
            self.assertIn(fn, html)

    def test_root_goes_to_batches(self):
        """/ 是旧书签和 ./run.py web 打印的地址：转到批次页，不是 404。

        2026-11-20 起默认门面是批次（原来是综合评估）：批次页是"先挑批次"
        那条链的第一格。
        """
        resp = self.client().get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/batches"))

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


class TestExtremePage(WebCase):
    """冷热极点页读 `results.db` 的 price + price_stat，不读 shared.db。

    这里造的是 `stock_extreme_point` 落库后同形状的数据；页面只读，不写任何东西。
    """

    def _seed(self, days=("2026-11-13", "2026-11-16"), index=True, closes=None):
        from stock_extreme_point import store
        conn = store.connect(self.db_path)
        try:
            store.ensure(conn, "results")
            for day in days:
                rows = [
                    {"date": day, "symbol": "sh600519", "name": "贵州茅台",
                     "close": (closes or {}).get(day, 1453.9), "pct_chg": 1.30,
                     "pe_ttm": 22.12,
                     "pb_mrq": 6.70, "ps_ttm": 10.42,
                     "total_mv": 1.82e12, "circ_mv": 1.82e12},
                    {"date": day, "symbol": "sz000001", "name": "亏损银行",
                     "close": 11.20, "pct_chg": -0.89, "pe_ttm": -5.0,
                     "pb_mrq": 0.52, "ps_ttm": 1.71,
                     "total_mv": 2.17e11, "circ_mv": 2.17e11},
                ]
                store.write_day(conn, day, rows, with_stat=True, src="test")
                if index:
                    store.write_index(conn, day, {
                        "date": day, "symbol": "sh000300", "name": "沪深300",
                        "close": 3979.80, "pct_chg": -0.91, "src": "test"})
        finally:
            conn.close()

    def test_empty_price_table_degrades_to_hint(self):
        """没跑过 price：页面 200 + 一句先跑什么，不是 500。"""
        resp = self.client().get("/extreme")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("还没有全市场行情", resp.get_data(as_text=True))

    def test_reads_results_db_not_shared_db(self):
        """结果库有价格、共享库故意指向不存在的文件：页面仍要出数据。"""
        self._seed()
        self._old_shared = os.environ.get("STOCK_SHARED_DB")
        os.environ["STOCK_SHARED_DB"] = os.path.join(self.tmp, "nope-shared.db")
        try:
            html = self.client(with_data=False).get("/extreme").get_data(as_text=True)
        finally:
            if self._old_shared is None:
                os.environ.pop("STOCK_SHARED_DB", None)
            else:
                os.environ["STOCK_SHARED_DB"] = self._old_shared
        self.assertIn("贵州茅台", html)
        self.assertIn("2026-11-16", html)
        self.assertIn("上涨 / 下跌", html)          # price_stat 的四张卡

    def test_index_card_shows_hs300(self):
        """第五张卡：沪深300 收盘 + 当日涨跌幅 + 000300.SH。"""
        self._seed()
        html = self.client(with_data=False).get("/extreme").get_data(as_text=True)
        self.assertIn("沪深300", html)
        self.assertIn("3,979.80", html)
        self.assertIn("-0.91%", html)
        self.assertIn("000300.SH", html)

    def test_index_card_missing_degrades(self):
        """老库没有 index_price：页面仍 200，第五张卡写「—」，不 500。"""
        self._seed(index=False)
        html = self.client(with_data=False).get("/extreme").get_data(as_text=True)
        self.assertIn("沪深300", html)
        self.assertIn("还没有沪深300快照", html)

    def test_search_unique_hit_shows_history(self):
        self._seed()
        html = self.client(with_data=False).get("/extreme?q=600519").get_data(as_text=True)
        self.assertIn("贵州茅台", html)
        self.assertIn("共有 2 个时点", html)         # 两个交易日都在 price 里

    @staticmethod
    def _chart_points(html: str) -> list[dict]:
        import json
        import re
        m = re.search(r'id="price-history-data" type="application/json">(.*?)</script>',
                      html, re.S)
        if not m:
            return []
        return json.loads(m.group(1))

    def test_history_chart_cumulative_and_range(self):
        """默认三点归一化：100 → 110 → 121 = +10% → +21%。"""
        self._seed(days=("2026-11-13", "2026-11-16", "2026-11-17"),
                   closes={"2026-11-13": 100.0, "2026-11-16": 110.0,
                           "2026-11-17": 121.0})
        html = self.client(with_data=False).get("/extreme?q=600519").get_data(as_text=True)
        self.assertIn('id="price-history-chart"', html)
        self.assertIn("累计涨跌幅", html)
        self.assertIn("+21.00%", html)
        self.assertIn("累计涨跌", html)              # 表格新增列
        pts = self._chart_points(html)
        self.assertEqual([p["date"] for p in pts],
                         ["2026-11-13", "2026-11-16", "2026-11-17"])
        self.assertEqual([p["close"] for p in pts], [100.0, 110.0, 121.0])
        self.assertEqual([p["cum"] for p in pts], [0.0, 10.0, 21.0])

        html = self.client(with_data=False).get(
            "/extreme?q=600519&start=2026-11-16&end=2026-11-17").get_data(as_text=True)
        self.assertIn("+10.00%", html)
        self.assertIn("当前 2 / 3 个时点", html)
        self.assertEqual([p["date"] for p in self._chart_points(html)],
                         ["2026-11-16", "2026-11-17"])

    def test_history_table_is_oldest_first(self):
        """明细表按时间正序：11-13 在 11-16 上面，和曲线方向一致。"""
        from web import db as webdb
        self._seed(days=("2026-11-13", "2026-11-16", "2026-11-17"),
                   closes={"2026-11-13": 100.0, "2026-11-16": 110.0,
                           "2026-11-17": 121.0})
        data = webdb.extreme_page(q="600519", db=self.db_path)
        self.assertEqual([r["date"] for r in data["rows"]],
                         ["2026-11-13", "2026-11-16", "2026-11-17"])
        self.assertEqual([r["cum"] for r in data["rows"]],
                         [0.0, 10.0, 21.0])

    def test_history_range_swaps_and_invalid_falls_back(self):
        self._seed(days=("2026-11-13", "2026-11-16", "2026-11-17"),
                   closes={"2026-11-13": 100.0, "2026-11-16": 110.0,
                           "2026-11-17": 121.0})
        html = self.client(with_data=False).get(
            "/extreme?q=600519&start=2026-11-17&end=2026-11-13").get_data(as_text=True)
        self.assertIn("起止已对调", html)
        self.assertIn("+21.00%", html)

        html = self.client(with_data=False).get(
            "/extreme?q=600519&start=2020-01-01&end=2026-11-17").get_data(as_text=True)
        self.assertIn("日期不在该股历史，已显示全部", html)
        self.assertIn("+21.00%", html)

    def test_history_single_point_has_no_chart(self):
        self._seed(days=("2026-11-16",), closes={"2026-11-16": 100.0})
        html = self.client(with_data=False).get("/extreme?q=600519").get_data(as_text=True)
        self.assertNotIn('id="price-history-chart"', html)
        self.assertIn("只有 1 个时点", html)

    def test_history_gap_is_null_not_zero(self):
        """别的票有 11-14、这只停牌：曲线上是 null，不是 0%。"""
        from stock_extreme_point import store
        self._seed(days=("2026-11-13", "2026-11-16"),
                   closes={"2026-11-13": 100.0, "2026-11-16": 110.0})
        conn = store.connect(self.db_path)
        try:
            store.write_day(conn, "2026-11-14", [{
                "date": "2026-11-14", "symbol": "sz000001", "name": "亏损银行",
                "close": 12.0, "pct_chg": 1.0, "pe_ttm": -5.0,
                "pb_mrq": 0.52, "ps_ttm": 1.71,
                "total_mv": 2.17e11, "circ_mv": 2.17e11,
            }], with_stat=True, src="test")
        finally:
            conn.close()
        html = self.client(with_data=False).get("/extreme?q=600519").get_data(as_text=True)
        pts = self._chart_points(html)
        self.assertEqual(len(pts), 3)
        self.assertEqual(pts[1]["date"], "2026-11-14")
        self.assertIsNone(pts[1]["close"])
        self.assertIsNone(pts[1]["cum"])

    def test_eastmoney_quote_links(self):
        """全市场表每只票都有「打开」→ 东财行情页；北交所路径与沪深不同。"""
        from web.app import _quote_url
        self.assertEqual(_quote_url("sh600519"),
                         "https://quote.eastmoney.com/sh600519.html#fullScreenChart")
        self.assertEqual(_quote_url("sz002286"),
                         "https://quote.eastmoney.com/sz002286.html#fullScreenChart")
        self.assertEqual(_quote_url("bj920992"),
                         "https://quote.eastmoney.com/bj/920992.html#fullScreenChart")
        self.assertEqual(_quote_url("bad"), "")
        self._seed()
        html = self.client(with_data=False).get("/extreme").get_data(as_text=True)
        self.assertIn('href="https://quote.eastmoney.com/sh600519.html#fullScreenChart"',
                      html)
        self.assertIn('target="_blank"', html)
        self.assertIn("打开 ↗", html)

    def test_all_summary_tables_have_eastmoney_links(self):
        """周期 / 质量 / 顶部 / 综合评估 四张汇总表都要有「打开 ↗」。"""
        client = self.client()
        for url in ("/cycle", "/quality", "/top", "/review"):
            with self.subTest(url=url):
                html = client.get(url).get_data(as_text=True)
                self.assertIn(
                    'href="https://quote.eastmoney.com/sh600019.html#fullScreenChart"',
                    html)
                self.assertIn("打开 ↗", html)

    def test_summary_tables_scroll_and_jump_by_name(self):
        """四张长表都要有固定高度的滚动框和按名称定位的下拉。"""
        client = self.client()
        cases = (("/cycle", "cycle-table"), ("/quality", "quality-table"),
                 ("/top", "top-table"), ("/review", "review-all-table"))
        for url, target in cases:
            with self.subTest(url=url):
                html = client.get(url).get_data(as_text=True)
                self.assertIn(f'id="{target}"', html)
                self.assertIn(f'data-stock-jump="{target}"', html)
                self.assertIn('class="scroll stock-scroll"', html)
                self.assertIn("stock-jump.js", html)
                self.assertIn('data-symbol="sh600019"', html)
                self.assertIn('<option value="sh600019">', html)

    def test_pe_sort_keeps_losses_last(self):
        """PE 升序时负值（亏损）排最后，不能冒充"最便宜"。"""
        self._seed()
        html = self.client(with_data=False).get(
            "/extreme?sort=pe&order=asc").get_data(as_text=True)
        self.assertLess(html.index("贵州茅台"), html.index("亏损银行"))

    def test_nav_has_price_entry(self):
        self.assertIn("冷热极点", self.client().get("/review").get_data(as_text=True))


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

    def test_missing_cell_shows_reason_and_command(self):
        """缺工具时不是只写「未跑」：给原因，并给可展开的补跑命令。"""
        client = self.client(with_data=False)
        results_db.save("select", [{"symbol": "sh600019", "name": "宝钢股份"}],
                        "2026-11-13", argv="test", db=self.db_path)
        html = client.get("/batches").get_data(as_text=True)
        self.assertIn('class="miss-code">未跑</code>', html)
        self.assertIn("还没有批量周期弹性记录，可直接补跑", html)
        self.assertIn("点击查看补跑命令", html)
        self.assertIn("./run.py cycle --date 2026-11-13", html)

    def test_missing_top_says_select_is_the_blocker(self):
        """没选股名单时，顶部不能直接 --from-select；要先把选股命令一起给出。"""
        client = self.client(with_data=False)
        results_db.save("quality", [{"symbol": "sz000001", "name": "平安银行"}],
                        "2026-11-16", argv="test", db=self.db_path)
        html = client.get("/batches").get_data(as_text=True)
        self.assertIn("缺少 2026-11-16 的选股名单", html)
        self.assertIn("顶部判断不能直接批量补跑", html)
        self.assertIn("./run.py select --date 2026-11-16", html)
        self.assertIn("./run.py top --from-select --date 2026-11-16", html)

    def test_sunday_batch_explains_why_it_cannot_run(self):
        """2026-11-15 是周日：选股/顶部都要说明不能补跑，不能给假命令。"""
        client = self.client(with_data=False)
        results_db.save("quality", [{"symbol": "sz000001", "name": "平安银行"}],
                        "2026-11-15", argv="test", db=self.db_path)
        html = client.get("/batches").get_data(as_text=True)
        self.assertIn("2026-11-15 是周日", html)
        self.assertIn("不能补跑选股", html)
        self.assertIn("不能补跑顶部判断", html)
        self.assertNotIn("./run.py select --date 2026-11-15", html)

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


class TestStockHistorySections(WebCase):
    """个股页只摆工具报告，不再列历次结果、不再画结论变化图。"""

    def test_history_sections_are_gone(self):
        client = self.client()
        results_db.save("quality", [{"symbol": "sh600019", "name": "宝钢股份",
                                     "q": 61.2, "v": 70.0, "conclusion": "观察"}],
                        "2026-11-13", argv="test", db=self.db_path, kind="single")
        html = client.get("/stock/sh600019").get_data(as_text=True)
        self.assertNotIn("历次结果", html)
        self.assertNotIn("结论怎么变的", html)
        self.assertNotIn('id="hist"', html)

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

    这一层最容易悄悄松掉的是**硬门槛**：一个"赔率分很高"的票如果命中了致命
    风险，必须还是排除。反过来也容易改过头——把软条件又写回否决项。两组都钉住。
    """

    def _row(self, q=70.0, v=70.0, conclusion="观察", reason="", fatal=None,
             is_cycle=False, position="", elastic="strong", elasticity=5.0,
             mcap=120.0, pct_pe=30.0, pct_pb=20.0, pct_ps=25.0, trend_hit=None,
             upside=0.25, pos250=-0.20, momentum="低位·走平", earnings="加速",
             pe_pct=30.0, in_select=True, detail=True):
        """一条合并行。

        默认参数刻意配成「7 分」（市值 1 + 估值 2 + 弹性 2 + 质量 1 + 目标价 1），
        正好在 `BUY_CUT = 5` 之上——绝大多数用例只想验别的规则，不该被基础分
        顺手拦下。要验某一项就自己传参。
        """
        quality = {}
        if conclusion is not None:
            risks = []
            for name in (trend_hit or []):
                risks.append({"group": "趋势", "name": name, "status": "hit"})
            payload = {"fatal": fatal or [], "label": conclusion, "reason": reason}
            if detail:
                payload["detail"] = {
                    "meta": {"industry": "测试", "mcap_yi": mcap},
                    "v": {"score": v, "grade": "",
                          "parts": [{"label": "机构目标价空间", "score": 100.0,
                                     "raw": "目标价 10.00 元（+25.0%）"}],
                          "historical": {"available": True, "metrics": [
                              {"key": "pe", "label": "PE(TTM)", "percentile": pct_pe},
                              {"key": "pb", "label": "PB", "percentile": pct_pb},
                              {"key": "ps", "label": "PS(TTM)", "percentile": pct_ps}]}},
                    "risks": risks, "fatal": fatal or [],
                }
            quality = {"symbol": "sh600000", "name": "测试", "q": q, "v": v,
                       "q_grade": "", "v_grade": "", "conclusion": conclusion,
                       "reason": reason, "risks_hit": 0, "risks_known": 14,
                       "data": {"payload": payload}}
        top = {}
        if pos250 is not None or momentum or earnings:
            top = {"symbol": "sh600000", "pos250": pos250, "momentum_state": momentum,
                   "turnover_state": "中性", "earnings_state": earnings,
                   "volume_state": "未触发", "cycle_state": "", "pe_pct": pe_pct,
                   "target_upside": upside, "data": {}}
        return {"symbol": "sh600000", "name": "测试",
                "select": ({"symbol": "sh600000", "market_cap_yi": mcap}
                           if in_select else {}),
                "cycle": {"symbol": "sh600000", "is_cycle": is_cycle,
                          "position_label": position, "position_pct": 10.0,
                          "elastic_label": elastic, "elasticity": elasticity},
                "quality": quality, "top": top}

    # ---------------------------------------------------------- 硬门槛

    def test_hard_floor_is_not_negotiable(self):
        """Q < 45 就是排除，赔率分再高也不抵——照抄工具的硬线。"""
        from web import review
        item = review.evaluate(self._row(q=42.0, v=96.5, conclusion="回避",
                                        reason="质量分 42.0 低于 45，禁止买入"))
        self.assertEqual(item["tier"], "out")
        self.assertEqual(item["notes"][0]["label"], "Q 不足")

    def test_fatal_risk_vetoes_even_with_high_scores(self):
        from web import review
        item = review.evaluate(self._row(q=88.0, v=88.0, conclusion="回避",
                                        reason="出现致命风险：利息保障 < 1",
                                        fatal=["利息保障 < 1"]))
        self.assertEqual(item["tier"], "out")
        self.assertEqual(item["notes"][0]["label"], "致命风险")

    def test_quality_conclusion_missing_is_nodata(self):
        from web import review
        item = review.evaluate(self._row(conclusion=None))
        self.assertEqual(item["tier"], "nodata")

    # ---------------------------------------------------------- 宽松：软条件不否决

    def test_verdict_avoid_is_watch_not_out(self):
        """工具判「回避」但 Q 没破 45、也没有致命项：降为观察，不再排除。

        这是"宽松"最直接的一条——20 只这样的票原来直接消失在本页。
        """
        from web import review
        item = review.evaluate(self._row(q=68.0, v=70.0, conclusion="回避",
                                        reason="第二节走弱"))
        self.assertEqual(item["tier"], "watch")
        self.assertEqual(item["notes"][0]["label"], "工具判回避")
        self.assertGreaterEqual(item["score"], review.BUY_CUT)   # 分数照算，只是不进挡

    def test_missing_v_does_not_block_buy(self):
        """V 缺失不再一票否决——缺 V 的 63 只里 60 只市值<200 亿。"""
        from web import review
        item = review.evaluate(self._row(q=70.0, v=None))
        self.assertEqual(item["tier"], "buy")
        self.assertIn("缺 V 分", [g["label"] for g in item["notes"]])

    def test_trend_weak_penalises_but_does_not_veto(self):
        """毛利率走弱只扣 2 分：7 分 → 5 分，仍在优先研究里。"""
        from web import review
        base = review.evaluate(self._row())
        bad = review.evaluate(self._row(trend_hit=["① 毛利率环比改善"]))
        self.assertEqual(bad["score"], base["score"] + review.TREND_PENALTY)
        self.assertEqual(bad["tier"], "buy")
        self.assertIn("趋势走弱", [g["label"] for g in bad["notes"]])

    def test_trend_penalty_can_drop_below_cut(self):
        """扣分确实能把一只票推到观察档——只是"排后面"，不是"出局"。"""
        from web import review
        item = review.evaluate(self._row(mcap=800.0, pct_pe=60.0, pct_pb=60.0,
                                         pct_ps=60.0, elastic="weak",
                                         upside=0.05,
                                         trend_hit=["② 合同负债"]))
        self.assertEqual(item["tier"], "watch")
        self.assertNotEqual(item["tier"], "out")

    # ---------------------------------------------------------- 收分项

    def test_small_cap_scores_more(self):
        from web import review
        small = review.evaluate(self._row(mcap=40.0))
        big = review.evaluate(self._row(mcap=800.0))
        self.assertEqual(small["score"] - big["score"], 3 - 0)

    def test_valuation_percentile_steps(self):
        from web import review
        # 三项分位均值 10 / 40 / 60 / 90 → 3 / 2 / 1 / 0 分
        for pct, want in ((10.0, 3), (40.0, 2), (60.0, 1), (90.0, 0)):
            with self.subTest(pct=pct):
                item = review.evaluate(self._row(pct_pe=pct, pct_pb=pct, pct_ps=pct))
                term = next(t for t in item["terms"] if t["key"] == "value")
                self.assertEqual(term["points"], want)

    def test_cycle_low_adds_points_for_cycle_stocks_only(self):
        from web import review
        low = review.evaluate(self._row(is_cycle=True, position="low"))
        neutral = review.evaluate(self._row(is_cycle=True, position="neutral"))
        noncycle = review.evaluate(self._row(is_cycle=False, position="low"))
        self.assertEqual(low["score"] - neutral["score"], 2)
        # 非周期股拿到 low（金融股之类）也不加分：这个指标对它没意义
        self.assertEqual(noncycle["score"], neutral["score"] - 0)

    def test_moat_adds_points(self):
        from web import review
        plain = review.evaluate(self._row())
        moat = review.evaluate(self._row(), moat="独家品种")
        self.assertEqual(moat["score"] - plain["score"], 2)
        self.assertTrue(moat["has_moat"])
        self.assertIn("独家品种", [h["text"] for h in moat["highlights"]])

    def test_moat_none_is_not_penalised(self):
        """「无标注」和"没查过"同等待遇：0 分，不扣分。"""
        from web import review
        blank = review.evaluate(self._row(), moat="")
        reviewed = review.evaluate(self._row(), moat="无标注")
        self.assertEqual(blank["score"], reviewed["score"])
        self.assertFalse(blank["has_moat"])

    def test_unverified_moat_scores_nothing(self):
        """「待核实」是占位不是结论：2026-11-16 全市场铺开后必须 0 分。

        这条防的是"铺底数据自带上车"：一旦它被算进垄断分，全市场 4700 多只
        都会凭空多 2 分，买入门槛就形同虚设。
        """
        from web import review
        blank = review.evaluate(self._row(), moat="")
        todo = review.evaluate(self._row(), moat="待核实")
        self.assertEqual(blank["score"], todo["score"])
        self.assertFalse(todo["has_moat"])
        self.assertNotIn("待核实", [h["text"] for h in todo["highlights"]])
        self.assertEqual(todo["moat"], "待核实")   # 但页面上还是能看出"没核过"

    def test_unstable_elasticity_gets_half_credit(self):
        """「算不出来」给 1 分而不是 0：营收走平的公司往往是稳的。"""
        from web import review
        item = review.evaluate(self._row(is_cycle=True, elastic="unstable",
                                         elasticity=9.0))
        term = next(t for t in item["terms"] if t["key"] == "elastic")
        self.assertEqual(term["points"], review.ELASTIC_POINTS["unstable"])
        self.assertEqual(
            review.evaluate(self._row(is_cycle=True, elastic="weak",
                                      elasticity=1.2))["score"] + 1,
            item["score"])

    # ---------------------------------------------------------- 分档与排序

    def test_score_text_explains_the_number(self):
        from web import review
        item = review.evaluate(self._row(mcap=40.0))
        self.assertIn("市值小 +3", item["score_text"])
        self.assertIn("40 亿", item["score_text"])

    def test_sort_uses_score_desc(self):
        from web import review
        out = review.build([
            {"symbol": "sh600001", "name": "低分",
             "quality": {"q": 70.0, "v": 70.0, "conclusion": "观察"},
             "select": {"market_cap_yi": 900.0}, "cycle": {}, "top": {}},
            {"symbol": "sh600002", "name": "高分",
             "quality": {"q": 70.0, "v": 70.0, "conclusion": "观察"},
             "select": {"market_cap_yi": 30.0}, "cycle": {}, "top": {}},
        ])
        self.assertEqual([it["symbol"] for it in out["rows"]], ["sh600002", "sh600001"])

    def test_out_comes_before_watch_in_ranking(self):
        from web import review
        out = review.build([
            {"symbol": "sz000001", "name": "差", "select": {},
             "quality": {"q": 30.0, "v": 80.0, "conclusion": "回避", "reason": "x"},
             "cycle": {}, "top": {"pos250": -0.3}},
            {"symbol": "sh600002", "name": "好", "select": {"symbol": "sh600002"},
             "quality": {"q": 70.0, "v": 80.0, "conclusion": "观察"},
             "cycle": {"is_cycle": False, "elastic_label": "strong", "elasticity": 5.0},
             "top": {"pos250": -0.3}},
        ])
        self.assertEqual([it["symbol"] for it in out["rows"]], ["sh600002", "sz000001"])
        self.assertEqual(out["counts"]["out"], 1)
        self.assertEqual(out["counts"]["buy"] + out["counts"]["watch"], 1)

    def test_cut_is_the_only_knob(self):
        """分档只由 `BUY_CUT` 决定——改它一个数，档位跟着变。"""
        from web import review
        item = review.evaluate(self._row())
        self.assertEqual(item["tier"],
                         "buy" if item["score"] >= review.BUY_CUT else "watch")
        self.assertEqual(review.build([self._row()])["cut"], review.BUY_CUT)

    def test_build_accepts_moat_map(self):
        """`build(rows, moats)` 把标注注入进来，`evaluate` 本身不连库。"""
        from web import review
        row = self._row()
        row["symbol"] = "sh600519"
        plain = review.build([dict(row)])
        rich = review.build([dict(row)], {"sh600519": "独家品种"})
        self.assertEqual(rich["rows"][0]["score"] - plain["rows"][0]["score"], 2)


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
    """导航按取用顺序排列，且都指向真页面；名称的链接行为按页区分（2026-11-15 反馈）。"""

    def nav_labels(self, html):
        import re
        seg = html.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
        return re.findall(r">([^<]+)</a>", seg)

    def test_nav_order_and_removed_pages(self):
        html = self.client().get("/review").get_data(as_text=True)
        self.assertEqual(self.nav_labels(html),
                         ["首页", "选股", "周期", "质量", "顶部", "综合评估",
                          "冷热极点", "A股大势", "原书学习"])
        self.assertNotIn("运行记录", html)

    def test_book_page_is_served_whole_with_back_home(self):
        """原书学习：整本书原样吐出（自包含 HTML，不裁不改），只在收尾补返回浮标。"""
        resp = self.client().get("/book")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        html = resp.get_data(as_text=True)
        self.assertIn("成功投资的五条法则", html)
        self.assertIn('<a class="back-home" href="/batches"', html)
        self.assertTrue(html.rstrip().endswith("</html>"))

    def test_home_hero_has_dataflow_entry(self):
        """首页 hero 右上角那个「本项目代码执行流程」入口（2026-11-20 加）。"""
        html = self.client().get("/batches").get_data(as_text=True)
        self.assertIn('<a class="hero-flow" href="/dataflow"', html)
        self.assertIn("本项目代码执行流程", html)
        # 单独成入口，不混进导航（导航仍是 9 项，见上一个用例）
        self.assertNotIn("本项目代码执行流程", html.split("</nav>", 1)[0])

    def test_dataflow_page_is_served_with_back_home(self):
        """代码执行流程：dataflow.html 原样端出，只在收尾补返回浮标。"""
        resp = self.client().get("/dataflow")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        html = resp.get_data(as_text=True)
        self.assertIn("数据流转与筛选条件", html)
        self.assertIn('<a class="flow-back" href="/batches"', html)
        self.assertTrue(html.rstrip().endswith("</html>"))

    def test_dataflow_has_no_svg_sidecar(self):
        """纯 SVG 副本已下线（2026-11-23 删除 dataflow.svg），页面只留自包含 HTML。

        以前吸顶条上有一条「打开纯 SVG」的相对链接，`/dataflow.svg` 是给它接住的；
        正文改成 HTML 之后那条链接没了、文件也删了，路由跟着一起删。这里钉三件事，
        免得哪天又长回来：正文不引用、正文不拉外链、那个路径已经不响应。
        """
        html = self.client().get("/dataflow").get_data(as_text=True)
        self.assertNotIn("dataflow.svg", html)
        # 自包含：样式内联，没有外部 CSS / JS / 图片的引用
        self.assertNotIn("<link ", html)
        self.assertNotIn("<script src=", html)
        self.assertEqual(self.client().get("/dataflow.svg").status_code, 404)

    def test_select_names_are_not_links(self):
        """选股页的名称不点：那一页看的是"名单和排序依据"，不需要跳走。"""
        html = self.client().get("/select").get_data(as_text=True)
        self.assertNotIn("/stock/sh600019", html)
        self.assertIn("宝钢股份", html)
        # 长表在固定框里滚；图表联动靠行上的 data-*。
        self.assertIn('class="stock-scroll select-scroll"', html)
        self.assertIn('id="select-table"', html)
        self.assertIn('id="select-filter-clear"', html)
        self.assertIn('id="select-empty"', html)
        self.assertIn('id="mcap"', html)
        self.assertIn('data-industry="钢铁"', html)
        self.assertIn('data-x="1.6"', html)
        self.assertIn('data-y="0.4"', html)
        self.assertIn("data-mcap=", html)
        self.assertIn("pointerdown", html)
        # 与周期/质量/顶部页一致：代码旁给东财行情页。
        self.assertIn('href="https://quote.eastmoney.com/sh600019.html#fullScreenChart"', html)

    def test_tool_pages_filter_detail_tables_from_charts(self):
        """点图筛明细：质量 / 顶部 / 综合评估（2026-11-23 加，与周期页同一套）。

        图上点一处（圆点 / 横条 / 柱 / 环）→ 下面的表只留那只票或那一档；
        「显示全部」和空态行都在模板里接好，筛选层是共用的 chart-filter.js。
        """
        cases = {
            "/quality": ("quality-table", "quality", "data-concl="),
            "/top": ("top-table", "top", "data-mom="),
            "/review": ("review-all-table", "review", "data-tier="),
        }
        for url, (table, prefix, group_attr) in cases.items():
            with self.subTest(url=url):
                html = self.client().get(url).get_data(as_text=True)
                self.assertIn("chart-filter.js", html)          # 共用筛选层加载了
                for suffix in ("count", "filter", "filter-text",
                               "filter-clear", "empty"):
                    self.assertIn(f'id="{prefix}-{suffix}"', html)
                self.assertIn(f'id="{table}"', html)
                self.assertIn('class="stock-row', html)         # 行要被认出来
                self.assertIn(group_attr, html)                 # 分组筛的键
                self.assertEqual(html.count("onClick:"), 2)     # 两张图都能点
                self.assertIn("createChartFilter", html)

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
                # h2 后面可能跟一个 <span class="tag">，所以取到 </h2> 为止再 strip
                # h2 里可能跟一个 <span class="tag">，去标签后再比
                heads = [re.sub(r"<[^>]+>", "", h).strip()
                         for h in re.findall(r"<h2>(.*?)</h2>", html, re.S)]
                # 标题后面可能跟 <span class="tag">，所以只比前缀
                self.assertTrue(heads[0].startswith(first), heads)



def _demo_report(title: str, body: str) -> str:
    """演示用「原版报告」：和工具真实入库同一种压缩格式（gzip+base64）。"""
    return results_db.pack_html(
        f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>'
        f'<style>body{{font:14px/1.6 sans-serif}}</style></head>'
        f'<body><h1>{title}</h1>{body}</body></html>')


class TestStockDetail(WebCase):
    """个股页的质量/周期两块：有明细就摆明细，旧行降级成一句提示。"""

    def test_quality_detail_sections(self):
        html = self.client().get("/stock/sh600019?focus=quality").get_data(as_text=True)
        for text in ("Q 分项", "V 分项", "风险检查", "估值分位", "机构预测",
                     "跟踪清单", "ROE 中位数", "目标价 7.47", "净现比(TTM) 3.25"):
            with self.subTest(text=text):
                self.assertIn(text, html)

    def test_quality_historical_fallback_is_display_only(self):
        """机构 V 为空但历史分位可用时，页面显示参考分并明确“不参与结论”。"""
        client = self.client()
        results_db.save("quality", [{
            "symbol": "sh600019", "name": "宝钢股份", "q": 55.0,
            "q_grade": "合格", "v": None, "v_grade": "数据不足",
            "conclusion": "数据不足，不下结论", "reason": "无机构覆盖",
            "risks_hit": 0, "risks_watch": 0, "risks_known": 14,
            "payload": {"detail": {
                "meta": {"industry": "钢铁"},
                "q": {"score": 55.0, "grade": "合格", "coverage": 100,
                      "groups": []},
                "v": {"score": None, "grade": "数据不足", "coverage": 0,
                      "parts": [],
                      "historical": {"available": True, "score": 45.0,
                                     "grade": "中偏高", "verdict": "略偏贵",
                                     "median_percentile": 55.0,
                                     "sample_weeks": 252, "spread": 22.0,
                                     "metrics": [
                                         {"key": "pb", "label": "PB",
                                          "percentile": 44.0},
                                         {"key": "ps", "label": "PS(TTM)",
                                          "percentile": 66.0}]}},
                "risks": [], "risk_counts": {}, "fatal": [],
                "valuation": {"rows": [], "sample": ""},
                "forecast": {"available": False, "rows": []},
                "tracking": [],
                "data_quality": {"risks_known": 14, "risks_total": 14,
                                 "coverage": 100, "institution": 0,
                                 "blockers": []},
                "decision": {"label": "数据不足，不下结论",
                             "reason": "无机构覆盖"},
            }},
        }], "2026-11-14", argv="test", db=self.db_path)
        html = client.get("/stock/sh600019?focus=quality").get_data(as_text=True)
        self.assertIn("历史分位参考", html)
        self.assertIn("45.0", html)
        self.assertIn("只展示，不参与结论", html)

    def test_cycle_detail_sections_and_charts(self):
        html = self.client().get("/stock/sh600019?focus=cycle").get_data(as_text=True)
        self.assertIn("判断① 是不是周期股", html)
        self.assertIn("相对波动通道", html)          # notes
        for canvas in ("cycGm", "cycRatio", "cycTurn"):
            self.assertIn(canvas, html)              # 三条指标历史图
        self.assertIn("5.41", html)                  # 序列进了 JSON

    def test_old_rows_fall_back_with_hint(self):
        """旧行没有 detail：给薄表 + "重跑一次就有"，不能 500 也不能空白。"""
        results_db.save("quality", [{"symbol": "sz000002", "name": "旧行",
                                     "q": 40.0, "v": 50.0, "conclusion": "回避",
                                     "reason": "旧", "risks_hit": 2}],
                        "2026-11-14", argv="test", db=self.db_path)
        html = self.client().get("/stock/sz000002?focus=quality").get_data(as_text=True)
        self.assertIn("没有明细", html)
        self.assertIn("--force", html)


class TestStockReports(WebCase):
    """工具渲染好的原版报告：存进 payload.report_html，看板原样吐出来。"""

    #: 各工具那一行至少要有这些字段，页面上的卡片/明细才不会拿到一堆空值
    BASE = {
        "top": {"code": None, "date": "2026-11-15", "ok": True, "close": 6.18,
                "pos250": -0.21, "days_since_high": 242, "phase": "反弹"},
        "quality": {"q": 50.0, "v": 60.0, "conclusion": "观察", "reason": "演示"},
        "cycle": {"is_cycle": True, "gm_std_pp": 2.3, "gm_cv": 37.0, "elasticity": -1.17,
                  "elastic_label": "weak", "position_pct": 63.7, "position_label": "high"},
    }

    def _save_with_report(self, tool: str, symbol: str, title: str, body: str):
        row = {"symbol": symbol, "name": "演示", **self.BASE[tool],
               "payload": {"report_html": _demo_report(title, body)}}
        if tool == "top":
            row["code"] = symbol
        results_db.save(tool, [row], "2026-11-15", argv="test", db=self.db_path)

    def test_report_route_serves_the_stored_html(self):
        client = self.client()                    # 先把演示库落下来，再加这一行
        self._save_with_report("quality", "sh600019", "原版质量报告", "<p>五节正文</p>")
        resp = client.get("/stock/sh600019/report/quality")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.mimetype.startswith("text/html"))
        html = resp.get_data(as_text=True)
        self.assertIn("原版质量报告", html)
        self.assertIn("五节正文", html)

    def test_stock_page_embeds_the_report_instead_of_detail_view(self):
        client = self.client()
        self._save_with_report("quality", "sh600019", "原版质量报告", "<p>五节正文</p>")
        html = client.get("/stock/sh600019?focus=quality").get_data(as_text=True)
        self.assertIn('/stock/sh600019/report/quality', html)     # iframe src
        self.assertNotIn("Q 分项", html)                          # 明细视图让位
        # 一次只摆一份：另外两个工具只有标签，没有报告 iframe
        self.assertEqual(html.count("iframe class=\"report-frame\""), 1)
        self.assertNotIn("/report/top?", html)
        self.assertNotIn("/report/cycle?", html)

    def test_tabs_switch_which_report_is_shown(self):
        client = self.client()
        for tool in ("top", "quality", "cycle"):
            self._save_with_report(tool, "sh600019", f"{tool} 报告", f"<p>{tool} 正文</p>")
        html = client.get("/stock/sh600019?focus=cycle").get_data(as_text=True)
        self.assertIn('src="/stock/sh600019/report/cycle?run=', html)     # 只有这一份报告
        self.assertNotIn('src="/stock/sh600019/report/quality', html)
        self.assertEqual(html.count('iframe class="report-frame"'), 1)
        # 三个标签都在，且当前那个带 on
        for tool, label in (("top", "顶部 · 四因子"), ("quality", "企业质量初筛"),
                            ("cycle", "周期弹性初筛")):
            self.assertIn(f'href="/stock/sh600019?focus={tool}"', html)
            self.assertIn(label, html)
        self.assertIn('class="tab on" href="/stock/sh600019?focus=cycle"', html)

    def test_report_page_drops_dashboard_chrome(self):
        """有原版报告时整页换成报告版式：不再套深蓝 hero 和四张摘要卡。"""
        client = self.client()
        self._save_with_report("cycle", "sh600019", "周期报告", "<p>正文</p>")
        html = client.get("/stock/sh600019?focus=cycle").get_data(as_text=True)
        self.assertIn('class="stock-report"', html)       # body 换成报告底色
        self.assertNotIn('class="cards"', html)           # 四张摘要卡让位
        self.assertNotIn('class="hero', html)             # 报告自己的表头才是 hero

    def test_old_row_without_report_keeps_dashboard_chrome(self):
        """旧行没有原版报告：保留看板 hero + 卡片 + 明细视图。"""
        client = self.client()
        html = client.get("/stock/sh600019?focus=cycle").get_data(as_text=True)
        self.assertNotIn('class="stock-report"', html)
        self.assertIn('class="cards"', html)
        self.assertIn('class="hero"', html)

    def test_report_route_404_when_row_has_no_report(self):
        resp = self.client().get("/stock/sh600019/report/top")
        self.assertEqual(resp.status_code, 404)

    def test_report_route_unknown_tool_is_404(self):
        self.assertEqual(self.client().get("/stock/sh600019/report/nope").status_code, 404)

    def test_report_route_by_run_id(self):
        client = self.client()
        run_id = results_db.save("quality", [{"symbol": "sh600019", "name": "演示", "q": 50.0,
                                              "payload": {"report_html": _demo_report("第一次", "A")}}],
                                 "2026-11-15", argv="test", db=self.db_path)
        self._save_with_report("quality", "sh600019", "第二次", "B")
        html = client.get(f"/stock/sh600019/report/quality?run={run_id}").get_data(as_text=True)
        self.assertIn("第一次", html)
        self.assertIn("A", html)


class TestReadOnly(WebCase):
    def test_queries_do_not_write(self):
        """看板只读：打开几个页面后，库文件的 mtime 与大小都不变。"""
        from stock_extreme_point import store
        client = self.client()                    # 先落四个工具的演示数据
        conn = store.connect(self.db_path)
        try:
            store.ensure(conn, "results")
            store.write_day(conn, "2026-11-13", [
                {"date": "2026-11-13", "symbol": "sh600519", "name": "贵州茅台",
                 "close": 1453.9, "pct_chg": 1.30, "pe_ttm": 22.12,
                 "pb_mrq": 6.70, "ps_ttm": 10.42,
                 "total_mv": 1.82e12, "circ_mv": 1.82e12}], with_stat=True)
        finally:
            conn.close()
        before = Path(self.db_path).stat()
        for url in ("/review", "/top", "/extreme", "/extreme?q=600519",
                    "/stock/sh600019", "/stock/sh600019?focus=cycle"):
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


class AnnotationStateTest(unittest.TestCase):
    """`annotation.row_to_moat` 的三种"没有垄断"状态要能分清。

    个股报告靠 `annotated` 决定要不要渲染垄断块；把「待核实」判成 True，
    等于给 4700 多家没核过的公司印了一份"垄断性"说明。
    """

    @staticmethod
    def _rec(moat_type):
        from stock_data_fetch import annotation as A
        return A.row_to_moat({"moat_type": moat_type, "verified": "",
                              "evidence": "", "controller": "", "sources": None})

    def test_unverified_is_not_annotated(self):
        rec = self._rec("待核实")
        self.assertFalse(rec["annotated"])
        self.assertEqual(rec["moat_type"], "待核实")

    def test_none_and_moat(self):
        self.assertFalse(self._rec("无标注")["annotated"])
        self.assertTrue(self._rec("技术壁垒")["annotated"])


class DemandSuffixTest(unittest.TestCase):
    """`_demand_suffix()`：合同负债怎么并进「位置」这句话。"""

    INFO = {"yoy": 0.5, "share": 0.03, "value": 1e8, "prev": 6e7}

    def _s(self, label, **kw):
        from web.app import _demand_suffix
        return _demand_suffix(label, {**self.INFO, **kw})

    def test_wording_follows_position(self):
        self.assertEqual(self._s("low")["text"], "订单回升")
        self.assertEqual(self._s("high")["text"], "订单仍强")
        self.assertEqual(self._s("low", yoy=-0.30)["text"], "订单仍弱")
        self.assertEqual(self._s("high", yoy=-0.30)["text"], "需求转弱")

    def test_immaterial_is_silent(self):
        """占营收 <2%：说它有订单变化是噪声，不如不说。"""
        self.assertIsNone(self._s("low", share=0.01))

    def test_flat_is_silent(self):
        self.assertIsNone(self._s("low", yoy=0.03))

    def test_missing_data_is_silent(self):
        self.assertIsNone(self._s("low", yoy=None))
        self.assertIsNone(self._s("low", share=None))
        self.assertIsNone(self._demand_none())

    def _demand_none(self):
        from web.app import _demand_suffix
        return _demand_suffix("low", None)

    def test_tone_matches_direction(self):
        self.assertEqual(self._s("low")["tone"], "good")
        self.assertEqual(self._s("high", yoy=-0.30)["tone"], "warn")


class ContractLiabilityTest(unittest.TestCase):
    """`db.contract_liability()` 只算两个数：同比、占年化营收。"""

    def _call(self, fin):
        from web import db
        orig = db.financials
        db.financials = lambda symbols, _db=None: {
            "sh600000": fin} if symbols else {}
        try:
            return db.contract_liability(["sh600000"])["sh600000"]
        finally:
            db.financials = orig

    def test_yoy_and_share(self):
        v = self._call({"cl": 2e8, "cl_prev": 1e8, "rev_q": 5e8})
        self.assertAlmostEqual(v["yoy"], 1.0)          # 2亿 / 1亿 - 1
        self.assertAlmostEqual(v["share"], 0.10)       # 2亿 / (5亿*4)

    def test_missing_prev_year_leaves_yoy_none(self):
        v = self._call({"cl": 2e8, "cl_prev": None, "rev_q": 5e8})
        self.assertIsNone(v["yoy"])
        self.assertAlmostEqual(v["share"], 0.10)

    def test_zero_prev_is_not_divided_by_zero(self):
        v = self._call({"cl": 2e8, "cl_prev": 0.0, "rev_q": 5e8})
        self.assertIsNone(v["yoy"])


class TurnoverSeriesTest(unittest.TestCase):
    """`/trend` 顶部那张量能图的数据源。

    这张图是页面上**唯一**读 `shared.db` 的地方（其余都读 `results.db`），
    所以两条底线要钉住：只取最后 N 周；文件/表缺失时返回空壳，让页面少画一张图，
    而不是 500。
    """

    def _make_shared(self, n: int = 200) -> str:
        import datetime as dt
        import sqlite3
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        try:
            conn.execute("create table market_index_week(symbol text, date text, "
                         "turnover real, pct_chg real, close real)")
            start = dt.date(2020, 1, 3)
            conn.executemany(
                "insert into market_index_week values(?,?,?,?,?)",
                [("sh000300", (start + dt.timedelta(weeks=i)).isoformat(),
                  float(i % 50 + 1), 0.5, 3000.0 + i * 3) for i in range(n)])
            conn.commit()
        finally:
            conn.close()
        return path

    def test_takes_last_n_weeks(self):
        from web import db as webdb
        path = self._make_shared(200)
        try:
            out = webdb.turnover_series(n=156, db=path)
        finally:
            os.remove(path)
        self.assertEqual(out["n"], 156)
        self.assertEqual(out["latest"], out["weeks"][-1]["date"])
        self.assertGreater(out["latest"], out["weeks"][0]["date"])

    def test_quantiles_are_ordered(self):
        """三条线必须 q10 < q90 ≤ q95，否则图上的虚线会自己交叉。"""
        from web import db as webdb
        path = self._make_shared(200)
        try:
            out = webdb.turnover_series(n=156, db=path)
        finally:
            os.remove(path)
        self.assertLess(out["q10"], out["q90"])
        self.assertLessEqual(out["q90"], out["q95"])

    def test_lines_match_rules(self):
        """图上的三条线必须和 `rules` 的分档口径逐字相同。

        两边各存一份（web 不 import 命令行包），所以要用测试把它们钉在一起：
        地量线比常量，另外三条比**算法**——它们都是逐周现算的，不是一个数。
        """
        from stock_market_trend import rules
        from web import db as webdb
        self.assertEqual(webdb.TURNOVER_DRY_LINE, rules.TURNOVER_DRY)
        self.assertEqual(webdb.TURNOVER_LOW_MULT, rules.TURNOVER_LOW_MULT)
        self.assertEqual(webdb.TURNOVER_HIGH_MULT, rules.TURNOVER_HIGH_MULT)
        self.assertEqual(webdb.TURNOVER_HOT_MULT, rules.TURNOVER_HOT_MULT)
        self.assertEqual(webdb.TURNOVER_HEAVY_WIN, rules.TURNOVER_HEAVY_WIN)
        path = self._make_shared(120)
        try:
            out = webdb.turnover_series(n=80, db=path)
        finally:
            os.remove(path)
        self.assertEqual(out["dry_line"], rules.TURNOVER_DRY)
        turns = [w["turnover"] for w in out["weeks"]]
        lines = rules.volume_lines(turns)
        self.assertEqual(out["heavy_line"], lines["heavy"])
        self.assertEqual(out["low_line"], lines["low"])
        self.assertEqual(out["high_line"], lines["high"])

    def test_lines_are_per_week(self):
        """三条线逐周不同：图上是三条跟着水位走的折线，不是平线。"""
        from web import db as webdb
        path = self._make_shared(200)
        try:
            out = webdb.turnover_series(n=60, db=path)
        finally:
            os.remove(path)
        for key in ("low_series", "high_series", "heavy_series"):
            self.assertEqual(len(out[key]), out["n"], key)
        self.assertEqual(out["heavy_line"], out["heavy_series"][-1])
        self.assertEqual(out["low_line"], out["low_series"][-1])
        self.assertEqual(out["high_line"], out["high_series"][-1])
        self.assertLessEqual(out["low_line"], out["high_line"])
        self.assertLessEqual(out["high_line"], out["heavy_line"])
        self.assertIsNotNone(out["median52"])

    def test_running_week_is_flagged(self):
        """本周还没走完的那根要标出来：`partial_index` 指到最后一根。

        它照样画（价格是真的），但不能按它判量能档位——0.61% 只累计了周一一天。
        """
        import datetime as dt
        import sqlite3
        from web import db as webdb

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        last = "2026-11-23"                       # 周一
        start = dt.date.fromisoformat(last) - dt.timedelta(weeks=79)
        conn = sqlite3.connect(path)
        try:
            conn.execute("create table market_index_week(symbol text, date text, "
                         "turnover real, pct_chg real, close real)")
            conn.executemany(
                "insert into market_index_week values(?,?,?,?,?)",
                [("sh000300", (start + dt.timedelta(weeks=i)).isoformat(),
                  2.0 if i < 79 else 0.61, 0.1, 3900.0 + i) for i in range(80)])
            conn.commit()
            mid = webdb.turnover_series(n=80, db=path, today="2026-11-23")
            done = webdb.turnover_series(n=80, db=path, today="2026-11-27")
        finally:
            conn.close()
            os.remove(path)
        self.assertEqual(mid["partial_index"], mid["n"] - 1)
        self.assertEqual(done["partial_index"], None)

    def test_running_week_matches_rules(self):
        """「本周至今」的判据必须和 `rules.split_partial` 一致，两边各存一份。"""
        from stock_market_trend import rules
        from web import db as webdb
        cases = [("2026-11-23", "2026-11-23"),   # 周一：未走完
                 ("2026-11-23", "2026-11-27"),   # 同一周的周五：走完
                 ("2026-11-23", "2026-11-29"),   # 周日：走完
                 ("2026-11-23", "2026-12-01"),   # 下一周：走完
                 ("2026-11-27", "2026-11-23")]   # 数据超前：不算
        for day, today in cases:
            with self.subTest(day=day, today=today):
                weeks = [{"date": day, "turnover": 1.0}]
                _, part = rules.split_partial(weeks, today)
                self.assertEqual(part is not None, webdb._running_week(day, today))

    def test_missing_file_returns_empty(self):
        from web import db as webdb
        out = webdb.turnover_series(db="/nonexistent/shared.db")
        self.assertEqual(out["weeks"], [])
        self.assertIsNone(out["q90"])
        self.assertIsNone(out["latest"])
        # 图还能画：地量线是常量，其余三条没有数据就不画
        self.assertEqual(out["dry_line"], webdb.TURNOVER_DRY_LINE)
        for key in ("low_line", "high_line", "heavy_line"):
            self.assertIsNone(out[key], key)
        for key in ("low_series", "high_series", "heavy_series"):
            self.assertEqual(out[key], [], key)

    def test_price_series_aligns_with_turnover(self):
        """周收盘和换手必须同长同序——两条线共用一根横轴，错一位整张图就错。"""
        from web import db as webdb
        path = self._make_shared(200)
        try:
            out = webdb.turnover_series(n=156, db=path)
        finally:
            os.remove(path)
        self.assertEqual(len(out["weeks"]), out["n"])
        for w in out["weeks"]:
            self.assertIsNotNone(w["close"])
        self.assertEqual(out["latest_close"], out["weeks"][-1]["close"])
        self.assertLessEqual(out["close_min"], out["close_max"])

    def test_week_without_close_keeps_the_week(self):
        """个别周没收盘价时点位给 null（图上是个断点），整周不能被吞掉。"""
        import sqlite3
        from web import db as webdb
        path = self._make_shared(80)
        try:
            conn = sqlite3.connect(path)
            conn.execute("update market_index_week set close=null where date="
                         "(select max(date) from market_index_week)")
            conn.commit()
            conn.close()
            out = webdb.turnover_series(n=60, db=path)
        finally:
            os.remove(path)
        self.assertEqual(out["n"], 60)
        self.assertIsNone(out["weeks"][-1]["close"])
        self.assertIsNone(out["latest_close"])
        self.assertIsNotNone(out["latest_value"])


class DaySeriesTest(unittest.TestCase):
    """`/trend` 量能图切到"日线"时的数据源（`shared.db.market_index_day`）。

    日线是**另一套口径**，不是"更细的周线"：日换手率一天约顶五分之一，
    所以四条线要按 252 个交易日重算，地量线也从 1% 换成 0.2%。
    这里的断言只守两件事：口径别串（不能拿周常量去算日线）、没原件时别炸。
    """

    def _make_shared(self, n: int = 300) -> str:
        import datetime as dt
        import sqlite3
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        try:
            conn.execute("create table market_index_day(symbol text, date text, "
                         "turnover real, pct_chg real, close real)")
            start = dt.date(2025, 1, 2)
            rows, d = [], start
            while len(rows) < n:
                if d.weekday() < 5:
                    i = len(rows)
                    rows.append(("sh000300", d.isoformat(),
                                 0.4 + (i % 10) * 0.02, 0.1, 3900.0 + i))
                d += dt.timedelta(days=1)
            conn.executemany("insert into market_index_day values(?,?,?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()
        return path

    def test_takes_last_n_days(self):
        from web import db as webdb
        path = self._make_shared(300)
        try:
            out = webdb.day_series(db=path)
        finally:
            os.remove(path)
        self.assertTrue(out["available"])
        self.assertEqual(out["n"], webdb.TURNOVER_DAYS)
        self.assertEqual(len(out["points"]), out["n"])
        self.assertEqual(out["latest"], out["points"][-1]["date"])
        for key in ("low_series", "high_series", "heavy_series"):
            self.assertEqual(len(out[key]), out["n"], key)

    def test_day_lines_use_day_window_and_floor(self):
        """日线必须用日口径：252 日窗口 + 0.2% 地量线 + 0.01% 粒度。

        用周口径（52 / 1.0 / 0.1）算会得到完全不同的数——这条断言就是防止
        `_volume_lines()` 的默认值被顺手复用到日线上。

        取 `n = 252` 不是随手写的：`day_series()` 每次多抓一个窗口再截断，
        只有 n 正好等于窗口时，"最后一根的窗口"才与"截断后的整条序列"重合，
        才能拿同一条序列去对期望值（否则会差两天、中位数就差一格）。
        """
        from web import db as webdb
        path = self._make_shared(300)
        try:
            out = webdb.day_series(n=webdb.TURNOVER_DAY_WIN, db=path)
        finally:
            os.remove(path)
        self.assertEqual(out["dry_line"], webdb.TURNOVER_DAY_DRY_LINE)
        turns = [p["turnover"] for p in out["points"]]
        day = webdb._volume_lines(turns, win=webdb.TURNOVER_DAY_WIN,
                                  dry=webdb.TURNOVER_DAY_DRY_LINE,
                                  step=webdb._DAY_STEP,
                                  heavy_step=webdb._DAY_HEAVY_STEP)
        self.assertEqual(out["low_line"], day["low"])
        self.assertEqual(out["high_line"], day["high"])
        self.assertEqual(out["heavy_line"], day["heavy"])
        # 日口径取整到 0.01%，周口径取整到 0.1%：同一个中位数两边不该相等
        week = webdb._volume_lines(turns)
        self.assertNotEqual(out["low_line"], week["low"])

    def test_missing_table_returns_unavailable(self):
        """没有日K 表（还没跑过新版 trend）→ 页面把"日"置灰，不是画空图也不是 500。"""
        import sqlite3
        from web import db as webdb
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        sqlite3.connect(path).close()
        try:
            out = webdb.day_series(db=path)
        finally:
            os.remove(path)
        self.assertFalse(out["available"])
        self.assertEqual(out["points"], [])
        self.assertEqual(out["dry_line"], webdb.TURNOVER_DAY_DRY_LINE)
        self.assertFalse(webdb.day_series(db="/nonexistent/shared.db")["available"])

    def test_end_window_and_rank(self):
        """区间浏览器的两个核心：`end` 取往前 n 根、每点 `rank` 随点不随窗口。"""
        from web import db as webdb
        path = self._make_shared(300)
        try:
            # 默认：最后 250 根
            full = webdb.day_series(db=path)
            # 只看"截至倒数第 20 根"那段，往前数 100 根
            cut = webdb.day_series(n=100, end=full["points"][-20]["date"], db=path)
        finally:
            os.remove(path)
        self.assertTrue(cut["available"])
        self.assertEqual(cut["n"], 100)
        self.assertEqual(cut["latest"], full["points"][-20]["date"])
        self.assertEqual(cut["first"], cut["points"][0]["date"])
        # 每点都带 rank，且 rank 是 0~100 的整数
        for p in cut["points"]:
            self.assertIn("rank", p)
            self.assertTrue(0 <= p["rank"] <= 100)
        # 首尾边界字段：db_first / db_last 供前端置灰翻页按钮
        self.assertIsNotNone(cut["db_first"])
        self.assertIsNotNone(cut["db_last"])
        self.assertEqual(cut["db_last"], full["db_last"])

    def test_start_window_rolls_forward(self):
        """只给 `start`：从那天往后取，给"后一段"翻页用。"""
        from web import db as webdb
        path = self._make_shared(300)
        try:
            full = webdb.day_series(db=path)
            # 从第 100 根往后翻 80 根
            nxt = webdb.day_series(n=80, start=full["points"][100]["date"], db=path)
        finally:
            os.remove(path)
        self.assertTrue(nxt["available"])
        self.assertEqual(nxt["n"], 80)
        self.assertEqual(nxt["first"], full["points"][100]["date"])
        # 边界：已经从最新往后翻，db_last 还是全表最大日期
        self.assertEqual(nxt["db_last"], full["db_last"])


if __name__ == "__main__":
    unittest.main()
