"""Flask 应用：把 results.db 渲染成看板。

为什么是服务端渲染（不是前后端分离）
------------------------------------
数据量是"几十只票"，一次查询 2ms。为了这点数据引一个前端框架、
再写一套 fetch/loading/empty 状态，除了把 `db.py` 的 SQL 搬到浏览器端，
什么也没换来。Jinja 直接吐 HTML，Chart.js 只负责画那几张图。

页面分工
--------
    /            跳转到 /batches（首页：批次表 + A股大势；原来的「概览」页已删）
    /batches     首页：全流程总览 + 每周五盘后那一批票四步齐不齐；顶部是 A股大势横幅
    /select      选股：池内换手分位（排序依据）+ 行业分布（名称不可点）
    /cycle       周期：三个判断的分布 + 毛利率波动 × 弹性散点
    /quality     质量：Q × V 散点（结论用颜色区分）+ 结论分布
    /top         顶部：距 250 日高点的横条（谁在高位）+ 四因子状态矩阵
    /review      综合评估：硬门槛（只做标识）+ 赔率分（排序），标出「可以开始研究」的票
    /extreme       冷热极点：**results.db** 的全市场价格/估值（按日横截面 + 每日摘要）
    /book        原书学习：《成功投资的五条法则》全书页（static 下的自包含 HTML，
                 只在末尾补一个「返回看板」浮标，正文一个字不动）
    /dataflow    本项目代码执行流程：仓库根目录的 dataflow.html（数据流转与筛选条件
                 全景图，自包含 HTML）原样吐出，同样只补一个返回浮标
    /stock/<code> 个股：四边结论并排 + 旁证/周期（**只摆最近一份**，取 `run.id` 最大的那条）
                 `?focus=cycle|quality|top` 决定哪一块排最上面（从哪个工具页点进来的）

两种"时间"要分开看
------------------
* **批次**（`run.target_date`）= 那个周五。四个工具同一天的结果是一批，
  页面上永远整批看，不把周三单跑的一只票掺进周五的统计。
* **运行**（`run.started_at`）= 实际跑的时刻。同一只票可能被跑过很多次，
  个股页**只取 `run.id` 最大的那一次**（周三单只重算过，就不该再显示周五那批的旧数）。
  早期有一版把历次记录按时间排成一张表，2026-11-15 去掉了：一只票几十条，
  看完仍然不知道该信哪条，不如只摆最新那份。

工具页都接受两个参数：`?date=YYYY-MM-DD` 看指定批次，`?run=<id>` 看那一次运行；
不给就停在最新批次。页面之间靠股票代码串联，没有登录、没有写操作。
"""
from __future__ import annotations

import datetime as dt
import os
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from . import db, review
from stock_review_assess import config as review_config

#: 状态词 → 色调（与四工具报告的 CSS 变量同一套语言）
TONE_MAP = (
    ("天量", "hot"), ("放量", "warn"),
    ("缩量", "ok"), ("地量", "good"), ("平稳", "muted"),
    ("高位", "warn"), ("偏高", "warn"), ("过热", "warn"), ("偏热", "warn"),
    ("加速", "warn"), ("命中", "warn"), ("回避", "warn"), ("贵", "warn"),
    ("低位", "muted"), ("偏低", "muted"), ("偏弱", "muted"), ("走平", "muted"),
    ("减速", "muted"), ("未触发", "muted"),
    ("中位", "info"), ("中性", "info"), ("观察", "info"), ("加速", "info"),
    ("低估", "good"), ("强", "good"), ("通过", "good"),
)


def tone(state: str | None) -> str:
    """把一个状态词映射成色调。认不出来的一律 info（不用 warn，免得虚报风险）。"""
    text = str(state or "")
    for word, tone_name in TONE_MAP:
        if word in text:
            return tone_name
    return "info"


def _fmt_pct(value, digits=1):
    if value is None:
        return "—"
    return f"{value * 100:+.{digits}f}%"


