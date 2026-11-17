"""命令行：抓一天的全市场价格/估值，落两个库，算每日摘要。

    python3 -m stock_price_show                     # 今天（过了 15:05）或上一交易日
    python3 -m stock_price_show --date 2026-11-16
    python3 -m stock_price_show --date 2026-11-16 --force       # 忽略库里已有，重取
    python3 -m stock_price_show --restat                        # 只重算摘要（不联网）
    python3 -m stock_price_show --sync                          # 原始库 → 结果库（不联网）
    python3 -m stock_price_show --prune-keep 30                 # 顺手删 30 天前的

日常用 `./run.py get-price`（带进度条、把输出收进日志），这个模块负责真干活。

## 一次运行干了什么

    上游（东财按日横截面 6 个请求 + 沪深300 日 K 1 个请求）
      └─ shared.db  price + index_price          原件：整天整份替换
           └─ results.db price + index_price     复制品（同一份数据）
                + price_stat                     摘要：与 price 同事务算出来

**先写原件、再从原件复制**，而不是"抓一次写两个库"。理由：复制这一步就是 `--sync`
的那段代码，只有一条路径——"结果库缺一天"永远能用同一个动作补上，不用两套逻辑。
复制不联网，抓取失败也不会影响已有数据。

退出码：0 成功 / 1 取数或写库失败 / 2 用法错误（含"不是交易日"）。
"""

from __future__ import annotations

import argparse
import sys
import time

from . import calendar as cal_mod
from . import config, source, store


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _m(v, digits: int = 2) -> str:
    return "—" if v is None else f"{float(v):.{digits}f}"


def _pct(v) -> str:
    return "—" if v is None else f"{float(v):+.2f}%"


def _stat_line(s: dict) -> str:
    """`price_stat` 一行 → 屏幕上那句话（与看板四张卡同一口径）。"""
    return (f"涨 {s['n_up']} / 跌 {s['n_down']} / 平 {s['n_flat']}"
            f" · 涨停 {s['n_limit_up']} / 跌停 {s['n_limit_down']}"
            f" · PE 中位 {_m(s['pe_median'])} · PB 中位 {_m(s['pb_median'])}"
            f" · PS 中位 {_m(s['ps_median'])}")


def _counts(rows: list[dict]) -> str:
    def cnt(key: str) -> int:
        return sum(1 for r in rows if r.get(key) is not None)
    return (f"{len(rows)} 只 · PE {cnt('pe_ttm')} · PB {cnt('pb_mrq')} · "
            f"PS {cnt('ps_ttm')}（非空只数）")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    t0 = time.time()
    cal = cal_mod.Calendar.load(args.calendar)

    try:
        if args.restat:
            return _do_restat(args)
        if args.sync:
            return _do_sync(args)
        return _do_fetch(args, cal, t0)
    except FileNotFoundError as e:
        _say(f"❌ 库不存在：{e}")
        return 1
    except source.SourceError as e:
        _say(f"❌ {e}")
        return 1


# ---------------------------------------------------------------- 取数

