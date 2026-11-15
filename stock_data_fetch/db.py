"""连接、PRAGMA 校验、事务、版本检查。

三条规矩：

1. **PRAGMA 要回读**。`PRAGMA journal_mode = WAL` 在网络盘/只读盘上会**静默失败**并返回原值，
   不检查就会带着 rollback journal 跑，于是两个进程同时写就 `database is locked`。
   实测 `busy_timeout=0` 时两进程各 5 批会丢 3–4 批（`讨论纪要` §18.3）。
2. **写事务一律 `BEGIN IMMEDIATE`**。WAL 下 `DEFERRED` 在"先读后写"升级时可能 `SQLITE_BUSY`
   且**不能安全重试**。
3. **建库 ≠ 迁移**。库不存在就照 `schema.sql` 建；库存在但版本不符就报错，不补表不补列。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import SCHEMA_VERSION
from .errors import HubNotReady, SchemaMismatch

BUSY_TIMEOUT_MS = 5000
"""实测：0 时两进程各 5 批丢 3–4 批；5000 时 0 失败。不能省。"""


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """打开连接并落实 PRAGMA。`isolation_level=None` 让事务边界由我们显式控制。"""
    p = Path(path)
    if readonly:
        if not p.exists():
            raise HubNotReady(f"库不存在：{p}")
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0,
                               isolation_level=None)
    else:
        conn = sqlite3.connect(p, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not readonly:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def pragma_problems(conn: sqlite3.Connection) -> list[str]:
    """回读关键 PRAGMA，返回不符合的项。空表示通过。"""
    problems: list[str] = []
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        problems.append(f"journal_mode={mode}（要求 wal，多半是网络盘或只读盘）")
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    if int(busy) < 1000:
        problems.append(f"busy_timeout={busy}（要求 >=1000）")
    return problems


def create_db(path: Path | str, schema_path: Path | str) -> None:
    """按 `schema.sql` 建库。只在库文件不存在时调用。"""
    p = Path(path)
    s = Path(schema_path)
    if not s.exists():
        raise HubNotReady(f"建库脚本不存在：{s}（可用 STOCKHUB_SCHEMA 指定）")
    p.parent.mkdir(parents=True, exist_ok=True)
    sql = s.read_text(encoding="utf-8")
    conn = sqlite3.connect(p, isolation_level=None)
    try:
        conn.executescript(sql)
    except sqlite3.Error as e:
        conn.close()
        try:                                   # 建到一半失败：删掉半成品，避免下次误判"库已存在"
            p.unlink()
        except OSError:
            pass
        raise HubNotReady(f"建库失败：{e}") from e
    conn.close()


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def check_version(conn: sqlite3.Connection, expected: int = SCHEMA_VERSION) -> None:
    cur = schema_version(conn)
    if cur != expected:
        raise SchemaMismatch(
            f"库版本 {cur}，需要 {expected}。数据可再生，请备份后删库重建"
            f"（rm <db> 再重跑）",
            got=cur, want=expected)


AUTO_UPGRADE: set[tuple[int, int]] = {(1, 2), (2, 3), (3, 4), (4, 5), (5, 6)}
"""允许**自动**迁移的单步版本对。空集以外每加一条都要能说清"为什么它是安全的"。

**这里登记的是"一步"。** 从 v1 直接升到 v3 不需要额外登记 `(1,3)`——
`upgrade()` 会按 `(1,2) → (2,3)` 逐跳走完。跳数一多就自动覆盖了组合，
不必为每一对都写一条（那是指数级增长，而且每写一条就要重新论证一遍）。

`(1, 2)`：v2 相对 v1 只多了一张 `scan` 表和一条索引（按日全市场横截面），
没动任何既有列、没改主键。`schema.sql` 里全部 DDL 都是 `IF NOT EXISTS`，
对老库执行一遍等于只把新表补上——不碰数据，也不碰 `meta` 里的既有键
（`INSERT OR IGNORE`）。所以这一步没有对应的 `UPGRADE_STEPS` 动作，
重放 `schema.sql` 就够了。

