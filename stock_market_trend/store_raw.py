"""原件层：写 `shared.db` 的四张 raw 表，并登记 `watermark`。

四张表都**只存上游原值**：没有 MA、没有分位、没有任何派生列。
一旦在原件层算派生，改阈值时就不知道该信哪一层了。

`market_index_day` 是后来加的（日K）。它和 `market_index_week` 是**两条独立的序列**，
不是粗细之分：日换手率按日累计，一周的量约等于五天之和，所以两份数据各有各的阈值
口径。日K 只喂看板画图，判定仍然走周K。

`watermark` 是顺手登记的（`domain='market'`）——`stock_data_fetch/doctor.py`
的 `check_watermark` 就是拿它和表里 max(date) 对比，登记了等于免费获得一份
新鲜度体检，不用另写一套。

为什么不做 `DELETE WHERE date` 整天替换（`price` 表是那么做的）
------------------------------------------------------------
`price` 表一天一份全市场快照，"这一天有哪些票"本身是原子事实，所以整天替换。
这里四张表是**序列**，每次抓的是全历史区间，新数据只会追加/修订最后几天；
按主键 upsert 就够了，也就不会因为上游少回几行把历史挖出个洞。

**唯一的例外**是周K 末尾那根「本周未走完」的半根 bar——它必须被清掉，
见 `_drop_older_in_week()`。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from . import config

BUSY_TIMEOUT_MS = 5000
"""和 `stock_extreme_point` 同值：`shared.db` 可能同时有别的工具在写。"""

DOMAIN = "market"

DDL = """
create table if not exists market_index_week(
    symbol     text not null,
    date       text not null,
    open       real, high real, low real, close real,
    volume     real, amount real,
    turnover   real,          -- 周换手率 %
    pct_chg    real,          -- 周涨跌幅 %
    src        text,
    updated_at text not null,
    primary key(symbol, date)
) without rowid;

create table if not exists market_index_day(
    symbol     text not null,
    date       text not null,
    open       real, high real, low real, close real,
    volume     real, amount real,
    turnover   real,          -- 日换手率 %（f61，按日计）
    pct_chg    real,          -- 日涨跌幅 %
    src        text,
    updated_at text not null,
    primary key(symbol, date)
) without rowid;

create table if not exists market_index_perf(
    symbol      text not null,
    date        text not null,
    close       real,
    pe_ttm      real,         -- 滚动市盈率（中证 peg 字段）
    sample_size integer,      -- 样本数量
    src         text,
    updated_at  text not null,
    primary key(symbol, date)
) without rowid;