def _do_fetch(args, cal, t0: float) -> int:
    day = (args.date or "").strip() or (cal_mod.target(cal) if cal else "")
    if not day:
        _say("❌ 没有交易日历，定不出「今天」，请显式给 --date YYYY-MM-DD")
        return 2
    if not _check_day(cal, day):
        return 2

    shared = store.connect(config.shared_db(args.shared_db))
    store.ensure(shared, "shared")
    results = None
    try:
        have = store.count(shared, day)
        if have and not args.force:
            # 不加 --force 就跳过取数：与数据层的"按需取"同一个用户体验。
            # 但**不跳过复制**——结果库那一步可能上次失败了。
            _say(f"[1/3] {day} 原始库已有 {have} 行，跳过全市场取数（要重取加 --force）")
        else:
            if args.force and have:
                _say(f"[1/3] 取 {day} 全市场价格/估值（--force 重取）...")
            else:
                _say(f"[1/3] 取 {day} 全市场价格/估值（东财按日横截面）...")
            rows = source.fetch_day(day)
            if not rows:
                _empty_hints(shared, cal, day)
                return 1
            n = store.write_day(shared, day, rows)
            _say(f"      → {config.shared_db(args.shared_db)} · price {n} 行 · "
                 f"{_counts(rows)}")

        # 指数单独取：价格已经成功时，它失败只警告，不把整条命令判失败。
        # 老库/旧版本抓过的日子没有 index_price，这里顺手补上（不必 --force 重抓 5579 行）。
        _ensure_index(shared, day, args.force)

        _say(f"[2/3] 复制到结果库 → {config.results_db(args.results_db)}"
             "（price + price_stat + 沪深300）")
        try:
            results = store.connect(config.results_db(args.results_db))
            store.ensure(results, "results")
            store.sync(shared, results, day)
        except Exception as e:                   # noqa: BLE001 - 原件已落库，复制失败不该整体算失败
            store.warn(f"结果库写入失败（{e}）；稍后用 --sync 补，不联网")
        else:
            row = results.execute("SELECT * FROM price_stat WHERE date=?",
                                  (day,)).fetchone()
            if row:
                _say(f"      → price_stat 1 行 · {_stat_line(dict(row))}")

        _say(f"[3/3] 完成 · {time.time() - t0:.1f}s")
        if args.prune_keep > 0:
            _prune(args, shared, results)
        return 0
    finally:
        shared.close()
        if results is not None:
            results.close()


def _ensure_index(shared, day: str, force: bool) -> None:
    """确保 `day` 有沪深300快照；失败只 warn，不影响已经落库的全市场价格。"""
    if store.index_row(shared, day) and not force:
        return
    try:
        row = source.fetch_index(day)
    except source.SourceError as e:
        store.warn(f"沪深300 {day} 取数失败（{e}）；全市场价格不受影响")
        return
    if not row:
        store.warn(f"{day} 上游没有沪深300 K 线（非交易日或当天还没生成）")
        return
    store.write_index(shared, day, row)
    _say(f"      → 沪深300 {_m(row.get('close'))}（{_pct(row.get('pct_chg'))}）")


def _empty_hints(shared, cal, day: str) -> None:
    """"这一天上游没有数据"的三种可能，按可能性排。"""
    _say(f"❌ 上游没有 {day} 的任何一行（这天的估值快照还不存在）。")
    _say("   两个常见原因：① 当天还没收盘——这个接口收盘后才发布；"
         "② 该日不是交易日。")
    last = store.last_day(shared)
    prev = cal.on_or_before(day) if cal else None
    if last and last == prev:
        _say(f"   库里和日历都指向 {last}，想先看那天：--date {last}")
    else:
        if last:
            _say(f"   库里最近一份是 {last}，想先看那天：--date {last}")
        if prev and prev != day and prev != last:
            _say(f"   日历里它之前最近的交易日是 {prev}：--date {prev}")


def _check_day(cal, day: str) -> bool:
    """日期合法性：格式、是否交易日。**不合法就说清楚，别去打上游。**"""
    import time as _t
    try:
        _t.strptime(day, "%Y-%m-%d")
    except ValueError:
        _say(f"❌ 日期格式不对：{day}（要 YYYY-MM-DD）")
        return False
    if cal is None:
        return True                              # 没有日历就交给上游回答
    if day > cal.end:
        # 日历还没更新到这一天（例如周五盘后跑，日历停在周四）。不拦：
        # 是不是交易日由上游说了算，这里只留一句，免得事后看着像没检查过。
        _say(f"⚠️  交易日历只到 {cal.end}，无法确认 {day} 是不是交易日；继续（上游会回答）")
        return True
    if not cal.is_trading(day):
        _say(f"❌ {day} 不是交易日（日历 {cal.days[0]} .. {cal.end}）。")
        prev = cal.on_or_before(day)
        if prev:
            _say(f"   上一个交易日是 {prev}：--date {prev}")
        return False
    return True


# ---------------------------------------------------------------- 只重算摘要

