"""只读结果库访问层。

**一切都在这里读，别在路由里写 SQL。** 路由关心的是"页面要哪些数字"，
这里关心的是"results.db 里怎么取"——两件事混在一起时，改一个字段名
要翻十几个路由函数。

三条约定：

1. **一律只读**（`mode=ro`）。网站不该成为第二个写库的进程：`results.db`
   是 WAL 模式，工具在写、Front 在读，很容易踩到 "database is locked"。
   只读连接不会加写锁，也不会顺手把 -wal 文件 checkpoint 掉。
   连 schema 升级（`results.py` 的 `_migrate`）也不在这里做——老库缺 `kind`
   列时这里退化成"全按批量算"，页面照常开，升级交给下一次跑工具。
2. **批次 = 基准日**（`run.target_date`）。每周五盘后那一批票是一个批次，
   看板默认停在最新批次，也能往上翻历史批次。批次只统计 `kind='batch'`
   的运行，单只随手的结论不混进周五那一批。
3. **单只 = `kind='single'`**。同一只票可能在不同时间被跑过很多次，
   个股页**只摆最近一份**（`symbol_row()`，取 `run.id` 最大的那条）。
   原来还有一张"历次结果"时间线（`symbol_timeline()` + `symbol_history()`
   + `_brief()`），2026-11-15 页面去掉后没有任何调用者，2026-11-17 一并删掉——
   留着只会让下一个读代码的人以为页面还有这一块。
4. **payload 是任意 JSON**：工具加字段不必改库表，网站按需读取。
   读不到就给 None，页面显示 `—`，不报错。
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import json
import math
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "results.db"
SHARED_DB = Path(os.environ.get("STOCK_SHARED_DB") or ROOT / "shared.db")
"""共享库（只剩 `annotation` 在这边）。环境变量是为了"拿副本调试"，
与 `STOCK_RESULTS_DB` 同一个用途——线上默认就是项目根那份。"""

RESULTS_DB = Path(os.environ.get("STOCK_RESULTS_DB") or DEFAULT_DB)
"""结果库（`price` / `price_stat` 也在这边）。`create_app()` 传进来的路径优先。"""

TRADE_CALENDAR = Path(os.environ.get("STOCKHUB_CALENDAR")
                      or ROOT / "state" / "trading_calendar.json")
"""交易日历。批次页用它区分"能补跑"和"周日/节假日不能补跑"；
读不到就退回按周一至周五判断。"""

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _trading_days() -> set[str]:
    """日历里的交易日；文件缺失/损坏时返回空集合，由调用方按工作日兜底。"""
    try:
        data = json.loads(TRADE_CALENDAR.read_text(encoding="utf-8"))
        return {str(d) for d in (data.get("days") or [])}
    except (OSError, ValueError, TypeError):
        return set()


def _is_trading_day(day: str, days: set[str]) -> bool:
    if days:
        return day in days
    try:
        return dt.date.fromisoformat(day).weekday() < 5
    except ValueError:
        return False

#: 工具 → (表名, 中文名, 一句话说明, 页面路径)
TOOLS = {
    "select": ("select_result", "选股", "5 日涨幅池 → K 线形态 → 池内换手分位", "/select"),
    "cycle": ("cycle_result", "周期弹性", "是不是周期股 / 弹性强弱 / 在周期哪个位置", "/cycle"),
    "quality": ("quality_result", "质量初筛", "质量分 Q + 估值分 V + 风险检查", "/quality"),
    "top": ("top_result", "顶部判断", "动量 / 换手 / 业绩 / 量价 四因子独立观察", "/top"),
}
ORDER = ("select", "cycle", "quality", "top")

#: `run.kind` 的两个取值。老库（schema v1）没有这一列，读的时候按 batch 算。
KIND_BATCH = "batch"
KIND_SINGLE = "single"
KIND_LABEL = {KIND_BATCH: "批量", KIND_SINGLE: "单只"}


def connect(db: str | Path | None = None) -> sqlite3.Connection:
    p = Path(db or DEFAULT_DB)
    if not p.exists():
        raise FileNotFoundError(p)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- 共享库（只读）

def moats(db: str | Path | None = None) -> dict[str, str]:
    """垄断性标注：`{symbol: moat_type}`，来自 `shared.db` 的 `annotation` 表。

    为什么不去 payload 里读：`stock_quality_screen` 确实算了 `resource_moat`
    （`screen/score.py`），但 `stock_report.py` 存库时只把 `detail` 和
    `report_html` 塞进 payload，`resource_moat` 只出现在 HTML 报告里。
    所以想看标注只有两条路——解析 HTML，或者直接读这张表。选后者。

    标注是**人工维护**的（`stock_data_fetch.annotation`），`"无标注"` 是结论
    （查过、没有），不是缺口；没进这张表的股票是真的没查过。

    读不到（库不存在 / 没有这张表 / 打不开）就给空 dict：页面上所有票显示
    「未标注」，只是失去那一项加分，不该让整页 500。
    """
    p = Path(db or SHARED_DB)
    if not p.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "select symbol, moat_type from annotation where kind='moat'").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(s).lower(): (t or "无标注") for s, t in rows}


FIN_PERIODS = 5
"""取最近几个报告期。门槛要"最近四季扣非"，Q3 报告下四季要跨到去年年报，
所以 5 个报告期留一格余量（某只票缺一期时不至于整条算不出来）。"""


def financials(symbols, db: str | Path | None = None,
               periods: int = FIN_PERIODS) -> dict[str, dict]:
    """`{symbol: fin}`——硬门槛要的那几个数，从 `shared.db.finance` 拼出来。

    为什么不在 `stock_review_assess.gates` 里直接查库：那个包要和
    `stock_kline_screen` 一样"只吃字典"，才能单测、才能换数据源。
    这里负责把 `raw_json` 里那几个只在原始字段里有的数（单季营收同比、
    单季扣非同比、累计扣非）挖出来，拼成一个扁平字典。

    `cl` / `cl_prev` 是合同负债的最新一季与**去年同季**（不是上一季）：
    合同负债是年末集中确认的，环比比出来的多半是季节性，同比才看得出来
    "订单在手是变厚还是变薄"。

    覆盖情况（2026-11-16 批 311 只）：财务数据 311/311，单季营收同比 303，
    单季扣非同比 304，季度成本 292，合同负债两期齐全 299。缺的那些让门槛
    落到 `unknown`，页面标「未核实」——不猜，也不当通过。
    """
    p = Path(db or SHARED_DB)
    if not p.exists() or not symbols:
        return {}
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        marks = ",".join("?" * len(symbols))
        rows = conn.execute(
            f"select symbol, period, OperatingRevenue_Q, OperatingCost_Q, "
            f"ContractLiability, raw_json "
            f"from finance where period in (select distinct period from finance "
            f"order by period desc limit ?) and symbol in ({marks})",
            (periods, *symbols)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    by_sym: dict[str, list] = {}
    for r in rows:
        by_sym.setdefault(str(r["symbol"]).lower(), []).append(r)

    out = {}
    for sym, items in by_sym.items():
        items.sort(key=lambda r: r["period"], reverse=True)
        latest = items[0]
        by_period = {r["period"]: r for r in items}
        j = _js(latest["raw_json"])
        inc = j.get("income") or {}
        cf = j.get("cashflow") or {}
        cut_qs = [_num((_js(r["raw_json"]).get("cashflow") or {})
                       .get("NPDeductNonRecurringPL_Q")) for r in items[:4]]
        cut_ttm = (sum(cut_qs) if len(cut_qs) == 4
                   and all(x is not None for x in cut_qs) else None)
        out[sym] = {
            "period": latest["period"],
            "rev_yoy_q": _num(inc.get("OperatingRevenueGrowRate_Q")),
            "cut_yoy_q": _num(inc.get("NPParentCompanyCutYOY_Q")),
            "cut_cum": _num(inc.get("NPDeductNonRecurringPL")),
            "np_cum": _num(inc.get("NPParentCompanyOwners")),
            "cut_q": next((x for x in cut_qs if x is not None), None),
            "cut_ttm": cut_ttm,
            "gm_q": [_gm(r) for r in items[:3]],
            "rev_q": _num(latest["OperatingRevenue_Q"]),
            "cl": _num(latest["ContractLiability"]),
            "cl_prev": _num(by_period.get(_last_year(latest["period"]),
                                          {"ContractLiability": None})
                            ["ContractLiability"]),
        }
    return out


def contract_liability(symbols, db: str | Path | None = None) -> dict[str, dict]:
    """`{symbol: {value, prev, yoy, share}}`——合同负债（预收款）的体检。

    **只给页面展示用，不参与任何判断。** 周期页拿它当"需求端先行指标"看：
    产能处在低位、预收款同比在涨，比单看产能分位多一个方向。

    `yoy` 是同比（去年同季，不是环比——合同负债在 Q4 集中确认）；
    `share` 是占年化营收（本季营收 ×4）的比重。两者都算不出来就是 None。
    """
    out = {}
    for sym, fin in financials(symbols, db).items():
        cl, prev, rev = fin.get("cl"), fin.get("cl_prev"), fin.get("rev_q")
        yoy = cl / prev - 1.0 if (cl is not None and prev and prev > 0) else None
        share = cl / (rev * 4.0) if (cl is not None and rev and rev > 0) else None
        out[sym] = {"value": cl, "prev": prev, "yoy": yoy, "share": share}
    return out


def _last_year(period: str) -> str:
    """`2026-09-30` → `2025-09-30`。合同负债要同比，不能拿上一季环比。"""
    return f"{int(period[:4]) - 1}{period[4:]}"


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _gm(r) -> float | None:
    """单季毛利率（%）。成本缺失（银行/券商没有营业成本）就是 None。"""
    rev, cost = _num(r["OperatingRevenue_Q"]), _num(r["OperatingCost_Q"])
    if rev is None or cost is None or rev <= 0:
        return None
    return (rev - cost) / rev * 100.0


# ---------------------------------------------------------------- 冷热极点（results.db 的 price）
#
# 「冷热极点」页读的是**结果库**：`stock_extreme_point` 抓完原始层后会把同一天
# `price` 复制过来，并在同一个事务里写 `price_stat`（一天一行，涨跌家数 /
# 估值中位数）。看板只读 results.db，上游怎么变、抓取怎么失败都不影响看图。
#
# 为什么不再读 shared.db：原始层被 `maintain` VACUUM / 清理时，页面上的历史
# 时点会跟着消失；而 results.db 是给人看的结果层，天然该留一份。
#
# 三条与 `moats()` 一致的约定：
# 1. 只读（`mode=ro`），打不开就返回 None，页面降级成"还没有数据"而不是 500；
# 2. 表可能不存在（`price` 还没跑过）：所有查询都先探表，缺就返回空；
# 3. **排序字段走白名单**，绝不把 `?sort=` 拼进 SQL。

#: `?sort=` → (列名, 页面上的中文)。顺序就是页面上按钮的顺序。
PRICE_SORTS = {
    "pct": ("pct_chg", "涨跌幅"),
    "pe": ("pe_ttm", "PE"),
    "pb": ("pb_mrq", "PB"),
    "ps": ("ps_ttm", "PS"),
    "mv": ("total_mv", "总市值"),
    "close": ("close", "收盘价"),
    "code": ("symbol", "代码"),
}

#: 一页最多渲染多少行。全市场 5579 行整页吐出去约 1.5MB HTML，
#: 浏览器滚动没问题，但真出现"一天几万行"的异常数据时别把进程拖死。
PRICE_ROW_CAP = 8000


def _price_conn(db: str | Path | None = None):
    """只读打开结果库。不存在/打不开 → None（页面按"没数据"处理）。"""
    p = Path(db or RESULTS_DB)
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


#: 摘要行的一个"空壳"：只有日期和行数，其余未知。老库（有 `price` 没 `price_stat`）
#: 与"某天漏跑 `--restat`"都走这条降级路，页面把缺的显示成 `—`。
_EMPTY_STAT = {"n": None, "n_up": None, "n_down": None, "n_flat": None,
               "n_limit_up": None, "n_limit_down": None, "n_pe": None,
               "n_pb": None, "n_ps": None, "pe_median": None,
               "pb_median": None, "ps_median": None, "mv_sum": None,
               "src": None, "updated_at": None}


def price_dates(conn) -> list[dict]:
    """可选的日期，新→旧；摘要能补的就补上，补不上的留空。

    **取两张表的并集**，不是"有摘要就只看摘要"：`price_stat` 可以缺天
    （老库、或某天只跑了取数没跑 `--restat`），而那一天在 `price` 里明明有数据。
    只认摘要的话，那些时点会从下拉里**凭空消失**——用户会以为那天没数据。
    反过来（有摘要没明细）虽然不该发生，也一并容忍。
    """
    if conn is None:
        return []
    days: dict[str, dict] = {}
    if _has(conn, "price_stat"):
        for row in conn.execute(
                "SELECT date,n,n_up,n_down,n_flat,n_limit_up,n_limit_down,n_pe,"
                "       n_pb,n_ps,pe_median,pb_median,ps_median,mv_sum,src,updated_at "
                "FROM price_stat").fetchall():
            days[row["date"]] = dict(row)
    if _has(conn, "price"):
        for row in conn.execute(
                "SELECT date, COUNT(*) AS n FROM price GROUP BY date").fetchall():
            item = days.setdefault(row["date"], {"date": row["date"], **_EMPTY_STAT})
            if item.get("n") is None:             # 摘要缺，用真实的明细行数顶上
                item["n"] = row["n"]
    return sorted(days.values(), key=lambda d: str(d["date"]), reverse=True)


def price_stat(conn, date: str) -> dict | None:
    """某天的摘要行。注意：**日期在、摘要不在**是正常状态（老库），返回 None 由页面降级。"""
    if conn is None or not date or not _has(conn, "price_stat"):
        return None
    row = conn.execute("SELECT * FROM price_stat WHERE date=?", (date,)).fetchone()
    return dict(row) if row else None


def price_index(conn, date: str) -> dict | None:
    """某天的沪深300快照；老库还没 `index_price` 表时返回 None（页面显示 —）。"""
    if conn is None or not date or not _has(conn, "index_price"):
        return None
    row = conn.execute(
        "SELECT date,symbol,name,close,pct_chg FROM index_price WHERE date=?",
        (date,)).fetchone()
    return dict(row) if row else None


def price_rows(conn, date: str, sort: str = "pct", order: str = "desc") -> list[dict]:
    """某天的全市场行。排序字段走白名单，未知值退回涨跌幅。

    `NULL` 一律排在最后（"是不是坏值"作为第一个排序键）——PE 为空的票
    在"按 PE 升序"里挤到最前面只会挡住真正便宜的那批。

    **估值列还要求"正值"**：负 PE 是亏损，绝对值没有意义，`PE 升序` 时
    它会排在最前面，看着像"最便宜的一批"，其实是亏得最狠的。所以 PE/PB/PS
    的坏值判定是 `IS NULL OR <= 0`，与 `price_stat._pos()` 算中位数只取正值
    是同一个口径。
    """
    if conn is None or not date or not _has(conn, "price"):
        return []
    col, _label = PRICE_SORTS.get(sort, PRICE_SORTS["pct"])
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    if col in ("pe_ttm", "pb_mrq", "ps_ttm"):
        bad = f"CASE WHEN {col} IS NULL OR {col} <= 0 THEN 1 ELSE 0 END"
    else:
        bad = f"({col} IS NULL)"
    sql = ("SELECT date,symbol,name,close,pct_chg,pe_ttm,pb_mrq,ps_ttm,"
           "       total_mv,circ_mv "
           f"FROM price WHERE date=? ORDER BY {bad}, {col} {direction}, symbol "
           "LIMIT ?")
    return [dict(r) for r in conn.execute(sql, (date, PRICE_ROW_CAP)).fetchall()]


def price_count(conn, date: str) -> int:
    """那天一共有多少行（与渲染上限无关——页面要能说清"5579 只，显示前 8000 行"）。"""
    if conn is None or not date or not _has(conn, "price"):
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM price WHERE date=?",
                            (date,)).fetchone()[0])


def price_search(conn, q: str, limit: int = 60) -> list[dict]:
    """按代码/名称模糊找股票，每只带上**最近一天**的行情。

    返回的是"候选清单"：一只票一行（`symbol` / `name` / `last_date` / `last_pct` /
    `n_dates`），由页面决定是"唯一命中直接看历史"还是"多命中先给清单"。
    """
    text = str(q or "").strip()
    if conn is None or not text or not _has(conn, "price"):
        return []
    like = f"%{text}%"
    rows = conn.execute(
        "SELECT p.symbol, p.name, p.date AS last_date, p.pct_chg AS last_pct, "
        "       p.close AS last_close, m.n_dates "
        "FROM price p JOIN (SELECT symbol, MAX(date) AS d, COUNT(*) AS n_dates "
        "                   FROM price GROUP BY symbol) m "
        "  ON p.symbol = m.symbol AND p.date = m.d "
        "WHERE p.symbol LIKE ? OR p.name LIKE ?", (like, like)).fetchall()
    out = [dict(r) for r in rows]
    low = text.lower()

    def rank(r: dict) -> tuple:
        sym = str(r["symbol"] or "").lower()
        name = str(r["name"] or "")
        # 精确代码 > 代码后缀 > 名称完全相等 > 名称前缀 > 其余；同档按天数多的在前。
        return (sym != low, not sym.endswith(low), name != text,
                not name.startswith(text), -int(r.get("n_dates") or 0), sym)

    out.sort(key=rank)
    return out[:limit]


def price_history(conn, symbol: str) -> list[dict]:
    """一只票**在 price 表里的所有时点**，新→旧。搜索框唯一命中时展示这个。"""
    if conn is None or not symbol or not _has(conn, "price"):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT date,symbol,name,close,pct_chg,pe_ttm,pb_mrq,ps_ttm,total_mv,circ_mv "
        "FROM price WHERE symbol=? ORDER BY date DESC LIMIT ?",
        (symbol, PRICE_ROW_CAP)).fetchall()]


def _history_range(rows: list[dict], dates: list[dict],
                   start: str | None, end: str | None) -> dict:
    """把一只票的历史行整理成「区间 + 曲线」需要的形状。

    `rows` 是 `price_history()` 的**新→旧**结果；下拉选项只放这只票真实有数据的
    日期，曲线横轴却用全市场交易日做骨架——停牌日插 `None`，Chart.js 用
    `spanGaps:false` 断开，别把缺口画成一条连续上涨/下跌的线。

    起点/终点不合法（不在该股历史里）就退回全部；起 > 止则对调并留一个标记。
    这两种情况都**不静默**，模板会显示一行提示。
    """
    hist = sorted(rows, key=lambda r: str(r["date"]))
    if not hist:
        return {"rows": [], "hist_dates": [], "hist_series": [], "hist_count": 0,
                "hist_all_total": 0, "hist_swapped": False, "hist_reset": False}

    stock_dates = [str(r["date"]) for r in hist]
    valid = set(stock_dates)
    first, last = stock_dates[0], stock_dates[-1]
    # 只丢掉**真的给了但不在这只票历史里**的那一端；比如只给了 start，
    # end 应该落回最新，而不是把用户选的 start 也一起清掉。
    reset = ((start is not None and start not in valid)
             or (end is not None and end not in valid))
    if start not in valid:
        start = first
    if end not in valid:
        end = last
    swapped = False
    if start > end:
        start, end = end, start
        swapped = True

    by_date = {str(r["date"]): r for r in hist}
    selected = [by_date[d] for d in stock_dates if start <= d <= end]
    if not selected:                         # 理论上不会发生，防御一下
        return {"rows": [], "hist_dates": [], "hist_series": [], "hist_count": 0,
                "hist_all_total": len(hist), "hist_swapped": swapped,
                "hist_reset": reset}

    base = selected[0].get("close")
    for r in selected:
        close = r.get("close")
        r["cum"] = (round((float(close) / float(base) - 1) * 100, 4)
                    if base and close is not None and float(base) > 0 else None)

    # 横轴用全市场日期：这段区间里该股没有行 = 停牌/缺数据，曲线要断开。
    spine = [str(d["date"]) for d in dates if start <= str(d["date"]) <= end]
    spine.sort()
    series = []
    for d in spine:
        r = by_date.get(d)
        if r is None:
            series.append({"date": d, "close": None, "pct_chg": None, "cum": None})
        else:
            series.append({"date": d, "close": r.get("close"),
                           "pct_chg": r.get("pct_chg"), "cum": r.get("cum")})

    last_row = selected[-1]
    ret = None
    if base and last_row.get("close") is not None and float(base) > 0:
        ret = round((float(last_row["close"]) / float(base) - 1) * 100, 4)
    return {
        "rows": selected,                     # 表格按时间从早到晚
        "hist_dates": list(reversed(stock_dates)),  # 下拉最新在前
        "hist_series": series,
        "hist_count": len(selected),
        "hist_all_total": len(hist),
        "hist_start": selected[0]["date"],
        "hist_end": last_row["date"],
        "hist_base_close": base,
        "hist_last_close": last_row.get("close"),
        "hist_ret": ret,
        "hist_swapped": swapped,
        "hist_reset": reset,
        "range_start": start,
        "range_end": end,
    }


def extreme_page(*, date: str | None = None, q: str = "", sort: str = "pct",
               order: str = "desc", start: str | None = None,
               end: str | None = None, db: str | Path | None = None) -> dict:
    """「冷热极点」页要的全部数据。**路由只调这一个函数，不写 SQL。**

    返回的 `shape` 决定模板摆哪块：

    | shape | 什么时候 | 页面上是 |
    |---|---|---|
    | `empty` | 结果库没有 price 表 / 一行都没有 | 一句"先跑 price" |
    | `no_match` | 搜索没命中 | 搜索框 + 一句"没找到" |
    | `pick` | 搜索命中多只 | 候选清单（点一只看历史） |
    | `history` | 搜索唯一命中 | 这只票的区间曲线 + 全部时点 |
    | `market` | 没搜索 | 当天的全市场表（默认视图） |
    """
    conn = _price_conn(db)
    try:
        dates = price_dates(conn)
        cur = date if date in {d["date"] for d in dates} else (dates[0]["date"] if dates else None)
        text = str(q or "").strip()
        sort = sort if sort in PRICE_SORTS else "pct"
        order = "asc" if str(order).lower() == "asc" else "desc"
        out = {
            "dates": dates, "cur_date": cur, "q": text, "sort": sort, "order": order,
            # `cur` = 当前时点在下拉里的那一行（可能只有日期与家数，摘要是空的）。
            # 顶部的四张卡另看 `stat`：摘要缺失时那几张卡不显示，而不是四张 `—`。
            "cur": next((d for d in dates if d["date"] == cur), None),
            "sorts": PRICE_SORTS, "stat": price_stat(conn, cur) if cur else None,
            "index": price_index(conn, cur) if cur else None,
            "rows": [], "total": 0, "matches": [], "hist_symbol": None,
            "hist_name": None, "hist_dates": [], "hist_series": [],
            "hist_count": 0, "hist_all_total": 0, "hist_ret": None,
            "hist_start": None, "hist_end": None, "hist_base_close": None,
            "hist_last_close": None, "hist_swapped": False, "hist_reset": False,
            "range_start": None, "range_end": None, "shape": "empty",
        }
        if not dates or not cur:
            return out

        if text:
            matches = price_search(conn, text)
            out["matches"] = matches
            if not matches:
                out["shape"] = "no_match"
            elif len(matches) == 1:
                sym = matches[0]["symbol"]
                out["shape"] = "history"
                out["hist_symbol"] = sym
                out["hist_name"] = matches[0].get("name")
                detail = _history_range(price_history(conn, sym), dates,
                                        (start or "").strip() or None,
                                        (end or "").strip() or None)
                out.update(detail)
                out["total"] = detail["hist_count"]
            else:
                out["shape"] = "pick"
            return out

        rows = price_rows(conn, cur, sort, order)
        out["rows"] = rows
        out["total"] = price_count(conn, cur)
        out["shape"] = "market"
        return out
    finally:
        if conn is not None:
            conn.close()


# ------------------------------------------------------------------ run 台账

#: 每个表有哪些列。`sqlite3.Connection` 不能挂自定义属性，只能自己缓存；
#: 键用库路径 + 表的修改时间（`pragma table_info` 本身也便宜，但一个页面
#: 会问十几次，缓存能省掉大部分）。
_COLS: dict[tuple[str, str], set[str]] = {}


def _columns(conn, table: str) -> set[str]:
    """表有哪些列（老库缺 `kind` 时要退化成常量）。"""
    key = (conn.execute("pragma database_list").fetchone()[2], table)
    if key not in _COLS:
        _COLS[key] = {r[1] for r in conn.execute(f"pragma table_info({table})")}
    return _COLS[key]


def _kind_expr(conn) -> str:
    """`run.kind` 的 SQL 表达式。

    schema v1 的库没有这一列，而看板是只读连接（不该顺手改库结构）。
    缺列时按 'batch' 常量算：v1 时代只有批量流程，这个假设是准的。
    """
    return "r.kind" if "kind" in _columns(conn, "run") else f"'{KIND_BATCH}'"


def _run(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("kind", "note"):
        d.setdefault(key, None)
    d["kind"] = d["kind"] or KIND_BATCH
    d["kind_label"] = KIND_LABEL.get(d["kind"], d["kind"])
    return d


def run_row(conn, run_id: int) -> dict | None:
    row = conn.execute(f"select r.*, {_kind_expr(conn)} as kind from run r "
                       "where r.id=?", (run_id,)).fetchone()
    return _run(row) if row else None


def runs(conn, limit: int = 60) -> list[dict]:
    """最近的运行（两种 kind 都在内，按时间倒序）。"""
    rows = conn.execute(
        f"select r.*, {_kind_expr(conn)} as kind from run r "
        "order by r.id desc limit ?", (limit,)).fetchall()
    return [_run(r) for r in rows]


def summary(conn, date: str | None = None) -> list[dict]:
    """每个工具在**某个批次**里的运行；没跑的工具也占一行。

    `date` 为空时取最新批次。这是首页四张卡的来源——原来是"每个工具各自
    最近一次运行"，那会把周三单跑的一只票当成周五那一批的一部分。
    """
    date = date or batch_date(conn)
    out = []
    for tool in ORDER:
        row = None
        if date:
            row = conn.execute(
                f"select r.*, {_kind_expr(conn)} as kind from run r "
                "where r.tool=? and r.target_date=? and "
                f"{_kind_expr(conn)}=? order by r.id desc limit 1",
                (tool, date, KIND_BATCH)).fetchone()
        _t, label, desc, href = TOOLS[tool]
        item = {
            "tool": tool, "label": label, "desc": desc, "href": href,
            "run_id": row["id"] if row else None,
            "target_date": row["target_date"] if row else None,
            "started_at": row["started_at"] if row else None,
            "n_ok": row["n_ok"] if row else 0,
            "n_fail": row["n_fail"] if row else 0,
        }
        out.append(item)
    return out


# ------------------------------------------------------------------ 批次

def batch_date(conn, skip: int = 0) -> str | None:
    """最新（或第 skip 新）的批次基准日。"""
    rows = conn.execute(
        f"select distinct r.target_date from run r "
        f"where {_kind_expr(conn)}=? order by r.target_date desc limit ?",
        (KIND_BATCH, skip + 1)).fetchall()
    return rows[-1][0] if len(rows) > skip else None


#: 历史表**表头**的阈值说明（悬停看）。写在这里而不是模板里：阈值是这套方法的
#: 全部可调项，跟 `stock_market_trend/rules.py` 是同一组数字，改的时候只改两处
#: （那边改判定、这边改说法），别让页面上的"≥80 便宜"跟代码里的 80 对不上。
REGIME_HEAD_TIPS = {
    "trend": "沪深300 周线\n"
             "连续 2 周收盘 < MA20 → 偏弱\n"
             "MA20 < MA60 且 MA60 低于 4 周前 → 空头条件\n"
             "两条同时成立 → 空头确认",
    "erp": "ERP = 100/PE(TTM) − 10 年国债\n"
           "近 10 年分位：≥80 便宜 · ≥95 极值 · ≤20 偏贵",
    "pe": "PE(TTM) = 沪深300 整体滚动市盈率（中证官方）\n"
          "便宜/贵不看这个数，看「ERP 分位」——\n"
          "估值要扣掉国债的机会成本，13 倍贵不贵取决于国债多少",
    "bond": "10 年期国债收益率 = 无风险利率 = 持股的机会成本\n"
            "国债越低，同样 PE 下股票越有吸引力（ERP 被抬高）",
    "below": "全A破净率（股价低于每股净资产的公司占比）\n"
             "近 10 年分位：≥90 恐慌 · ≥70 偏冷",
    "volume": "沪深300 周换手率 · 一个水位派生出四条线\n"
              "水位 = 近 52 周换手中位数\n"
              "地量 ≤1%（固定）· 缩量 <0.9× · 平稳 · 放量 ≥1.1× · 天量 ≥1.5×\n"
              "三条线逐周现算，跟着市场水位走，不用人工校准",
}


def regime(conn, limit: int | None = 8, *, start: str | None = None,
           end: str | None = None) -> dict:
    """A股大势的最近几周（最新在前）。

    `limit=None` 表示不截断（历史页用）；`start` / `end` 是闭区间，
    按**基准日**过滤，不是 `data_asof`——两者不是一回事。

    **不在这里算任何东西**：分位、趋势、状态全是 `stock_market_trend` 跑完就定格的，
    看板只负责摆出来。也不去 `shared.db` 取原件——页面只读这一个库。

    三种"没有"都返回空壳，由页面说人话：
    * 表不存在（从没跑过这个功能）→ `row=None`
    * 表在但没有行（跑过、但三路都没算出来）→ `row=None`
    * 某几列是 NULL（那一路当天没数据）→ 字段为 None，页面显示 `—`
    """
    empty = {"row": None, "weeks": [], "total": 0, "matched": 0,
             "head_tips": REGIME_HEAD_TIPS}
    if conn is None or not _has(conn, "market_regime"):
        return empty

    cols = ("date, computed_at, data_asof, stale, trend_state, trend_score, "
            "trend_close, ma20, ma60, ma60_slope, weeks_below_ma20, erp, "
            "pe_ttm, bond10y, erp_pct, below_na, below_na_pct, turnover, "
            "turnover_q10, volume_dry, payload")

    where, params = [], []
    if start:
        where.append("date >= ?")
        params.append(start)
    if end:
        where.append("date <= ?")
        params.append(end)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute("SELECT count(*) FROM market_regime").fetchone()[0]
    matched = conn.execute(
        f"SELECT count(*) FROM market_regime{clause}", params).fetchone()[0]

    sql = f"SELECT {cols} FROM market_regime{clause} ORDER BY date DESC"
    qparams = list(params)
    if limit is not None:
        sql += " LIMIT ?"
        qparams.append(int(limit))
    rows = conn.execute(sql, qparams).fetchall()
    if not rows:
        return {**empty, "total": total, "matched": 0}

    def _row(r) -> dict:
        d = dict(r)
        d["stale"] = bool(d.get("stale"))
        d["volume_dry"] = None if d.get("volume_dry") is None else bool(d["volume_dry"])
        # 量能分档是后加的字段，只存在 payload 里（表结构不动，符合"payload 是
        # 任意 JSON"那条约定）。老行没有这几个键 → 退回「地量 / 未地量」两挡。
        payload_text = d.pop("payload", None)
        sent = _payload_sentiment(payload_text)
        d["volume_state"] = sent.get("volume_state")
        d["turnover_pct"] = sent.get("turnover_pct")
        d["turnover_q90"] = sent.get("turnover_q90")
        d["dry_line"] = sent.get("dry_line")
        d["low_line"] = sent.get("low_line")
        d["high_line"] = sent.get("high_line")
        d["heavy_line"] = sent.get("heavy_line")
        d["median52"] = sent.get("median52")
        # 「本周未走完」：档位是按上一完整周判的，页面要说出来
        d["partial_week"] = sent.get("partial_week")
        # ERP 的近 10 年均值在 payload.valuation 里（表里没这列），
        # 给「10Y 国债」格的悬停用——光看 1.59% 不知道高还是低，
        # 对着"这个数曾把 ERP 拉到均值 4.82"才有感觉。
        d["erp_mean"] = _payload_valuation(payload_text).get("mean")
        d["tips"] = _regime_tips(d)
        return d

    weeks = [_row(r) for r in rows]
    # 卡片永远展示“最新一周”，不受历史页筛选影响；筛选只作用于下表。
    latest = conn.execute(
        f"SELECT {cols} FROM market_regime ORDER BY date DESC LIMIT 1").fetchone()
    return {"row": _row(latest), "weeks": weeks, "total": total,
            "matched": matched, "head_tips": REGIME_HEAD_TIPS}


def _payload_valuation(payload) -> dict:
    """从 `market_regime.payload` 里取估值那路（`mean` = ERP 近 10 年均值）。

    与 `_payload_sentiment` 同一约定：payload 是任意 JSON、加字段不改表；
    读不出就给空 dict，调用方按 `None` 处理。
    """
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    val = data.get("valuation") if isinstance(data, dict) else None
    return val if isinstance(val, dict) else {}


def _payload_sentiment(payload) -> dict:
    """从 `market_regime.payload` 里取情绪那一路。读不出来就给空 dict。

    后加的字段（`volume_state` / `turnover_pct` / `turnover_q90`）不进表，
    只在 payload 里——这条库的约定就是"加字段不改表"，看板按需读取。
    """
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    sent = data.get("sentiment") if isinstance(data, dict) else None
    return sent if isinstance(sent, dict) else {}


def _regime_tips(w: dict) -> dict[str, str]:
    """历史表四列各自的 `title` 文本（鼠标悬停看明细）。

    为什么不写在模板里：四条提示要拼十几个可空字段，Jinja 里会变成一片
    `{{ x or '—' }}` 的噪音，还不如在这里用普通 Python 写清楚。
    文本口径与页面上那三张卡片**保持一致**——同一行数据，两处说法不能打架。
    """
    def n(v, suffix=""):
        return "—" if v is None else f"{v}{suffix}"

    trend = [f"趋势：{w.get('trend_state') or '—'}"
             f"（{w.get('trend_score') if w.get('trend_score') is not None else '—'}/2）",
             f"收盘 {n(w.get('trend_close'))} · MA20 {n(w.get('ma20'))} / MA60 {n(w.get('ma60'))}",
             f"已 {n(w.get('weeks_below_ma20'))} 周收在 MA20 下方"]
    if w.get("ma60_slope") is not None:
        trend.append(f"MA60 四周 {w['ma60_slope']:+.2f}%")

    erp = [f"赔率：{_regime_state_erp(w.get('erp_pct'))}",
           f"ERP {n(w.get('erp'), '%')} = 100/PE {n(w.get('pe_ttm'))} − 国债 {n(w.get('bond10y'), '%')}",
           f"近 10 年 {n(w.get('erp_pct'), '%')} 分位（≥80 便宜，≥95 极值）"]

    # PE 和国债各拿一格悬停：单看 PE 容易误判"便宜/贵"——13 倍贵不贵，
    # 取决于国债给的机会成本，所以这格只报数、把结论推给 ERP 那列。
    earn = (None if w.get("pe_ttm") in (None, 0)
            else round(100.0 / w["pe_ttm"], 2))
    pe = [f"PE(TTM) {n(w.get('pe_ttm'))} · 沪深300 整体滚动市盈率（中证官方）",
          f"盈利收益率 100/PE = {n(earn, '%')}"
          + (f" − 国债 {n(w.get('bond10y'), '%')} = ERP {n(w.get('erp'), '%')}"
             if earn is not None and w.get('bond10y') is not None else ""),
          "PE 本身不分档：便宜/贵看「ERP 分位」列（估值要扣掉国债的机会成本）"]

    bond = [f"10 年期国债收益率 {n(w.get('bond10y'), '%')}（无风险利率）",
            f"它是 ERP 的减数：持股的机会成本，国债越低，同样 PE 下股票越有吸引力"
            + (f"（现 {n(w.get('bond10y'), '%')} → ERP 抬到 {n(w.get('erp'), '%')}，"
               f"近 10 年均值约 {n(w.get('erp_mean'))}）"
               if w.get('erp_mean') is not None else ""),
            ]
    if w.get("bond10y") is not None and w.get("erp_pct") is not None:
        bond.append(f"当前组合的 ERP 分位 {w['erp_pct']}% 已包含这个国债水平")

    below = [f"情绪：{_regime_state_na(w.get('below_na_pct'))}",
             f"全A破净率 {n(w.get('below_na'), '%')}",
             f"近 10 年 {n(w.get('below_na_pct'), '%')} 分位（≥90 恐慌）"]

    vs = w.get("volume_state")
    if not vs and w.get("volume_dry") is not None:
        # 老行（没有分档字段）：退回两挡说法，别硬猜分位。
        vs = "地量" if w["volume_dry"] else "未地量"
    if vs is None:
        volume = ["量能：—", "缺周换手数据"]
    else:
        volume = [f"量能：{vs}", f"沪深300 周换手 {n(w.get('turnover'), '%')}"]
        dry = w.get("dry_line")
        low = w.get("low_line")
        high = w.get("high_line")
        heavy = w.get("heavy_line")
        if dry is not None and heavy is not None:
            volume.append(f"地量 ≤{dry}%（固定）· 缩量 <{n(low, '%')}"
                          f"（近一年中位 ×0.9）")
            volume.append(f"放量 ≥{n(high, '%')}（×1.1）· 天量 ≥{heavy}%（×1.5）")
        if w.get("turnover_pct") is not None:
            volume.append(f"参考：近 5 年 {w['turnover_pct']}% 分位")
        part = w.get("partial_week")
        if part:
            volume.insert(1, f"⚠ 本周未走完：{part.get('date')} 至今只累计了"
                             f" {n(part.get('turnover'), '%')}，档位按上一完整周"
                             f"（{part.get('prev_date') or '—'}）判")

    return {"trend": "\n".join(trend), "erp": "\n".join(erp),
            "pe": "\n".join(pe), "bond": "\n".join(bond),
            "below": "\n".join(below), "volume": "\n".join(volume)}


def _regime_state_erp(pct) -> str:
    if pct is None:
        return "—"
    if pct >= 95:
        return "极值"
    if pct >= 80:
        return "便宜"
    if pct <= 20:
        return "偏贵"
    return "中性"


def _regime_state_na(pct) -> str:
    if pct is None:
        return "—"
    if pct >= 90:
        return "恐慌"
    if pct >= 70:
        return "偏冷"
    return "正常"


# ---------------------------------------------------------------- 量能曲线（shared.db 只读）
#
# 这一处**破例读 shared.db**，理由只有一条：图上那条线是原件（每周换手率序列），
# `results.db.market_regime` 一周只有一行结论，凑不出 260 根周线。`moats()` /
# `financials()` 已经是同一个先例，三条约定照抄：只读（`mode=ro`）、打不开就返回空、
# 页面降级成"没有这张图"而不是 500。
#
# **自动延续**：序列直接来自 `market_index_week`。每周跑 `run.py all`，那一步会把
# 新周线 upsert 进表；图下次刷新就自己往右长一根，没有任何人需要手工补点、补日期。

TURNOVER_WEEKS = 260
"""画多少根周线 = 5 年。