def _fmt_num(value, digits=1):
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_ratio(value, digits=2):
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_yi(value, digits=0):
    """元 → 亿元。市值动辄 1.1e14，用"元"写在表里没人读得出来。"""
    if value is None:
        return "—"
    try:
        return f"{float(value) / 1e8:,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _demand_suffix(label: str | None, info: dict | None) -> dict | None:
    """把合同负债（预收款）并进「位置」这个结论，做成一句限定语。

    为什么不单独占一列：合同负债是**需求端**的量，单独摆一列既没人看、
    也读不出结论；只有和「位置」放在一起才有意义——「低位 + 订单回升」
    和「低位 + 订单还在掉」是两回事。

    为什么不改分位本身：判断③ 的分位是产能周转率（供给端）的历史位置，
    算法是干净的，塞一个资产负债表项进去这个数就没法复算了。所以做法和
    工具里已有的 `capacity_direction`（扩产中 / 产能在退）完全一样：
    **只加限定语，不改标签**。

    门限：占比 ≥ 2%（这批 113 只的中位数是 1.5%，压在略高一点的位置）才认，
    同比 ≥ +20% 记回升、≤ −10% 记走弱。比 `/review` 那道 5% 松——那边 CL 要让「供需」判过，
    是门槛；这边只是一句话，严了就等于没有。
    """
    if not info:
        return None
    yoy, share = info.get("yoy"), info.get("share")
    if yoy is None or share is None or share < 0.02:
        return None
    if yoy >= review_config.GATE3_CL_YOY_MIN:
        direction, tone = "up", "good"
    elif yoy <= review_config.GATE3_CL_YOY_FAIL:
        direction, tone = "down", "warn"
    else:
        return None

    # 措辞跟着位置走：低位讲"能不能起来了"，高位讲"是不是还在撑着"
    text = {
        ("low", "up"): "订单回升",
        ("low", "down"): "订单仍弱",
        ("neutral", "up"): "订单回升",
        ("neutral", "down"): "订单走弱",
        ("high", "up"): "订单仍强",
        ("high", "down"): "需求转弱",
    }.get((label, direction))
    if text is None:
        return None
    tip = (f"合同负债 {_fmt_yi(info['value'], 1)} 亿（去年同季 "
           f"{_fmt_yi(info.get('prev') or 0, 1)} 亿），同比 {yoy:+.0%}，"
           f"占年化营收 {share:.1%}。\n"
           f"这是需求端的方向，只给「位置」加一句限定语，"
           f"不改变分位和标签（位置仍由产能周转率分位决定）。")
    return {"text": text, "tone": tone, "tip": tip}


def _quote_url(symbol):
    """`sh600519` / `sz002286` / `bj920992` → 东方财富行情页。

    东财的路径并不统一：沪深是 `quote.eastmoney.com/sh600519.html`，
    北交所是 `quote.eastmoney.com/bj/920992.html`（少了斜杠会 404）。
    末尾 `#fullScreenChart` 是"打开就是全屏 K 线"，与用户手点进去的页面一致。

    认不出的代码返回空串：模板据此不渲染链接，而不是给一个必然 404 的地址。
    """
    s = str(symbol or "").strip().lower()
    if len(s) < 3:
        return ""
    code = s[2:]
    if not code.isdigit():
        return ""
    if s.startswith("bj"):
        return f"https://quote.eastmoney.com/bj/{code}.html#fullScreenChart"
    if s.startswith(("sh", "sz")):
        return f"https://quote.eastmoney.com/{s}.html#fullScreenChart"
    return ""


def _fmt_pe(value, digits=2):
    """PE(TTM)：负值是亏损，写「亏损」而不是一个看不懂的负号数字。

    `-70,160.44` 这种数只会让人以为"便宜到极致"——分子是负的，比值本身
    没有意义。缺数据仍然是 `—`：亏损和没数据是两回事。
    """
    if value is None:
        return "—"
    try:
        return "亏损" if float(value) <= 0 else f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pos(value, digits=2):
    """只对正值有意义的比率（PB / PS）：负值给 `—`，不摆一个负数上去。

    与 `price_stat` 算中位数只取正值、`db.price_rows` 把 `<= 0` 排到最后
    是同一套口径：这三处对"什么算有效估值"必须一致。
    """
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}" if float(value) > 0 else "—"
    except (TypeError, ValueError):
        return str(value)


