"""Flask 应用：把 results.db 渲染成看板。

为什么是服务端渲染（不是前后端分离）
------------------------------------
数据量是"几十只票"，一次查询 2ms。为了这点数据引一个前端框架、
再写一套 fetch/loading/empty 状态，除了把 `db.py` 的 SQL 搬到浏览器端，
什么也没换来。Jinja 直接吐 HTML，Chart.js 只负责画那几张图。

页面分工
--------
    /            跳转到 /review（原来的「概览」页已删，合并表就是综合评估的全表）
    /batches     批次：每周五盘后那一批票，四步齐不齐一目了然
    /select      选股：池内换手分位（排序依据）+ 行业分布（名称不可点）
    /cycle       周期：三个判断的分布 + 毛利率波动 × 弹性散点
    /quality     质量：Q × V 散点（结论用颜色区分）+ 结论分布
    /top         顶部：距 250 日高点的横条（谁在高位）+ 四因子状态矩阵
    /review      综合评估：四边结论拼成优先级排序，标出「可以开始研究」的票
    /price       冷热时点：**results.db** 的全市场价格/估值（按日横截面 + 每日摘要）
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

#: 状态词 → 色调（与四工具报告的 CSS 变量同一套语言）
TONE_MAP = (
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
        # 现在查"某一次运行"从批次页点进那只票，或直接用 `?run=<id>` 定位。
        return {"nav": [
            ("/batches", "批次"), ("/select", "选股"),
            ("/cycle", "周期"), ("/quality", "质量"), ("/top", "顶部"),
            ("/review", "综合评估"), ("/price", "冷热时点"),
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
        """根路径直接进综合评估（页签里不再有「概览」）。保留 `/` 是为了
        旧书签、`./run.py web` 打印的那个地址都还能用。"""
        return redirect(url_for("review_page"))

    @app.route("/review")
    def review_page():
        """综合评估：不查库里的新表，全在 `review.build()` 里现算。

        四个工具的结论本来就各存一份（`merged()` 已经把它们并到一只票一行），
        这一页要的是**规则**，不是新数据——所以规则放 `web/review.py`，
        这里只负责挑批次、把结果交给模板。
        """
        with conn() as c:
            date, _run_id = _pick(c)
            date = date or db.batch_date(c)
            # 垄断性标注在 `shared.db`（不是结果库），单独读一次带进来——
            # `review.py` 保持纯函数，不自己连库。
            report = review.build(db.merged(c, date), db.moats())
            return render_template("review.html", meta=None, cur_date=date,
                                   batches=db.batches(c), review=report,
                                   summary=db.summary(c, date),
                                   counts=db.counts(c, date))

    @app.route("/price")
    def price_page():
        """冷热时点：`results.db` 的 `price`（全市场价格/估值）+ `price_stat`（每日摘要）。

        与四个工具页**不共用** `_bar.html` 那个批次选择器：它的选项是"哪天跑过工具"，
        这里是"哪天有全市场价格"，两者天然不同步——`price` 每个交易日都能有，
        而四个工具一周才跑一批。混用一个下拉会让人误以为"没跑工具的日子没行情"。

        `?q=` 搜索把页面切成三种形态（唯一命中看历史 / 多命中给清单 / 没命中），
        判断全在 `db.price_page()` 里，这里只负责把参数交过去。
        """
        data = db.price_page(
            date=(request.args.get("date") or "").strip() or None,
            q=(request.args.get("q") or "").strip(),
            sort=(request.args.get("sort") or "pct").strip(),
            order=(request.args.get("order") or "desc").strip(),
            start=(request.args.get("start") or "").strip() or None,
            end=(request.args.get("end") or "").strip() or None,
            db=app.config["DB_PATH"])
        return render_template("price.html", **data)

    @app.route("/batches")
    def batches():
        with conn() as c:
            return render_template("batches.html", batches=db.batches(c),
                                   cur_date=db.batch_date(c), meta=None,
                                   summary=db.summary(c), counts=db.counts(c))

    @app.route("/select")
    def select():
        return _page("select.html", "select")

    @app.route("/cycle")
    def cycle():
        return _page("cycle.html", "cycle")

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