注意：这个窗口只管画多点历史，不再决定"地量/天量"那条线。分位仍算出来当参考值，
图上不画。
"""

TURNOVER_DRY_LINE = 1.0
TURNOVER_LOW_MULT = 0.9
TURNOVER_HIGH_MULT = 1.1
TURNOVER_HOT_MULT = 1.5
TURNOVER_HEAVY_WIN = 52
_EPS = 1e-9                    # 取整时抵掉浮点噪声（与 rules 同值，别单独调）
"""量能分档的口径，与 `stock_market_trend.rules` 同值（副本）。

地量线是一个固定数（1%）；缩量线 / 放量线 / 天量线都不是数，而是
`近 52 周中位数 × 0.9 / × 1.1 / × 1.5`（前两条取整到 0.1%，天量线向上取整到
0.5%）——所以它们逐周不同，图上是**三条跟着市场起伏的折线**，不是平线。
四条线全部从同一个水位派生，市场整体缩量或放量时一起漂，不用人工校准。
`tests/test_web.py` 有一条断言把两边的常量与算法钉在一起，改一边不改另一边会挂测试。
"""

TURNOVER_DAYS = 250
"""日线视图画多少个交易日 ≈ 1 年。

日线不用周线那个 260 根的窗口：260 个交易日只有一年出头，但 260 个**交易日**
画成折线已经很密了；再放长就看不见日与日的差别，日视图也就失去了意义。
想看更长的历史，用周视图（260 周 = 5 年）。
"""

TURNOVER_DAY_WIN = 252
"""日口径的水位窗口：近 252 个交易日 ≈ 1 年。

