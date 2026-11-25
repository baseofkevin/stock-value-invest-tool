"""need / coverage 测试（离线，不联网，不打上游）。

跑法：

    cd /home/kevin/MyData/stock_value_invest_tool/stock_data_fetch && \
    /home/kevin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
        -m stock_data_fetch.tests.test_coverage

覆盖 `CONTRACT` §16 第 2 步的验收：用例 5（人为删一天 → `InsufficientDepth`，不返回 299 根）、
补充用例 h（hole 退避状态机），以及 `latest_report_period` 的法定披露边界。

**这里没有一个断言依赖"今天"**——`now` 一律注入，日历用仓库里那份真实的
`trading_calendar.json`（2022-11-14 .. 2026-11-13，970 个交易日）。
"""

from __future__ import annotations

import datetime as dt
import os
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent        # stock_value_invest_tool/（包的父目录）
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import coverage as C                     # noqa: E402
from stock_data_fetch import db, need as N                     # noqa: E402
from stock_data_fetch.calendar import TradingCalendar          # noqa: E402
from stock_data_fetch.config import DEFAULT_CALENDAR, DEFAULT_SCHEMA  # noqa: E402
from stock_data_fetch.errors import (HoleUnreachable, InsufficientDepth,  # noqa: E402
                              PartialData)
from stock_data_fetch.need import Need, latest_report_period, want  # noqa: E402

def mkdtemp(prefix: str) -> Path:
    """测试临时目录。

    **不能用 `/tmp`**：本机 `/tmp` 是 10MB 的 tmpfs，建几个 SQLite（含 WAL）就会撞墙，
    表现为进程被 SIGKILL、连报错都没有（`EXIT=137`）。放到磁盘上，
    可用 `STOCKHUB_TMP` 改位置。
    """
    root = Path(os.environ.get("STOCKHUB_TMP")
                or HERE.parent.parent.parent / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))

PASS = 0
FAIL: list[str] = []

