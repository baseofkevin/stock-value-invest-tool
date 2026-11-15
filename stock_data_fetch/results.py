"""派生结果的存储层：`results.db`（**与 shared.db 无关，可随时删掉重建**）。

为什么要单独一个库
------------------
`shared.db` 存的是**上游数据**：K 线、财务、估值、覆盖表。它的不变量是
"只追加、raw、覆盖与数据同事务写入"，清理脚本（`maintain`）会动它。
四个工具每次跑出来的**结论**（选股名单、弹性档位、Q/V、四因子状态）是另一类
东西：由 shared.db 算出来的、可以整库丢弃重算的派生数据。

混在一起的风险很具体：`maintain.py --apply` 会 VACUUM 并清理 coverage，
如果结果也躺在里面，一次清理就可能把上周的选股名单一起带走，而且
`shared.db` 的备份策略（VACUUM INTO）会把结果一起复制，越滚越大。

为什么**一个库**而不是每个工具一个
----------------------------------
真正要回答的问题是跨工具的："这只票选股进去了吗？Q 多少？顶部四因子怎么说？"
一个库里 JOIN 就完了；四个库要用 ATTACH 拼，还得记住四个路径。
体量也不构成理由：一次全流程 = 4 张表各 38 行左右，一年 52 周也就几万行。

表结构（每工具一张表 + 一张 run 台账）
--------------------------------------
    run            每次运行一行：工具、基准日、起止时间、命令行、批量/单只、成功/失败数
    select_result  选股：启动日、量能倍数、距 MA%、换手分位、星级、收盘、市值
    cycle_result   周期弹性：是否周期股、毛利率 σ/CV/中位、弹性比值与档位、产能分位
    quality_result 质量初筛：Q/V 与档位、结论、风险命中/提示/已知
    top_result     顶部判断：收盘、距 250 日高点、四因子状态、周期状态、PE 分位

每张表都带 `payload`（该工具的完整结果 JSON）：加字段不必改表，
但常用字段单独成列，方便直接 SQL 查。

批量 / 单只（`run.kind`）
-----------------------
看板要回答两种问题："这周五那批票怎么样"和"我刚才单跑的那只现在什么结论"。
两者都写进同一张表，靠 `kind` 区分：

* `batch` —— 一次覆盖整份名单（`--from-select` / `--csv` / 选股全池）
* `single` —— 单只模式（`--stock` / `--stock-code` / 一个代码）

没有这一列时只能拿"行数 == 1"去猜，而季报期前后单只跑出来 1 行、
批量也可能只选出 1 只，猜错就会把单只结果混进周五那一批的统计里。

写入约定
--------
* **写失败绝不影响主流程**：结果库是附加产物，磁盘满/锁冲突时只 warn 一行。
* 同一 (tool, target_date) 重复跑 → 追加新的 run，旧 run 保留（留痕，便于对比
  两次运行的差异）。要"只留最新"，用 `results.py --prune` 或直接删库。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

#: 结果库路径：默认仓库根目录的 results.db，可用 `STOCK_RESULTS_DB` 覆盖。
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "results.db"

TOOLS = ("select", "cycle", "quality", "top")

SCHEMA = """
create table if not exists run(
    id          integer primary key autoincrement,
    tool        text not null,
    target_date text not null,
    started_at  text not null,
    finished_at text,
    argv        text,
    kind        text not null default 'batch',
    n_ok        integer not null default 0,
    n_fail      integer not null default 0,
    note        text
);
create index if not exists run_tool_date on run(tool, target_date, started_at);

create table if not exists select_result(
    run_id integer not null references run(id) on delete cascade,
    symbol text not null,
    name text,
    industry text,
    launch_date text,
    days_since_launch integer,
    vol_multiple real,
    launch_gain_pct real,
    ma20_gap_pct real,
    ma60_gap_pct real,
    gross_margin_up text,
    turnover20 real,
    pool_turnover_rank real,
    pool_size integer,
    stars text,
    close real,
    market_cap_yi real,
    payload text,
    primary key(run_id, symbol)
);
create index if not exists select_lookup on select_result(symbol);