和 `TURNOVER_HEAVY_WIN = 52` 是**同一段时间的两种刻度**，不是两个不同的规则：
`rules.volume_lines` 的一年是 52 周，这里的一年是 252 个交易日。
所以这两套线的数值不可互换——见 `TURNOVER_DAY_DRY_LINE` 的说明。
"""

TURNOVER_DAY_DRY_LINE = 0.2
"""日口径地量线（%）。

周口径是 1.0%，取的是"十年 10% 分位"——那个数十年没怎么动，所以钉成常数。
日换手率按日累计，一周的换手约等于五天之和，所以同分位的日值大致是周值的
**五分之一**。

2026-11-24 用 3000 根真实日K（2014-07 起）实测，这个换算成立：

| | 中位 | 10% 分位 |
|---|---|---|
| 周（601 周） | 1.90 | 1.17 |
| 日（3000 日） | 0.40 | 0.24 |

同分位比值 0.21；周口径的 1.0 本来就压在它自己 10% 分位（1.17）**下方**，
所以日口径同样取在日频 10% 分位（0.24）下方一点：0.2。
两边是同一个逻辑——"缩到十年少见的位置才算地量"，不是简单按五分之一折算。

只影响日视图，不影响任何判定（判定走 `rules.sentiment` 的周口径）。
"""

_DAY_STEP = 100
_DAY_HEAVY_STEP = 20
"""日口径的取整粒度：0.01% / 0.05%。

