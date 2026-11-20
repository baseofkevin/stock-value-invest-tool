"""CLI：抓原件 → 算三路 → 写结果层 → 打一行摘要。

    python -m stock_market_regime --date 2026-11-20
    python -m stock_market_regime --date 2026-11-20 --recompute    # 不联网，用 shared.db 重算
    python -m stock_market_regime --json /tmp/regime.json

降级规则（这是原件层存在的意义）
--------------------------------
任何一路下载失败 → 退回 `shared.db` 里上一次成功的原件，结果行标 `stale=1`，
页面横幅照实写"部分数据沿用上次"。**只有三路全都没有时才算失败**（退出码 2），
不让一个 403 把整份周报拖没。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from . import rules, source, store, store_raw


def _warn(msg: str) -> None:
    print(f"[regime] {msg}", file=sys.stderr)


def _progress(index: int, total: int, name: str, quiet: bool) -> None:
    """`run.py` 的进度条认 `[n/m]` 开头的行；独立运行时它也就是个序号。"""
    if not quiet:
        print(f"[{index}/{total}] {name}", flush=True)


def _fetch_or_fallback(*, name: str, index: int, total: int, quiet: bool,
                       fetcher, saver, reader, conn, recompute: bool):
    """抓一路；失败就退回库里的原件。返回 (rows, stale)。"""
    if not recompute:
        _progress(index, total, name, quiet)
        try:
            rows = fetcher()
            saver(conn, rows)
            return rows, False
        except Exception as e:  # noqa: BLE001  上游什么样都可能
            _warn(f"{name} 抓取失败（{e}），改用 shared.db 上次的原件")

    rows = reader(conn)
    if not rows:
        _warn(f"{name} 库里也没有原件——这一路留空")
    return rows, bool(rows) and not recompute


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m stock_market_regime",
        description="A股大势粗判：趋势 / 赔率 / 情绪，写 results.db.market_regime")
    ap.add_argument("--date", help="基准日 YYYY-MM-DD（默认今天；run.py 会传周五）。"
                                   "**只是标签**：三路始终用截至最新的数据算，"
                                   "拿它回填旧日期会把那一周的记录改成今天的数")
    ap.add_argument("--recompute", action="store_true",
                    help="不联网，直接用 shared.db 里的原件重算（改阈值后用）")
    ap.add_argument("--json", dest="json_path", help="额外把结果写成 JSON")
    ap.add_argument("--quiet", action="store_true", help="不打印抓取进度")
    args = ap.parse_args(argv)

    day = args.date or dt.date.today().isoformat()
    total = 4

    raw = store_raw.connect()
    result_db = store.connect()

    weeks, s1 = _fetch_or_fallback(
        name="沪深300 周K", index=1, total=total, quiet=args.quiet,
        fetcher=source.hs300_week, saver=store_raw.save_week,
        reader=store_raw.read_week, conn=raw, recompute=args.recompute)

    perf, s2 = _fetch_or_fallback(
        name="沪深300 滚动PE", index=2, total=total, quiet=args.quiet,
        fetcher=source.hs300_perf, saver=store_raw.save_perf,
        reader=store_raw.read_perf, conn=raw, recompute=args.recompute)

    bonds, s3 = _fetch_or_fallback(
        name="10年国债", index=3, total=total, quiet=args.quiet,
        fetcher=source.bond10y,
        saver=lambda c, rows: store_raw.save_macro(c, "bond10y", rows, "eastmoney"),
        reader=lambda c: store_raw.read_macro(c, "bond10y"),
        conn=raw, recompute=args.recompute)

    below, s4 = _fetch_or_fallback(
        name="全A破净率", index=4, total=total, quiet=args.quiet,
        fetcher=source.below_na,
        saver=lambda c, rows: store_raw.save_macro(c, "below_na", rows, "legulegu"),
        reader=lambda c: store_raw.read_macro(c, "below_na"),
        conn=raw, recompute=args.recompute)

    stale = any((s1, s2, s3, s4))
    result = rules.judge(weeks=weeks, perf=perf, bonds=bonds, below=below)

    if not any((result["trend"], result["valuation"], result["sentiment"])):
        _warn("三路都没算出来，不写库")
        return 2

    store.save(result_db, day, result, stale=stale)

    if not args.quiet:
        print()
    _report(day, result, stale)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump({"date": day, "stale": stale, **result}, fh,
                      ensure_ascii=False, indent=2)
        if not args.quiet:
            print(f"  JSON → {args.json_path}")
    return 0


def _report(day: str, result: dict, stale: bool) -> None:
    t = result["trend"]
    v = result["valuation"]
    s = result["sentiment"]

    print(f"大势 {day}" + ("  ⚠️ 有源沿用上次" if stale else ""))
    if t:
        print(f"  趋势  {t['state']} · {t['close']} · 已 {t['weeks_below_ma20']} 周在 MA20 下方"
              f"（MA20 {t['ma20']} / MA60 {t['ma60']} · MA60 4周 {t['ma60_slope']:+.2f}%）")
    else:
        print("  趋势  —")
    if v:
        print(f"  估值  ERP {v['erp']}% · 近10年 {v['pct']}% 分位 · "
              f"PE {v['pe_ttm']} / 10Y {v['bond10y']}%")
    else:
        print("  估值  —")
    if s:
        dry = "—" if s["volume_dry"] is None else ("地量" if s["volume_dry"] else "未地量")
        turn = "—" if s["turnover"] is None else s["turnover"]
        q10 = "—" if s["turnover_q10"] is None else s["turnover_q10"]
        print(f"  情绪  破净 {s['below_na']}%（{s['below_na_pct']}% 分位）· {dry}"
              f"（周换手 {turn}% vs 3年10%分位 {q10}%）")
    else:
        print("  情绪  —")
    print(f"  数据截至 {result['data_asof']} · 已写入 results.db（market_regime {day}）")


if __name__ == "__main__":
    sys.exit(main())
