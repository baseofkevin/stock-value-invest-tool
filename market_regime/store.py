"""结果层：写 `results.db` 的 `market_regime`，一周一行。

为什么一周一行而不是每次运行一行
------------------------------
这是个**周记**，不是日志：同一个周五重跑三次，应该覆盖成"我最后看到的那一份"。
看板读的也是"每个基准日当时的判断"，跟 `run` 表按次记录的语义不一样。

为什么这张表不进 `stock_data_fetch/results.py` 的 SCHEMA
------------------------------------------------------
那是四个工具的结果库，schema 版本一涨，所有工具都得跟着走一遍迁移。
这张表归本包自己建（`create table if not exists`），不跑本包就永远不存在，
老库零影响。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from . import config

BUSY_TIMEOUT_MS = 5000

DDL = """
create table if not exists market_regime(
    date              text primary key,   -- 基准日（周五）
    computed_at       text not null,      -- 实际计算时刻
    data_asof         text,               -- 三路里最旧的那一天
    stale             integer not null default 0,   -- 有源降级到上次原件

    trend_state       text,
    trend_score       integer,
    trend_close       real,
    ma20              real,
    ma60              real,
    ma60_slope        real,
    weeks_below_ma20  integer,

    erp               real,
    pe_ttm            real,
    bond10y           real,
    erp_pct           integer,

    below_na          real,
    below_na_pct      integer,
    turnover          real,
    turnover_q10      real,
    volume_dry        integer,

    payload           text                -- 整份 JSON，以后加字段不改表
);
"""


def connect(path: str | Path | None = None, *, create: bool = True) -> sqlite3.Connection:
    p = Path(path or config.results_db())
    if not p.exists() and not create:
        raise FileNotFoundError(p)
    conn = sqlite3.connect(str(p))
    conn.execute("pragma journal_mode=wal")
    conn.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
    if create:
        conn.executescript(DDL)
        conn.commit()
    return conn


def save(conn: sqlite3.Connection, day: str, result: dict, *, stale: bool = False) -> None:
    t = result.get("trend") or {}
    v = result.get("valuation") or {}
    s = result.get("sentiment") or {}

    row = (
        day,
        dt.datetime.now().replace(microsecond=0).isoformat(),
        result.get("data_asof"),
        int(bool(stale)),
        t.get("state"), t.get("score"), t.get("close"), t.get("ma20"),
        t.get("ma60"), t.get("ma60_slope"), t.get("weeks_below_ma20"),
        v.get("erp"), v.get("pe_ttm"), v.get("bond10y"), v.get("pct"),
        s.get("below_na"), s.get("below_na_pct"), s.get("turnover"),
        s.get("turnover_q10"),
        None if s.get("volume_dry") is None else int(s["volume_dry"]),
        json.dumps(result, ensure_ascii=False),
    )
    with conn:
        conn.execute(
            "insert or replace into market_regime(date,computed_at,data_asof,stale,"
            "trend_state,trend_score,trend_close,ma20,ma60,ma60_slope,"
            "weeks_below_ma20,erp,pe_ttm,bond10y,erp_pct,below_na,below_na_pct,"
            "turnover,turnover_q10,volume_dry,payload) "
            "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
