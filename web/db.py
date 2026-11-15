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
   个股页按"跑的时间"逐条列出来，不退化成"只留最后一次"。
4. **payload 是任意 JSON**：工具加字段不必改库表，网站按需读取。
   读不到就给 None，页面显示 `—`，不报错。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "results.db"

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

    out = []
    for date in sorted(groups, reverse=True):
        g = groups[date]
        out.append({
            "date": date,
            "tools": {t: g["tools"].get(t) for t in ORDER},
            "missing": [t for t in ORDER if t not in g["tools"]],
            "n_tools": len(g["tools"]),
            "runs": g["runs"],
        })
    return out


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


def symbol_history(conn, tool: str, symbol: str) -> list[dict]:
    """这只票在该工具上的历次结果（旧→新），带这次运行的时间与来源。

    同一只票可能被"周五批量"算过、又被"周三单只"重算过：两条都留着，
    页面上按时间排开，否则"我刚刚跑的为什么没生效"永远说不清。
    """
    table = TOOLS[tool][0]
    rows = conn.execute(
        f"select t.*, r.id as run_id, r.target_date, r.started_at, "
        f"{_kind_expr(conn)} as kind "
        f"from {table} t join run r on r.id=t.run_id "
        "where t.symbol=? order by r.id", (symbol,)).fetchall()
    out = []
    for row in rows:
        d = _load(row)
        d["kind"] = d.get("kind") or KIND_BATCH
        d["kind_label"] = KIND_LABEL.get(d["kind"], d["kind"])
        out.append(d)
    return out


def symbol_histories(conn, symbol: str) -> dict[str, list[dict]]:
    return {t: symbol_history(conn, t, symbol) for t in ORDER}


def _brief(tool: str, row: dict) -> str:
    """一行结论摘要（个股页的时间线用）。认不出的字段给 —，不抛。"""
    def n(key, digits=1):
        v = row.get(key)
        if v is None:
            return "—"
        try:
            return f"{float(v):.{digits}f}"
        except (TypeError, ValueError):
            return str(v)

    if tool == "select":
        stars = row.get("stars") or ""
        return f"入选 · 换手分位 {n('pool_turnover_rank')} {stars}".strip()
    if tool == "cycle":
        kind = "周期股" if row.get("is_cycle") else "非周期"
        ela = {"strong": "强", "weak": "弱", "unstable": "不稳定"}.get(
            row.get("elastic_label") or "", row.get("elastic_label") or "—")
        pos = {"low": "低位", "high": "高位", "neutral": "中位",
               "na": "不适用"}.get(row.get("position_label") or "",
                                   row.get("position_label") or "—")
        return f"{kind} · 弹性 {ela} · 产能 {pos}"
    if tool == "quality":
        if row.get("q") is None and row.get("v") is None:
            # 早期从 HTML 回读的那几批只有文字，没有结构化字段
            return "（旧记录：只有报告，没有 Q/V）"
        return f"Q {n('q')} / V {n('v')} · {row.get('conclusion') or '—'}"
    if tool == "top":
        gap = row.get("pos250")
        gap = f"{float(gap) * 100:+.1f}%" if gap is not None else "—"
        return (f"距高点 {gap} · 动量 {row.get('momentum_state') or '—'}"
                f" · 换手 {row.get('turnover_state') or '—'}")
    return ""


def symbol_timeline(conn, symbol: str) -> list[dict]:
    """这只票在四个工具上的全部运行记录，**按跑的先后倒序**。

    同一只票可能周五批量算过、周一单只又算过：这两条都要出现，
    而且"什么时候跑的 / 属于哪个批次 / 批量还是单只"要写在同一行上，
    否则没法解释"为什么结论和周五那批不一样"。
    """
    out = []
    for tool in ORDER:
        for row in symbol_history(conn, tool, symbol):
            out.append({
                "tool": tool, "tool_label": TOOLS[tool][1], "href": TOOLS[tool][3],
                "run_id": row.get("run_id"),
                "started_at": row.get("started_at"),
                "target_date": row.get("target_date"),
                "kind": row.get("kind"),
                "kind_label": row.get("kind_label"),
                "brief": _brief(tool, row),
            })
    out.sort(key=lambda x: (x.get("started_at") or "", x.get("run_id") or 0),
             reverse=True)
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