周口径是 0.1% / 0.5%（`step=10` / `heavy_step=2`），按量级等比例缩到 1/10。
不这么做的话日线会退化成一根根台阶：日换手率常在 0.4~0.8 之间，
按 0.1 取整只剩四五个可能取值。
"""


def _median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _running_week(day: str, today: str | None = None) -> bool:
    """这根周线是不是「本周还没走完」（判据与 `rules.split_partial` 一致）。

    东财周K 在周中会给一根**当周至今**的 bar：close 是当天收盘（画价格没问题），
    但 volume/turnover 只累计了几天。周中跑 `run.py all` 时，最后一根就是它。
    这里只用来在图上把它标出来（空心点 + 悬停提示），不参与档位判定——
    判定那边 `rules.split_partial` 已经把它摘掉了。
    """
    ref = today or dt.date.today().isoformat()
    try:
        if day > ref:
            return False
        a = dt.date.fromisoformat(day).isocalendar()[:2]
        b = dt.date.fromisoformat(ref).isocalendar()[:2]
        return a == b and dt.date.fromisoformat(ref).weekday() < 4
    except ValueError:
        return False


def _volume_lines(values: list[float], *, win: int = TURNOVER_HEAVY_WIN,
                  dry: float = TURNOVER_DRY_LINE,
                  step: int = 10, heavy_step: int = 2) -> dict | None:
    """三条线，取法与 `rules.volume_lines` 逐字一致（含取整方向与被地量线兜住）。

    `win` / `dry` / `step` / `heavy_step` 是给**日口径**留的旋钮：周口径用默认值
    （52 周、1%、0.1% / 0.5%），日口径传 252 / 0.2 / 0.01% / 0.05%。
    默认值一变就等于改判定，`tests/test_web.py` 会挂——那条断言正是为此设的。
    """
    window = values[-win:]
    if len(window) < win // 2:
        return None
    m = _median(window)
    low = max(dry, math.floor(m * TURNOVER_LOW_MULT * step + _EPS) / step)
    high = max(low, math.ceil(m * TURNOVER_HIGH_MULT * step - _EPS) / step)
    return {"low": low, "high": high,
            "heavy": math.ceil(m * TURNOVER_HOT_MULT * heavy_step - _EPS) / heavy_step}


def _heavy_line(values: list[float]) -> float | None:
    """只要天量线时的简写。"""
    lines = _volume_lines(values)
    return None if lines is None else lines["heavy"]


def _quantile(values: list[float], pct: float) -> float:
    """窗口内的 pct 分位值——取法与 `rules._quantile` 逐字一致。"""
    v = sorted(values)
    return v[max(0, min(len(v) - 1, int(len(v) * pct / 100) - 1))]


def turnover_series(n: int = TURNOVER_WEEKS, db: str | Path | None = None,
                    today: str | None = None) -> dict:
    """沪深300 最近 n 周换手率 + 逐周三档线 + 周收盘价。读不到就给空壳。

    `dry_line` 是一个数（1%）；`low` / `high` / `heavy` 都是**逐周**的值——
    缩量线 / 放量线 / 天量线 = 该周往前 52 周的换手中位数 × 0.9 / 1.1 / 1.5，
    所以图上它们是三条跟着市场起伏的折线。`q10` / `q90` 只是同期分位，
    留给需要"排第几"的地方参考，图上不画。

    收盘价是给同一张图画「价」用的：量能和价格共用一根时间轴，
    才能看出放量到底是涨着放还是跌着放。

    `partial_index` 指向「本周还没走完」的那根（周中才有，周五盘后为 None）。
    它照样画（价格是真的），但量能不能按它判，页面上画成空心点、悬停说明。
    """
    empty = {"weeks": [], "q10": None, "q90": None, "q95": None,
             "dry_line": TURNOVER_DRY_LINE,
             "low_line": None, "high_line": None, "heavy_line": None,
             "low_series": [], "high_series": [], "heavy_series": [],
             "median52": None, "partial_index": None,
             "n": 0, "latest": None, "latest_value": None,
             "latest_close": None, "close_min": None, "close_max": None}
    p = Path(db or SHARED_DB)
    if not p.exists():
        return empty
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return empty
    try:
        # 多取 52 行再截掉：最早画出来的那根周线，它的三条线也要有完整的 52 周
        # 窗口，否则图的左端会是一段"窗口不够、线偏低"的假台阶。
        rows = conn.execute(
            "select date, turnover, pct_chg, close from market_index_week "
            "where symbol='sh000300' and turnover is not null "
            "order by date desc limit ?",
            (int(n) + TURNOVER_HEAVY_WIN,)).fetchall()
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
    if not rows:
        return empty

    rows.reverse()
    vals = [float(t) for _, t, _, _ in rows]
    total = len(rows)
    # 最后一根如果是「本周至今」，它自己的三天换手不能参与自己的 52 周中位——
    # 那是拿一天的量去拉低水位。按"不含它自己"算，等于这条线停在上一完整周，
    # 与 `rules.split_partial` 摘掉它再判的口径完全一致。
    part_idx = total - 1 if _running_week(str(rows[-1][0]), today) else None
    lines = [_volume_lines(vals[:i] if i == part_idx else vals[:i + 1])
             for i in range(total)]
    low = [None if x is None else x["low"] for x in lines]
    high = [None if x is None else x["high"] for x in lines]
    heavy = [None if x is None else x["heavy"] for x in lines]
    cut = slice(-int(n), None)
    rows, vals = rows[cut], vals[cut]
    low, high, heavy = low[cut], high[cut], heavy[cut]
    # 截断后再把下标挪回窗口内
    part_idx = None if part_idx is None else part_idx - max(0, total - len(rows))
    closes = [float(c) for _, _, _, c in rows if c is not None]
    recent = vals[-TURNOVER_HEAVY_WIN:]
    return {
        "weeks": [{"date": str(d), "turnover": round(float(t), 2),
                   "pct_chg": None if c is None else round(float(c), 2),
                   "close": None if k is None else round(float(k), 2)}
                  for d, t, c, k in rows],
        "q10": round(_quantile(vals, 10), 2),
        "q90": round(_quantile(vals, 90), 2),
        "q95": round(_quantile(vals, 95), 2),
        "dry_line": TURNOVER_DRY_LINE,
        "low_line": low[-1],
        "high_line": high[-1],
        "heavy_line": heavy[-1],
        "low_series": low,
        "high_series": high,
        "heavy_series": heavy,
        "median52": round(_median(recent), 2) if len(recent) >= 26 else None,
        "partial_index": part_idx,
        "n": len(rows),
        "latest": str(rows[-1][0]),
        "latest_value": round(float(rows[-1][1]), 2),
        "latest_close": None if rows[-1][3] is None else round(float(rows[-1][3]), 2),
        "close_min": round(min(closes), 2) if closes else None,
        "close_max": round(max(closes), 2) if closes else None,
    }


DAY_MAX_N = 1300
"""日视图一次最多画多少根。

