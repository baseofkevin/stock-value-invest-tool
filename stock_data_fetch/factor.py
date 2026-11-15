"""`factor()` —— 复权因子。**只算，不取数、不写库。**

    r_d       = P_prev / P_ref(d)
    P_ref(d)  = (P_prev − cash + rights × rights_price) / (1 + bonus + transfer)
    factor(t) = Π_{d ≤ t} r_d
    qfq(t)    = raw(t) × factor(t) / factor(anchor)

为什么算"因子"而不是直接算前复权价
--------------------------------
因子只跟除权事实有关，与"锚在哪一天"无关。`raw → qfq` 是一个比值，锚一变整列都变，
锚不变则整列都不变。把它拆开的好处是：

1. **幂等**（`CONTRACT` 用例 i）：K 线窗口向前滚动、旧除权日掉出窗口时，
   `factor(t)/factor(anchor)` 的分子分母同时少一个 `r`，比值不变——同一根 bar
   今天和三个月后的前复权价一致。直接算价格则很难保证这一点。
2. **可缓存**：缓存的是 `(d, r)` 列表，与调用方传什么 `dates` 无关。

窗口外的除权日为什么可以跳过
--------------------------
`factor(t)/factor(anchor)` 里，起点之前的所有 `r` 在分子分母里同时出现，**严格抵消**
（`CONTRACT` §8.1）。所以只需要窗口内的除权日——这也顺带解决了"算最早那根 bar 时
`P_prev` 落在窗口外"的问题：那根 bar 之前的除权日本来就不需要。

**但"跳过"必须区分两种情况**，混起来就会静默算错：

| 情况 | 处理 |
|---|---|
| 除权日早于最早一根 bar | 合法跳过（抵消），`log.debug` 记一笔 |
| 除权日在窗口内，但前一日没有 bar | **抛 `InsufficientDepth`**——这是数据缺口，跳过会让 qfq 整段偏高 |

四条守卫（`CONTRACT` §8.2）
--------------------------
1. `dividend.*_per_share` 已经是**每股**（入库时 /10），这里只校验量级；
2. 除权日非交易日 → 取 `ex_date` 及之前最后一个交易日；
3. 四项全 0 → `r = 1`，跳过（不产生因子，也不产生误差）；
4. `P_ref <= 0` 或 `cash >= P_prev` → 抛 `FactorError`，**不允许跳过**。
   跳过会让 qfq 静默偏高且看不出来；报错至少能被人发现。
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass

from .calendar import TradingCalendar
from .errors import FactorError, InsufficientDepth

log = logging.getLogger("stock_data_fetch.factor")

PRICE_COLS = ("open", "high", "low", "close")
"""参与复权的列。`volume` / `amount` 不复权——量在除权日不会因为送股而"变小"，
两者本来就要分开看（换手率用未复权量配未复权价才对得上）。"""


@dataclass(frozen=True)
class Event:
    """一次除权。`day` 是**实际生效的交易日**（`ex_date` 非交易日时向前回退）。"""

    ex_date: str
    day: str
    r: float
    cash: float = 0.0
    bonus: float = 0.0
    transfer: float = 0.0
    rights: float = 0.0
    rights_price: float | None = None
    prev_close: float = 0.0


# ---------------------------------------------------------------- 缓存

_CACHE: dict[str, tuple[tuple, tuple[Event, ...]]] = {}
"""`symbol → (签名, 事件表)`。签名由 dividend 与 kline 的"行数 + 边界"组成。

