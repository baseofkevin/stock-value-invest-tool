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


# ---------------------------------------------------------------- 冷热时点（results.db 的 price）
#
# 「冷热时点」页读的是**结果库**：`stock_price_show` 抓完原始层后会把同一天
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


def price_page(*, date: str | None = None, q: str = "", sort: str = "pct",
               order: str = "desc", start: str | None = None,
               end: str | None = None, db: str | Path | None = None) -> dict:
    """「冷热时点」页要的全部数据。**路由只调这一个函数，不写 SQL。**

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


def regime(conn, limit: int = 8) -> dict:
    """A股大势的最近几周（最新在前）。

    **不在这里算任何东西**：分位、趋势、状态全是 `stock_market_regime` 跑完就定格的，
    看板只负责摆出来。也不去 `shared.db` 取原件——页面只读这一个库。

    三种"没有"都返回空壳，由页面说人话：
    * 表不存在（从没跑过这个功能）→ `row=None`
    * 表在但没有行（跑过、但三路都没算出来）→ `row=None`
    * 某几列是 NULL（那一路当天没数据）→ 字段为 None，页面显示 `—`
    """
    empty = {"row": None, "weeks": []}
    if conn is None or not _has(conn, "market_regime"):
        return empty

    cols = ("date, computed_at, data_asof, stale, trend_state, trend_score, "
            "trend_close, ma20, ma60, ma60_slope, weeks_below_ma20, erp, "
            "pe_ttm, bond10y, erp_pct, below_na, below_na_pct, turnover, "
            "turnover_q10, volume_dry")
    rows = conn.execute(
        f"SELECT {cols} FROM market_regime ORDER BY date DESC").fetchall()
    if not rows:
        return empty

    def _row(r) -> dict:
        d = dict(r)
        d["stale"] = bool(d.get("stale"))
        d["volume_dry"] = None if d.get("volume_dry") is None else bool(d["volume_dry"])
        return d

    weeks = [_row(r) for r in rows[:limit]]
    return {"row": weeks[0], "weeks": weeks}


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
