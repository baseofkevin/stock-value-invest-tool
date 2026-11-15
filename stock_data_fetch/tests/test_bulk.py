"""`ensure_bulk()` 的增量语义测试（离线，不联网）。

**为什么单独建一个文件**：`ensure_bulk` 是四个工具取 K 线的主路径（一次 4000+ 只），
但在这之前**一条测试都没有**——`grep ensure_bulk stock_data_fetch/tests/*.py` 零命中。
结果是它悄悄跟单只路径 `_pull()` 分了叉：

    `_pull()`     先 `missing_segments()` 算出"缺哪一段"，只取那一段（增量）
    `ensure_bulk()` 直接拿 `need` 整段去取（永远 300 根 × 全部股票）

2026-11-15 实测后果：日增量该回 4304 行，实际请求 1,291,200 行（300 倍），
一次批量就把上游配额桶打穿，4304 只要跑 50–75 分钟。本文件把那三条语义钉住：

1. 只缺最后一天 → 只请求那一天（`start = 覆盖末端 + 1 天`），不是整段；
2. 完全没有覆盖 → 才整段取；
3. 批量里大面积丢码 → 拆半再取（原版 `_kline_fetch` 的做法），而不是整批重试。

跑法：

    cd /home/kevin/MyData/stock-value-invest-tool && \
    STOCKHUB_TMP=/home/kevin/DeepSeekHarness/.review-tmp \
    /home/kevin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
        -m stock_data_fetch.tests.test_bulk
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import api, sources                        # noqa: E402
from stock_data_fetch import coverage as C                       # noqa: E402
from stock_data_fetch import db                                  # noqa: E402
from stock_data_fetch.calendar import TradingCalendar            # noqa: E402
from stock_data_fetch.config import DEFAULT_CALENDAR, DEFAULT_SCHEMA  # noqa: E402
from stock_data_fetch.need import want                           # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(f"{name}{('：' + detail) if detail else ''}")
        print(f"  ❌ {name}" + (f"（{detail}）" if detail else ""))


def mkdtemp(prefix: str) -> Path:
    root = Path(os.environ.get("STOCKHUB_TMP") or PROJECT / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


class FakeHub:
    """只实现 `ensure_bulk()` 真正用到的那几个属性。"""

    def __init__(self, conn, cal, target):
        self.conn = conn
        self.cal = cal
        self.target = target
        self.throttle = object()          # 真值即可，免得去构造 Throttle
        self.cfg = None
        self.pruned = []


class RecordingRouter:
    """记录每次 `fetch()` 拿到的 need，并按指定行为返回行。

    `drop_first`：第一次调用故意只回一半，用来触发拆半重取。
    """

    def __init__(self, bar_for, drop_first=False):
        self.calls: list[tuple[list[str], object]] = []
        self.bar_for = bar_for
        self.drop_first = drop_first
        self.spec = sources.get("westock")

    def fetch(self, domain, codes, need):
        self.calls.append((list(codes), need))
        got = sources.Got()
        use = list(codes)
        if self.drop_first and len(self.calls) == 1:
            use = codes[: max(1, len(codes) // 4)]      # 只回 1/4，触发大面积丢码
        for sym in use:
            row = self.bar_for(sym, need.end)
            if row:
                got.rows[sym] = [row]
        return got, self.spec


def seed_bars(conn, cal, sym, end, n):
    """往库里塞 n 根 bar，最后一天是 `end`。"""
    days = [d for d in cal.days if d <= end][-n:]
    with db.tx(conn):
        conn.executemany(
            "INSERT OR REPLACE INTO kline(symbol,date,open,high,low,close,volume,amount) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(sym, d, 10.0, 10.5, 9.5, 10.2, 1e6, 1e7) for d in days])
    return days


def main() -> int:
    tmp = mkdtemp("bulk-")
    db_path = tmp / "t.db"
    db.create_db(db_path, DEFAULT_SCHEMA)
    conn = db.connect(db_path)
    cal = TradingCalendar.load(DEFAULT_CALENDAR)

    end = cal.days[-1]                          # 2026-11-13，日历末端
    need = want.kline(cal, end, bars=300, min_bars=250)
    horizon = "2026-11-16"                      # 日历之后的下一交易日（人为越过末端）

    # ---------------------------------------------------------------- 用例 1
    print("\n用例 1：只缺最后一天 → 只请求那一天")
    sym = "sh600000"
    seed_bars(conn, cal, sym, end, 300)
    with db.tx(conn):
        C.merge(conn, cal=cal, need=need, symbol=sym, src="eastmoney")
    check("种下后命中", C.hit(C.read(conn, "kline", sym, need.fp), need) is True)

    need2 = dataclasses.replace(need, end=horizon)
    rows = RecordingRouter(lambda s, e: {"date": e, "open": 11.0, "high": 11.5,
                                         "low": 10.5, "close": 11.2,
                                         "volume": 2e6, "amount": 2e7})
    api._router = lambda hub, throttle: rows                # 打桩，不打上游
    hub = FakeHub(conn, cal, horizon)
    out = api.ensure_bulk(hub, "kline", [sym], need2)

    check("确实取了一次", len(rows.calls) == 1, str(len(rows.calls)))
    codes, used = rows.calls[0]
    check("请求的是增量段（起点=覆盖末端+1天）", used.start == "2026-11-14", str(used.start))
    check("请求段终点=need.end", used.end == horizon, str(used.end))
    check("配额估算按增量算（points ≤ 3，而不是 300）", int(used.points) <= 3,
          str(used.points))
    check("落库成功", sym in out.ok, str(list(out.failed)))
    cov = C.read(conn, "kline", sym, need2.fp)
    check("覆盖末端推进到新日期", cov.end == horizon, str(cov.end))
    check("加了一根（300 → 301）", cov.n == 301, str(cov.n))
    check("改完之后命中，不再重复取", C.hit(cov, need2) is True)

    # ---------------------------------------------------------------- 用例 2
    print("\n用例 2：完全没有覆盖 → 整段取")
    fresh = "sh600001"
    rows2 = RecordingRouter(lambda s, e: {"date": e, "open": 9.0, "high": 9.5,
                                          "low": 8.5, "close": 9.2,
                                          "volume": 1e6, "amount": 1e7})
    api._router = lambda hub, throttle: rows2
    api.ensure_bulk(hub, "kline", [fresh], need)
    codes, used = rows2.calls[0]
    check("新股请求的是完整窗口", used.start == need.start and used.end == need.end,
          f"{used.start}..{used.end} vs {need.start}..{need.end}")
    check("新股配额按整段算", int(used.points) == 300, str(used.points))

    # ---------------------------------------------------------------- 用例 3
    print("\n用例 3：批量大面积丢码 → 拆半重取")
    batch = ["sh601000", "sh601001", "sh601002", "sh601003"]
    rows3 = RecordingRouter(lambda s, e: {"date": e, "open": 8.0, "high": 8.5,
                                          "low": 7.5, "close": 8.2,
                                          "volume": 1e6, "amount": 1e7},
                            drop_first=True)
    api._router = lambda hub, throttle: rows3
    out3 = api.ensure_bulk(hub, "kline", batch, need)
    check("第一次只回了 1/4，触发了补取", len(rows3.calls) > 1, str(len(rows3.calls)))
    check("四只全部落库", len(out3.ok) == 4, str(list(out3.failed)))
    refetched = [c for codes_, _ in rows3.calls[1:] for c in codes_]
    check("补取只针对丢掉的代码", sorted(refetched) == sorted(batch[1:]),
          f"补取={refetched} 期望={batch[1:]}")

    # ---------------------------------------------------------------- 用例 4
    print("\n用例 4：少数几只没回（停牌）→ 不拆半、不重取")
    batch4 = ["sh603000", "sh603001", "sh603002", "sh603003",
              "sh603004", "sh603005", "sh603006", "sh603007"]

    def bar_but_skip_suspended(s, e):
        if s in ("sh603007",):                 # 模拟停牌：上游不回这一只
            return None
        return {"date": e, "open": 7.0, "high": 7.5, "low": 6.5, "close": 7.2,
                "volume": 1e6, "amount": 1e7}

    rows4 = RecordingRouter(bar_but_skip_suspended)
    api._router = lambda hub, throttle: rows4
    out4 = api.ensure_bulk(hub, "kline", batch4, need)
    check("只取了一次（没有为一只停牌股拆半重问）", len(rows4.calls) == 1,
          str(len(rows4.calls)))
    check("7 只成功、1 只进 failed", len(out4.ok) == 7 and len(out4.failed) == 1,
          f"ok={len(out4.ok)} failed={len(out4.failed)}")

    conn.close()
    print("\n" + "=" * 50)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  失败：" + f)
    print(f"临时目录（可删）：{tmp}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
