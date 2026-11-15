"""Flask 应用：把 results.db 渲染成看板。

为什么是服务端渲染（不是前后端分离）
------------------------------------
数据量是"几十只票"，一次查询 2ms。为了这点数据引一个前端框架、
再写一套 fetch/loading/empty 状态，除了把 `db.py` 的 SQL 搬到浏览器端，
什么也没换来。Jinja 直接吐 HTML，Chart.js 只负责画那几张图。

页面分工
--------
    /            概览：四个工具的状态 + 一张合并表（谁入选了、四边结论并排）
    /select      选股：池内换手分位（排序依据）+ 行业分布
    /cycle       周期：三个判断的分布 + 毛利率波动 × 弹性散点
    /quality     质量：Q × V 散点（结论用颜色区分）+ 结论分布
    /top         顶部：距 250 日高点的横条（谁在高位）+ 四因子状态矩阵
    /stock/<code> 个股：四个工具的结论并排 + 顶部四因子/旁证/周期 + 历史
    /runs        运行台账：每次运行的时间、基准日、成功失败数

页面之间靠股票代码串联（合并表每行点得进去），没有登录、没有写操作。
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from flask import Flask, abort, render_template

from . import db

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
                                 poslabel=_poslabel, cycle_state=_cycle_state)
    app.jinja_env.globals.update(TOOLS=db.TOOLS, ORDER=db.ORDER, TODAY=dt.date.today)

    def conn():
        return db.connect(app.config["DB_PATH"])

    @app.context_processor
    def inject_globals():
        return {"nav": [
            ("/", "概览"), ("/select", "选股"), ("/cycle", "周期"),
            ("/quality", "质量"), ("/top", "顶部"), ("/runs", "运行记录"),
        ]}

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
            return render_template("index.html",
                                   summary=db.summary(c),
                                   merged=db.merged(c),
                                   counts=db.counts(c))

    @app.route("/select")
    def select():
        with conn() as c:
            rows = db.select_rows(c)
            return render_template("select.html", rows=rows,
                                   summary=db.summary(c), counts=db.counts(c))

    @app.route("/cycle")
    def cycle():
        with conn() as c:
            rows = db.cycle_rows(c)
            return render_template("cycle.html", rows=rows,
                                   summary=db.summary(c), counts=db.counts(c))

    @app.route("/quality")
    def quality():
        with conn() as c:
            rows = db.quality_rows(c)
            return render_template("quality.html", rows=rows,
                                   summary=db.summary(c), counts=db.counts(c))

    @app.route("/top")
    def top():
        with conn() as c:
            rows = db.top_rows(c)
            for r in rows:
                r["extras"] = (r.get("data") or {}).get("extras") or {}
                r["gaps"] = (r.get("data") or {}).get("gaps") or []
                r["views"] = (r.get("data") or {}).get("views") or []
            return render_template("top.html", rows=rows,
                                   summary=db.summary(c), counts=db.counts(c))

    @app.route("/stock/<symbol>")
    def stock(symbol):
        with conn() as c:
            rows = {t: db.symbol_row(c, t, symbol) for t in db.ORDER}
            if not any(rows.values()):
                abort(404)
            hist = {t: db.symbol_history(c, t, symbol) for t in db.ORDER}
            name = next((r.get("name") for r in rows.values() if r and r.get("name")), symbol)
            top_row = rows.get("top") or {}
            top_data = top_row.get("data") or {}
            return render_template(
                "stock.html", symbol=symbol, name=name, rows=rows, hist=hist,
                views=top_data.get("views") or [],
                extras=top_data.get("extras") or {},
                gaps=top_data.get("gaps") or [],
                cyc=(rows.get("cycle") or {}).get("data") or {},
                qua=(rows.get("quality") or {}).get("data") or {})

    @app.route("/runs")
    def runs():
        with conn() as c:
            return render_template("runs.html", rows=db.runs(c),
                                   summary=db.summary(c))

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