create table if not exists cycle_result(
    run_id integer not null references run(id) on delete cascade,
    symbol text not null,
    name text,
    is_cycle integer,
    gm_std_pp real,
    gm_cv_pct real,
    gm_median_pct real,
    elasticity real,
    elastic_label text,
    position_pct real,
    position_label text,
    payload text,
    primary key(run_id, symbol)
);
create index if not exists cycle_lookup on cycle_result(symbol);

create table if not exists quality_result(
    run_id integer not null references run(id) on delete cascade,
    symbol text not null,
    name text,
    q real,
    q_grade text,
    v real,
    v_grade text,
    conclusion text,
    reason text,
    risks_hit integer,
    risks_watch integer,
    risks_known integer,
    payload text,
    primary key(run_id, symbol)
);
create index if not exists quality_lookup on quality_result(symbol);

create table if not exists top_result(
    run_id integer not null references run(id) on delete cascade,
    symbol text not null,
    name text,
    close real,
    pos250 real,
    days_since_high integer,
    momentum_state text,
    turnover_state text,
    earnings_state text,
    volume_state text,
    cycle_state text,
    pe_pct real,
    target_upside real,
    payload text,
    primary key(run_id, symbol)
);
create index if not exists top_lookup on top_result(symbol);
"""

TABLE = {"select": "select_result", "cycle": "cycle_result",
         "quality": "quality_result", "top": "top_result"}


def path(db: str | Path | None = None) -> Path:
    if db:
        return Path(db).expanduser()
    env = os.environ.get("STOCK_RESULTS_DB")
    return Path(env).expanduser() if env else DEFAULT_PATH


def connect(db: str | Path | None = None) -> sqlite3.Connection:
    p = path(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma foreign_keys=on")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


#: 结果库当前 schema 版本。与 shared.db 的 user_version 是两套编号，互不相干。
SCHEMA_VERSION = 2


def _migrate(conn) -> None:
    """把老库补到当前 schema。

    v1 → v2：`run` 加 `kind`（批量/单只）。这个库可以整库删掉重跑，
    但"删库"不该是升级的默认要求——手上正好有一批想对比的历史时，
    让人为了看板丢掉它不合适。加列是 additive 的，老行按 'batch' 算。
    """
    ver = conn.execute("pragma user_version").fetchone()[0]
    if ver >= SCHEMA_VERSION:
        return
    if ver < 2:
        cols = {r[1] for r in conn.execute("pragma table_info(run)")}
        if "kind" not in cols:
            conn.execute("alter table run add column kind text not null default 'batch'")
    conn.execute(f"pragma user_version={SCHEMA_VERSION}")
    conn.commit()


def norm_symbol(code) -> str:
    """统一成 `sh600519` / `sz000001` / `bj920002` 这种带市场前缀的形式。

    四个工具内部各有各的写法：kline_screen 用 `sh600519`，cycle 的 `codes.parse`
    给的是 `600519`，judge_top 是 `sh600519`。结果库要能跨工具 JOIN，
    **必须**在入库时归一，否则同一只票在两张表里是两个 symbol——
    这个坑不报错，只是查询静默查不到。
    """
    raw = str(code or "").strip().lower()
    if not raw:
        return ""
    if raw[:2] in ("sh", "sz", "bj") and len(raw) == 8:
        return raw
    if "." in raw:                                # 600519.SH / 600519.XSHG
        num, _, suffix = raw.partition(".")
        mkt = {"sh": "sh", "xshg": "sh", "ss": "sh", "sz": "sz",
               "xshe": "sz", "bj": "bj", "bse": "bj"}.get(suffix, "")
        return f"{mkt}{num}" if mkt else num
    if raw.isdigit() and len(raw) == 6:
        if raw[0] in ("0", "3"):
            return "sz" + raw
        if raw[0] in ("4", "8"):
            return "bj" + raw
        if raw[:3] in ("920",):
            return "bj" + raw
        return "sh" + raw
    return raw


def _num(v):
    """转成 float；非数值（'—'、'不适用'、'***'）→ None，绝不抛。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


#: `run.kind` 的合法值。`batch` = 覆盖整份名单，`single` = 单只模式。
KINDS = ("batch", "single")