PLAIN = 0          # 只打印，不计数


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def expect_raise(name: str, exc, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc as e:
        check(name, True)
        print(f"      → {type(e).__name__}: {e}")
        return
    except Exception as e:                              # noqa: BLE001
        check(name, False, f"抛的是 {type(e).__name__}: {e}")
        return
    check(name, False, "没有抛异常")


def section(title: str) -> None:
    print(f"\n{title}")


# ================================================================ 1. 报告期

def test_report_period() -> None:
    section("latest_report_period（法定披露期限）")
    cases = [
        # (今天, 应该已能看到的最新报告期, 说明)
        ("2026-01-15", "2025-09-30", "1 月中，年报还没出"),
        ("2026-04-30", "2025-09-30", "4-30 当天：年报卡最后一天披露，给 1 天宽限"),
        ("2026-05-01", "2026-03-31", "过宽限：年报与一季报同一天截止，一季报更晚所以胜出"),
        ("2026-08-31", "2026-03-31", "半年报截止当天，还没出"),
        ("2026-09-01", "2026-06-30", "过宽限，半年报可见"),
        ("2026-10-31", "2026-06-30", "三季报截止当天"),
        ("2026-11-01", "2026-09-30", "过宽限，三季报可见"),
        ("2026-11-14", "2026-09-30", "本次运行的目标日"),
        ("2025-01-01", "2024-09-30", "跨年：不回到 2024 年报"),
    ]
    for day, expect, note in cases:
        got = latest_report_period(day)
        check(f"{day} → {expect}（{note}）", got == expect, f"得到 {got}")

    # 一整年逐日单调不降、且只落在 4 个合法报告期上
    legal = {f"2025-{s}" for s in ("03-31", "06-30", "09-30", "12-31")} | \
            {f"2026-{s}" for s in ("03-31", "06-30", "09-30")}
    d = dt.date(2026, 1, 1)
    prev, mono, all_legal = "", True, True
    while d <= dt.date(2026, 11, 14):
        got = latest_report_period(d.isoformat())
        mono &= got >= prev
        all_legal &= got in legal
        prev, d = got, d + dt.timedelta(days=1)
    check("2026 全年逐日单调不降", mono)
    check("2026 全年只落在合法报告期", all_legal)


# ================================================================ 2. want

def test_want(cal: TradingCalendar) -> None:
    section("want 构造器：点数与区间分离")
    end = "2026-11-13"

    k = want.kline(cal, end, bars=300, min_bars=250)
    check("kline.points == 300", k.points == 300, str(k.points))
    check("kline.min_points == 250（不受 points 影响）", k.min_points == 250, str(k.min_points))
    check("kline 起点能从日历反算出 300 根", cal.count(k.start, k.end) == 300,
          f"{k.start}..{k.end} 共 {cal.count(k.start, k.end)} 根")
    check("kline.fp == fq=raw", k.fp == "fq=raw", k.fp)
    check("kline 起点早于终点", k.start < k.end)

    k2 = want.kline(cal, end, bars=300, min_bars=71)
    check("改门槛不改区间（同一 start）", k2.start == k.start)
    check("改门槛只改 min_points", k2.min_points == 71 and k2.points == 300)

    f = want.finance("2026-11-14")
    check("finance.end 取法定报告期", f.end == "2026-09-30", str(f.end))
    check("finance 无下界", f.start is None)
    check("finance.min_points == 40（四家工具的门槛由调用方给）", f.min_points == 40)
    check("finance.fp", f.fp == "type=all;fields=all", f.fp)
    check("finance 记录 periods 到 extra", f.extra.get("periods") == 43, str(f.extra))

    # 估值：门槛与起点在 2026-11-14 都改过，这两条断言是那次改动的看门人。
    v = want.valuation(cal, end, years=10)
    check("valuation.end == target", v.end == end)
    # 旧写法 `cal.on_or_after(day) or cal.days[0]` 会返回日历首日（二分插入点 0），
    # 把"日历只有 4 年"伪装成"数据只有 4 年"，于是 480 周门槛永远过不了。
    # 现在的语义分两种，两种都得成立（2026-11-17 起默认日历是 12 年那份）：
    #   日历覆盖十年 → 起点对齐到"10 年整那天或之后的第一个交易日"；
    #   日历更短     → 起点就用 10 年整那天，**原样保留**，不往首日上夹。
    raw_start = (dt.date.fromisoformat(end) - dt.timedelta(days=365 * 10 + 3)).isoformat()
    cal_first = cal.days[0]
    if cal_first <= raw_start:
        expect_start = cal.on_or_after(raw_start)
        check("valuation 起点对齐到交易日（日历覆盖十年）", v.start == expect_start,
              f"{v.start} vs {expect_start}（日历首日 {cal_first}）")
    else:
        expect_start = raw_start
        check("valuation 起点不被短日历夹取", v.start == raw_start,
              f"{v.start} vs {raw_start}（日历首日 {cal_first}）")
    # 无论哪条分支，起点都该落在"10 年整"附近；被夹到日历首日会差出整整几年。
    check("valuation 起点没被砍到日历首日",
          v.start <= (dt.date.fromisoformat(raw_start) + dt.timedelta(days=7)).isoformat(),
          f"{v.start} vs 日历首日 {cal_first}")
    check("valuation.points == 520", v.points == 520, str(v.points))
    check("valuation 默认门槛 430（上游最多 454 周，480 不可达）",
          v.min_points == 430, str(v.min_points))
    check("valuation.fp == freq=week", v.fp == "freq=week", v.fp)

    # 日历够长时才对齐到交易日：给一份"首日显著早于 10 年窗口"的日历（工作日即交易日），
    # 起点应当落在交易日上，且不早于 10 年整那天。
    # 2026-11-17 改：原来用 `[expect_start, "2016-11-16", end]` 三天构造，首日恰好等于
    # 对齐结果，`cal.days[0] <= day` 不成立——走的仍是"短日历"分支，断言反而在考错重点。
    long_days, _d = [], dt.date.fromisoformat(raw_start) - dt.timedelta(days=30)
    while _d <= dt.date.fromisoformat(end):
        if _d.weekday() < 5:
            long_days.append(_d.isoformat())
        _d += dt.timedelta(days=1)
    long_cal = TradingCalendar(long_days, long_days[0], end)
    v2 = want.valuation(long_cal, end, years=10)
    check("日历够长 → 起点对齐到交易日", long_cal.is_trading(v2.start), str(v2.start))
    check("日历够长 → 起点不早于 10 年整那天", v2.start >= raw_start,
          f"{v2.start} vs {raw_start}")
    check("日历够长 → 起点仍在日历内", v2.start >= long_cal.days[0], str(v2.start))

    # 反过来：日历首日比 10 年窗口晚（真实场景是只有 4 年那份日历），起点必须原样保留。
    short_cal = TradingCalendar(["2022-11-14", "2022-11-15", end], "2022-11-14", end)
    v3 = want.valuation(short_cal, end, years=10)
    check("日历比 10 年窗口短 → 起点保留 10 年整那天（不夹到首日）", v3.start == raw_start,
          f"{v3.start} vs {raw_start}（日历首日 {short_cal.days[0]}）")

    dv = want.dividend(k.start)
    check("dividend 无上界", dv.end is None)
    check("dividend 起点比 K 线再早 365 天",
          dv.start == (dt.date.fromisoformat(k.start) - dt.timedelta(days=365)).isoformat(),
          dv.start)
    check("dividend.fp 为空串", dv.fp == "")

    q = want.quote(end)
    check("quote 是单点（start == end）", q.start == q.end == end)
    check("quote.min_points == 1", q.min_points == 1)

    s = want.snapshot("mainop", "2026-09-30")
    check("snapshot.domain 仍是 snapshot", s.domain == "snapshot")
    check("snapshot.extra 记住子域", s.extra.get("snapshot_domain") == "mainop", str(s.extra))


# ================================================================ 3. hit

def test_hit(cal: TradingCalendar) -> None:
    section("hit：命中判据")
    end = "2026-11-13"
    need = want.kline(cal, end, bars=300, min_bars=250)
    good = C.Coverage("kline", "sh600519", "fq=raw", start=need.start, end=end, n=300)

    check("无记录 → 不命中", C.hit(None, need) is False)
    check("完好覆盖 → 命中", C.hit(good, need) is True)

    bad_fp = C.Coverage("kline", "sh600519", "fq=qfq", start=need.start, end=end, n=300)
    check("fp 不符 → 不命中", C.hit(bad_fp, need) is False)

    part = C.Coverage("kline", "sh600519", "fq=raw", start=need.start, end=end, n=300, partial=1)
    check("partial=1 → 不命中", C.hit(part, need) is False)

    short_end = C.Coverage("kline", "sh600519", "fq=raw", start=need.start, end="2026-11-12", n=300)
    check("末端差一天 → 不命中", C.hit(short_end, need) is False)

    hole = C.Coverage("kline", "sh600519", "fq=raw", start=need.start, end=end, n=299,
                      holes=1, detail=[C.Hole("2026-03-02", "2026-03-02", n=1)])
    check("有阻塞洞 → 不命中（哪怕 n 够）", C.hit(hole, need) is False)

    perm = C.Coverage("kline", "sh600519", "fq=raw", start=need.start, end=end, n=299,
                      holes=1, detail=[C.Hole("2026-03-02", "2026-03-02", n=1,
                                              retry=6, cls="permanent")])
    check("只剩 permanent 洞 → 命中（停牌股不能永远不命中）", C.hit(perm, need) is True)

    shallow = C.Coverage("kline", "sh600519", "fq=raw", start=need.start, end=end, n=200)
    check("点数不够（200 < 250）→ 不命中", C.hit(shallow, need) is False)

    # 次新股：起点到不了 300 根之前，但点数够 → 必须命中，否则每次运行都重取
    late = C.Coverage("kline", "sh600519", "fq=raw", start="2025-06-01", end=end, n=300)
    check("cov.start 晚于 need.start 但点数够 → 命中（不看 start）", C.hit(late, need) is True)

    # dividend 是唯一看 start 的域（这里的 need.start 直接给，不走 want.dividend 的 -365 天）
    dvn = Need("dividend", "", None, "2019-01-02", 0, 0)
    dv_ok = C.Coverage("dividend", "sh600519", "", start="2019-01-02", end="2024-06-01", n=3)
    dv_short = C.Coverage("dividend", "sh600519", "", start="2022-11-14", end="2024-06-01", n=3)
    dv_none = C.Coverage("dividend", "sh600519", "", start="2019-01-02", end=None, n=0)
    check("dividend 查过足够早 → 命中", C.hit(dv_ok, dvn) is True)
    check("dividend 起点不够早 → 不命中", C.hit(dv_short, dvn) is False)
    check("dividend 一条分红都没有也算命中（十年不分红合法）", C.hit(dv_none, dvn) is True)


def test_miss_reasons(cal: TradingCalendar) -> None:
    section("miss_reasons：别让人对着 False 猜")
    need = want.kline(cal, "2026-11-13", bars=300, min_bars=250)
    check("无记录有话说", C.miss_reasons(None, need) == ["无覆盖记录"])

    cov = C.Coverage("kline", "sh600519", "fq=qfq", start=need.start, end="2026-11-10",
                     n=100, partial=1, holes=1,
                     detail=[C.Hole("2026-03-02", "2026-03-03", n=2)])
    reasons = "；".join(C.miss_reasons(cov, need))
    for kw in ("口径不符", "限频截断", "末端不足", "待重试缺口", "点数不足"):
        check(f"原因含「{kw}」", kw in reasons, reasons)


# ================================================================ 4. 缺口重算

def test_recount(cal: TradingCalendar) -> None:
    section("recount_holes：按交易日历重算、连续段合并")
    days = cal.between("2026-11-02", "2026-11-13")
    check("样本区间有 10 个交易日", len(days) == 10, str(days))

    check("一天不缺 → 0 个洞", C.recount_holes(cal, set(days), days[0], days[-1]) == (0, []))

    have = set(days) - {days[3]}
    n, detail = C.recount_holes(cal, have, days[0], days[-1])
    check("缺 1 天 → 1 个洞，n=1", n == 1 and detail[0].n == 1, f"{n} {detail}")

    have = set(days) - {days[3], days[4]}
    n, detail = C.recount_holes(cal, have, days[0], days[-1])
    check("连缺 2 天 → 仍是 1 个洞、n=2", n == 1 and detail[0].n == 2, f"{n} {detail}")

    # 跨周末的缺口：周五 + 周一在交易日序号上连续，必须合成一个洞
    fri, mon = cal.on_or_before("2026-11-06"), cal.on_or_after("2026-11-09")
    have = set(days) - {fri, mon}
    n, detail = C.recount_holes(cal, have, days[0], days[-1])
    check("跨周末的缺口合为 1 个洞", n == 1 and (detail[0].start, detail[0].end) == (fri, mon),
          f"{n} {detail}")

    have = set(days) - {days[1], days[7]}
    n, detail = C.recount_holes(cal, have, days[0], days[-1])
    check("两处独立缺口 → 2 个洞", n == 2, f"{n} {detail}")

    old = [C.Hole(days[3], days[4], n=2, retry=3,
                  next_try_at="2026-12-01T00:00:00", reason="upstream_skip")]
    n, detail = C.recount_holes(cal, set(days) - {days[3], days[4]}, days[0], days[-1], old)
    check("同一段沿用旧退避状态（retry 不清零）", detail[0].retry == 3, str(detail[0]))
    check("同一段沿用旧 reason", detail[0].reason == "upstream_skip", str(detail[0]))

    check("start > end → 空", C.recount_holes(cal, set(), "2026-11-13", "2026-11-02") == (0, []))
    check("缺 start/end → 空", C.recount_holes(cal, set(), None, None) == (0, []))


# ================================================================ 5. 待补区间

def test_segments(cal: TradingCalendar) -> None:
    section("missing_segments：末端延伸 / 起点前补 / 到期缺口")
    now = dt.datetime(2026, 11, 14, 12, 0)

    need = want.kline(cal, "2026-11-13", bars=300, min_bars=250)
    check("无记录 → 整段", C.missing_segments(None, need, cal, now) == [(need.start, need.end)])

    cov = C.Coverage("kline", "sh600519", "fq=raw", start=need.start, end="2026-11-10", n=295)
    check("末端延伸从下一个交易日算起",
          C.missing_segments(cov, need, cal, now) == [("2026-11-11", "2026-11-13")],
          str(C.missing_segments(cov, need, cal, now)))

    cov = C.Coverage("kline", "sh600519", "fq=raw", start=need.start, end="2026-11-06", n=300)
    need2 = want.kline(cal, "2026-11-09", bars=300, min_bars=250)
    check("跨周末延伸只取交易日（周五 → 下周一）",
          C.missing_segments(cov, need2, cal, now) == [("2026-11-09", "2026-11-09")],
          str(C.missing_segments(cov, need2, cal, now)))

    dvn = Need("dividend", "", None, "2026-10-20", 0, 0)
    dvc = C.Coverage("dividend", "sh600519", "", start="2026-11-02", end="2024-06-01", n=1)
    check("dividend 起点前补（截止到上一交易日）",
          C.missing_segments(dvc, dvn, cal, now) == [("2026-10-20", "2026-10-30")],
          str(C.missing_segments(dvc, dvn, cal, now)))

    due = C.Hole("2026-11-05", "2026-11-06", n=2, retry=1,
                 next_try_at="2026-11-14T00:00:00")
    wait = C.Hole("2026-11-09", "2026-11-09", n=1, retry=1,
                  next_try_at="2026-11-20T00:00:00")
    perm = C.Hole("2026-11-02", "2026-11-02", n=1, retry=6, cls="permanent")
    cov = C.Coverage("kline", "sh600519", "fq=raw", start=need.start, end="2026-11-13", n=297,
                     holes=3, detail=[due, wait, perm])
    segs = C.missing_segments(cov, need, cal, now)
    check("到期缺口被安排重取", ("2026-11-05", "2026-11-06") in segs, str(segs))
    check("退避中的缺口不重取", ("2026-11-09", "2026-11-09") not in segs, str(segs))
    check("permanent 缺口不重取", ("2026-11-02", "2026-11-02") not in segs, str(segs))


# ================================================================ 6. 退避状态机

def test_backoff() -> None:
    section("Hole.bump：退避序列（CONTRACT §6.3）")
    base = dt.datetime(2026, 11, 14, 12, 0, 0)
    h = C.Hole("2026-03-02", "2026-03-05", n=4)
    seq = [(1, 1), (2, 6), (3, 24), (4, 72), (5, 168)]
    for want_retry, hours in seq:
        h = h.bump(base)
        expect_at = (base + dt.timedelta(hours=hours)).isoformat(timespec="seconds")
        check(f"第 {want_retry} 次重试 → +{hours}h",
              h.retry == want_retry and h.next_try_at == expect_at,
              f"retry={h.retry} at={h.next_try_at}")
        check(f"第 {want_retry} 次仍是 retry 类", not h.permanent)

    h6 = h.bump(base)
    check("第 6 次 → permanent", h6.permanent and h6.retry == 6, str(h6))
    check("permanent 没有下次重试时间", h6.next_try_at is None, str(h6.next_try_at))

    check("退避中 → waiting", C.hole_is_waiting(C.Hole("a", "b", retry=1,
          next_try_at="2026-11-14T13:00:00"), base) is True)
    check("已到点 → 不 waiting", C.hole_is_waiting(C.Hole("a", "b", retry=1,
          next_try_at="2026-11-14T11:00:00"), base) is False)
    check("permanent 不算 waiting", C.hole_is_waiting(C.Hole("a", "b", cls="permanent",
          next_try_at="2030-01-01T00:00:00"), base) is False)
    check("没记录 next_try_at → 立即可试", C.hole_is_waiting(C.Hole("a", "b"), base) is False)

    cov = C.Coverage("kline", "x", "fq=raw", end="2026-11-13", n=299, detail=[
        C.Hole("2026-11-05", "2026-11-05", next_try_at="2026-11-20T00:00:00"),
        C.Hole("2026-11-06", "2026-11-06", next_try_at="2026-11-01T00:00:00")])
    check("waiting / due 分得清", len(cov.waiting(base)) == 1 and len(cov.due(base)) == 1)
    check("waiting 也算阻塞（数据不完整，不是「已完整」）", cov.blocking == 2, str(cov.blocking))


# ================================================================ 7. 落库

def test_store(cal: TradingCalendar, tmp: Path) -> None:
    section("merge / write / read：覆盖落库")
    dbp = tmp / "cov.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)

    sym = "sh600519"
    end = "2026-11-13"
    bars = cal.days[-300:]
    with db.tx(conn):
        conn.executemany("INSERT INTO kline(symbol,date,open,high,low,close,volume,amount,src) "
                         "VALUES(?,?,1,1,1,1,100,100,'tdx')",
                         [(sym, d) for d in bars])
    need = want.kline(cal, end, bars=300, min_bars=250)

    with db.tx(conn):
        cov = C.merge(conn, cal=cal, need=need, symbol=sym, src="tdx",
                      now=dt.datetime(2026, 11, 14, 12, 0))
    check("merge 后 n=300", cov.n == 300, str(cov.n))
    check("merge 后无洞", cov.holes == 0, str(cov.detail))
    check("merge 后命中", C.hit(C.read(conn, "kline", sym, "fq=raw"), need) is True)
    check("start 取数据最早一天（不留旧边界）", cov.start == bars[0], f"{cov.start} vs {bars[0]}")

    # 用例 5：人为删掉中间一天
    mid = bars[150]
    with db.tx(conn):
        conn.execute("DELETE FROM kline WHERE symbol=? AND date=?", (sym, mid))
        cov2 = C.merge(conn, cal=cal, need=need, symbol=sym, src="tdx")
    check("删一天后 n=299", cov2.n == 299, str(cov2.n))
    check("删一天后 holes=1 且阻塞", cov2.holes == 1 and cov2.blocking == 1, str(cov2.detail))
    check("用例 5：删一天后不命中", C.hit(cov2, need) is False)
    expect_raise("用例 5：抛 InsufficientDepth 而不是返回 299 根",
                 InsufficientDepth, C.verdict, cov2, need)
    try:
        C.verdict(cov2, need)
    except InsufficientDepth as e:
        check("异常文案里点了缺口日期", mid in str(e), str(e))

    # hole_detail JSON 往返
    re_read = C.read(conn, "kline", sym, "fq=raw")
    check("hole_detail 往返保住起止", (re_read.detail[0].start, re_read.detail[0].end) == (mid, mid),
          str(re_read.detail))

    # partial：不推进 end_date
    end_before = re_read.end
    with db.tx(conn):
        C.mark_partial(conn, "kline", sym, "fq=raw", now=dt.datetime(2026, 11, 14, 13, 0))
    p = C.read(conn, "kline", sym, "fq=raw")
    check("mark_partial 置 partial=1", p.partial == 1)
    check("mark_partial 不推进 end_date", p.end == end_before, f"{p.end} vs {end_before}")
    expect_raise("partial 时抛 PartialData", PartialData, C.verdict, p, need)

    # 补回中间那天，再只留最后 200 根 → 无洞、纯点数不足
    with db.tx(conn):
        conn.execute("INSERT INTO kline(symbol,date,open,high,low,close,volume,amount,src) "
                     "VALUES(?,?,1,1,1,1,100,100,'tdx')", (sym, mid))
        conn.execute("DELETE FROM kline WHERE symbol=? AND date<?", (sym, bars[100]))
        cov3 = C.merge(conn, cal=cal, need=need, symbol=sym, src="tdx")
    check("只剩 200 根、无洞", cov3.n == 200 and cov3.holes == 0, f"n={cov3.n} {cov3.detail}")
    expect_raise("点数不足抛 InsufficientDepth", InsufficientDepth, C.verdict, cov3, need)

    # 只剩 permanent 洞 + 点数不足 → HoleUnreachable
    with db.tx(conn):
        C.write(conn, C.Coverage("kline", sym, "fq=raw", start=bars[0], end=end, n=200,
                                 holes=1, detail=[C.Hole(mid, mid, n=1, retry=6,
                                                         cls="permanent")]))
    perm = C.read(conn, "kline", sym, "fq=raw")
    expect_raise("只剩永久洞且点数不足 → HoleUnreachable",
                 HoleUnreachable, C.verdict, perm, need)

    # clear_holes
    with db.tx(conn):
        C.clear_holes(conn, "kline", sym, "fq=raw")
    check("clear_holes 清空洞", C.read(conn, "kline", sym, "fq=raw").detail == [])

    # 分红：查过但没有
    dvn = want.dividend(bars[0])
    with db.tx(conn):
        dvc = C.merge(conn, cal=cal, need=dvn, symbol="sz000651", src="westock")
    check("分红 0 条时 start 记成查过的起点", dvc.start == dvn.start, f"{dvc.start} vs {dvn.start}")
    check("分红 0 条时命中（不会每次重问上游）",
          C.hit(C.read(conn, "dividend", "sz000651", ""), dvn) is True)

    # 快照型
    qn = want.quote(end)
    with db.tx(conn):
        C.merge_point(conn, domain="quote", symbol=sym, as_of=end, src="westock",
                      now=dt.datetime(2026, 11, 14, 12, 0))
    q = C.read(conn, "quote", sym, "")
    check("merge_point 股一行", q.n == 1 and q.start == q.end == end)
    check("quote 命中", C.hit(q, qn) is True)

    # 脏 JSON 不该让整次运行崩
    conn.execute("UPDATE coverage SET hole_detail='{not json' "
                 "WHERE domain='kline' AND symbol=? AND fp='fq=raw'", (sym,))
    check("hole_detail 脏 JSON → 当没有洞", C.read(conn, "kline", sym, "fq=raw").detail == [])

    # touch_access
    conn.execute("INSERT OR REPLACE INTO stock(symbol,name,exch,setcode,updated_at) "
                 "VALUES(?,?,?,?,?)", (sym, "贵州茅台", "SH", 1, "2026-11-01T00:00:00"))
    with db.tx(conn):
        C.touch_access(conn, sym, now=dt.datetime(2026, 11, 14, 14, 0))
    got = conn.execute("SELECT last_access_at FROM stock WHERE symbol=?", (sym,)).fetchone()[0]
    check("touch_access 写了 last_access_at（只留痕，股票级 GC 未实现）", got == "2026-11-14T14:00:00", str(got))

    # 不存在的域必须报错，而不是静默写空覆盖
    expect_raise("merge 对 quote 报错（要用 merge_point）", ValueError, C.merge,
                 conn, cal=cal, need=want.quote(end), symbol=sym, src="westock")

    conn.close()


# ================================================================ main

def main() -> int:
    tmp = mkdtemp("stockhub-cov-")
    print(f"临时目录：{tmp}")
    cal = TradingCalendar.load(DEFAULT_CALENDAR)

    test_report_period()
    test_want(cal)
    test_hit(cal)
    test_miss_reasons(cal)
    test_recount(cal)
    test_segments(cal)
    test_backoff()
    test_store(cal, tmp)

    print(f"\n{'=' * 50}")
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  失败：{f}")
    print(f"临时目录（可删）：{tmp}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