`(2, 3)`：v3 只给 `snapshot.domain` 的 CHECK 加了一个值 `gbalance`。
**这一条不靠重放 `schema.sql`**（`CREATE TABLE IF NOT EXISTS` 不会改已有表的 CHECK，
实测直接报 `CHECK constraint failed`），而是走 `UPGRADE_STEPS[(2,3)]` 里的表重建：
建 `snapshot_new` → 拷数据 → 删旧表 → 改名。没有二级索引要重建，主键不变，
既有 `mainop`/`consensus`/`rating` 三种值都在新 CHECK 里，所以数据一行不丢。

`(3, 4)`：v4 给 `stock` 表加 `sector` / `business` 两列（主营业务描述）。
这一条走 `ALTER TABLE ADD COLUMN`，既不改既有的列、也不改主键和 CHECK——
SQLite 加列是原地操作，不重建表、不拷数据，比 `(2,3)` 那次表重建安全得多。
`ADD COLUMN` 本身不能重复执行（列已存在会报 `duplicate column name`），
所以 `UPGRADE_STEPS[(3,4)]` 里先查 `PRAGMA table_info` 再决定加不加。

`(4, 5)`：v5 新增 `annotation` 表（人工标注：垄断性等）。
纯新增表，没有任何既有列被改动——`CREATE TABLE IF NOT EXISTS` 本身幂等，
重放 `schema.sql` 就完成了，所以**不需要** `UPGRADE_STEPS` 动作。
注意它跟 `(1,2)` 是同一类：只多加一张表，不碰旧数据。

**这不是"以后版本都能自动升"的口子。** 改列类型、拆表那种变更在 SQLite 里做不到
幂等，只能删库重建（本层的数据全部可再生）。往这张表里加一条之前，先问一句
"重复执行两次会不会出错"——`UPGRADE_STEPS` 里的每一步都必须能安全重跑。
但是 `UPGRADE_STEPS` 里没有登记的步骤，就只是重放 `schema.sql`（幂等）。
"""


def _sql_statements(sql: str) -> list[str]:
    """把一段 DDL 脚本拆成单条语句。

    **为什么不能用 `conn.executescript()`**（2026-11-15 实测）：`executescript` 在
    Python 3.13 上会**先隐式 COMMIT 掉挂起的事务**，然后逐条在自动提交模式下执行。
    于是 `upgrade()` 里"整个升级包在一个事务里"这句承诺是假的：外面 `tx()` 包着的
    `BEGIN IMMEDIATE` 被它悄悄提交，退出 `tx()` 时 `COMMIT` 直接抛
    `cannot commit - no transaction is active`。更糟的是迁移本身变成非原子的：
    中途崩在 `DROP TABLE` 与 `RENAME` 之间，数据就停在半迁移状态。

    `schema.sql` 是纯 DDL（无触发器、无 `BEGIN...END`），每句以行尾 `;` 结束，
    所以按行累积即可。整行注释丢弃；行尾 `--` 注释先切掉再判断是否结束。

    **跳过的两类语句**：
    - `PRAGMA user_version`：版本号由 `upgrade()` 显式设置，免得脚本里的值覆盖目标版本；
    - 其余 `PRAGMA` 全部跳过。`schema.sql` 头部那几条是**建库期**设置，其中
      `synchronous` / `journal_mode` / `page_size` / `auto_vacuum` 在事务里改会直接报错
      （`Safety level may not be changed inside a transaction`——实测就是这么炸的），
      而且对老库本来就改不动：连接级的几条由 `db.connect()` 每次落实，
      `journal_mode` 已经持久化在库文件里。重放它们只有害没有益。
    """
    out: list[str] = []
    buf: list[str] = []
    for raw in sql.splitlines():
        code = raw.split("--", 1)[0].rstrip()
        if not code.strip():
            continue
        buf.append(raw)
        if code.endswith(";"):
            stmt = "\n".join(buf).strip()
            buf = []
            if stmt and not stmt.upper().startswith("PRAGMA"):
                out.append(stmt)
    if buf:
        tail = "\n".join(buf).strip()
        if tail and not tail.upper().startswith("PRAGMA"):
            out.append(tail)
    return out


def _rebuild_snapshot(conn: sqlite3.Connection) -> None:
    """v2 → v3：重建 `snapshot`，把 `gbalance` / `gcashflow` 加进 CHECK。

    **可重入**：比对的是"现有表的 CHECK 里是否已经没有旧值遗漏"，做法是看
    `gcashflow`（两个新值里最后加的那个）在不在建表 SQL 里，在就直接返回。
    这样 `upgrade()` 被重复调用、或者迁移中途崩了重跑，都不会出错。
    `sqlite_master` 里查不到表时也直接返回——新库由 `schema.sql` 一次建好，不需要迁移。

    **判据要用"最后加的那个值"**：如果只看 `gbalance`，那在"只加过 gbalance 的中间状态"
    下会误认为已完成，`gcashflow` 就永远进不去 CHECK，症状是写入时 `CHECK constraint failed`。
    加新值的时候记得把这里的判据一起换成新值。
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='snapshot'"
                       ).fetchone()
    if row is None or "gcashflow" in (row[0] or ""):
        return
    # 逐条 execute，不用 executescript：它会隐式提交，把 `upgrade()` 的事务边界
    # 打穿（见 `_sql_statements()` 的说明）。这五句本来就是一步，必须同进同出。
    for stmt in _sql_statements("""
        CREATE TABLE snapshot_new (
          symbol  TEXT NOT NULL,
          domain  TEXT NOT NULL CHECK (domain IN ('mainop','consensus','rating',
                                                 'gbalance','gcashflow')),
          as_of   TEXT,
          payload TEXT NOT NULL,
          src     TEXT,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (symbol, domain)
        ) WITHOUT ROWID;
        INSERT INTO snapshot_new(symbol,domain,as_of,payload,src,updated_at)
            SELECT symbol,domain,as_of,payload,src,updated_at FROM snapshot;
        DROP TABLE snapshot;
        ALTER TABLE snapshot_new RENAME TO snapshot;"""):
        conn.execute(stmt)