def start_run(conn, tool: str, target_date: str, argv: str | None = None,
              note: str | None = None, kind: str = "batch") -> int:
    if kind not in KINDS:
        raise ValueError(f"未知 kind {kind!r}，只支持 {KINDS}")
    cur = conn.execute(
        "insert into run(tool, target_date, started_at, argv, kind, note) "
        "values(?,?,?,?,?,?)",
        (tool, target_date, dt.datetime.now().isoformat(timespec="seconds"),
         argv, kind, note))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn, run_id: int, n_ok: int, n_fail: int) -> None:
    conn.execute("update run set finished_at=?, n_ok=?, n_fail=? where id=?",
                 (dt.datetime.now().isoformat(timespec="seconds"),
                  n_ok, n_fail, run_id))
    conn.commit()


# ------------------------------------------------------------------ 各工具的映射

def _select_row(r: dict) -> tuple:
    return (
        r.get("symbol") or r.get("代码"), r.get("name") or r.get("名称"),
        r.get("industry") or r.get("一级行业"),
        r.get("launch_date") or r.get("启动日"),
        r.get("days_since_launch") if r.get("days_since_launch") is not None
        else r.get("距启动交易日"),
        _num(r.get("vol_multiple") if r.get("vol_multiple") is not None
             else r.get("启动日量能倍数")),
        _num(r.get("launch_gain_pct") if r.get("launch_gain_pct") is not None
             else r.get("启动日涨幅%")),
        _num(r.get("ma20_gap_pct") if r.get("ma20_gap_pct") is not None
             else r.get("距MA20%")),
        _num(r.get("ma60_gap_pct") if r.get("ma60_gap_pct") is not None
             else r.get("距MA60%")),
        str(r.get("gross_margin_up") or r.get("毛利率改善") or ""),
        _num(r.get("turnover20") if r.get("turnover20") is not None
             else r.get("20日换手%")),
        _num(r.get("pool_turnover_rank") if r.get("pool_turnover_rank") is not None
             else r.get("池内换手分位")),
        r.get("pool_size"),
        str(r.get("stars") or r.get("分位星级") or ""),
        _num(r.get("close") if r.get("close") is not None else r.get("最新收盘价")),
        _num(r.get("market_cap_yi") if r.get("market_cap_yi") is not None
             else r.get("总市值（亿）")),
        json.dumps(r, ensure_ascii=False, default=str),
    )


def _cycle_row(r: dict) -> tuple:
    return (
        r.get("symbol") or r.get("code"), r.get("name"),
        1 if r.get("is_cycle") else 0,
        _num(r.get("gm_std")), _num(r.get("gm_cv")), _num(r.get("gm_median")),
        _num(r.get("elasticity")), r.get("elastic_label"),
        _num(r.get("position_pct")), r.get("position_label"),
        json.dumps(r, ensure_ascii=False, default=str),
    )


def _quality_row(r: dict) -> tuple:
    return (
        r.get("symbol") or r.get("code"), r.get("name"),
        _num(r.get("q")), r.get("q_grade"), _num(r.get("v")), r.get("v_grade"),
        r.get("conclusion"), r.get("reason"),
        r.get("risks_hit"), r.get("risks_watch"), r.get("risks_known"),
        json.dumps(r, ensure_ascii=False, default=str),
    )


#: 卡片标题 → 结果库列名（标题在 views 里是中文，批量里存的是 (标题, 状态) 元组）
_VIEW_KEYS = {"动量": "momentum", "换手": "turnover", "业绩": "earnings",
              "量价": "volume", "周期位置": "cycle"}


def _top_row(r: dict) -> tuple:
    ex = r.get("extras") or {}
    val = (ex.get("valuation") or {}).get("metrics") or {}
    con = ex.get("consensus") or {}
    views: dict = {}
    for v in (r.get("views") or []):
        if isinstance(v, (tuple, list)) and len(v) >= 2:
            key = _VIEW_KEYS.get(str(v[0]).strip())
            if key:
                views[key] = v[1]
        elif isinstance(v, dict):
            key = v.get("key") or _VIEW_KEYS.get(str(v.get("title") or "").strip())
            if key:
                views[key] = v.get("state")
    return (
        r.get("code") or r.get("symbol"), r.get("name"),
        _num(r.get("close")), _num(r.get("pos250")), r.get("days_since_high"),
        views.get("momentum"), views.get("turnover"),
        views.get("earnings"), views.get("volume"), views.get("cycle"),
        _num((val.get("pe") or {}).get("percentile")),
        _num(con.get("upside")),
        json.dumps(r, ensure_ascii=False, default=str),
    )