「5 年」那档是 1250 根，留 50 根余量。上限不是怕慢（1250 根连滚动三线约 110ms），
是怕线太密：1300 根摊在 1000px 画布上已经 1.3 点/像素，再多就只剩一团墨。
想看更早的用"看这天以前"跳过去，不必一眼看完全部（全量 3000 根 ≈ 277KB JSON）。
"""


def day_series(n: int = TURNOVER_DAYS, db: str | Path | None = None,
               today: str | None = None, *, start: str | None = None,
               end: str | None = None) -> dict:
    """沪深300 某一段**交易日**的日换手率 + 日收盘 + 逐日三档线。

    这张图和 `turnover_series()` 是两条独立序列、两套口径，不是"粗细两个版本"：

    * 周：一年 = 52 周，水位 = 近 52 周中位数，地量线 1.0%，取整到 0.1% / 0.5%；
    * 日：一年 = 252 个交易日，水位 = 近 252 日中位数，地量线 0.2%，
      取整到 0.01% / 0.05%。

    为什么要能指定区间
    ------------------
    日K 从 2014-07 起一共 3000 根，页面上却只画最近 250 根。想回头看
    "2025-11-12 之前那段"，光有"最近 N 根"是够不着的。所以窗口由四个口子决定：

    | 传入 | 画哪一段 |
    |---|---|
    | 都不传 | 最后 `n` 个交易日（默认 250 根，和以前一样） |
    | 只给 `end` | `end`（含）往前 `n` 个交易日 |
    | 只给 `start` | `start`（含）往后 `n` 个交易日 |
    | 两个都给 | 这段区间里的全部，`n` 只作上限 |

    只给 `start` 那一种是给"往后翻一段"用的：调用方只需把上一段的最后一天 +1 天
    传进来，不必自己数交易日，日历的事留给 SQL。

    **滚动线一律向左多取 252 根**：窗口最左那根的三条线也要有完整窗口，否则图左端
    会出现一段"窗口不够、线偏低"的假台阶（与周图同理）。所以任何一段的图都是自洽的，
    不因为"从哪儿开始看"而改变线的位置。

    `rank` 是**每点相对它自己往前 252 个交易日**的分位，不是相对所选窗口。否则跨度
    从 1 年切到 5 年，悬停里那句"近 1 年 X% 分位"就变成了"近 5 年"，同一根日K 的
    说法跟着窗口漂。

    **不参与判定**：列表里的"量能"仍由 `rules.sentiment()` 按周线判。日视图只是
    背景细节，页面上写明这一点，所以图和表不会出现"图上放量、表里平稳"的错觉——
    那不是矛盾，是两个刻度。

    表不存在（还没跑过带日K 的新版 trend）→ `available=False`，
    页面把"日"按钮置灰并说明怎么补，而不是画一张空图。
    """
    empty = {"available": False, "points": [], "n": 0,
             "dry_line": TURNOVER_DAY_DRY_LINE,
             "low_line": None, "high_line": None, "heavy_line": None,
             "low_series": [], "high_series": [], "heavy_series": [],
             "median": None, "q90": None,
             "latest": None, "latest_value": None, "latest_close": None,
             "first": None, "db_first": None, "db_last": None,
             "is_today": False}
    p = Path(db or SHARED_DB)
    if not p.exists():
        return empty
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return empty
    try:
        if not conn.execute(
                "select 1 from sqlite_master where type='table' "
                "and name='market_index_day'").fetchone():
            return empty
        n = max(20, min(int(n), DAY_MAX_N))
        cols = "date, turnover, pct_chg, close"
        base = ("from market_index_day "
                "where symbol='sh000300' and turnover is not null")
        bounds = conn.execute(f"select min(date), max(date) {base}").fetchone()
        db_first, db_last = bounds[0], bounds[1]

        # 四种窗口各捞各的，共同点：都用 desc + limit 命中尾部（或前部），
        # 再 reverse 成正序，最后按 offset 切掉为了算线而多捞的那截。
        if start and end:
            span = conn.execute(
                f"select count(*) {base} and date >= ? and date <= ?",
                (start, end)).fetchone()[0]
            rows = conn.execute(
                f"select {cols} {base} and date <= ? order by date desc limit ?",
                (end, span + TURNOVER_DAY_WIN)).fetchall()
            rows.reverse()
            offset = len(rows) - span
        elif start:
            # 往后翻：`start` 之前多捞 252 根纯为算线，不画。
            pre = conn.execute(
                f"select {cols} {base} and date < ? order by date desc limit ?",
                (start, TURNOVER_DAY_WIN)).fetchall()
            pre.reverse()
            body = conn.execute(
                f"select {cols} {base} and date >= ? order by date asc limit ?",
                (start, n)).fetchall()
            rows = pre + body
            offset = len(pre)
        elif end:
            rows = conn.execute(
                f"select {cols} {base} and date <= ? order by date desc limit ?",
                (end, n + TURNOVER_DAY_WIN)).fetchall()
            rows.reverse()
            offset = len(rows) - n
        else:
            rows = conn.execute(
                f"select {cols} {base} order by date desc limit ?",
                (n + TURNOVER_DAY_WIN,)).fetchall()
            rows.reverse()
            offset = len(rows) - n
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
    if not rows or offset >= len(rows):
        return empty

    vals = [float(t) for _, t, _, _ in rows]
    kw = {"win": TURNOVER_DAY_WIN, "dry": TURNOVER_DAY_DRY_LINE,
          "step": _DAY_STEP, "heavy_step": _DAY_HEAVY_STEP}
    lines = [_volume_lines(vals[:i + 1], **kw) for i in range(len(vals))]
    low = [None if x is None else x["low"] for x in lines]
    high = [None if x is None else x["high"] for x in lines]
    heavy = [None if x is None else x["heavy"] for x in lines]
    # 分位按"该点往前 252 个交易日"算（含窗口外那截 lookback），切窗口不改变它。
    ranks = []
    for i, cur in enumerate(vals):
        win = vals[max(0, i - TURNOVER_DAY_WIN + 1):i + 1]
        ranks.append(round(sum(1 for x in win if x < cur) / len(win) * 100))

    cut = slice(offset, None)
    rows, vals = rows[cut], vals[cut]
    low, high, heavy = low[cut], high[cut], heavy[cut]
    ranks = ranks[offset:]

    ref = today or dt.date.today().isoformat()
    last_date = str(rows[-1][0])
    recent = vals[-TURNOVER_DAY_WIN:]
    return {
        "available": True,
        "points": [{"date": str(d), "turnover": round(float(t), 2),
                    "pct_chg": None if c is None else round(float(c), 2),
                    "close": None if k is None else round(float(k), 2),
                    "rank": rk}
                   for (d, t, c, k), rk in zip(rows, ranks)],
        "dry_line": TURNOVER_DAY_DRY_LINE,
        "low_line": low[-1],
        "high_line": high[-1],
        "heavy_line": heavy[-1],
        "low_series": low,
        "high_series": high,
        "heavy_series": heavy,
        "median": round(_median(recent), 2) if len(recent) >= 126 else None,
        "q90": round(_quantile(vals, 90), 2),
        "n": len(rows),
        "latest": last_date,
        "latest_value": round(float(rows[-1][1]), 2),
        "latest_close": None if rows[-1][3] is None else round(float(rows[-1][3]), 2),
        # 这一段的两头，外加全表的两头——前端靠后者决定"前一段/后一段"该不该置灰。
        "first": str(rows[0][0]),
        "db_first": str(db_first) if db_first else None,
        "db_last": str(db_last) if db_last else None,
        # 最后一根是不是"今天"。日K 收盘才定稿，盘中跑出来的那根不完整，
        # 页面据此提示一句，但不改任何值、不判档。
        "is_today": last_date == ref,
    }


def batches(conn, limit: int = 52) -> list[dict]:
    """批次列表（每个基准日一条），新的在前。

    "批次"取的是**基准日**而不是"某天几点跑的"：周五盘后那一批票，
    四个工具的 `target_date` 都是那个周五，这就是同一批。同一批次里
    重复跑（改了参数重算）会追加新的 run，这里只取每个工具的最新一次，
    但"跑过几次"单独记下来——重跑本身是值得知道的信息。
    """
    rows = conn.execute(
        f"select r.id, r.tool, r.target_date, r.started_at, r.n_ok, r.n_fail, "
        f"{_kind_expr(conn)} as kind from run r "
        f"where {_kind_expr(conn)}=? order by r.id desc", (KIND_BATCH,)).fetchall()

    groups: dict[str, dict] = {}
    for row in rows:
        date = row["target_date"]
        g = groups.get(date)
        if g is None:
            if len(groups) >= limit:
                continue
            g = groups[date] = {"date": date, "tools": {}, "runs": 0}
        g["runs"] += 1
        if row["tool"] not in g["tools"]:          # 每个工具留最新一次
            g["tools"][row["tool"]] = _run(row)

    days = _trading_days()
    out = []
    for date in sorted(groups, reverse=True):
        g = groups[date]
        missing = [t for t in ORDER if t not in g["tools"]]
        trading = _is_trading_day(date, days)
        out.append({
            "date": date,
            "tools": {t: g["tools"].get(t) for t in ORDER},
            "missing": missing,
            "miss": {t: _missing_note(t, date, g["tools"], trading)
                     for t in missing},
            "n_tools": len(g["tools"]),
            "runs": g["runs"],
        })
    return out


def _tool_command(tool: str, date: str) -> str:
    if tool == "top":
        return f"./run.py top --from-select --date {date}"
    return f"./run.py {tool} --date {date}"


def _missing_note(tool: str, date: str, tools: dict, trading: bool) -> dict:
    """某个批次缺某一步时，页面要显示的原因和补跑命令。

    "未跑"本身不是错误；但用户需要知道**为什么没跑、能不能补**。这里把
    交易日/周日、缺选股名单这两类原因区分开：周日的批次补不了，缺名单的
    下游工具也不能直接 `--from-select`，得先补选股。
    """
    label = TOOLS[tool][1]
    if not trading:
        try:
            weekday = _WEEKDAYS[dt.date.fromisoformat(date).weekday()]
        except ValueError:
            weekday = ""
        when = f"{date} 是{weekday}，" if weekday else f"{date} "
        return {
            "reason": f"{when}不在交易日历，不能补跑{label}。",
            "commands": [],
        }

    if tool == "select":
        return {
            "reason": f"{date} 还没有批量选股记录，可直接补跑。",
            "commands": [_tool_command("select", date)],
        }

    commands = []
    if "select" not in tools:
        commands.append(_tool_command("select", date))
        reason = f"缺少 {date} 的选股名单，{label}不能直接批量补跑；先补选股。"
    else:
        reason = f"{date} 还没有批量{label}记录，可直接补跑。"
    commands.append(_tool_command(tool, date))
    return {"reason": reason, "commands": commands}


def batch_runs(conn, date: str) -> dict[str, dict]:
    """某批次里每个工具的最新一次批量运行。"""
    out = {}
    for tool in ORDER:
        row = conn.execute(
            f"select r.*, {_kind_expr(conn)} as kind from run r "
            "where r.tool=? and r.target_date=? and "
            f"{_kind_expr(conn)}=? order by r.id desc limit 1",
            (tool, date, KIND_BATCH)).fetchone()
        if row:
            out[tool] = _run(row)
    return out


# ------------------------------------------------------------------ 取数

def _load(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["data"] = _js(row["payload"])
    return d


def _js(text) -> dict:
    """payload 列 → dict；坏数据给空 dict（老库、手改过的库都不该让页面 500）。"""
    if not text:
        return {}
    try:
        val = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def inner(row: dict) -> dict:
    """工具自己那份原始 payload。

    历史原因：写入时 `results.save` 存的是"整行 dict"，而调用方又在行里放了
    一个 `payload` 字段，于是 JSON 里套了一层。这里统一向下剥，
    页面只认 `inner()` 的结果，不必知道这层历史。
    """
    data = row.get("data") or {}
    seen = 0
    while isinstance(data.get("payload"), dict) and seen < 3:
        data = data["payload"]
        seen += 1
    return data


def report_html(row: dict | None) -> str:
    """工具在算完时渲染好的原版报告（gzip+base64，存在 payload.report_html）。

    看板只解压、不渲染：四个工具各有同名 `config.py`，同一个 Flask 进程里
    没法同时 import 它们。解不开就给空串，页面退回明细视图。
    """
    blob = (inner(row) or {}).get("report_html")
    if not isinstance(blob, str) or not blob:
        return ""
    try:
        return gzip.decompress(base64.b64decode(blob.encode("ascii"))).decode("utf-8")
    except Exception:                            # noqa: BLE001
        return ""


def row_of_run(conn, tool: str, run_id: int, symbol: str) -> dict | None:
    """指定那一次运行里这只票的结果（`?run=<id>` 定向到某个时间点）。"""
    table = TOOLS[tool][0]
    row = conn.execute(
        f"select t.*, r.id as run_id, r.target_date, r.started_at, "
        f"{_kind_expr(conn)} as kind "
        f"from {table} t join run r on r.id=t.run_id "
        "where t.run_id=? and t.symbol=?", (run_id, symbol)).fetchone()
    if not row:
        return None
    out = _load(row)
    out["kind_label"] = KIND_LABEL.get(out.get("kind") or KIND_BATCH, out.get("kind"))
    return out


def rows_of(conn, tool: str, run_id: int) -> list[dict]:
    table = TOOLS[tool][0]
    rows = conn.execute(f"select * from {table} where run_id=?", (run_id,)).fetchall()
    return [_load(r) for r in rows]


def scope(conn, tool: str, date: str | None = None,
          run_id: int | None = None) -> tuple[dict | None, list[dict]]:
    """某个工具"这一屏要显示哪一次运行"。

    优先级：`run_id`（精确到某一次运行）> `date`（某批次的最新一次批量运行）
    > 最新批次 > 任何最新一次运行。返回 (运行台账, 结果行)。
    """
    meta = None
    if run_id:
        meta = run_row(conn, run_id)
        if meta and meta["tool"] != tool:
            meta = None                            # 别让 ?run= 跨工具串页面
    if meta is None and date:
        meta = batch_runs(conn, date).get(tool)
    if meta is None and not date and not run_id:
        latest = batch_date(conn)
        if latest:
            meta = batch_runs(conn, latest).get(tool)
        if meta is None:                           # 只有单只跑过
            row = conn.execute(
                f"select r.*, {_kind_expr(conn)} as kind from run r "
                "where r.tool=? order by r.id desc limit 1", (tool,)).fetchone()
            meta = _run(row) if row else None
    if meta is None:
        return None, []
    return meta, rows_of(conn, tool, meta["id"])


def merged(conn, date: str | None = None) -> list[dict]:
    """合并表：一只票一行，四个工具的结论并排（默认最新批次）。"""
    date = date or batch_date(conn)
    runs = batch_runs(conn, date) if date else {}
    by_tool = {t: {r["symbol"]: r for r in rows_of(conn, t, runs[t]["id"])}
               for t in ORDER if t in runs}
    for t in ORDER:
        by_tool.setdefault(t, {})
    out = []
    for sym in sorted(set().union(*[set(v) for v in by_tool.values()])):
        sel = by_tool["select"].get(sym) or {}
        cyc = by_tool["cycle"].get(sym) or {}
        qua = by_tool["quality"].get(sym) or {}
        top = by_tool["top"].get(sym) or {}
        out.append({
            "symbol": sym,
            "name": top.get("name") or qua.get("name") or cyc.get("name")
                    or sel.get("name") or "",
            "select": sel, "cycle": cyc, "quality": qua, "top": top,
            "in_select": bool(sel),
        })
    return out


def counts(conn, date: str | None = None) -> dict:
    """批次内每个工具的行数（页面标题与图例用）。"""
    date = date or batch_date(conn)
    runs = batch_runs(conn, date) if date else {}
    return {t: (len(rows_of(conn, t, runs[t]["id"])) if t in runs else 0)
            for t in ORDER}


def market_caps(conn, symbols) -> dict[str, float]:
    """`symbol → 总市值（亿元）`，取每只票在 `price` 表里**最新一天**的 `total_mv`。

    为什么取各自最新一行、而不是"当前批次那一天"：`price` 只有跑过
    `./run.py price` 才有新行，停牌或那天没抓到的票，在那一天就是空的；
    而"这只票现在多大"跟页面在看哪一批无关。口径与
    `select_result.market_cap_yi`（启动日快照）一致，都回答同一个问题。

    只为**页面上的市值过滤**服务，所以缺价的票不进字典——调用方按
    「查不到市值就留下」处理：缺行情不是"大市值"的证据，宁可多显示一只，
    也不该把票悄悄藏起来（藏了还没人知道）。

    量级：全市场 5000+ 只按 500 一批分块查，避免 `IN (...)` 超过 SQLite 的
    参数上限（999）。
    """
    syms = [str(s) for s in (symbols or []) if s]
    if conn is None or not syms or not _has(conn, "price"):
        return {}
    out: dict[str, float] = {}
    chunk = 500
    for start in range(0, len(syms), chunk):
        part = syms[start:start + chunk]
        marks = ",".join("?" * len(part))
        sql = ("SELECT p.symbol, p.total_mv FROM price p "
               f"WHERE p.symbol IN ({marks}) AND p.total_mv IS NOT NULL "
               "  AND p.date = (SELECT MAX(q.date) FROM price q "
               "                WHERE q.symbol = p.symbol "
               "                  AND q.total_mv IS NOT NULL)")
        for symbol, total_mv in conn.execute(sql, part).fetchall():
            try:
                out[symbol] = float(total_mv) / 1e8
            except (TypeError, ValueError):
                continue
    return out


# ------------------------------------------------------------------ 个股

def symbol_row(conn, tool: str, symbol: str) -> dict | None:
    """某工具上这只票**最近一次**的结果（批量或单只都算）。"""
    table = TOOLS[tool][0]
    row = conn.execute(
        f"select t.*, r.id as run_id, r.target_date, r.started_at, "
        f"{_kind_expr(conn)} as kind "
        f"from {table} t join run r on r.id=t.run_id "
        "where t.symbol=? order by r.id desc limit 1", (symbol,)).fetchone()
    if not row:
        return None
    out = _load(row)
    out["kind_label"] = KIND_LABEL.get(out.get("kind") or KIND_BATCH, out.get("kind"))
    return out


def union_symbols(conn, date: str | None = None) -> list[dict]:
    """某批次里出现过的全部股票（代码 + 名称），用于导航与合并表。"""
    date = date or batch_date(conn)
    runs = batch_runs(conn, date) if date else {}
    seen: dict[str, dict] = {}
    for tool in ORDER:
        if tool not in runs:
            continue
        for r in rows_of(conn, tool, runs[tool]["id"]):
            sym = r["symbol"]
            item = seen.setdefault(sym, {"symbol": sym, "name": r.get("name")})
            if not item.get("name") and r.get("name"):
                item["name"] = r["name"]
    return sorted(seen.values(), key=lambda x: x["symbol"])