def _add_stock_columns(conn: sqlite3.Connection) -> None:
    """v3 → v4：给 `stock` 表加 `sector` / `business` 两列。

    为什么需要这一步：`schema.sql` 里已经写了这两列，但
    `CREATE TABLE IF NOT EXISTS` 对**已存在**的表什么都不做——老库的 `stock`
    表仍然是 v1 那 9 列。所以只能显式 `ALTER TABLE ADD COLUMN`。

    **可重入**：`ADD COLUMN` 不能重复执行（列已存在报 `duplicate column name`），
    所以先读 `PRAGMA table_info(stock)`，只加缺的那些。这样 `upgrade()` 重跑、
    或迁移中途崩了重来，都不会出错。表不存在时也直接返回——新库由
    `schema.sql` 一次建好，不需要迁移。

    为什么用 `ADD COLUMN` 而不是像 `(2,3)` 那样重建表：这次只是**纯加列**，
    没有改任何既有列的类型、主键或 CHECK。SQLite 的加列是原地改元数据，
    不拷数据、不占额外空间（旧行为 NULL），既然能做到零风险，就没必要重建。

    两列的用途（这是加它们的原因，删列之前先想清楚谁在用）：
    - `sector`  ← `data_profile.sector`，`industry` 缺失时的备用（quality 的行业判定读它）；
    - `business`→ `data_profile.business`，报告第一节「生意本身」的那句话，
                  "这家公司到底做什么生意"的第一手信息。上游一直有，之前没存。
    """
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock'"
                       ).fetchone()
    if row is None:
        return
    have = {r[1] for r in conn.execute("PRAGMA table_info(stock)")}
    for col in ("sector", "business"):
        if col not in have:
            conn.execute(f"ALTER TABLE stock ADD COLUMN {col} TEXT")


def _drop_kline_date_index(conn: sqlite3.Connection) -> None:
    """v5 → v6：删掉 `ix_kline_date`（实测 33 MB，占库体积 22%，零查询使用）。

    这条索引是 v1 时"给按日扫全市场预留"的（见 `schema.sql` 旧注释），但横截面
    查询全部走 `scan` 表，`kline` 侧只有"按 symbol 取一段"，主键 `(symbol, date)`
    已经聚簇够用。实测 122 万行时它是 33.2 MB，而 `shared.db` 总共 153 MB。

    真需要按日扫全市场时，临时建一条再删也比常驻划算——那种查询一天最多几次，
    而这条索引是每次写入都要维护的。

    **可重入**：`DROP INDEX IF EXISTS`，重跑无害；表不存在也无害。
    """
    conn.execute("DROP INDEX IF EXISTS ix_kline_date")