**不做跨进程缓存**：同一个库被四个工具读，跨进程失效的成本远大于重算（毫秒级）。
签名里必须带 kline 的边界——`P_prev` 是从 kline 查出来的，只盯 dividend 会在
"补了一批老 K 线"之后继续用旧因子。
"""


def _sig(conn, symbol: str) -> tuple:
    d = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM dividend WHERE symbol=?",
        (symbol,)).fetchone()
    k = conn.execute(
        "SELECT COUNT(*), COALESCE(MIN(date), ''), COALESCE(MAX(date), '') "
        "FROM kline WHERE symbol=?", (symbol,)).fetchone()
    return (tuple(d or ()), tuple(k or ()))


def clear_cache(symbol: str | None = None) -> None:
    """给测试和"手工改了库"的场景用。"""
    if symbol is None:
        _CACHE.clear()
    else:
        _CACHE.pop(symbol, None)


# ---------------------------------------------------------------- 事件表

def events(conn, cal: TradingCalendar, symbol: str, *, use_cache: bool = True) -> tuple[Event, ...]:
    """这只股票的全部除权事件，按生效日升序。空 = 从未分红（合法状态）。"""
    sig = _sig(conn, symbol)
    if use_cache:
        hit = _CACHE.get(symbol)
        if hit and hit[0] == sig:
            return hit[1]

    ev = _build(conn, cal, symbol)
    _CACHE[symbol] = (sig, ev)
    return ev


def _build(conn, cal: TradingCalendar, symbol: str) -> tuple[Event, ...]:
    divs = [dict(r) for r in conn.execute(
        "SELECT * FROM dividend WHERE symbol=? ORDER BY ex_date", (symbol,)).fetchall()]
    if not divs:
        return ()                      # 没分过红：factor ≡ 1.0，不必碰 kline

    closes = {r[0]: float(r[1]) for r in conn.execute(
        "SELECT date, close FROM kline WHERE symbol=? AND close IS NOT NULL", (symbol,))}
    if not closes:
        raise InsufficientDepth(
            f"{symbol} 有分红记录但没有 K 线，无法确定除权前收盘价。"
            "请先 ensure('kline')", symbol=symbol, dividends=len(divs))
    first_bar = min(closes)

    out: list[Event] = []
    for d in divs:
        cash = float(d.get("cash_per_share") or 0.0)
        bonus = float(d.get("bonus_per_share") or 0.0)
        transfer = float(d.get("transfer_per_share") or 0.0)
        rights = float(d.get("rights_per_share") or 0.0)
        rprice = d.get("rights_price")

        if cash == 0.0 and bonus == 0.0 and transfer == 0.0 and rights == 0.0:
            continue                   # 守卫 3：四项全 0 → r = 1，跳过

        ex_date = str(d["ex_date"])
        day = cal.on_or_before(ex_date) or ex_date      # 守卫 2：非交易日回退
        if day < first_bar:
            log.debug("%s 的除权日 %s 早于最早 bar %s，因子在比值里抵消，跳过",
                      symbol, ex_date, first_bar)
            continue

        prev = cal.prev(day)
        p_prev = closes.get(prev) if prev else None
        if p_prev is None:
            if prev and prev < first_bar:
                log.debug("%s 除权日 %s 的前一日 %s 在窗口外，因子抵消，跳过",
                          symbol, ex_date, prev)
                continue
            raise InsufficientDepth(
                f"{symbol} 的除权日 {ex_date}（生效 {day}）缺少前一日 {prev} 的收盘价，"
                "因子算不出来。这一段 K 线有洞，先补齐再算复权",
                symbol=symbol, ex_date=ex_date, prev=prev)

        if cash >= p_prev:
            raise FactorError(
                f"{symbol} 的除权日 {ex_date}：每股派现 {cash} >= 前收盘 {p_prev}，"
                "上游分红数据自相矛盾（多半是「每 10 股」没除）。不允许跳过",
                symbol=symbol, ex_date=ex_date)     # 守卫 4a

        p_ref = (p_prev - cash + rights * float(rprice or 0.0)) / (1.0 + bonus + transfer)
        if p_ref <= 0:
            raise FactorError(
                f"{symbol} 的除权日 {ex_date}：除权参考价 {p_ref:.4f} <= 0，"
                "送转/派现比例不可信。不允许跳过",
                symbol=symbol, ex_date=ex_date)     # 守卫 4b

        out.append(Event(ex_date=ex_date, day=day, r=p_prev / p_ref,
                         cash=cash, bonus=bonus, transfer=transfer,
                         rights=rights, rights_price=rprice, prev_close=p_prev))

    out.sort(key=lambda e: e.day)
    return tuple(out)


# ---------------------------------------------------------------- 对外

def factor(conn, cal: TradingCalendar, symbol: str, dates, *,
           use_cache: bool = True) -> list[float]:
    """与 `dates` 一一对应的复权因子。没分过红 → 全 1.0。

    `dates` 不必有序、不必是交易日；不在任何除权日之后的日期得到 1.0。
    """
    dates = list(dates)
    ev = events(conn, cal, symbol, use_cache=use_cache)
    if not ev:
        return [1.0] * len(dates)
    days = [e.day for e in ev]
    pre = [1.0]
    for e in ev:
        pre.append(pre[-1] * e.r)
    return [pre[bisect_right(days, d)] for d in dates]


def qfq(rows, factors, anchor_factor: float, *,
        cols: tuple[str, ...] = PRICE_COLS) -> list[dict]:
    """把 raw 行按 `factor(t) / factor(anchor)` 缩放。**返回新行，不改入参。**

    `anchor_factor <= 0` 直接抛 `FactorError`——那意味着锚点当天的因子被算成了 0 或负数，
    继续算下去整列都是 0，比报错危险得多。
    """
    if anchor_factor <= 0:
        raise FactorError(f"锚点因子为 {anchor_factor}，不能做除权缩放")
    out: list[dict] = []
    for row, f in zip(rows, factors):
        k = f / anchor_factor
        new = dict(row)
        for c in cols:
            v = row.get(c)
            new[c] = None if v is None else float(v) * k
        out.append(new)
    return out


def adjusted(conn, cal: TradingCalendar, symbol: str, rows, *,
             anchor: str | None = None, use_cache: bool = True,
             cols: tuple[str, ...] = PRICE_COLS) -> list[dict]:
    """`rows` 的**前复权**版本（`cols` 被缩放，其余列原样带过来）。

    `anchor` 默认 = 最后一根 bar 的日期。这保证**同样的输入得到同样的输出**，
    测试可离线断言。想要"以今天为锚的真实前复权价"，显式传 `anchor=hub.target`——
    两者只在"最后一根 bar 之后又发生过分红"时才有差别，而那正是最容易被忘掉的情形。
    """
    rows = list(rows)
    if not rows:
        return []
    dates = [str(r["date"]) for r in rows]
    anchor = anchor or dates[-1]
    f = factor(conn, cal, symbol, dates + [anchor], use_cache=use_cache)
    return qfq(rows, f[:-1], f[-1], cols=cols)
