"""只读结果库访问层。

**一切都在这里读，别在路由里写 SQL。** 路由关心的是"页面要哪些数字"，
这里关心的是"results.db 里怎么取"——两件事混在一起时，改一个字段名
要翻十几个路由函数。

三条约定：

1. **一律只读**（`mode=ro`）。网站不该成为第二个写库的进程：`results.db`
   是 WAL 模式，工具在写、Front 在读，很容易踩到 "database is locked"。
   只读连接不会加写锁，也不会顺手把 -wal 文件 checkpoint 掉。
2. **每工具取最近一次运行**（`run.id` 最大）。首页是"当前状态"，不是"历史"；
   历史只在个股页用。
3. **payload 是任意 JSON**：工具加字段不必改库表，网站按需读取。
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


def connect(db: str | Path | None = None) -> sqlite3.Connection:
    p = Path(db or DEFAULT_DB)
    if not p.exists():
        raise FileNotFoundError(p)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------ 运行台账

def summary(conn) -> list[dict]:
    """每个工具最近一次运行的台账；没有记录的工具也占一行。"""
    out = []
    for tool in ORDER:
        row = conn.execute(
            "select id, target_date, started_at, finished_at, n_ok, n_fail "
            "from run where tool=? order by id desc limit 1", (tool,)).fetchone()
        table, label, desc, href = TOOLS[tool]
        out.append({
            "tool": tool, "label": label, "desc": desc, "href": href,
            "run_id": row["id"] if row else None,
            "target_date": row["target_date"] if row else None,
            "started_at": row["started_at"] if row else None,
            "n_ok": row["n_ok"] if row else 0,
            "n_fail": row["n_fail"] if row else 0,
        })
    return out


def latest_run_id(conn, tool: str) -> int | None:
    row = conn.execute("select max(id) from run where tool=?", (tool,)).fetchone()
    return row[0] if row else None


def runs(conn, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        "select id, tool, target_date, started_at, finished_at, n_ok, n_fail, argv "
        "from run order by id desc limit ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 各工具最新一批

def _rows(conn, tool: str) -> list[dict]:
    table = TOOLS[tool][0]
    run_id = latest_run_id(conn, tool)
    if run_id is None:
        return []
    rows = conn.execute(f"select * from {table} where run_id=?", (run_id,)).fetchall()
    return [_load(r) for r in rows]


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


def select_rows(conn) -> list[dict]:
    return _rows(conn, "select")


def cycle_rows(conn) -> list[dict]:
    return _rows(conn, "cycle")


def quality_rows(conn) -> list[dict]:
    return _rows(conn, "quality")


def top_rows(conn) -> list[dict]:
    return _rows(conn, "top")


# ------------------------------------------------------------------ 个股

def symbol_row(conn, tool: str, symbol: str) -> dict | None:
    """某工具最近一次运行里这只票的结果。"""
    table = TOOLS[tool][0]
    run_id = latest_run_id(conn, tool)
    if run_id is None:
        return None
    row = conn.execute(f"select * from {table} where run_id=? and symbol=?",
                       (run_id, symbol)).fetchone()
    return _load(row) if row else None


def symbol_history(conn, tool: str, symbol: str) -> list[dict]:
    """这只票在该工具上的历次结果（旧→新）。"""
    table = TOOLS[tool][0]
    rows = conn.execute(
        f"select r.target_date, r.started_at, t.* from {table} t "
        "join run r on r.id=t.run_id where t.symbol=? order by r.id", (symbol,)
    ).fetchall()
    return [_load(r) for r in rows]


def union_symbols(conn) -> list[dict]:
    """四个工具出现过的全部股票（代码 + 名称），用于导航与合并表。"""
    seen: dict[str, dict] = {}
    for tool in ORDER:
        for r in _rows(conn, tool):
            sym = r["symbol"]
            item = seen.setdefault(sym, {"symbol": sym, "name": r.get("name")})
            if not item.get("name") and r.get("name"):
                item["name"] = r["name"]
    return sorted(seen.values(), key=lambda x: x["symbol"])


def merged(conn) -> list[dict]:
    """首页那张合并表：一只票一行，四个工具的结论并排。

    `run` 的基准日可能不同（周期的"基准日"是最近财报期而非交易日），
    所以这里只按 symbol 对齐，日期在页面上单独标。
    """
    by_tool = {t: {r["symbol"]: r for r in _rows(conn, t)} for t in ORDER}
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


def counts(conn) -> dict:
    """页面顶部的小计数（图例与标题用）。"""
    def n(tool):
        run_id = latest_run_id(conn, tool)
        if run_id is None:
            return 0
        table = TOOLS[tool][0]
        return conn.execute(f"select count(*) from {table} where run_id=?",
                            (run_id,)).fetchone()[0]
    return {t: n(t) for t in ORDER}