UPGRADE_STEPS: dict[tuple[int, int], object] = {
    (2, 3): _rebuild_snapshot,
    (3, 4): _add_stock_columns,
    (5, 6): _drop_kline_date_index,
}
"""需要"额外动作"的版本升级。不在这张表里的版本对只重放 `schema.sql`。"""


def upgrade(conn: sqlite3.Connection, schema_path: Path | str,
            from_version: int | None = None,
            to_version: int = SCHEMA_VERSION) -> int:
    """把已有库升到 `to_version`。返回新的 `user_version`。

    调用方必须先确认这条升级路径是允许的（`can_upgrade()`）——这里不做判断，
    因为"能不能升"是策略，不是机制。

    **逐跳执行**：`from_version` 到 `to_version` 之间，每一个在 `AUTO_UPGRADE` 里
    登记过的单步都会按顺序跑到（先跑该步在 `UPGRADE_STEPS` 里的动作，再重放
    `schema.sql` 补新表）。所以 v1 直接升 v3 会自动走 `(1,2)` 然后 `(2,3)`，
    不需要额外登记 `(1,3)`。

    顺序是**先做动作、再重放 `schema.sql`**：动作处理"已有的表要改"，
    重放负责"新表要补上"。反过来会让 `schema.sql` 里那条旧定义的
    `CREATE TABLE IF NOT EXISTS snapshot` 先匹配到旧表，迁移再重建一次，
    多绕一圈且日志里看不出差别。

    整个升级包在一个事务里：中途崩了库还停在旧版本，重跑一次即可
    （`UPGRADE_STEPS` 的每一步都是可重入的）。

    **原子性在 2026-11-15 才真正做到。** 原来靠 `conn.executescript()`，而它会在
    Python 3.13 上先隐式 COMMIT 掉挂起事务、再逐条自动提交——于是
    "整个升级包在一个事务里"是假的，且外面套着 `db.tx()` 时 COMMIT 必然抛
    `cannot commit - no transaction is active`（实测撞到）。现在改成
    `_sql_statements()` 拆句 + 逐条 `execute()`，并自己在没有外层事务时开一个。
    """
    p = Path(schema_path)
    if not p.exists():
        raise HubNotReady(f"建库脚本不存在：{p}")

    if from_version is None:
        from_version = schema_version(conn)

    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for step_from in range(from_version, to_version):
            action = UPGRADE_STEPS.get((step_from, step_from + 1))
            if action is not None:
                action(conn)
        for stmt in _sql_statements(p.read_text(encoding="utf-8")):
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {to_version}")
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise

    # `PRAGMA journal_mode` 改不了在事务里（见 `_sql_statements()`），所以放在提交之后
    # 补一次。非 WAL 的老库多半是绕过 `create_db()` 建的（手工 `sqlite3 < ddl` 之类），
    # 补上之后闸 4 才不会在"刚迁移完"的库上误报。失败不管：闸 4 会给出准确报错。
    if own_tx:
        try:
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            pass
    return to_version


def can_upgrade(from_version: int, to_version: int = SCHEMA_VERSION) -> bool:
    """`from_version` 能否**逐跳**走到 `to_version`（每一跳都在 `AUTO_UPGRADE` 里）。

    这是 `upgrade()` 那句"调用方必须先确认"的实现。写成函数而不是让调用方
    自己写 `(cur, SCHEMA_VERSION) in AUTO_UPGRADE`：后者在"中间隔了几个版本"时
    会误判——v1 到 v3 这一对没登记过，但路径是通的。
    """
    if from_version == to_version:
        return True
    if from_version > to_version:
        return False
    return all((v, v + 1) in AUTO_UPGRADE for v in range(from_version, to_version))


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务。`BEGIN IMMEDIATE` 立刻拿写锁，避免读→写升级的死锁。

    **COMMIT 失败要回滚**（2026-11-15）：原来 `conn.execute("COMMIT")` 裸着，
    一旦它抛（`cannot commit - no transaction is active` / 锁超时），事务就挂在
    半开状态，后续任何 `tx()` 都会撞 `cannot start a transaction within a transaction`。
    实测触发场景：`db.upgrade()` 里的 `executescript` 会隐式提交，外面这层
    `tx()` 的 COMMIT 就必然失败（见 `upgrade()` 的说明）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    try:
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


# ---------------------------------------------------------------- meta

def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with tx(conn):
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """各表行数，供 doctor 与测试用。"""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    return {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
