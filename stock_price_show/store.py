"""落库：写 `shared.db.price`，镜像到 `results.db.price`，再算 `price_stat`。

三条硬约定（每一条都对应过一个具体的坑）：

1. **整天整份替换**（`DELETE WHERE date=?` + 批量 `INSERT`，一个事务）。
   某一天的"全市场"是一个原子事实。上游这次回 5579 只、上次 5500 只，逐行 upsert
   会把上次多出来的留在库里，于是"这天全市场 PE 中位数"被一批当天根本不在的股票
   悄悄拉偏，而任何地方都不会报错。
2. **价格与摘要同事务**。`price_stat` 是 `price` 的派生行，分开写会出现"价格换了
   一批、摘要还是上一批"的静默错配——页面上的涨跌家数和表格对不上，却没有报错。
3. **写结果库失败不影响原始库**。`shared.db` 是原件：它写成功了，结果库这一份随时
   可以用 `sync()` 补（不联网）。所以 `mirror` 单独的 try 里失败只记 warning。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

from . import schema, stat

BUSY_TIMEOUT_MS = 5000
"""实测：0 时两进程各写 5 批会丢 3–4 批；5000 时 0 失败。不能省。"""


def warn(msg: str) -> None:
    """非致命问题打到 stderr。落库失败不该让整条命令失败，但也不能没声音。"""
    print(f"[price] {msg}", file=sys.stderr)


_warn = warn


def connect(path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    """打开一个库并落实 PRAGMA，顺带把表补上（DDL 全是 `IF NOT EXISTS`）。

    `create=False` 时库必须已存在（只读巡检、`--restat` 前先判断有没有东西可算）。
    """
    p = Path(path)
    if not create and not p.exists():
        raise FileNotFoundError(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000.0,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def ensure(conn: sqlite3.Connection, which: str) -> None:
    """按 `which`（`shared` / `results`）建表。幂等，随时可调。"""
    ddl = schema.RESULTS_DDL if which == "results" else schema.SHARED_DDL
    with _tx(conn):
        schema.apply(conn, ddl)


@contextmanager
def _tx(conn: sqlite3.Connection):
    """`BEGIN IMMEDIATE` 事务。WAL 下 `DEFERRED` 在"先读后写"升级时可能 `SQLITE_BUSY`
    且**不能安全重试**，所以一律用 IMMEDIATE。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def write_day(conn: sqlite3.Connection, day: str, rows: list[dict], *,
              with_stat: bool = False, src: str | None = None) -> int:
    """把 `day` 这一天的全市场行写进 `price`（整天替换），返回写入行数。

    `with_stat=True` 时在同一事务里重算并写 `price_stat`（只有结果库需要）。
    只写 `date` 恰好等于 `day` 的行：上游混进别的日期由 `source` 拦下，
    这里是最后一道——真漏进来也不会污染其它日子的数据。
    """
    keep = [r for r in rows if str(r.get("date") or "") == day
            and r.get("symbol")]
    stamp = _now()
    src = src or (keep[0].get("src") if keep else None)
    cols = schema.PRICE_COLUMNS
    sql = (f"INSERT OR REPLACE INTO price({','.join(cols)}) "
           f"VALUES({','.join('?' * len(cols))})")
    with _tx(conn):
        conn.execute("DELETE FROM price WHERE date=?", (day,))
        conn.executemany(sql, [
            (r["date"], r["symbol"], r.get("name"), r.get("close"), r.get("pct_chg"),
             r.get("pe_ttm"), r.get("pb_mrq"), r.get("ps_ttm"), r.get("total_mv"),
             r.get("circ_mv"), src, stamp)
            for r in keep])
        if with_stat and keep:
            _upsert_stat(conn, stat.summarize(keep, src=src, updated_at=stamp))
    return len(keep)


def write_index(conn: sqlite3.Connection, day: str, row: dict) -> int:
    """写 `day` 的指数行（目前只有沪深300），整天替换。**不传 row 就不删旧值。**

    指数与全市场分开写：`price` 的"整天替换"保证 5579 只票是一个原子事实，
    指数只是一行辅助数据。它抓失败时价格照样可用，页面显示 `—` 即可。
    """
    if not row or str(row.get("date") or "") != day or not row.get("symbol"):
        return 0
    stamp = _now()
    cols = schema.INDEX_COLUMNS
    sql = (f"INSERT OR REPLACE INTO index_price({','.join(cols)}) "
           f"VALUES({','.join('?' * len(cols))})")
    with _tx(conn):
        conn.execute("DELETE FROM index_price WHERE date=?", (day,))
        conn.execute(sql, (row["date"], row["symbol"], row.get("name"),
                           row.get("close"), row.get("pct_chg"),
                           row.get("src"), stamp))
    return 1


