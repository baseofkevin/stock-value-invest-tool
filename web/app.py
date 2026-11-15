"""Flask 应用：把 results.db 渲染成看板。

为什么是服务端渲染（不是前后端分离）
------------------------------------
数据量是"几十只票"，一次查询 2ms。为了这点数据引一个前端框架、
再写一套 fetch/loading/empty 状态，除了把 `db.py` 的 SQL 搬到浏览器端，
什么也没换来。Jinja 直接吐 HTML，Chart.js 只负责画那几张图。

页面分工
--------
    /            概览：**某个批次**的四工具状态 + 合并表（谁入选了、四边并排）
    /review      综合评估：四边结论拼成优先级排序，标出「可以开始研究」的票
    /batches     批次：每周五盘后那一批票，四步齐不齐一目了然
    /select      选股：池内换手分位（排序依据）+ 行业分布
    /cycle       周期：三个判断的分布 + 毛利率波动 × 弹性散点
    /quality     质量：Q × V 散点（结论用颜色区分）+ 结论分布
    /top         顶部：距 250 日高点的横条（谁在高位）+ 四因子状态矩阵
    /stock/<code> 个股：四边结论并排 + 旁证/周期 + **历次结果（按时间）**
    /runs        运行台账：每次运行的时间、基准日、批量/单只、成功失败数

两种"时间"要分开看
------------------
* **批次**（`run.target_date`）= 那个周五。四个工具同一天的结果是一批，
  页面上永远整批看，不把周三单跑的一只票掺进周五的统计。
* **运行**（`run.started_at`）= 实际跑的时刻。同一只票可能被跑过很多次，
  个股页按时间逐条列出来。

工具页都接受两个参数：`?date=YYYY-MM-DD` 看指定批次，`?run=<id>` 看那一次运行；
不给就停在最新批次。页面之间靠股票代码串联，没有登录、没有写操作。
"""
from __future__ import annotations

import datetime as dt
import os
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, abort, render_template, request

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
                                 weekday=_weekday,
                                 poslabel=_poslabel, cycle_state=_cycle_state)
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
        return {"nav": [
            ("/", "概览"), ("/review", "综合评估"), ("/batches", "批次"),
            ("/select", "选股"),
            ("/cycle", "周期"), ("/quality", "质量"), ("/top", "顶部"),
            ("/runs", "运行记录"),
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
        with conn() as c:
            date, _run_id = _pick(c)
            date = date or db.batch_date(c)
            return render_template("index.html", cur_date=date,
                                   batches=db.batches(c),
                                   summary=db.summary(c, date),
                                   merged=db.merged(c, date),
                                   counts=db.counts(c, date))

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
            report = review.build(db.merged(c, date))
            return render_template("review.html", meta=None, cur_date=date,
                                   batches=db.batches(c), review=report,
                                   summary=db.summary(c, date),
                                   counts=db.counts(c, date))

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

    @app.route("/stock/<symbol>")
    def stock(symbol):
        with conn() as c:
            rows = {t: db.symbol_row(c, t, symbol) for t in db.ORDER}
            if not any(rows.values()):
                abort(404)
            hist = db.symbol_histories(c, symbol)
            timeline = db.symbol_timeline(c, symbol)
            name = next((r.get("name") for r in rows.values() if r and r.get("name")), symbol)
            top_row = rows.get("top") or {}
            top_data = top_row.get("data") or {}
            return render_template(
                "stock.html", symbol=symbol, name=name, rows=rows, hist=hist,
                timeline=timeline,
                # views_full 比 views 多出 headline / metrics：单只页那张卡要能
                # 说清楚"为什么是这个结论"，只有状态词是不够的。
                views=top_data.get("views_full") or top_data.get("views") or [],
                extras=top_data.get("extras") or {},
                gaps=top_data.get("gaps") or [],
                cyc=(rows.get("cycle") or {}).get("data") or {},
                qua=(rows.get("quality") or {}).get("data") or {})

    @app.route("/runs")
    def runs():
        with conn() as c:
            return render_template("runs.html", rows=db.runs(c), meta=None,
                                   batches=db.batches(c),
                                   cur_date=db.batch_date(c),
                                   summary=db.summary(c),
                                   counts=db.counts(c))

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m web", description="results.db 的本地看板（Flask + Chart.js）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址，默认 127.0.0.1（只本机可访问）")
    ap.add_argument("--port", type=int, default=5000, help="端口，默认 5000")
    ap.add_argument("--db", default=None, help="结果库路径（默认 results.db）")
    ap.add_argument("--debug", action="store_true", help="Flask 调试模式（改模板自动重载）")
    a = ap.parse_args(argv)

    app = create_app(a.db)
    print(f"看板：http://{a.host}:{a.port}   （结果库 {app.config['DB_PATH']}）")
    app.run(host=a.host, port=a.port, debug=a.debug)
    return 0


if __name__ == "__main__":                        # pragma: no cover
    raise SystemExit(main())
