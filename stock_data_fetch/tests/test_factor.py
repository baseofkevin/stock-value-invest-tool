"""`factor.py` 测试（**全离线**，不打上游）。

跑法：

    cd /home/kevin/MyData/stock-value-invest-tool/stock_data_fetch && \\
    /home/kevin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \\
        -m stock_data_fetch.tests.test_factor

覆盖 `CONTRACT` §16 第 4 步的验收：用例 3（除权日前后 qfq 连续）、用例 4（送转，除数 3.0）、
补充用例 i（窗口滚动后重叠区间幂等），以及 §8.2 四条守卫与 §8.3 缓存失效。

**两组真实价格做算术自洽验证**（2026-11-14 记录，见纪要 §16）：

    格力   2026-08-26  raw 41.58 → qfq 39.58   差 2.00 = 每股派息
    比亚迪 2025-07-28  raw 337.00 → 2025-07-29 qfq 111.42   除数≈3.0 + 派息

比亚迪的"派息 2.75 / 送 2 股"是按这两个价格**反推**的，不是上游分红原文——
所以这条断言证明的是"公式与记录里的价格对得上"，不是"上游数据长这样"。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent        # stock-value-invest-tool/（包的父目录）
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import db, factor as F                        # noqa: E402
from stock_data_fetch.calendar import TradingCalendar               # noqa: E402
from stock_data_fetch.config import DEFAULT_CALENDAR, DEFAULT_SCHEMA  # noqa: E402
from stock_data_fetch.errors import FactorError, InsufficientDepth  # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL.append(name)
        print(f"  \033[31m✗\033[0m {name}" + (f"  ← {detail}" if detail else ""))


def close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def mkdtemp(prefix: str) -> Path:
    """临时目录。**不能用 `/tmp`**：本机 `/tmp` 是 10MB tmpfs，建几个 WAL 库就 SIGKILL。"""
    root = Path(os.environ.get("STOCKHUB_TMP")
                or HERE.parent.parent.parent / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


# ---------------------------------------------------------------- 夹具

def days_between(cal: TradingCalendar, start: str, end: str) -> list[str]:
    return cal.between(start, end)


def put_kline(conn, symbol: str, pairs: list[tuple[str, float]]) -> None:
    """按键写入一串 (日期, 收盘)。开高低就用收盘，够用——因子只看 close。"""
    F.clear_cache(symbol)
    conn.executemany(
        "INSERT INTO kline(symbol,date,open,high,low,close,volume,amount,src) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,date) DO UPDATE SET close=excluded.close",
        [(symbol, d, c, c, c, c, 1000.0, c * 1000.0, "test") for d, c in pairs])


def put_dividend(conn, symbol: str, ex_date: str, **kw) -> None:
    F.clear_cache(symbol)
    cols = {"cash_per_share": 0.0, "bonus_per_share": 0.0, "transfer_per_share": 0.0,
            "rights_per_share": 0.0, "rights_price": None, "plan_text": "", "src": "test"}
    cols.update(kw)
    conn.execute(
        "INSERT INTO dividend(symbol,ex_date,cash_per_share,bonus_per_share,"
        "transfer_per_share,rights_per_share,rights_price,plan_text,src,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,ex_date) DO UPDATE SET "
        "cash_per_share=excluded.cash_per_share, bonus_per_share=excluded.bonus_per_share, "
        "transfer_per_share=excluded.transfer_per_share, "
        "rights_per_share=excluded.rights_per_share, rights_price=excluded.rights_price",
        (symbol, ex_date, cols["cash_per_share"], cols["bonus_per_share"],
         cols["transfer_per_share"], cols["rights_per_share"], cols["rights_price"],
         cols["plan_text"], cols["src"], "2026-11-14T15:00:00"))


# ================================================================ 1. 基本功

def test_basics(cal: TradingCalendar, tmp: Path) -> None:
    section("1. 无分红 / 纯派息 / 派息+送转")
    dbp = tmp / "f1.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)

    # ---- 没分过红：factor ≡ 1 ----
    sym = "sh600001"
    put_kline(conn, sym, [("2026-08-26", 10.0), ("2026-08-27", 10.5)])
    f = F.factor(conn, cal, sym, ["2026-08-26", "2026-08-27"])
    check("没分红 → 因子全 1.0", f == [1.0, 1.0], str(f))
    check("没分红 → adjusted 就是 raw",
          close(F.adjusted(conn, cal, sym, [{"date": "2026-08-27", "close": 10.5}])[0]["close"],
                10.5))

    # ---- 纯派息：格力那组真实价格 ----
    sym = "sz000651"
    put_kline(conn, sym, [("2026-08-28", 41.58), ("2026-08-31", 39.58)])
    put_dividend(conn, sym, "2026-08-31", cash_per_share=2.00,
                 plan_text="10派20.000元")
    ev = F.events(conn, cal, sym)
    check("事件表 1 条", len(ev) == 1, str(ev))
    check("r = P_prev / (P_prev − cash)", close(ev[0].r, 41.58 / 39.58), str(ev[0].r))
    check("生效日 = 除权日（8-31 是交易日）", ev[0].day == "2026-08-31", ev[0].day)

    rows = [{"date": "2026-08-28", "close": 41.58}, {"date": "2026-08-31", "close": 39.58}]
    got = F.adjusted(conn, cal, sym, rows)
    check("复权后两根都等于 39.58（派息填平了跳空）",
          close(got[0]["close"], 39.58, 1e-9) and close(got[1]["close"], 39.58, 1e-9),
          f"{got[0]['close']:.4f} / {got[1]['close']:.4f}")
    check("用例 3：|qfq[除权前] − qfq[除权日]| < 0.01",
          abs(got[0]["close"] - got[1]["close"]) < 0.01,
          f"{abs(got[0]['close'] - got[1]['close']):.6f}")

    # ---- 派息 + 送转：比亚迪那组（除数 ≈ 3.0）----
    sym = "sz002594"
    put_kline(conn, sym, [("2025-07-28", 337.00), ("2025-07-29", 111.42)])
    put_dividend(conn, sym, "2025-07-29", cash_per_share=2.75, bonus_per_share=2.0,
                 plan_text="10派27.500元送20.000股")
    rows = [{"date": "2025-07-28", "close": 337.00}, {"date": "2025-07-29", "close": 111.42}]
    got = F.adjusted(conn, cal, sym, rows)
    check("用例 4：送转后复权差 < 0.01（记录里 tdx 前复权 111.42）",
          abs(got[0]["close"] - 111.42) < 0.01 and abs(got[1]["close"] - 111.42) < 0.01,
          f"{got[0]['close']:.4f} / {got[1]['close']:.4f}")
    r = F.events(conn, cal, sym)[0].r
    check("送转的除数确实是 1+bonus+transfer = 3",
          close(r, 337.00 / ((337.00 - 2.75) / 3.0)), f"r={r:.6f}")
    conn.close()


# ================================================================ 2. 守卫

def test_guards(cal: TradingCalendar, tmp: Path) -> None:
    section("2. 四条守卫")
    dbp = tmp / "f2.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)

    # ---- 守卫 3：四项全 0 → 不产生事件 ----
    sym = "sh600002"
    put_kline(conn, sym, [("2026-08-27", 20.0), ("2026-08-28", 20.0)])
    put_dividend(conn, sym, "2026-08-28", plan_text="10派0元（占位）")
    check("全 0 → 事件表为空（r=1 不落成事件）", F.events(conn, cal, sym) == ())
    check("全 0 → 因子全 1.0",
          F.factor(conn, cal, sym, ["2026-08-27"]) == [1.0])

    # ---- 守卫 2：除权日非交易日 → 回退 ----
    sym = "sh600003"
    put_kline(conn, sym, [("2026-08-27", 30.0), ("2026-08-28", 28.0), ("2026-08-31", 28.5)])
    put_dividend(conn, sym, "2026-08-29", cash_per_share=2.0)      # 周六
    ev = F.events(conn, cal, sym)
    check("除权日撞周六 → 生效日回退到 8-28",
          len(ev) == 1 and ev[0].day == "2026-08-28", str([e.day for e in ev]))
    check("回退后 P_prev 取 8-27 的 30.00（不是 8-28 的 28.00）",
          bool(ev) and close(ev[0].prev_close, 30.0), str(ev[0].prev_close if ev else None))
    check("r = 30 / (30 − 2) = 1.0714…",
          bool(ev) and close(ev[0].r, 30.0 / 28.0), str(ev[0].r if ev else None))

    # ---- 守卫 4a：cash >= P_prev ----
    sym = "sh600004"
    put_kline(conn, sym, [("2026-08-27", 1.50), ("2026-08-28", 1.45)])
    put_dividend(conn, sym, "2026-08-28", cash_per_share=2.00)
    try:
        F.events(conn, cal, sym)
        check("cash >= P_prev → 抛 FactorError", False, "没有抛")
    except FactorError as e:
        check("cash >= P_prev → 抛 FactorError", True, str(e)[:40])

    # ---- 守卫 4b：P_ref <= 0 ----
    sym = "sh600005"
    put_kline(conn, sym, [("2026-08-27", 10.0), ("2026-08-28", 8.0)])
    put_dividend(conn, sym, "2026-08-28", bonus_per_share=-1.5)    # 上游脏数据
    try:
        F.events(conn, cal, sym)
        check("P_ref <= 0 → 抛 FactorError", False, "没有抛")
    except FactorError as e:
        check("P_ref <= 0 → 抛 FactorError", True, str(e)[:40])

    # ---- 缺口：窗口内前一日没 bar ----
    sym = "sh600006"
    put_kline(conn, sym, [("2026-08-24", 50.0), ("2026-08-28", 48.0)])   # 8-27 缺
    put_dividend(conn, sym, "2026-08-28", cash_per_share=2.0)
    try:
        F.events(conn, cal, sym)
        check("窗口内缺前一日 → 抛 InsufficientDepth", False, "没有抛")
    except InsufficientDepth as e:
        check("窗口内缺前一日 → 抛 InsufficientDepth", True, str(e)[:40])

    # ---- 有分红但一根 K 线都没有 ----
    sym = "sh600007"
    put_dividend(conn, sym, "2026-08-28", cash_per_share=1.0)
    try:
        F.events(conn, cal, sym)
        check("有分红无 K 线 → 抛 InsufficientDepth", False, "没有抛")
    except InsufficientDepth:
        check("有分红无 K 线 → 抛 InsufficientDepth", True)

    # ---- qfq 的锚点守卫 ----
    try:
        F.qfq([{"date": "2026-08-28", "close": 10.0}], [1.0], 0.0)
        check("anchor_factor <= 0 → 抛 FactorError", False, "没有抛")
    except FactorError:
        check("anchor_factor <= 0 → 抛 FactorError", True)

    conn.close()


# ================================================================ 3. 窗口外除权

def test_outside_window(cal: TradingCalendar, tmp: Path) -> None:
    section("3. 窗口外的除权日应跳过（而不是报错/算错）")
    dbp = tmp / "f3.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)

    sym = "sh600010"
    # 库里只有 8 月这段；除权日 3 月（早于最早 bar）
    put_kline(conn, sym, [("2026-08-27", 20.0), ("2026-08-28", 20.0)])
    put_dividend(conn, sym, "2026-03-10", cash_per_share=1.0)
    ev = F.events(conn, cal, sym)
    check("窗口外除权 → 事件表为空", ev == (), str(ev))
    check("窗口外除权 → 因子 1.0（比值里抵消）",
          F.factor(conn, cal, sym, ["2026-08-27"]) == [1.0])

    # 窗口内一个、窗口外一个：只剩窗口内那个
    put_dividend(conn, sym, "2026-08-28", cash_per_share=1.0)
    ev = F.events(conn, cal, sym)
    check("窗口内+窗口外 → 只留窗口内 1 条",
          len(ev) == 1 and ev[0].day == "2026-08-28", str([e.day for e in ev]))
    conn.close()


# ================================================================ 4. 幂等（用例 i）

def test_rolling_window(cal: TradingCalendar, tmp: Path) -> None:
    section("4. 用例 i：K 线窗口滚动后，重叠区间 qfq 逐点不变")
    dbp = tmp / "f4.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)

    sym = "sh600519"
    # 60 个交易日，中间（第 30 天）除权一次
    days = cal.between("2026-05-06", "2026-08-28")
    check("夹具够长（>=40 天）", len(days) >= 40, str(len(days)))
    ex = days[30]
    prev_ex = days[29]

    # 除权前价格 41.58，除权日起 39.58（跳空 2.00），之后缓慢上行
    pairs = []
    for i, d in enumerate(days):
        pairs.append((d, 41.58 if i < 30 else round(39.58 + 0.05 * (i - 30), 4)))
    put_kline(conn, sym, pairs)
    put_dividend(conn, sym, ex, cash_per_share=2.00)

    old = [{"date": d, "close": c} for d, c in pairs[:40]]
    new = [{"date": d, "close": c} for d, c in pairs[1:41]]      # 丢最早一根、加最新一根

    a = F.adjusted(conn, cal, sym, old)
    b = F.adjusted(conn, cal, sym, new)

    by_date_a = {r["date"]: r["close"] for r in a}
    overlap = [r["date"] for r in b if r["date"] in by_date_a]
    check("重叠区间有 39 天", len(overlap) == 39, str(len(overlap)))
    worst = max(abs(by_date_a[d] - next(r["close"] for r in b if r["date"] == d))
                for d in overlap)
    check("重叠区间 qfq 逐点完全相同（误差 0）", worst == 0.0, f"{worst:.3e}")

    # 反向确认：如果按"每根 bar 独立把除权折算回去"的错误做法，会漂
    check("最早那根 bar 在两次计算里都等于 39.58",
          close(by_date_a[days[0]], 39.58, 1e-6),
          f"{by_date_a[days[0]]:.6f}")

    # 锚在"今天" vs 锚在最后一根 bar：这里之后没分红，应一致
    c = F.adjusted(conn, cal, sym, new, anchor="2026-11-13")
    worst2 = max(abs(x["close"] - y["close"]) for x, y in zip(b, c))
    check("之后没分红 → 换锚点结果不变", worst2 < 1e-12, f"{worst2:.3e}")
    conn.close()


# ================================================================ 5. 缓存

def test_cache(cal: TradingCalendar, tmp: Path) -> None:
    section("5. 缓存：签名变化必须失效")
    dbp = tmp / "f5.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)

    sym = "sh600030"
    put_kline(conn, sym, [("2026-08-27", 20.0), ("2026-08-28", 19.0)])
    put_dividend(conn, sym, "2026-08-28", cash_per_share=1.0)

    e1 = F.events(conn, cal, sym)
    e2 = F.events(conn, cal, sym)
    check("相同输入命中缓存（返回同一个对象）", e1 is e2)

    # 新分红入库 → 签名变 → 必须重算
    put_dividend(conn, sym, "2026-07-10", cash_per_share=0.5)   # 窗口外，会被跳过
    e3 = F.events(conn, cal, sym)
    check("dividend 变更后重新计算（不再是旧对象）", e3 is not e1)
    check("窗口外的新增仍不影响因子", F.factor(conn, cal, sym, ["2026-08-27"]) == [1.0])

    # 补了一根更早的 K 线 → 窗口外的那次除权变成窗口内 → 必须重算
    put_kline(conn, sym, [("2026-07-09", 20.5), ("2026-07-10", 20.0)])
    e4 = F.events(conn, cal, sym)
    days = [e.day for e in e4]
    check("kline 边界变化 → 缓存失效并纳入新事件",
          "2026-07-10" in days and len(e4) == 2, str(days))

    check("toggle use_cache=False 也拿到同样结果",
          F.events(conn, cal, sym, use_cache=False) == e4)
    conn.close()


# ================================================================ main

def main() -> int:
    tmp = mkdtemp("stockhub-factor-")
    print(f"临时目录：{tmp}")
    # 日历走 `config.DEFAULT_CALENDAR`（项目内 state/ 那份）：
    # 原来这里硬编码 `~/MyData/stock-kline-screen-tool/...`，那个仓库 2026-11-15
    # 改名后就不存在了，`STOCKHUB_CALENDAR` 没设时整组测试直接抛 HubNotReady。
    cal = TradingCalendar.load(Path(os.environ.get("STOCKHUB_CALENDAR") or DEFAULT_CALENDAR))

    test_basics(cal, tmp)
    test_guards(cal, tmp)
    test_outside_window(cal, tmp)
    test_rolling_window(cal, tmp)
    test_cache(cal, tmp)

    print("\n" + "=" * 50)
    if FAIL:
        print(f"\033[31m通过 {PASS} 项，失败 {len(FAIL)} 项\033[0m")
        for n in FAIL:
            print("  ✗", n)
        return 1
    print(f"\033[32m通过 {PASS} 项，失败 0 项\033[0m")
    print(f"临时目录（可删）：{tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