create table if not exists market_macro(
    series     text not null,  -- 'bond10y' | 'below_na'
    date       text not null,
    value      real not null,  -- 收益率 % / 破净占比 %
    extra      text,           -- 破净家数/总数等，JSON
    src        text,
    updated_at text not null,
    primary key(series, date)
) without rowid;
"""


def _now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def connect(path: str | Path | None = None, *, create: bool = True) -> sqlite3.Connection:
    p = Path(path or config.shared_db())
    if not p.exists() and not create:
        raise FileNotFoundError(p)
    conn = sqlite3.connect(str(p))
    conn.execute("pragma journal_mode=wal")
    conn.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
    if create:
        conn.executescript(DDL)
        conn.commit()
    return conn


# ---------------------------------------------------------------- 写

def save_week(conn: sqlite3.Connection, rows: list[dict], symbol: str = "sh000300") -> int:
    stamp = _now()
    payload = [(symbol, r["date"], r.get("open"), r.get("high"), r.get("low"),
                r.get("close"), r.get("volume"), r.get("amount"),
                r.get("turnover"), r.get("pct_chg"), "eastmoney", stamp)
               for r in rows if r.get("date")]
    with conn:
        conn.executemany(
            "insert into market_index_week(symbol,date,open,high,low,close,volume,"
            "amount,turnover,pct_chg,src,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?) "
            "on conflict(symbol,date) do update set open=excluded.open,"
            "high=excluded.high,low=excluded.low,close=excluded.close,"
            "volume=excluded.volume,amount=excluded.amount,"
            "turnover=excluded.turnover,pct_chg=excluded.pct_chg,"
            "src=excluded.src,updated_at=excluded.updated_at", payload)
        if payload:
            # 取 max 而不是 payload[-1]：上游眼下是升序给的，但这里不该依赖它。
            _drop_older_in_week(conn, symbol, max(p[1] for p in payload))
        _watermark(conn, "index_week", rows)
    return len(payload)


def save_day(conn: sqlite3.Connection, rows: list[dict], symbol: str = "sh000300") -> int:
    """日K 落 `market_index_day`。同样只 upsert，不做整天替换。

    日K 没有"半根"这回事——每天的 bar 收盘即定稿，所以这里不需要
    `_drop_older_in_week()` 那套清理。
    """
    stamp = _now()
    payload = [(symbol, r["date"], r.get("open"), r.get("high"), r.get("low"),
                r.get("close"), r.get("volume"), r.get("amount"),
                r.get("turnover"), r.get("pct_chg"), "eastmoney", stamp)
               for r in rows if r.get("date")]
    with conn:
        conn.executemany(
            "insert into market_index_day(symbol,date,open,high,low,close,volume,"
            "amount,turnover,pct_chg,src,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?) "
            "on conflict(symbol,date) do update set open=excluded.open,"
            "high=excluded.high,low=excluded.low,close=excluded.close,"
            "volume=excluded.volume,amount=excluded.amount,"
            "turnover=excluded.turnover,pct_chg=excluded.pct_chg,"
            "src=excluded.src,updated_at=excluded.updated_at", payload)
        _watermark(conn, "index_day", rows)
    return len(payload)


def _drop_older_in_week(conn: sqlite3.Connection, symbol: str, newest: str) -> None:
    """把「同一 ISO 周里比 `newest` 更早」的周线删掉——只可能命中半根 bar。

    东财周K 的日期是**那一周最后一个交易日**：正常是周五，节前是周三/周四，
    月末是当月末交易日。周中跑，最后会给一根"当周至今"的 bar，日期是**当天**
    （比如 2026-11-23 周一，换手只有 0.61%）。下一次周五跑，完整周落在新日期
    （2026-11-27），按 `(symbol,date)` upsert 顶不掉 11-23 那行，它就永久留在
    序列中间——图上多一根假坑、MA20/60 的窗口被挪位、52 周中位数被拉低。

    这里在每次写周K 之后，把最新一根所在 ISO 周里更早的行清掉。三条安全性：
    * 该周只有一根有效 bar 是上游的客观事实（同一周不可能有两个"最后交易日"）；
    * 节前那根（周三/周四）在自己的 ISO 周里是唯一一行，删不到东西；
    * 删的范围被 `date < newest` 和该周首尾日两个条件夹住，不会碰到别的周。
    历史数据已验证：601 根周线、601 个 ISO 周，一行都不会被这条规则命中。
    """
    try:
        d = dt.date.fromisoformat(str(newest))
    except ValueError:
        return
    monday = d - dt.timedelta(days=d.weekday())
    sunday = monday + dt.timedelta(days=6)
    conn.execute(
        "delete from market_index_week where symbol=? and date>=? and date<=? "
        "and date<?",
        (symbol, monday.isoformat(), sunday.isoformat(), str(newest)))


def save_perf(conn: sqlite3.Connection, rows: list[dict], symbol: str = "sh000300") -> int:
    stamp = _now()
    payload = [(symbol, r["date"], r.get("close"), r.get("pe_ttm"),
                int(r["sample_size"]) if r.get("sample_size") else None,
                "csindex", stamp)
               for r in rows if r.get("date")]
    with conn:
        conn.executemany(
            "insert into market_index_perf(symbol,date,close,pe_ttm,sample_size,"
            "src,updated_at) values(?,?,?,?,?,?,?) "
            "on conflict(symbol,date) do update set close=excluded.close,"
            "pe_ttm=excluded.pe_ttm,sample_size=excluded.sample_size,"
            "src=excluded.src,updated_at=excluded.updated_at", payload)
        _watermark(conn, "index_perf", rows)
    return len(payload)


def save_macro(conn: sqlite3.Connection, series: str, rows: list[dict],
               src: str) -> int:
    stamp = _now()
    payload = []
    for r in rows:
        extra = None
        if "below" in r:
            extra = f'{{"below":{int(r["below"])},"total":{int(r["total"])}}}'
        payload.append((series, r["date"], r["value"], extra, src, stamp))
    with conn:
        conn.executemany(
            "insert into market_macro(series,date,value,extra,src,updated_at) "
            "values(?,?,?,?,?,?) "
            "on conflict(series,date) do update set value=excluded.value,"
            "extra=excluded.extra,src=excluded.src,updated_at=excluded.updated_at",
            payload)
        _watermark(conn, series, rows)
    return len(payload)


def _watermark(conn: sqlite3.Connection, scope: str, rows: list[dict]) -> None:
    """登记这一路原件的最后日期。只前进，不回退（上游修订旧数据不该把水位拉低）。"""
    dates = [r.get("date") for r in rows if r.get("date")]
    if not dates:
        return
    conn.execute(
        "insert into watermark(domain,scope,last_ok_date,updated_at) values(?,?,?,?) "
        "on conflict(domain,scope) do update set "
        "last_ok_date=excluded.last_ok_date, updated_at=excluded.updated_at "
        "where excluded.last_ok_date > coalesce(watermark.last_ok_date,'')",
        (DOMAIN, scope, max(dates), _now()))


# ---------------------------------------------------------------- 读（重算 / 降级）

def read_week(conn: sqlite3.Connection, symbol: str = "sh000300") -> list[dict]:
    cur = conn.execute(
        "select date,open,high,low,close,volume,amount,turnover,pct_chg "
        "from market_index_week where symbol=? order by date", (symbol,))
    return [dict(zip([c[0] for c in cur.description], r)) for r in cur]


def read_day(conn: sqlite3.Connection, symbol: str = "sh000300") -> list[dict]:
    """日K 原件（重算用不到，但降级、校对、看板自检都要读它）。"""
    cur = conn.execute(
        "select date,open,high,low,close,volume,amount,turnover,pct_chg "
        "from market_index_day where symbol=? order by date", (symbol,))
    return [dict(zip([c[0] for c in cur.description], r)) for r in cur]


def read_perf(conn: sqlite3.Connection, symbol: str = "sh000300") -> list[dict]:
    cur = conn.execute(
        "select date,close,pe_ttm,sample_size from market_index_perf "
        "where symbol=? order by date", (symbol,))
    return [dict(zip([c[0] for c in cur.description], r)) for r in cur]


def read_macro(conn: sqlite3.Connection, series: str) -> list[dict]:
    cur = conn.execute(
        "select date,value,extra from market_macro where series=? order by date",
        (series,))
    return [dict(zip([c[0] for c in cur.description], r)) for r in cur]