def _fmt_spct(value, digits=2):
    """带符号的百分数：`+2.31%` / `-1.07%`。冷热页要看方向，符号不能省。

    恰好为 0 时不带符号（`0.00%`）——"+0.00%" 看着像涨了一点点。
    """
    if value is None:
        return "—"
    try:
        num = float(value)
        return f"{num:+.{digits}f}%" if abs(num) >= 0.005 else f"{0.0:.{digits}f}%"
    except (TypeError, ValueError):
        return str(value)


#: 周期三态的中文名。库里存的是英文枚举（low/high/neutral/na），
#: 页面上一律显示中文，别把两个词表混着摆。
POSITION_LABEL = {"low": "低位", "high": "高位", "neutral": "中位", "na": "不适用"}


def _poslabel(value):
    """位置枚举 → 中文；认不出来的原样返回（新枚举不该在页面上变成空白）。"""
    text = str(value or "").strip()
    if not text:
        return "—"
    return POSITION_LABEL.get(text.lower(), text)


def _cycle_state(value):
    """顶部那张卡里的周期位置：`周期·高位` → `高位`。

    列头已经写着「周期位置」，单元格里再重复一次「周期·」只是噪音。
    """
    text = str(value or "").strip()
    for sep in ("·", "・", "|"):
        if sep in text:
            text = text.split(sep, 1)[1]
    return _poslabel(text) if text else "—"


def _detail(row):
    """结果行 → 工具写进 payload 的那块「明细」（Q/V 分项、风险 14 条、指标序列…）。

    旧行没有这个键，返回 None：模板据此降级成薄表 + 一句"重跑一次就有"。
    """
    if not row:
        return None
    return (db.inner(row) or {}).get("detail") or None


def _has_report(row):
    """这一行里是否存着工具渲染好的原版报告。"""
    return bool(db.report_html(row))


def _date(value):
    return str(value or "")[:10]


#: 星期几。批次基本都在周五跑，把"周五"写出来比让人自己数日子直观。
_WEEK = "一二三四五六日"


def _weekday(value):
    """`2026-11-13` → `周五`；解析不了就给空串（不猜）。"""
    try:
        return "周" + _WEEK[dt.date.fromisoformat(str(value)[:10]).weekday()]
    except (TypeError, ValueError):
        return ""


def _clock(value):
    """ISO 时间戳 → 顺手看的 `11-15 15:23`。"""
    text = str(value or "")
    if len(text) < 16:
        return text or "—"
    return text[5:10].replace("-", "-") + " " + text[11:16]


def _iso_or_none(value):
    """查询参数里的日期：合法就归一成 `YYYY-MM-DD`，非法/空就返回 None。

    历史筛选是只读查询，参数脏了不该 500；忽略它、按“不限”处理，
    页面上再让用户重选即可。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except (TypeError, ValueError):
        return None


#: 《成功投资的五条法则》原书学习页：整本书是一个 5.5MB 的自包含 HTML
#: （样式、图、脚本全内联，没有外部依赖），放在 `static/` 下随看板一起发布。
#: 它不走静态路由直接访问，只因为要在页面里补一个「返回看板」的浮标——
#: 书页是独立排版、没有看板顶栏，读完除了浏览器后退没有别的出口。
BOOK_FILE = Path("static") / "five-rules-book-full-learning.html"

#: 塞在书页 `</body>` 前的浮标。刻意用书页自己的 CSS 变量上色，换主题
#: （海盐云纱 / 黑白水墨 / 竹月清）它会跟着变；位置选左下角，因为右下角
#: 被「术语提问」的悬浮球占了、右上角是书页自己的工具条。
BACK_HOME = """
<style>
.back-home { position: fixed; left: 20px; bottom: 20px; z-index: 121;
  display: inline-flex; align-items: center; gap: 6px; padding: 9px 15px;
  border: 1px solid var(--line); border-radius: 999px;
  background: var(--surface-strong); color: var(--ink);
  font: 600 13px/1 var(--font-display); text-decoration: none;
  box-shadow: 0 6px 18px rgba(40, 52, 76, .16); opacity: .92;
  transition: opacity .18s ease, transform .18s ease, border-color .18s ease; }
