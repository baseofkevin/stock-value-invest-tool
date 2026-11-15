"""`repair` —— 把"**我们记漏了**"的分红补回去。**默认只看不写，`--apply` 才落库。**

## 为什么需要一条修复通道

`dividend` 是 append-only：`api._write_dividend` 对已存在的行一条都不改。那条规矩防的是
"**上游改数**"——除权日是既成事实，今天说 1.0、明天说 1.2 的时候，信哪个都会让复权因子
的历史跟着动，而所有回测结论都建在那个历史上。

但有一种情况不是上游改数，是**我们记漏**：上游把"年度分红 + 特殊分红"拆成两条共用同一个
`exDiviDate`，早期版本逐条写库，后一条 `ON CONFLICT DO UPDATE` 掉前一条，库里只剩一半
（详见 `sources/westock._dividend_row` 的实测原文）。这种行**重跑 `ensure` 也修不好**——
append-only 让它连"想改"都不允许，`probe --dividend` 会年复一年地报同一条不一致。

所以补一条**窄口径**通道，只在能证明"库里这条是上游的若干分之一"时才动手：

    同一个除权日，上游有 >= 2 条方案，
    且库里的 (派, 送, 转) **恰好等于其中一条**，
    且上游合计**大于**库里的值
    → 库里这条就是漏记，改写成合计。

三条不动的原则：

1. **认不出这个签名就不碰。** 换源导致的正常差异、上游真的改数、我们真的多了钱——
   都不属于这一类，`repair` 只报"不修"，把判断留给 `probe` 的输出和人的眼睛。
2. **默认 dry-run。** 写库要显式 `--apply`；先看清要改哪几行再动手。
3. **每次改写留痕**到 `meta`：`dividend_repair/<symbol>/<ex_date>`，值是一行 JSON
   （旧值 / 新值 / 上游各条方案 / 时间）。留痕比日志可靠：日志会滚掉，`meta` 跟库走。

改完要清 `factor` 的进程内缓存——签名里含 `updated_at`，正常情况下会自动失效，
但"改了值却没变行数"正是最容易骗过缓存的情形，显式清一次不贵。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from . import db
from . import factor as F
from .config import Config
from .sources import westock as W

log = logging.getLogger("stock_data_fetch.repair")

SUM_FIELDS = ("cash_per_share", "bonus_per_share", "transfer_per_share", "rights_per_share")
"""参与求和的数值列。与 `sources/westock._SUM_FIELDS` 同口径——两边不一致就等于没修。"""

DEFAULT_YEARS = 11
"""问上游多少年。与 `_years_for` 的下限一致：少要一年就可能看不见要修的那条。"""


@dataclass
class Fix:
    """一条待修/已修的分红行。"""

    symbol: str
    ex_date: str
    old: tuple[float, float, float, float]
    new: tuple[float, float, float, float]
    plans: list[str]

    @property
    def text(self) -> str:
        return " + ".join(self.plans)[:200]

    def line(self) -> str:
        return (f"{self.symbol} {self.ex_date}  派 {self.old[0]:.4f} → {self.new[0]:.4f}"
                f"（{' + '.join(self.plans)}）")


def find_fixes(conn, symbol: str, plans: list[dict]) -> list[Fix]:
    """纯函数：给定上游方案链表，找出"库里漏记"的行。**不联网、不写库。**

    判据见模块头。逐条比 `_dividend_row` 的单方案行，而不是拿聚合值去比——
    "恰好等于其中一条"这个条件才是签名本身，用聚合值比会把"上游真的改数"也当成漏记。
    """
    single: dict[str, list[dict]] = {}
    for plan in plans:
        row = W._dividend_row(symbol, plan)
        if row:
            single.setdefault(row["ex_date"], []).append(row)
    summed = {r["ex_date"]: r for r in W._dividend_rows(symbol, plans)}

    out: list[Fix] = []
    for ex, group in sorted(single.items()):
        if len(group) < 2:
            continue                              # 只有一条方案：没有"漏记"这回事
        cur = conn.execute(
            f"SELECT {','.join(SUM_FIELDS)} FROM dividend WHERE symbol=? AND ex_date=?",
            (symbol, ex)).fetchone()
        if cur is None:
            continue                              # 缺行由 ensure 补，不归 repair 管
        have = tuple(float(cur[k] or 0.0) for k in SUM_FIELDS)
        matches = [g for g in group if _same(have, g)]
        if not matches:
            continue                              # 对不上任何单条方案 → 不是这个签名，不碰
        want = tuple(float(summed[ex].get(k) or 0.0) for k in SUM_FIELDS)
        if not any(want[i] > have[i] + 1e-9 for i in range(len(want))):
            continue                              # 合计不比库里大 → 没什么可补
        out.append(Fix(symbol, ex, have, want,
                       [g.get("plan_text") or "" for g in group]))
    return out


def _same(a: tuple, b: dict) -> bool:
    return all(abs(a[i] - float(b.get(k) or 0.0)) < 1e-9
               for i, k in enumerate(SUM_FIELDS))


def apply_fix(conn, fix: Fix, *, src: str = "tencent") -> None:
    """写回一行 + 留痕。**调用方负责事务**（`db.tx`）。

    留痕这里**不用 `db.meta_set`**：它自己开 `BEGIN IMMEDIATE`，套在 `db.tx` 里就是
    "transaction within a transaction"。分行提交也不行——要的是"改了必有痕、有痕必改了"，
    两条语句必须在同一个事务里。
    """
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE dividend SET cash_per_share=?, bonus_per_share=?, transfer_per_share=?, "
        "rights_per_share=?, plan_text=?, updated_at=? WHERE symbol=? AND ex_date=?",
        (*fix.new, fix.text, stamp, fix.symbol, fix.ex_date))
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"dividend_repair/{fix.symbol}/{fix.ex_date}",
         json.dumps({"old": list(fix.old), "new": list(fix.new),
                     "plans": fix.plans, "at": stamp, "src": src},
                    ensure_ascii=False)))


def repair_symbol(conn, symbol: str, *, apply: bool = False,
                  years: int | None = None) -> list[Fix]:
    """联网查一次上游，修（或只报告）这一只。**上游挂了不抛**——返回空并记一条 warning。"""
    years = years or DEFAULT_YEARS
    try:
        d = W.call("data_dividend", {"code": symbol, "years": years})
    except Exception as e:                        # noqa: BLE001 - 维护工具不该炸在半路
        log.warning("%s 取不到上游分红，跳过：%s", symbol, e)
        return []
    plans = (d or {}).get("plans") or []
    if not plans:
        return []
    fixes = find_fixes(conn, symbol, plans)
    if fixes and apply:
        with db.tx(conn):
            for f in fixes:
                apply_fix(conn, f)
        F.clear_cache(symbol)
        for f in fixes:
            log.info("已修 %s", f.line())
    return fixes


def run(*, symbols: list[str] | None = None, apply: bool = False,
        years: int | None = None, cfg: Config | None = None) -> list[Fix]:
    """批量修。没给 `symbols` 就修库里所有有分红记录的票。"""
    cfg = cfg or Config.from_env()
    conn = db.connect(cfg.db_path, readonly=not apply)
    try:
        todo = symbols or [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM dividend ORDER BY symbol")]
        if not todo:
            log.info("库里的 dividend 表是空的，没什么可修")
            return []
        found: list[Fix] = []
        for sym in todo:
            found += repair_symbol(conn, sym, apply=apply, years=years)
        return found
    finally:
        conn.close()


def render(fixes: list[Fix], *, applied: bool) -> str:
    if not fixes:
        return "没有需要修的行：库里没有「只记了上游其中一条方案」的分红。"
    head = (f"已修 {len(fixes)} 行：" if applied
            else f"发现 {len(fixes)} 行漏记（**没写库**，加 --apply 才落）：")
    return "\n".join([head, *(f"  - {f.line()}" for f in fixes)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="修被 append-only 卡住的「漏记分红」。默认只看不写。")
    ap.add_argument("target", choices=("dividend",), help="目前只有 dividend 一种")
    ap.add_argument("--symbols", default="", help="逗号分隔；默认库里所有有分红的票")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS)
    ap.add_argument("--db", default="", help="默认按 STOCKHUB_DB / Config 的库")
    ap.add_argument("--apply", action="store_true", help="真的写库（不加就是 dry-run）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    cfg = Config.from_env()
    if args.db:
        cfg.db_path = Path(args.db).expanduser()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    fixes = run(symbols=symbols, apply=args.apply, years=args.years, cfg=cfg)
    print(render(fixes, applied=args.apply))
    if fixes and not args.apply:
        print("\n（dry-run：以上都还没写库。确认无误后加 --apply）")
    return 0


if __name__ == "__main__":                        # pragma: no cover
    sys.exit(main())