_COLUMNS = {
    "select": ("symbol,name,industry,launch_date,days_since_launch,vol_multiple,"
               "launch_gain_pct,ma20_gap_pct,ma60_gap_pct,gross_margin_up,"
               "turnover20,pool_turnover_rank,pool_size,stars,close,"
               "market_cap_yi,payload"),
    "cycle": ("symbol,name,is_cycle,gm_std_pp,gm_cv_pct,gm_median_pct,"
              "elasticity,elastic_label,position_pct,position_label,payload"),
    "quality": ("symbol,name,q,q_grade,v,v_grade,conclusion,reason,"
                "risks_hit,risks_watch,risks_known,payload"),
    "top": ("symbol,name,close,pos250,days_since_high,momentum_state,"
            "turnover_state,earnings_state,volume_state,cycle_state,"
            "pe_pct,target_upside,payload"),
}

_MAPPER = {"select": _select_row, "cycle": _cycle_row,
           "quality": _quality_row, "top": _top_row}


def save(tool: str, rows: list[dict], target_date: str, *, argv: str | None = None,
         db: str | Path | None = None, note: str | None = None,
         kind: str = "batch") -> int:
    """把一次运行的结果写进结果库，返回 run_id。

    **不抛异常**：任何失败都只在 stderr 留一行，主流程照常结束。
    """
    if tool not in TOOLS:
        raise ValueError(f"未知工具 {tool!r}，只支持 {TOOLS}")
    # kind 写错是调用方的编程错误，不是"落库偶发失败"：必须抛出来，
    # 别被下面那个 `except Exception: warn` 吞掉，否则全库都变成 batch。
    if kind not in KINDS:
        raise ValueError(f"未知 kind {kind!r}，只支持 {KINDS}")
    if not rows:
        return 0
    try:
        conn = connect(db)
    except Exception as e:                      # noqa: BLE001
        _warn(f"结果库打不开（{e}），本次结果未落库")
        return 0
    try:
        run_id = start_run(conn, tool, target_date, argv, note, kind)
        mapper = _MAPPER[tool]
        cols = _COLUMNS[tool]
        sql = (f"insert or replace into {TABLE[tool]}(run_id,{cols}) "
               f"values(?," + ",".join("?" * len(cols.split(","))) + ")")
        n_ok = 0
        for r in rows:
            try:
                vals = list(mapper(r))
                vals[0] = norm_symbol(vals[0])
                if not vals[0]:
                    # 空 symbol 在 SQLite 里是合法主键（空串 ≠ NULL），
                    # 悄悄进库后每次查都是"一行没有代码的怪东西"。直接拒收。
                    _warn("跳过一行没有代码的结果")
                    continue
                conn.execute(sql, (run_id,) + tuple(vals))
                n_ok += 1
            except Exception as e:              # noqa: BLE001
                _warn(f"写入一行失败（{r.get('symbol') or r.get('code')}）：{e}")
        finish_run(conn, run_id, n_ok, len(rows) - n_ok)
        return run_id
    except Exception as e:                      # noqa: BLE001
        _warn(f"结果落库失败：{e}")
        return 0
    finally:
        conn.close()


def _warn(msg: str) -> None:
    import sys
    print(f"[results] {msg}", file=sys.stderr)


# ------------------------------------------------------------------ 查询