.back-home:hover { opacity: 1; transform: translateY(-1px);
  color: var(--ink); border-color: var(--gold); }
.back-home:focus-visible { outline: 2px solid var(--blue); outline-offset: 3px; }
@media print { .back-home { display: none; } }
</style>
<a class="back-home" href="/batches" title="返回 FRSI 看板">&larr; 返回看板</a>
"""


def _with_back_home(html: str, snippet: str = BACK_HOME) -> str:
    """在整页 HTML 收尾前插一段浮标；找不到 `</body>` 就原样返回（宁可不加，也不弄坏页面）。"""
    at = html.rfind("</body>")
    if at < 0:
        return html
    return html[:at] + snippet + html[at:]


#: 代码执行流程（dataflow.html）在仓库根目录，不在 web/ 下——它是给人看的一页图，
#: 由生成脚本直接写到根目录，看板只负责原样端出去。这里读根目录那一份，
#: 不再往 static/ 复制副本：图是会重画的，副本最容易忘记更新。
FLOW_HTML = "dataflow.html"

#: 流程图那一页没有看板顶栏（它有自己的吸顶条），所以同样补一个浮标。
#: 它不像书页那样有一套 CSS 变量可借（dataflow.html 全是硬编码色值），
#: 所以这里把颜色写死：墨绿 #2f6152 是那张图自己的强调色，底白、浅绿描边。
FLOW_BACK = """
<style>
.flow-back { position: fixed; left: 20px; bottom: 20px; z-index: 99;
  display: inline-flex; align-items: center; gap: 6px; padding: 9px 15px;
  border: 1px solid #b7cfc5; border-radius: 999px;
  background: #ffffff; color: #24503f;
  font: 700 13px/1 "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  text-decoration: none; box-shadow: 0 6px 18px rgba(31, 35, 40, .18);
  transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease; }
.flow-back:hover { transform: translateY(-1px); border-color: #2f6152;
  box-shadow: 0 9px 22px rgba(31, 35, 40, .22); }
