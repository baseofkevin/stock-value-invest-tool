"""`ensure_bulk()` 的增量语义测试（离线，不联网）。

**为什么单独建一个文件**：`ensure_bulk` 是四个工具取 K 线的主路径（一次 4000+ 只），
但在这之前**一条测试都没有**——`grep ensure_bulk stock_data_fetch/tests/*.py` 零命中。
结果是它悄悄跟单只路径 `_pull()` 分了叉：

    `_pull()`     先 `missing_segments()` 算出"缺哪一段"，只取那一段（增量）
    `ensure_bulk()` 直接拿 `need` 整段去取（永远 300 根 × 全部股票）

2026-11-15 实测后果：日增量该回 4304 行，实际请求 1,291,200 行（300 倍），
一次批量就把上游配额桶打穿，4304 只要跑 50–75 分钟。本文件把那三条语义钉住：

1. 只缺最后一天 → 只请求那一天（`start = 覆盖末端之后的下一个交易日`；
   日历末端不够时退回自然日 +1，见 `coverage._next_trading`），不是整段；
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

    def __init__(self, bar_for, drop_first=False, raise_on=None):
        self.calls: list[tuple[list[str], object]] = []
        self.bar_for = bar_for
        self.drop_first = drop_first
        self.raise_on = raise_on or (lambda syms: False)
        self.spec = sources.get("westock")

    def fetch(self, domain, codes, need):
        self.calls.append((list(codes), need))
        if self.raise_on(codes):
            raise RuntimeError("假装上游这次没回（偶发）")
        got = sources.Got()
        use = list(codes)
        if self.drop_first and len(self.calls) == 1:
            use = codes[: max(1, len(codes) // 4)]      # 只回 1/4，触发大面积丢码
        for sym in use:
            row = self.bar_for(sym, need.end)
            if row:
                got.rows[sym] = row if isinstance(row, list) else [row]
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

    # 锚点必须固定在**日历内部**，不能用 `cal.days[-1]`。
    # 2026-11-17 改：默认日历已由 `build_calendar.py` 生成到当天（末端 2026-11-17），
    # 旧写法 `end = cal.days[-1]` 会把锚点推到 11-17——比下面的 horizon(11-16) 还晚，
    # 于是用例 1 变成"已命中、零请求"，`rows.calls[0]` 直接 IndexError。
    end = "2026-11-13"                          # 周五，日历内部的固定锚点
    need = want.kline(cal, end, bars=300, min_bars=250)
    horizon = "2026-11-16"                      # 下一交易日（人为越过覆盖末端）

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
    # 起点 = 覆盖末端（2026-11-13 周五）之后的**下一个交易日** 11-16。
    # 2026-11-17 改：旧断言写死 "2026-11-14"，那是 `_next_trading()` 的**退路**——
    # 当时日历末端正好是 11-13，`cal.on_or_after("2026-11-14")` 返回 None 才退回自然日 +1。
    # 日历现在覆盖到 11-17，对齐到交易日 11-16 才是主线行为（也是更省的请求）。
    check("请求的是增量段（起点=覆盖末端之后的下一个交易日）", used.start == "2026-11-16",
          str(used.start))
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

    # ---------------------------------------------------------------- 用例 5
    print("\n用例 5：某一组上游抛异常 → 只把该组记失败，不许打断整跑")
    # 两组要落在**不同的缺段**上，否则会合并成一组、一起成败：
    #   好组覆盖到 11-06 → 缺 (11-09, 11-13)
    #   坏组覆盖到 11-05 → 缺 (11-06, 11-13)
    batch5 = ["sh605000", "sh605001"]
    bad5 = ["sh605002", "sh605003"]
    for sym5 in batch5:
        seed_bars(conn, cal, sym5, "2026-11-06", 300)
        with db.tx(conn):
            C.merge(conn, cal=cal, need=dataclasses.replace(
                need, end="2026-11-06", start="2025-08-18"), symbol=sym5, src="eastmoney")
    for sym5 in bad5:
        seed_bars(conn, cal, sym5, "2026-11-05", 300)
        with db.tx(conn):
            C.merge(conn, cal=cal, need=dataclasses.replace(
                need, end="2026-11-05", start="2025-08-18"), symbol=sym5, src="eastmoney")
    rows5 = RecordingRouter(lambda s, e: {"date": e, "open": 6.0, "high": 6.5, "low": 5.5,
                                          "close": 6.2, "volume": 1e6, "amount": 1e7},
                            raise_on=lambda syms: "sh605002" in syms)
    api._router = lambda hub, throttle: rows5
    try:
        out5 = api.ensure_bulk(hub, "kline", batch5 + bad5, need)
        raised = None
    except Exception as exc:                     # noqa: BLE001
        raised, out5 = exc, None
    check("没有把异常抛给调用方", raised is None, repr(raised))
    if out5 is not None:
        check("坏组进了 failed", set(bad5) <= set(out5.failed), str(list(out5.failed)))
        check("好组照样落库", set(batch5) <= set(out5.ok), str(list(out5.ok)))
        check("两组确实分成两批取", len(rows5.calls) == 2, str(len(rows5.calls)))

    # ---------------------------------------------------------------- 用例 6
    print("\n用例 6：覆盖已到末端但根数不够（次新股）→ 不打上游，直接判深度不足")
    newbie = "sh605100"
    seed_bars(conn, cal, newbie, "2026-11-13", 40)        # 只有 40 根，永远到不了 71
    with db.tx(conn):
        C.merge(conn, cal=cal, need=need, symbol=newbie, src="eastmoney")
    cov_nb = C.read(conn, "kline", newbie, need.fp)
    check("种下的次新股 n=40 且不命中", cov_nb.n == 40 and C.hit(cov_nb, need) is False,
          f"n={cov_nb.n}")
    rows6 = RecordingRouter(lambda s, e: None)
    api._router = lambda hub, throttle: rows6
    out6 = api.ensure_bulk(hub, "kline", [newbie], need)
    check("一次上游都没打", len(rows6.calls) == 0, str(len(rows6.calls)))
    check("记的是 InsufficientDepth 而不是「取数失败」",
          isinstance(out6.failed.get(newbie), api.InsufficientDepth),
          type(out6.failed.get(newbie)).__name__)

    # ---------------------------------------------------------------- 用例 7
    print("\n用例 7：成片历史缺口 → 小分组并成 1 个整段批次，别打成上百次请求")
    hole_syms = [f"sh6052{i:02d}" for i in range(10)]
    for i, sym7 in enumerate(hole_syms):
        seed_bars(conn, cal, sym7, "2026-11-06", 300)
        with db.tx(conn):
            C.merge(conn, cal=cal, need=dataclasses.replace(
                need, end="2026-11-06", start="2025-08-18"), symbol=sym7, src="eastmoney")
        gone = conn.execute("SELECT date FROM kline WHERE symbol=? ORDER BY date LIMIT 1 OFFSET ?",
                            (sym7, 100 + i * 7)).fetchone()[0]
        with db.tx(conn):
            conn.execute("DELETE FROM kline WHERE symbol=? AND date=?", (sym7, gone))
            C.merge(conn, cal=cal, need=dataclasses.replace(
                need, end="2026-11-06", start="2025-08-18"), symbol=sym7, src="eastmoney")
    blocked = [s for s in hole_syms if not C.hit(C.read(conn, "kline", s, need.fp), need)]
    check("10 只都被缺口挡住（各自的缺口日期不同）", len(blocked) == 10, str(blocked))
    rows7 = RecordingRouter(lambda sym, e: {"date": e, "open": 7.0, "high": 7.5, "low": 6.5,
                                            "close": 7.2, "volume": 1e6, "amount": 1e7})
    api._router = lambda hub, throttle: rows7
    api.ensure_bulk(hub, "kline", hole_syms, need)
    # 两组：① 10 只共用的末端段 (11-09..11-13)；② 10 个缺口合并成的整段批次。
    # 并批前这里是 11 组、11 次请求——正是把 westock 打成限频的那个形状。
    check("请求数从 11 降到 2（并批生效）", len(rows7.calls) == 2,
          f"{len(rows7.calls)} 次：{[(len(c), str(n.start), str(n.end)) for c, n in rows7.calls]}")
    whole7 = [n for c, n in rows7.calls if n.start == "2025-08-18"]
    check("缺口被并成一个整段批次（10 只一起）",
          len(whole7) == 1 and whole7[0].points >= 250, str([n.points for n in whole7]))

    # ---------------------------------------------------------------- 用例 8
    print("\n用例 8：缺口重取过还是缺 → 记一次退避，下一轮不再白打（原来永不收敛）")
    holed8 = "sh605300"
    # 覆盖要到 need.end（否则"末端不足"本身就该重取，测不出退避的效果）
    seed_bars(conn, cal, holed8, "2026-11-13", 300)
    gone8 = conn.execute("SELECT date FROM kline WHERE symbol=? ORDER BY date LIMIT 1 OFFSET 120",
                         (holed8,)).fetchone()[0]
    with db.tx(conn):
        conn.execute("DELETE FROM kline WHERE symbol=? AND date=?", (holed8, gone8))
        C.merge(conn, cal=cal, need=need, symbol=holed8, src="eastmoney")
    check("缺口存在且阻塞命中",
          C.read(conn, "kline", holed8, need.fp).blocking == 1,
          str(C.read(conn, "kline", holed8, need.fp).detail))

    # 上游"重取一遍还是缺那一天"：直接把库里现有的 299 根再回一遍。
    same8 = [r for r in api.read(hub, "kline", holed8, need).rows]
    rows8 = RecordingRouter(lambda sym, e: same8)
    api._router = lambda hub, throttle: rows8
    api.ensure_bulk(hub, "kline", [holed8], need)
    cov8 = C.read(conn, "kline", holed8, need.fp)
    check("重取过这一段仍缺 → retry 记到 1", cov8.detail and cov8.detail[0].retry == 1,
          str(cov8.detail))
    check("记了下次再试的时间（不再每轮都打）",
          bool(cov8.detail and cov8.detail[0].next_try_at), str(cov8.detail))
    n8 = len(rows8.calls)
    api.ensure_bulk(hub, "kline", [holed8], need)
    check("紧接着再跑一次不再打上游", len(rows8.calls) == n8, str(len(rows8.calls) - n8))

    conn.close()
    print("\n" + "=" * 50)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  失败：" + f)
    print(f"临时目录（可删）：{tmp}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
