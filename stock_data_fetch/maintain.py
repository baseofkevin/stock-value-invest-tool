"""`maintain` —— 库体积审计与清理。回答两个问题：**哪些数据能删？删了会怎样？**

    python -m stock_data_fetch.maintain              # 只审计，不写
    python -m stock_data_fetch.maintain --apply      # 执行清理（含 VACUUM）
    python -m stock_data_fetch.maintain --json       # 机读

与 `doctor` 的分工
------------------

- `doctor`：**能不能用**（不变式、覆盖、闸门），只读、每条都指向"数据可信吗"。
- `maintain`：**该不该留**（体积、孤儿、冗余），`--apply` 会写库。

两者都离线：审计一次上游都不打。

## 它会动什么（`--apply` 时）

| 动作 | 省多少 | 风险 |
|---|---|---|
| 升到最新 schema（v5→v6 删 `ix_kline_date`） | 33 MB | 零，改的是索引不是数据 |
| 孤儿覆盖收编（`fp=''` → 当前口径） | 0 | 零，只是让 375 只票不用白重取一遍 |
| `scan` 只留最近 N 天（默认 30） | 0（当前只有 7 天） | 零，工具只要 K+1 天 |
| `kline` 每只只留最近 N 根（默认 400） | 0（当前只有 300 个交易日） | 零，规则只要 71 根 |
| `VACUUM` | 真正把腾出的页还给文件系统 | 需要等量临时磁盘 |

**不做的事**：不删 `finance` / `valuation` / `snapshot` / `quote` 的行——那些是
另外三个工具的燃料，体积占比也小（合计 10 MB）。也不删 `finance.raw_json`
（6.6 MB）：没有任何工具读它，但它是"解析器以后改好了能重算"的唯一来源，
删了就得重新拉全市场财报。它该瘦身的方式是压缩或减期数，不是删列。

## 为什么"删 K 线"能省的远没有想象的多

`kline` 是 104 MB（68%），但它是 4,679 只 × 平均 262 根 = 122 万行，**每行 85 字节**，
已经是最紧的结构（`WITHOUT ROWID` + 主键聚簇 + 无冗余列）。想明显更小只有三条路，
每条都有代价：

1. **减深度**：300 → 250 根省 5.8 MB，但 judge_top 的下限正好是 250，边界会抖；
2. **只留候选池**：粗筛池每天变，今天删了明天还得重下，而且"启动日在 5 天窗口内"
   意味着池子本身就是 87% 的市场；
3. **整数化价格**（分 → INTEGER）：省约 25%，但要改所有读写路径与 `scale` 约定。

所以别指望从 K 线上再抠出几十 MB。真正的浪费从来不是"存了多少"，而是
**"为了存 1 根而请求了 300 根"**——那是取数链路的问题，已经在 `api.ensure_bulk` 里修掉。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import db
from .config import Config, KLINE_KEEP_BARS, SCAN_RETENTION_DAYS, SCHEMA_VERSION


def _size(conn: sqlite3.Connection) -> dict[str, int]:
    """按对象统计字节数（`dbstat` 虚表）。老 SQLite 没有它时返回空。"""
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
    except sqlite3.Error:
        return {}


def audit(conn: sqlite3.Connection) -> dict:
    """体检式审计。只读。"""
    tables = {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in tables}
    sizes = _size(conn)

    # 孤儿覆盖：fp='' 是早期版本没写口径留下的，现在的代码按 `fq=raw` 查，
    # 永远命中不了 → 这些股票的 K 线会被反复重取（不是白占空间，是白烧流量）。
    orphan = conn.execute(
        "SELECT COUNT(*) FROM coverage a WHERE domain='kline' AND fp='' "
        "AND NOT EXISTS(SELECT 1 FROM coverage b WHERE b.domain='kline' "
        "AND b.fp='fq=raw' AND b.symbol=a.symbol)").fetchone()[0]
    dup = conn.execute(
        "SELECT COUNT(*) FROM coverage WHERE domain='kline' AND fp=''").fetchone()[0] - orphan

    raw = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(raw_json)),0) FROM finance "
        "WHERE raw_json IS NOT NULL").fetchone()[0]

    return {
        "version": db.schema_version(conn),
        "want_version": SCHEMA_VERSION,
        "file_mb": round(sum(sizes.values()) / 1e6, 1) if sizes else None,
        "objects": {k: round(v / 1e6, 2) for k, v in
                    sorted(sizes.items(), key=lambda kv: -kv[1]) if v > 1e5},
        "rows": counts,
        "kline_bars_per_symbol": conn.execute(
            "SELECT MIN(c), CAST(AVG(c) AS INT), MAX(c) FROM "
            "(SELECT COUNT(*) c FROM kline GROUP BY symbol)").fetchone(),
        "kline_symbols": conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM kline").fetchone()[0],
        "cov_orphan_fp": orphan,
        "cov_dup_fp": dup,
        "finance_raw_json_mb": round(raw / 1e6, 2),
        "finance_rows_with_raw": conn.execute(
            "SELECT COUNT(*) FROM finance WHERE raw_json IS NOT NULL").fetchone()[0],
        "scan_days": conn.execute(
            "SELECT COUNT(DISTINCT date) FROM scan").fetchone()[0],
        "scan_span": conn.execute("SELECT MIN(date), MAX(date) FROM scan").fetchone(),
    }


def apply(conn: sqlite3.Connection, *, scan_days: int = SCAN_RETENTION_DAYS,
          kline_bars: int = KLINE_KEEP_BARS, verbose: bool = True) -> dict:
    """执行清理。返回每一步做了什么。"""
    did: dict = {}

    # ① 版本：v5→v6 会删掉那条 33 MB 的空索引
    before = db.schema_version(conn)
    if before != SCHEMA_VERSION:
        db.upgrade(conn, Config().schema_path, from_version=before)
        did["upgrade"] = f"{before} → {db.schema_version(conn)}"
    else:
        did["upgrade"] = "已是最新"

    # ② 孤儿覆盖收编：`fp=''` 的 K 线覆盖改挂到当前口径。
    #    数据本身是 raw（三个源都只给不复权），所以口径没变，只是当年没记 fp。
    #    有 `fq=raw` 同名记录的不动（可能那条更完整），只处理"仅剩空口径"的。
    with db.tx(conn):
        cur = conn.execute(
            "UPDATE coverage SET fp='fq=raw' WHERE domain='kline' AND fp='' "
            "AND NOT EXISTS(SELECT 1 FROM coverage b WHERE b.domain='kline' "
            "AND b.fp='fq=raw' AND b.symbol=coverage.symbol)")
        did["adopt_cov"] = cur.rowcount or 0

    # ②b 与 `fq=raw` 重复的旧口径行直接删——但只在"当前口径那条不比它差"时删，
    #     免得把更完整的历史覆盖丢掉。这些行是 `doctor` 误报的源头：它以前按
    #     symbol 单列 join，同一只票两行里有一行对不上就报 ERROR（见 doctor 里的说明）。
    with db.tx(conn):
        cur = conn.execute(
            "DELETE FROM coverage WHERE domain='kline' AND fp='' "
            "AND EXISTS(SELECT 1 FROM coverage b WHERE b.domain='kline' "
            "AND b.fp='fq=raw' AND b.symbol=coverage.symbol AND b.n >= coverage.n)")
        did["drop_dup_cov"] = cur.rowcount or 0

    # ③ scan 保留期。基准日取 scan 里最新的一天（等价于 hub.target），
    #    不用 date.today()——补跑历史时"今天是哪天"是相对的。
    cutoff = conn.execute("SELECT MAX(date) FROM scan").fetchone()[0]
    deleted_scan = 0
    if cutoff:
        from datetime import date, timedelta
        c = (date.fromisoformat(cutoff) - timedelta(days=scan_days)).isoformat()
        with db.tx(conn):
            deleted_scan = conn.execute("DELETE FROM scan WHERE date < ?", (c,)).rowcount or 0
            conn.execute("DELETE FROM coverage WHERE domain='scan' AND end_date < ?", (c,))
        did["prune_scan"] = deleted_scan
    else:
        did["prune_scan"] = 0

    # ④ kline 深度
    days = [r[0] for r in conn.execute("SELECT DISTINCT date FROM kline ORDER BY date")]
    deleted_kline = 0
    if len(days) > kline_bars:
        cut = days[-kline_bars]
        with db.tx(conn):
            deleted_kline = conn.execute(
                "DELETE FROM kline WHERE date < ?", (cut,)).rowcount or 0
            conn.execute(
                "UPDATE coverage SET "
                "  start_date = (SELECT MIN(date) FROM kline k WHERE k.symbol = coverage.symbol),"
                "  n          = (SELECT COUNT(*)   FROM kline k WHERE k.symbol = coverage.symbol),"
                "  holes = 0, hole_detail = '[]' "
                "WHERE domain='kline' AND start_date IS NOT NULL AND start_date < ?", (cut,))
        did["prune_kline"] = deleted_kline
    else:
        did["prune_kline"] = 0

    # ⑤ VACUUM：真正把腾出来的页还给文件系统（必须在事务外）
    did["vacuum"] = "skipped"
    try:
        conn.execute("VACUUM")
        did["vacuum"] = "ok"
    except sqlite3.Error as e:
        did["vacuum"] = f"失败：{e}"
    return did


def _print(a: dict) -> None:
    print(f"库版本 {a['version']}（代码要求 {a['want_version']}）"
          f"{'  ← 需要 migrate' if a['version'] != a['want_version'] else ''}")
    print(f"文件体积 {a['file_mb']} MB")
    print("\n最大的对象：")
    for k, v in list(a["objects"].items())[:8]:
        print(f"  {k:24} {v:8.2f} MB")
    print("\n行数：")
    for k, v in sorted(a["rows"].items(), key=lambda kv: -kv[1]):
        if v:
            print(f"  {k:20} {v:>10,}")
    lo, avg, hi = a["kline_bars_per_symbol"]
    print(f"\nkline：{a['kline_symbols']} 只 × 每只 {lo}–{hi} 根（平均 {avg}）")
    print(f"scan：{a['scan_days']} 天（{a['scan_span'][0]} .. {a['scan_span'][1]}）")
    print(f"finance.raw_json：{a['finance_rows_with_raw']} 期 / "
          f"{a['finance_raw_json_mb']} MB")
    if a["cov_orphan_fp"]:
        print(f"\n⚠️ {a['cov_orphan_fp']} 只股票的 K 线覆盖记在旧口径 `fp=''` 下，"
              f"现在的代码命中不了 → 每次都会白重取。`--apply` 会收编。")
    if a["cov_dup_fp"]:
        print(f"   （另有 {a['cov_dup_fp']} 条空口径记录与 `fq=raw` 重复，不影响使用，暂不处理）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m stock_data_fetch.maintain")
    ap.add_argument("--apply", action="store_true", help="执行清理（默认只审计）")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--scan-days", type=int, default=SCAN_RETENTION_DAYS)
    ap.add_argument("--kline-bars", type=int, default=KLINE_KEEP_BARS)
    a = ap.parse_args(argv)

    cfg = Config.from_env()
    if not Path(cfg.db_path).exists():
        print(f"库不存在：{cfg.db_path}", file=sys.stderr)
        return 2
    conn = db.connect(cfg.db_path)
    try:
        rep = audit(conn)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            _print(rep)
        if not a.apply:
            return 0
        print("\n开始清理 ...")
        did = apply(conn, scan_days=a.scan_days, kline_bars=a.kline_bars)
        for k, v in did.items():
            print(f"  {k}: {v}")
        after = audit(conn)
        if not a.json:
            print(f"\n清理后体积 {after['file_mb']} MB"
                  f"（前 {rep['file_mb']} MB，省 "
                  f"{(rep['file_mb'] or 0) - (after['file_mb'] or 0):.1f} MB）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