def latest(tool: str, db: str | Path | None = None) -> list[dict]:
    """某工具最近一次运行的全部结果（按 run id 最大）。"""
    conn = connect(db)
    try:
        row = conn.execute("select max(id) from run where tool=?", (tool,)).fetchone()
        if not row or row[0] is None:
            return []
        cur = conn.execute(
            f"select * from {TABLE[tool]} where run_id=? order by symbol", (row[0],))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def latest_for(tool: str, target_date: str | None = None,
               db: str | Path | None = None) -> list[dict]:
    """基准日恰好是 `target_date` 的那一批（取最新一次运行）。

    下游三个工具原来靠"读当天的选股 CSV"接上选股结果，现在改读这里：
    日期对不上就是没有输入，宁可返回空、让调用方报错，也不要悄悄拿
    最近一次运行凑数——那会把 11-13 的名单算成 11-20 的结论。

    `target_date` 给 None 时才退回"最近一次运行"。
    """
    if not target_date:
        return latest(tool, db)
    conn = connect(db)
    try:
        row = conn.execute(
            "select max(id) from run where tool=? and target_date=?",
            (tool, str(target_date)[:10])).fetchone()
        if not row or row[0] is None:
            return []
        cur = conn.execute(
            f"select * from {TABLE[tool]} where run_id=? order by symbol", (row[0],))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def symbols_for(tool: str, target_date: str | None = None,
                db: str | Path | None = None) -> list[dict]:
    """`[{symbol, name}]`——下游工具要的最小清单。"""
    try:
        rows = latest_for(tool, target_date, db)
    except Exception:                              # noqa: BLE001
        return []
    return [{"symbol": r["symbol"], "name": r.get("name")} for r in rows if r.get("symbol")]


def history(tool: str, symbol: str, db: str | Path | None = None) -> list[dict]:
    """某只票在某工具上的历史结果（旧→新），用于看结论怎么变的。"""
    conn = connect(db)
    try:
        cur = conn.execute(
            f"select r.target_date, t.* from {TABLE[tool]} t join run r on r.id=t.run_id "
            "where t.symbol=? order by r.target_date, r.id", (symbol,))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def summary(db: str | Path | None = None) -> list[dict]:
    """每个工具最近一次运行的台账。"""
    conn = connect(db)
    try:
        out = []
        for tool in TOOLS:
            row = conn.execute(
                "select id, target_date, started_at, finished_at, n_ok, n_fail "
                "from run where tool=? order by id desc limit 1", (tool,)).fetchone()
            out.append({"tool": tool, "run_id": row[0] if row else None,
                        "target_date": row[1] if row else None,
                        "started_at": row[2] if row else None,
                        "n_ok": row[4] if row else 0,
                        "n_fail": row[5] if row else 0})
        return out
    finally:
        conn.close()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m stock_data_fetch.results",
        description="查看结果库 results.db（四个工具的结论都落在这一张库里）")
    ap.add_argument("--db", help="结果库路径（默认 results.db）")
    ap.add_argument("--tool", choices=TOOLS, help="只看某个工具")
    ap.add_argument("--symbol", help="某只票的历史（配 --tool）")
    ap.add_argument("--prune", action="store_true",
                    help="每个工具只保留最近 1 次运行，删掉更早的")
    a = ap.parse_args(argv)
    db = path(a.db)

    if a.prune:
        conn = connect(db)
        try:
            for tool in TOOLS:
                keep = conn.execute("select max(id) from run where tool=?",
                                    (tool,)).fetchone()[0]
                if keep is None:
                    continue
                cur = conn.execute(
                    "delete from run where tool=? and id<?", (tool, keep))
                conn.execute(f"delete from {TABLE[tool]} where run_id not in "
                             f"(select id from run where tool=?)", (tool,))
                conn.commit()
                print(f"{tool}: 删掉 {cur.rowcount} 次旧运行")
        finally:
            conn.close()
        return 0

    if not db.exists():
        print(f"结果库还不存在：{db}（跑一次 any 工具就会创建）")
        return 0

    if a.tool and a.symbol:
        rows = history(a.tool, a.symbol, db)
        if not rows:
            print(f"{a.tool} 里没有 {a.symbol} 的记录")
            return 0
        for r in rows:
            print(f"{r['target_date']}  " + "  ".join(
                f"{k}={v}" for k, v in r.items()
                if k not in ("payload", "target_date", "run_id")))
        return 0

    print(f"结果库：{db}（{db.stat().st_size / 1024:.0f} KB）")
    for s in summary(db):
        if a.tool and s["tool"] != a.tool:
            continue
        if s["run_id"] is None:
            print(f"  {s['tool']:8s} 还没有记录")
            continue
        print(f"  {s['tool']:8s} 基准日 {s['target_date']}  成功 {s['n_ok']} / 失败 {s['n_fail']}"
              f"  跑于 {s['started_at']}")
    return 0


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