def _do_restat(args) -> int:
    path = config.results_db(args.results_db)
    results = store.connect(path, create=False)
    store.ensure(results, "results")
    try:
        n = store.restat(results, (args.date or "").strip() or None)
        total = results.execute("SELECT COUNT(*) FROM price_stat").fetchone()[0]
        _say(f"✅ 重算 {n} 天 · price_stat 共 {total} 行（不联网）")
        if args.date:
            row = results.execute("SELECT * FROM price_stat WHERE date=?",
                                  (args.date,)).fetchone()
            if row:
                _say(f"   {args.date}：{_stat_line(dict(row))}")
        return 0 if n else 1
    finally:
        results.close()


# ---------------------------------------------------------------- 原始库 → 结果库

def _do_sync(args) -> int:
    shared = store.connect(config.shared_db(args.shared_db), create=False)
    store.ensure(shared, "shared")
    results = store.connect(config.results_db(args.results_db))
    store.ensure(results, "results")
    try:
        day = (args.date or "").strip() or None
        done = store.sync(shared, results, day)
        if not done:
            _say(f"⚠️  原始库里没有{'这一天' if day else '任何一天'}的数据，无事可做")
            return 1
        _say(f"✅ 同步 {len(done)} 天 → {config.results_db(args.results_db)}"
             f"（含 price_stat + 沪深300，不联网）")
        for d in done[:5]:
            row = results.execute("SELECT * FROM price_stat WHERE date=?",
                                  (d,)).fetchone()
            if row:
                _say(f"   {d} · {_stat_line(dict(row))}")
        if len(done) > 5:
            _say(f"   ……（还有 {len(done) - 5} 天）")
        return 0
    finally:
        shared.close()
        results.close()


def _prune(args, shared, results) -> None:
    keep = args.prune_keep
    for label, conn in (("原始库", shared), ("结果库", results)):
        if conn is None:
            continue
        try:
            n = store.prune(conn, keep)
            if n:
                _say(f"   清理{label}：删掉 {keep} 天之前的 {n} 行 price")
        except Exception as e:                   # noqa: BLE001 - 清理失败不影响本次结果
            store.warn(f"{label}清理失败（不影响本次结果）：{e}")


# ---------------------------------------------------------------- 参数

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m stock_price_show",
        description="全市场价格/估值快照：写 shared.db.price + index_price，"
                    "复制到 results.db（price + price_stat + index_price）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""落库位置：
  shared.db   price + index_price             原件（从上游原样落库）
  results.db  price + index_price + price_stat
                                              结果层（看板只读这里）

示例：
  python3 -m stock_price_show                        # 今天（过了 15:05）或上一交易日
  python3 -m stock_price_show --date 2026-11-16
  python3 -m stock_price_show --date 2026-11-16 --force
  python3 -m stock_price_show --restat               # 只按结果库的 price 重算摘要
  python3 -m stock_price_show --sync                 # 原始库 → 结果库，不联网
  python3 -m stock_price_show --prune-keep 30

⚠️ 这是**收盘快照**：上游当天收盘后才发布，盘中跑「今天」会报「没有这一天的数据」。""")
    p.add_argument("--date", default=None,
                   help="交易日 YYYY-MM-DD；不给就用交易日历定（今天过 15:05 才算今天）")
    p.add_argument("--force", action="store_true",
                   help="忽略库里已有的那天，重新取一次（整天整份替换）")
    p.add_argument("--restat", action="store_true",
                   help="只按结果库里已有的 price 重算 price_stat（不联网）")
    p.add_argument("--sync", action="store_true",
                   help="把 shared.db 里已有的日子复制/覆盖到 results.db（不联网）")
    p.add_argument("--prune-keep", type=int, default=0, metavar="天",
                   help="取完之后删掉这么多天之前的 price 行；0 = 不清理（默认）")
    p.add_argument("--shared-db", default=None, help="原始库路径（默认 shared.db）")
    p.add_argument("--results-db", default=None, help="结果库路径（默认 results.db）")
    p.add_argument("--calendar", default=None, help="交易日历 JSON 路径")
    return p


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(main())