.flow-back:focus-visible { outline: 2px solid #2f6152; outline-offset: 3px; }
@media print { .flow-back { display: none; } }
</style>
<a class="flow-back" href="/batches" title="返回 FRSI 看板">&larr; 返回看板</a>
"""


def create_app(db_path: str | Path | None = None) -> Flask:
    here = Path(__file__).resolve().parent
    app = Flask(__name__, template_folder=str(here / "templates"),
                static_folder=str(here / "static"))
    app.json.ensure_ascii = False
    app.config["DB_PATH"] = db_path or os.environ.get("STOCK_RESULTS_DB") or db.DEFAULT_DB

    # Jinja 里要用的格式化/配色工具，一次性注入，模板里不用 import
    app.jinja_env.filters.update(pct=_fmt_pct, num=_fmt_num, ratio=_fmt_ratio,
                                 date=_date, clock=_clock, tone=tone,
                                 weekday=_weekday, yi=_fmt_yi, spct=_fmt_spct,
                                 pe=_fmt_pe, pos=_fmt_pos, quoteurl=_quote_url,
                                 poslabel=_poslabel, cycle_state=_cycle_state,
                                 detail=_detail, hasreport=_has_report)
    app.jinja_env.globals.update(TOOLS=db.TOOLS, ORDER=db.ORDER, TODAY=dt.date.today,
                                 KIND_LABEL=db.KIND_LABEL)

    @contextmanager
    def conn():
        """请求级连接：`with sqlite3.Connection` 只提交事务、**不关闭**，
        拿它当上下文管理器会让每个请求漏一个连接（跑久了报
        `ResourceWarning: unclosed database`）。这里显式关。"""
        c = db.connect(app.config["DB_PATH"])
        try:
            yield c
        finally:
            c.close()

    @app.context_processor
    def inject_globals():
        # 顺序就是看板的取用顺序：先挑批次，再看四个工具，最后看汇总排序。
        # 「概览」和「运行记录」删掉了——概览那张合并表就是综合评估的全表，
        # 运行台账原打算挂在个股页的「历次结果」里，2026-11-15 也一起去掉了；
        # 现在查"某一次运行"从首页点进那只票，或直接用 `?run=<id>` 定位。
        return {"nav": [
            ("/batches", "首页"), ("/select", "选股"),
            ("/cycle", "周期"), ("/quality", "质量"), ("/top", "顶部"),
            ("/review", "综合评估"), ("/extreme", "冷热极点"),
            ("/trend", "A股大势"), ("/book", "原书学习"),
        ]}

    def _pick(conn):
        """页面当前的批次与运行：`?date=` 看批次，`?run=` 看某一次运行。"""
        date = (request.args.get("date") or "").strip() or None
        try:
            run_id = int(request.args.get("run") or 0) or None
        except ValueError:
            run_id = None
        if run_id:
            meta = db.run_row(conn, run_id)
            if meta:
                date = date or meta["target_date"]
        return date, run_id

    def _page(template: str, tool: str | None = None):
        """工具页的公共壳：批次选择器 + 运行信息 + 结果行。"""
        with conn() as c:
            date, run_id = _pick(c)
            meta, rows = (db.scope(c, tool, date=date, run_id=run_id)
                          if tool else (None, []))
            if meta:
                date = meta["target_date"]
            date = date or db.batch_date(c)
            return render_template(
                template, rows=rows, meta=meta,
                batches=db.batches(c), cur_date=date,
                summary=db.summary(c, date), counts=db.counts(c, date))

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404,
                               msg="这个页面不存在。上面的导航能回去。"), 404

    @app.errorhandler(FileNotFoundError)
    def no_db(_e):
        return render_template(
            "error.html", code=500,
            msg="还找不到 results.db：先把四个工具跑一遍（./run.py all），"
                "结果会自动写进库。"), 500

    @app.route("/")
    def index():
        """根路径进首页（2026-11-20 用户指定）。保留 `/` 是为了旧书签和
        `./run.py web` 打印的那个地址都还能用。

        顺序是「先挑批次，再看四个工具，最后看汇总」——首页就是这条链的
        第一格，所以它当门面：四步齐不齐、缺哪一步、要不要补跑，一眼看完。
        """
        return redirect(url_for("batches"))

    @app.route("/review")
    def review_page():
        """综合评估：不查库里的新表，全在 `stock_review_assess.build()` 里现算。

        四个工具的结论本来就各存一份（`merged()` 已经把它们并到一只票一行），
        这一页要的是**规则**，不是新数据——规则在 `stock_review_assess/`（与
        `stock_kline_screen` 平级的顶层包），这里只负责挑批次、把三类外部
        数据读好交过去：

            垄断性    `shared.db` 的 `annotation`（`db.moats()`）
            财务      `shared.db` 的 `finance`（`db.financials()`，门槛用）

        `stock_review_assess` 自己不连库：它只吃字典，所以能单测、也能换数据源。
        """
        with conn() as c:
            date, _run_id = _pick(c)
            date = date or db.batch_date(c)
            rows = db.merged(c, date)
            report = review.build(
                rows, db.moats(), db.financials([r["symbol"] for r in rows]))
            return render_template("review.html", meta=None, cur_date=date,
                                   batches=db.batches(c), review=report,
                                   summary=db.summary(c, date),
                                   counts=db.counts(c, date))

    @app.route("/extreme")
    def extreme_page():
        """冷热极点：`results.db` 的 `price`（全市场价格/估值）+ `price_stat`（每日摘要）。

        与四个工具页**不共用** `_bar.html` 那个批次选择器：它的选项是"哪天跑过工具"，
        这里是"哪天有全市场价格"，两者天然不同步——`price` 每个交易日都能有，
        而四个工具一周才跑一批。混用一个下拉会让人误以为"没跑工具的日子没行情"。

        `?q=` 搜索把页面切成三种形态（唯一命中看历史 / 多命中给清单 / 没命中），
        判断全在 `db.extreme_page()` 里，这里只负责把参数交过去。
        """
        data = db.extreme_page(
            date=(request.args.get("date") or "").strip() or None,
            q=(request.args.get("q") or "").strip(),
            sort=(request.args.get("sort") or "pct").strip(),
            order=(request.args.get("order") or "desc").strip(),
            start=(request.args.get("start") or "").strip() or None,
            end=(request.args.get("end") or "").strip() or None,
            db=app.config["DB_PATH"])
        return render_template("extreme.html", **data)

    @app.route("/batches")
    def batches():
        with conn() as c:
            return render_template("batches.html", batches=db.batches(c),
                                   cur_date=db.batch_date(c), meta=None,
                                   regime=db.regime(c),
                                   summary=db.summary(c), counts=db.counts(c))

    @app.route("/trend")
    def trend_page():
        """大势历史：按基准日查全部周记。

        首页只摆最近 8 周（`db.regime` 默认 limit），这里不截断；`start` /
        `end` 是闭区间，口径是**基准日**，不是 `data_asof`。所以筛选结果
        不会因为中证晚一天而改变——那一天的判断仍然属于跑它的那个周五。
        """
        start = _iso_or_none(request.args.get("start"))
        end = _iso_or_none(request.args.get("end"))
        if start and end and start > end:
            start, end = end, start
        with conn() as c:
            return render_template(
                "trend.html", regime=db.regime(c, limit=None,
                                                start=start, end=end),
                turnover=db.turnover_series(),
                start=start or "", end=end or "",
                cur_date=db.batch_date(c), batches=db.batches(c), meta=None)

    @app.route("/book")
    def book_page():
        """原书学习：《成功投资的五条法则》全书页。

        书页是独立生成的单文件 HTML，这里只做两件事——读出来、补一个返回浮标。
        结果按文件 mtime 缓存：5.5MB 的正文没必要每个请求重读一遍、重拼一遍；
        重新生成书页后 mtime 一变，缓存自然失效。
        """
        path = here / BOOK_FILE
        try:
            mtime = path.stat().st_mtime
        except OSError:
            abort(404, description="原书学习页还没生成（缺 static/ 下的书页 HTML）")
        cache = app.config.get("BOOK_CACHE")
        if not cache or cache[0] != mtime:
            cache = (mtime, _with_back_home(path.read_text(encoding="utf-8")))
            app.config["BOOK_CACHE"] = cache
        return Response(cache[1], mimetype="text/html")

    @app.route("/dataflow")
    def dataflow_page():
        """本项目代码执行流程：仓库根目录的 dataflow.html。

        和 /book 一个套路——整页原样端出，只在末尾补一个返回浮标（这页也没有顶栏）。
        按 mtime 缓存：图重画一次就失效，不用手工清缓存。
        """
        path = here.parent / FLOW_HTML
        try:
            mtime = path.stat().st_mtime
        except OSError:
            abort(404, description="代码执行流程图还没生成（缺仓库根目录的 dataflow.html）")
        cache = app.config.get("FLOW_CACHE")
        if not cache or cache[0] != mtime:
            cache = (mtime, _with_back_home(path.read_text(encoding="utf-8"), FLOW_BACK))
            app.config["FLOW_CACHE"] = cache
        return Response(cache[1], mimetype="text/html")

    @app.route("/select")
    def select():
        return _page("select.html", "select")

    @app.route("/cycle")
    def cycle():
        """周期弹性。比 `_page()` 多一步：给每行挂上合同负债（预收款）。

        这一列**只做展示**：周期工具的判据只有三个（是不是周期股 / 弹性强弱 /
        位置），合同负债不参与其中任何一条——工具那边的阈值契约也不动。
        放这一列是因为产能分位只回答"供给端紧不紧"，预收款回答"需求端在不在
        回来"，周期底部两个方向一起看才有意义。
        """
        with conn() as c:
            date, run_id = _pick(c)
            meta, rows = db.scope(c, "cycle", date=date, run_id=run_id)
            if meta:
                date = meta["target_date"]
            date = date or db.batch_date(c)
            cl = db.contract_liability([r["symbol"] for r in rows]) if rows else {}
            for r in rows:
                r["demand"] = _demand_suffix(r.get("position_label"),
                                             cl.get(r["symbol"]))
            return render_template(
                "cycle.html", rows=rows, meta=meta,
                batches=db.batches(c), cur_date=date,
                summary=db.summary(c, date), counts=db.counts(c, date))

    @app.route("/quality")
    def quality():
        return _page("quality.html", "quality")

    @app.route("/top")
    def top():
        with conn() as c:
            date, run_id = _pick(c)
            meta, rows = db.scope(c, "top", date=date, run_id=run_id)
            for r in rows:
                r["extras"] = (r.get("data") or {}).get("extras") or {}
                r["gaps"] = (r.get("data") or {}).get("gaps") or []
                r["views"] = (r.get("data") or {}).get("views") or []
            date = (meta or {}).get("target_date") or date or db.batch_date(c)
            return render_template("top.html", rows=rows, meta=meta,
                                   batches=db.batches(c), cur_date=date,
                                   summary=db.summary(c, date),
                                   counts=db.counts(c, date))

    #: 从哪个工具页点进来，就把那一块排最上面。四因子原来永远第一，于是
    #: 从周期/质量页点进来的人看到的也先是顶部判断——2026-11-15 提的那个"都
    #: 跳到同一个页面"其实就是这个错觉。
    # 个股页一次只摆一份报告，用标签切换。标签顺序固定（跟导航一致），
    # `?focus=` 只决定"打开时停在哪一页"——从质量页点进来就停在质量。
    TAB_TOOLS = ["top", "quality", "cycle"]
    TAB_LABEL = {"top": "顶部 · 四因子", "quality": "企业质量初筛",
                 "cycle": "周期弹性初筛"}

    @app.route("/stock/<symbol>")
    def stock(symbol):
        focus = (request.args.get("focus") or "").strip()
        if focus not in TAB_TOOLS:
            focus = None
        active = focus or TAB_TOOLS[0]
        with conn() as c:
            rows = {t: db.symbol_row(c, t, symbol) for t in db.ORDER}
            if not any(rows.values()):
                abort(404)
            name = next((r.get("name") for r in rows.values() if r and r.get("name")), symbol)
            top_row = rows.get("top") or {}
            top_data = top_row.get("data") or {}
            return render_template(
                "stock.html", symbol=symbol, name=name, rows=rows,
                # views_full 比 views 多出 headline / metrics：单只页那张卡要能
                # 说清楚"为什么是这个结论"，只有状态词是不够的。
                views=top_data.get("views_full") or top_data.get("views") or [],
                extras=top_data.get("extras") or {},
                gaps=top_data.get("gaps") or [],
                active=active, tabs=TAB_TOOLS, tab_label=TAB_LABEL,
                active_row=rows.get(active),
                active_label=TAB_LABEL.get(active, ""))


    @app.route("/stock/<symbol>/report/<tool>")
    def stock_report(symbol, tool):
        """原版单股报告（工具渲染、存在 payload.report_html 里）。

        看板只做一件事：解压 + 原样返回。`?run=` 指定哪一次运行：不给就取这只票
        最近一份，给了就回那一次当时那份报告。
        """
        if tool not in db.TOOLS:
            abort(404)
        try:
            run_id = int(request.args.get("run") or 0) or None
        except ValueError:
            run_id = None
        with conn() as c:
            row = (db.row_of_run(c, tool, run_id, symbol) if run_id
                   else db.symbol_row(c, tool, symbol))
            html = db.report_html(row)
        if not html:
            return render_template(
                "error.html", code=404,
                msg="这只票的这条结果里没有原版报告（2026-11-15 之前跑的行，"
                    "或那次渲染失败）。个股页上还能看四边结论和工具明细。"), 404
        return Response(html, mimetype="text/html")


    return app


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m web", description="results.db 的本地看板（Flask + Chart.js）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址，默认 127.0.0.1（只本机可访问）")
    ap.add_argument("--port", type=int, default=5000, help="端口，默认 5000")
    ap.add_argument("--db", default=None, help="结果库路径（默认 results.db）")
    ap.add_argument("--debug", action=argparse.BooleanOptionalAction, default=True,
                    help="Flask 调试模式（默认开，改模板/代码自动重载；--no-debug 关闭）")
    a = ap.parse_args(argv)

    app = create_app(a.db)
    print(f"看板：http://{a.host}:{a.port}   （结果库 {app.config['DB_PATH']}）")
    app.run(host=a.host, port=a.port, debug=a.debug)
    return 0


if __name__ == "__main__":                        # pragma: no cover
    raise SystemExit(main())
