"""两份建表脚本：`shared.db` 有 `price` + `index_price`；
`results.db` 有 `price` + `index_price` + `price_stat`。

**DDL 是 `IF NOT EXISTS`，重复执行等于什么都不做**，所以两个库都是"打开就能用"：
第一次跑这个工具时自动把表补上，不需要单独一步 `init`，也不需要版本号。
这也是它敢不参与 `shared.db` 的 `user_version` 体系的原因——版本号是用来管
"改既有列/拆表"那种不能幂等的迁移的，而这里从头到尾只有"建表"。

两个库的 `price` **列必须完全一致**（看板可能以后要从结果库直接导出一份给别人看）。
所以 DDL 写成一份常量 + 一个索引名参数，避免哪天改了一边忘了另一边。
"""

from __future__ import annotations

#: `price` 的列顺序。写库、读库、两边建表全部照它来。
PRICE_COLUMNS = ("date", "symbol", "name", "close", "pct_chg", "pe_ttm", "pb_mrq",
                 "ps_ttm", "total_mv", "circ_mv", "src", "updated_at")

PRICE_DDL = """
CREATE TABLE IF NOT EXISTS price (
  date        TEXT NOT NULL,             -- YYYY-MM-DD（该交易日）
  symbol      TEXT NOT NULL,             -- sh600519 / sz000858 / bj920992
  name        TEXT,                      -- 当日上游给的名称
  close       REAL,                      -- 收盘价（元）
  pct_chg     REAL,                      -- **百分数**（-1.55 = 跌 1.55%）
  pe_ttm      REAL,                      -- 市盈率 TTM（亏损股为负）
  pb_mrq      REAL,                      -- 市净率 MRQ
  ps_ttm      REAL,                      -- 市销率 TTM（本表存在的理由：别的免费源都不给）
  total_mv    REAL,                      -- 总市值（元）
  circ_mv     REAL,                      -- 流通市值（元）
  src         TEXT,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (date, symbol)
) WITHOUT ROWID
"""

INDEX_DDL = """
CREATE INDEX IF NOT EXISTS {name} ON price(symbol)
"""

STAT_DDL = """
CREATE TABLE IF NOT EXISTS price_stat (
  date          TEXT PRIMARY KEY,        -- YYYY-MM-DD
  n             INTEGER NOT NULL,        -- 当天有价的只数
  n_up          INTEGER NOT NULL,        -- 上涨家数
  n_down        INTEGER NOT NULL,        -- 下跌家数
  n_flat        INTEGER NOT NULL,        -- 平盘家数（|涨跌幅| < 0.005%）
  n_limit_up    INTEGER NOT NULL,        -- 涨停家数
  n_limit_down  INTEGER NOT NULL,        -- 跌停家数
  n_pe          INTEGER NOT NULL,        -- 参与 PE 中位数的只数（只算正 PE）
  n_pb          INTEGER NOT NULL,
  n_ps          INTEGER NOT NULL,
  pe_median     REAL,
  pb_median     REAL,
  ps_median     REAL,
  mv_sum        REAL,                    -- 总市值合计（元）
  src           TEXT,
  updated_at    TEXT NOT NULL
)
"""

#: 沪深300 等指数**单独一张表**，不进 `price`。
#:
#: `price` 的每一行都代表"一只可交易的股票"，有 PE/PB/PS/市值，汇总卡按它算涨跌家数。
#: 指数塞进去会多出一个没有估值、没有市值的"票"，把 5579 这个数字和涨跌统计带偏。
INDEX_COLUMNS = ("date", "symbol", "name", "close", "pct_chg", "src", "updated_at")

INDEX_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS index_price (
  date        TEXT NOT NULL,             -- YYYY-MM-DD
  symbol      TEXT NOT NULL,             -- sh000300
  name        TEXT,                      -- 沪深300
  close       REAL,                      -- 收盘点位
  pct_chg     REAL,                      -- **百分数**（-0.91 = 跌 0.91%）
  src         TEXT,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (date, symbol)
) WITHOUT ROWID
"""

#: `shared.db`：原件。**只有 price + index_price**——摘要是"给人看的结论"，不属于原始层。
SHARED_DDL = (PRICE_DDL, INDEX_TABLE_DDL, INDEX_DDL.format(name="ix_price_symbol"))

#: `results.db`：给看板读的。价格的复制品 + 每日摘要 + 指数。
#: 索引名带 `_r` 后缀：两个库将来若 ATTACH 到一起查询，重名索引会冲突。
RESULTS_DDL = (PRICE_DDL, INDEX_TABLE_DDL,
               INDEX_DDL.format(name="ix_r_price_symbol"), STAT_DDL)


def apply(conn, ddl: tuple[str, ...]) -> None:
    """执行一组 DDL（全部幂等）。调用方负责事务。"""
    for stmt in ddl:
        conn.execute(stmt)