def index_row(conn: sqlite3.Connection, day: str) -> dict | None:
    """`day` 的指数行；表/行不存在都返回 `None`（看板和同步都要能缺省）。"""
    if not _has(conn, "index_price"):
        return None
    row = conn.execute(
        "SELECT date,symbol,name,close,pct_chg,src FROM index_price WHERE date=?",
        (day,)).fetchone()
    return dict(row) if row else None


def _upsert_stat(conn: sqlite3.Connection, row: dict) -> None:
    cols = stat.COLUMNS
    sql = (f"INSERT INTO price_stat({','.join(cols)}) "
           f"VALUES({','.join('?' * len(cols))}) "
           "ON CONFLICT(date) DO UPDATE SET "
           + ",".join(f"{c}=excluded.{c}" for c in cols if c != "date"))
    conn.execute(sql, [row.get(c) for c in cols])


def restat(conn: sqlite3.Connection, day: str | None = None) -> int:
    """按 `price` 里**已有**的数据重算 `price_stat`，返回重算的天数。**不联网。**

    为什么需要它（而不只是在写价格时顺手算）：摘要是派生数据，口径会变
    （涨跌停阈值、"只取正 PE"这类规则都调过），改完就得把历史各天重算一遍；
    另外 `price` 表可能早于摘要表存在（老库/手工导入）。两种情形都只能靠库里
    现成的价格重算——重新取一次价格要重打上游 6 个请求/天，而这里一次都不打。
    """
    days = [day] if day else [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM price ORDER BY date")]
    done = 0
    for d in days:
        rows = [dict(r) for r in conn.execute(
            "SELECT date,symbol,name,close,pct_chg,pe_ttm,pb_mrq,ps_ttm,total_mv,"
            "       circ_mv,src FROM price WHERE date=?", (d,))]
        if not rows:
            continue
        try:
            row = stat.summarize(rows, src=rows[0].get("src"), updated_at=_now())
            with _tx(conn):
                _upsert_stat(conn, row)
            done += 1
        except Exception as e:                   # noqa: BLE001 - 一天算不出来不该挡住别的天
            _warn(f"price_stat {d} 重算失败：{e}")
    return done


def days(conn: sqlite3.Connection) -> list[str]:
    """库里有价格的日期，新→旧。"""
    return [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC")]


def count(conn: sqlite3.Connection, day: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM price WHERE date=?", (day,)).fetchone()
    return int(row[0]) if row else 0


def last_day(conn: sqlite3.Connection, before: str | None = None) -> str | None:
    """最近一个有价格的日期（可限定 `before` 之前）。给错误提示用。"""
    if before:
        row = conn.execute("SELECT MAX(date) FROM price WHERE date<?",
                           (before,)).fetchone()
    else:
        row = conn.execute("SELECT MAX(date) FROM price").fetchone()
    return str(row[0]) if row and row[0] else None


def sync(shared: sqlite3.Connection, results: sqlite3.Connection,
         day: str | None = None) -> list[str]:
    """把 `shared.price` 里已有的日子补/覆盖到 `results`（含摘要）。**不联网。**

    用途有两个：原始库抓成功但结果库那次写失败（磁盘满、库被锁）；
    或者想从别的机器上拷一份 `shared.db` 过来只补结果库。
    """
    targets = [day] if day else days(shared)
    done = []
    for d in targets:
        rows = [dict(r) for r in shared.execute(
            "SELECT date,symbol,name,close,pct_chg,pe_ttm,pb_mrq,ps_ttm,total_mv,"
            "       circ_mv,src FROM price WHERE date=?", (d,))]
        if not rows:
            continue
        write_day(results, d, rows, with_stat=True)
        idx = index_row(shared, d)
        if idx:
            write_index(results, d, idx)
        done.append(d)
    return done


def prune(conn: sqlite3.Connection, keep_days: int, today: str | None = None) -> int:
    """删掉 `keep_days` 天之前的交易日，返回删了多少行 `price`。

    `keep_days <= 0` 直接返回 0（不删）：这张表是"按日 × 全市场"（约 0.45MB/天），
    留多少历史该由人决定，不该由默认值决定。

    `price_stat` 一起删：只删价格会留下"那天还在"的假摘要（卡片显示 5579 只、
    表格却是空的）。
    """
    if keep_days <= 0:
        return 0
    base = today or dt.date.today().isoformat()
    cutoff = (dt.date.fromisoformat(base) - dt.timedelta(days=keep_days)).isoformat()
    with _tx(conn):
        cur = conn.execute("DELETE FROM price WHERE date < ?", (cutoff,))
        n = cur.rowcount or 0
        if _has(conn, "price_stat"):
            conn.execute("DELETE FROM price_stat WHERE date < ?", (cutoff,))
        if _has(conn, "index_price"):
            conn.execute("DELETE FROM index_price WHERE date < ?", (cutoff,))
    return n


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


__all__ = ["connect", "ensure", "write_day", "write_index", "index_row",
           "restat", "sync", "prune", "days", "count", "last_day",
           "BUSY_TIMEOUT_MS"]
