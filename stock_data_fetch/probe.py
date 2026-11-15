"""`probe` —— 跨源探针。**会联网，只在手动排查时跑，不进启动路径。**

和 `doctor` 的分工：

| | `doctor` | `probe` |
|---|---|---|
| 网络 | 一次都不打 | 必须打 |
| 目的 | "库自己有没有坏" | "库和上游/另一家源**对不对得上**" |
| 跑法 | 四个工具启动前随手跑 | 换源、换口径、怀疑数据时手动跑 |

为什么不能塞进 `preflight()`：启动路径一旦依赖网络，上游抖一下四个工具就都起不来
（同 `hub` 里闸 5 的理由）。跨源对账是"维护动作"，不是"启动动作"。

五个探针
--------

1. `calendar`  本地日历 vs `data_trade_calendar`（**唯一权威**：它直接说哪天开市）
2. `qfq`       本地 `factor` 算的前复权 vs 上游前复权，逐点比对（见下面的口径说明）
3. `dividend`  本地分红 vs 上游**按除权日聚合后**的值（同日多方案的回归）
4. `valuation` 本地估值周频 vs 东财 `RPT_VALUEANALYSIS_DET` 原始行
5. `suspension` 本地 K 线的洞：是停牌（上游也没有）还是我们缺数据（上游有）

每条结论三档：`OK` / `MISMATCH`（要处理）/ `SKIP`（前置条件不满足，不算失败）。
退出码只反映 `MISMATCH`。

## `qfq` 这条为什么要把两种口径都算一遍

前复权有两套算法，**算出来的数不一样**，而两边都自称"前复权"：

| 口径 | 除权日之前的价怎么算 | 用它的 |
|---|---|---|
| 等比（乘因子） | `P × (p_ref/p_prev)` | **本库 `factor`** |
| 等差（减派现再除送转） | `(P − 派现 + 配股价×比例) / (1 + 送转)` | 东财 `fqt=1`、westock `fq=qfq` |

两种在除权日当天的锚点相同（都等于除权参考价 `p_ref`），差异只出现在**更早的日子**，
幅度约 `派现 × (1 − 当日价/前收盘)`。平安一笔 0.98 元派现落在 56 元股价上，
2026-07-02 那天两家差 0.13%——**这不是错误，是口径**。

判据不能靠猜"某家源用哪种"，因为猜错的方向两边都致命：把口径差当数据错，探针天天报假警；
把数据错当口径差，漏记的分红永远查不出来。所以**本地把两种口径都算出来**，
上游能对上任意一种就通过（并写明对上了哪种），两种都对不上才是数据问题。

我们自己的库采用等比，理由见 `README` §7 第 6 条：等比下"收益率可乘"，十年分位才可比。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from . import factor as F
from .calendar import TradingCalendar
from .config import Config
from .need import Need
from .sources import eastmoney as em
from .sources import westock as W

QFQ_TOL = 1e-4
"""前复权逐点相对容差。够严：漏一条 0.2 元/股的分红在 50 元的票上是 0.4%，超它 40 倍。"""

_CENT = 0.0051
"""上游前复权价只给到"分"，我们给到 4 位小数。分位取整本身就有 1e-4 的相对误差，
所以绝对差在半分钱以内的一律不算差异——否则测的是两家的四舍五入。"""


# ---------------------------------------------------------------- 结果模型

@dataclass
class Result:
    probe: str
    status: str                    # OK / MISMATCH / SKIP
    title: str
    detail: str = ""
    rows: list[str] = field(default_factory=list)


def ok(probe: str, title: str, detail: str = "", rows: list[str] | None = None) -> Result:
    return Result(probe, "OK", title, detail, rows or [])


def bad(probe: str, title: str, detail: str = "", rows: list[str] | None = None) -> Result:
    return Result(probe, "MISMATCH", title, detail, rows or [])


def skip(probe: str, title: str, detail: str = "") -> Result:
    return Result(probe, "SKIP", title, detail)


# ---------------------------------------------------------------- 1. 日历

def _upstream_days(start: str, end: str) -> set[str]:
    """按年问 `data_trade_calendar`。**它的参数是 `year`，不是 `start`/`end`。**

    与 `build_calendar._from_westock` 同一口径：先 `trading_only=True`，再按
    `isTrading` 筛一遍——上游把"标志位"和"筛选参数"混用的情况不是没遇到过。
    传 `start`/`end` 不报错、只是被忽略，然后拿回整年——这种"看着成功"的错要躲。
    """
    out: set[str] = set()
    for year in range(int(start[:4]), int(end[:4]) + 1):
        d = W.call("data_trade_calendar",
                   {"year": year, "trading_only": True, "limit": 400})
        for x in ((W._unwrap(d) or {}).get("days") or []):
            day = W._date(x.get("date"))
            if day and x.get("isTrading") and start <= day <= end:
                out.add(day)
    return out


def probe_calendar(cal: TradingCalendar, *, days: int = 250) -> list[Result]:
    """本地日历 vs 上游交易日历。**这一条最该常跑**：日历算错的连锁反应最远——
    盘中闸、目标日、K 线洞全都建立在它上面，而且错了不报错，只是悄悄多算或少算一天。
    """
    if not cal.days:
        return [skip("calendar", "本地日历是空的", "先跑 build_calendar")]
    end = cal.days[-1]
    start = cal.days[-min(days, len(cal.days))]
    upstream = _upstream_days(start, end)
    if not upstream:
        return [skip("calendar", "上游没返回交易日", f"{start}..{end}")]

    local = {x for x in cal.days if start <= x <= end}
    extra = sorted(local - upstream)          # 本地有、上游说休市 → 我们会凭空要求一天数据
    missing = sorted(upstream - local)        # 上游有、本地没有 → 那天永远补不上

    out: list[Result] = []
    if extra:
        out.append(bad("calendar", f"本地多出 {len(extra)} 个交易日",
                       "本地认为开市、上游说休市。我们会为这些日子要求数据，"
                       "结果只能是永不消失的洞", extra[:5]))
    if missing:
        out.append(bad("calendar", f"本地缺 {len(missing)} 个交易日",
                       "上游开市、本地没有。这些日子的数据会被当成「不在区间内」而跳过",
                       missing[:5]))
    if not out:
        out.append(ok("calendar", f"对账一致（{start}..{end}，{len(local)} 个交易日）"))
    return out


# ---------------------------------------------------------------- 2. 前复权

def probe_qfq(conn, cal: TradingCalendar, symbol: str, *, n: int = 30,
              max_bars: int = 500) -> list[Result]:
    """本地算的前复权 vs 上游给的前复权。**这条不过就别信任何历史收益率。**

    两家锚点必须同日：本地锚 `target`（最新交易日），上游 qfq 也以最新价为锚。
    只比 `close`——其他列同一套缩放，重复比没有增量信息。

    **窗口必须跨过一次除权日**，否则这条探针是瞎的：前复权是"每个点乘一个系数"，
    窗口内没有除权日时，本地和上游乘的是同一个系数，比值恒等于 1，怎么错都"一致"。
    实测漏 0.2 元/股（平安 2018-06-07）时，最近 30 根 bar 全部一致——因为那次除权
    早就在窗口外了。所以这里把起点往前拉到最近一个除权日之前，再退 10 个交易日。

    **这条验的是"数据"，不是"公式"**：上游序列能对上"由我们的分红 + 除权日 + 前收盘
    推出来的等差序列"，说明分红金额、除权日、前收盘这三个输入是对的；
    至于 `factor` 的公式本身（等比连乘）对不对，归 `test_factor` 的单元测试管，
    外部没有"同口径"的源可以拿来当裁判。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM kline WHERE symbol=? ORDER BY date DESC LIMIT ?", (symbol, n))]
    if len(rows) < 5:
        return [skip("qfq", f"{symbol} 库里 K 线不足（{len(rows)} 根）",
                     "先跑一次 ensure('kline', ...) 再验")]
    rows.reverse()
    note = f"窗口内没有除权日，只验位数不验因子（{QFQ_TOL:.0e}）"

    ex = _last_ex_date(conn, symbol, rows[-1]["date"])
    if ex and ex < rows[0]["date"]:
        start = _back(cal, ex, 10)
        wide = [dict(r) for r in conn.execute(
            "SELECT * FROM kline WHERE symbol=? AND date>=? ORDER BY date",
            (symbol, start))]
        if len(wide) <= max_bars:
            rows, note = wide, f"窗口跨过 {ex} 除权日（{len(wide)} 根）"
        else:
            note = f"最近一次除权日 {ex} 太早，K 线只有 {len(wide)} 根（上限 {max_bars}）"

    try:
        mine = F.adjusted(conn, cal, symbol, rows, anchor=rows[-1]["date"])
    except Exception as e:                    # noqa: BLE001 - 探针不许炸
        return [bad("qfq", f"{symbol} 本地算因子失败", str(e))]
    sub = _subtractive(conn, cal, symbol, rows)

    up, src = _upstream_qfq(symbol, rows[0]["date"], rows[-1]["date"], len(rows))
    if not up:
        return [skip("qfq", f"{symbol} 拿不到可比的前复权序列", src)]

    eq_worst, eq_bad, checked = _diff(mine, up)
    sub_worst, sub_bad, _ = _diff(sub, up)
    if not checked:
        return [skip("qfq", f"{symbol} 两边的日期对不上", "没有共同交易日可比")]

    where = f"{note}；对账源：{src}"
    if not eq_bad:
        return [ok("qfq", f"{symbol} 前复权一致（等比口径，{checked} 点，"
                   f"最大差 {eq_worst:.1e}）", where)]
    if not sub_bad:
        return [ok("qfq", f"{symbol} 前复权一致（上游是等差口径，{checked} 点）",
                   f"{where}。两套口径只在除权日之前有差，差值 = 派现 × (1 − 当日价/前收盘)，"
                   f"最大 {sub_worst * 100:.2f}%，属口径差不是数据错")]
    # 两种口径都对不上 → 不是口径问题，是数据问题
    closer, worst = (mine, eq_worst) if eq_worst <= sub_worst else (sub, sub_worst)
    _, bads, _ = _diff(closer, up)
    return [bad("qfq", f"{symbol} 前复权不一致（等比差 {eq_worst * 100:.2f}%、"
                f"等差差 {sub_worst * 100:.2f}%，都不在容许范围内）",
                "先查 dividend 表：漏一条分红就会让整段历史偏高/偏低", bads[:5])]


def _diff(mine: list[dict], up: dict[str, float]) -> tuple[float, list[str], int]:
    """逐点比 `close`。返回（最大相对差，超限明细，比了几个点）。

    容差**不是纯相对**：上游的前复权价只给到分（`47.94`），我们给到 4 位小数。
    一股 50 元的票，分位取整本身就带来 1e-4 的相对误差——纯相对容差会把
    "两家的四舍五入"报成"数据不一致"。
    """
    worst, out, checked = 0.0, [], 0
    for r in mine:
        u = up.get(r["date"])
        if u is None or not r.get("close"):
            continue
        checked += 1
        d = abs(float(r["close"]) - float(u))
        rel = d / max(abs(float(u)), 1e-9)
        worst = max(worst, rel)
        if d > max(_CENT, QFQ_TOL * abs(float(u))):
            out.append(f"{r['date']} 本地 {r['close']:.4f} vs 上游 {float(u):.4f}"
                       f"（{rel * 100:.3f}%）")
    return worst, out, checked


def _subtractive(conn, cal: TradingCalendar, symbol: str, rows: list[dict]) -> list[dict]:
    """同一批行，按**等差**口径复权：`(P − 派现 + 配股价×比例) / (1 + 送转)` 逐个事件作用。

    为什么要在本地再算一遍：上游（东财 `fqt=1`、westock `fq=qfq`）用的是这个口径，
    我们用的是等比。**光看数字分不清"口径不同"和"数据错了"**——两边都算出来再比，
    谁对得上就是谁的口径，都对不上才是数据错。这比猜某家源用哪种口径可靠。

    现金派现的事件里两种顺序等价（减法可交换）；有送转时顺序会影响结果，
    这里按时间升序处理，与上游逐次复权的做法一致。
    """
    out = [dict(r) for r in rows]
    try:
        evs = F.events(conn, cal, symbol)
    except Exception:                         # noqa: BLE001 - 算不出来就不给这个候选
        return []
    for e in evs:
        k = 1.0 + e.bonus + e.transfer
        if k <= 0:
            continue
        adj = -e.cash + e.rights * float(e.rights_price or 0.0)
        for row in out:
            if str(row["date"]) >= e.day:
                continue
            for c in F.PRICE_COLS:
                v = row.get(c)
                if v is not None:
                    row[c] = (float(v) + adj) / k
    return out


def _upstream_qfq(symbol: str, start: str, end: str,
                  limit: int) -> tuple[dict[str, float], str]:
    """上游前复权序列：**东财优先**（一次给全区间、带 4 位精度），westock 兜底。"""
    em_qfq, why = _em_qfq(symbol, start, end)
    if em_qfq:
        return em_qfq, "东财 fqt=1"
    w_qfq, w_why = _w_qfq(symbol, start, end, limit)
    if w_qfq:
        return w_qfq, f"westock fq=qfq（东财不可用：{why}）"
    return {}, f"东财：{why}；westock：{w_why}"


def _em_qfq(symbol: str, start: str, end: str) -> tuple[dict[str, float], str]:
    """东财前复权。**失败不当错**：这条腿被封时只是换一条退路。"""
    try:
        rows, reason = em._fetch_one(
            symbol, Need("kline", "probe-qfq", end, start, 0, 0), fqt="1")
    except Exception as e:                    # noqa: BLE001
        return {}, f"{type(e).__name__}: {str(e)[:60]}"
    if not rows:
        return {}, reason or "返回空"
    return {r["date"]: float(r["close"]) for r in rows if r.get("close") is not None}, ""


def _w_qfq(symbol: str, start: str, end: str, limit: int) -> tuple[dict[str, float], str]:
    try:
        d = W.call("data_kline", {"code": symbol, "period": "day", "start": start,
                                  "end": end, "fq": "qfq", "limit": limit + 20})
    except Exception as e:                    # noqa: BLE001
        return {}, f"{type(e).__name__}: {str(e)[:60]}"
    nodes = (W._unwrap(d) or {}).get("nodes") or []
    out = {}
    for x in nodes:
        day = W._date(x.get("date"))
        v = x.get("last") if x.get("last") is not None else x.get("close")
        if day and v is not None:
            out[day] = float(v)
    return out, ("" if out else "返回空")


def _last_ex_date(conn, symbol: str, before: str) -> str | None:
    """`before` 之前最近的一次除权日。没有就 `None`（十年不分红是合法状态）。"""
    row = conn.execute(
        "SELECT MAX(ex_date) FROM dividend WHERE symbol=? AND ex_date<=?",
        (symbol, before)).fetchone()
    return row[0] if row and row[0] else None


def _back(cal: TradingCalendar, day: str, n: int) -> str:
    """`day` 往前数 `n` 个交易日。日历里查不到就退回自然日估算。"""
    cur = day
    for _ in range(n):
        prev = cal.prev(cur)
        if not prev or prev >= cur:
            break
        cur = prev
    if cur == day:                            # 日历没覆盖到那么早
        cur = (dt.date.fromisoformat(day) - dt.timedelta(days=int(n * 1.5))).isoformat()
    return cur


# ---------------------------------------------------------------- 3. 分红

def probe_dividend(conn, symbol: str, *, years: int = 11) -> list[Result]:
    """本地分红 vs 上游**按除权日聚合后**的值。

    聚合口径必须一致才算对得上：上游把"年度分红 + 特殊分红"拆成两条共用 `exDiviDate`，
    拿逐条去比会把"合并正确"误判成"库里偏高"。
    """
    local = {r["ex_date"]: dict(r) for r in conn.execute(
        "SELECT ex_date,cash_per_share,bonus_per_share,transfer_per_share "
        "FROM dividend WHERE symbol=?", (symbol,))}
    if not local:
        return [skip("dividend", f"{symbol} 库里没有分红记录", "先 ensure('dividend', ...)")]

    d = W.call("data_dividend", {"code": symbol, "years": years})
    plans = (d or {}).get("plans") or []
    up = {r["ex_date"]: r for r in W._dividend_rows(symbol, plans)}
    if not up:
        return [skip("dividend", f"{symbol} 上游没给分红", "data_dividend 返回空")]

    diffs: list[str] = []
    for ex, mine in sorted(local.items()):
        theirs = up.get(ex)
        if theirs is None:
            continue
        for k, label in (("cash_per_share", "派"), ("bonus_per_share", "送"),
                         ("transfer_per_share", "转")):
            a, b = float(mine.get(k) or 0.0), float(theirs.get(k) or 0.0)
            if abs(a - b) > 1e-6:
                diffs.append(f"{ex} {label}：库里 {a:.4f} vs 上游 {b:.4f}")
    only_local = sorted(set(local) - set(up))
    if diffs:
        return [bad("dividend", f"{symbol} 分红不一致（{len(diffs)} 处）",
                    "最常见的原因是同日多方案没合并（只留了其中一条）", diffs[:5])]
    if only_local:
        return [ok("dividend", f"{symbol} 数值一致，但库里多 {len(only_local)} 个除权日",
                   "库里更全不算错（上游只回 years 年），但要知道差异来自哪",
                   only_local[:5])]
    return [ok("dividend", f"{symbol} 分红一致（{len(local)} 个除权日）")]


# ---------------------------------------------------------------- 4. 估值

def probe_valuation(conn, symbol: str, *, n: int = 3) -> list[Result]:
    """本地估值周频 vs 东财原始日频行。抽最近几个周频点，逐点比 PE/PB。

    **东财被封时这条只能 SKIP**——它是唯一估值源，被封就无从对账。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT trade_date,pe_ttm,pb_mrq FROM valuation WHERE symbol=? "
        "ORDER BY trade_date DESC LIMIT ?", (symbol, n))]
    if not rows:
        return [skip("valuation", f"{symbol} 库里没有估值", "先 ensure('valuation', ...)")]
    try:
        raw = em._datacenter("RPT_VALUEANALYSIS_DET", em.VALUATION_COLS,
                             f'(SECUCODE="{em.secucode(symbol)}")',
                             sort="TRADE_DATE", stop_before=rows[-1]["trade_date"])
    except Exception as e:                    # noqa: BLE001 - 探针不许炸
        return [skip("valuation", f"{symbol} 取不到上游估值", str(e)[:120])]
    up = {em._date(r.get("TRADE_DATE")): r for r in raw}

    diffs: list[str] = []
    checked = 0
    for r in rows:
        u = up.get(r["trade_date"])
        if not u:
            continue
        checked += 1
        for col, key, label in (("pe_ttm", "PE_TTM", "PE"), ("pb_mrq", "PB_MRQ", "PB")):
            a = r[col]
            b = em._num(u.get(key))
            if a is None or b is None:
                continue
            if abs(float(a) - float(b)) > 1e-6:
                diffs.append(f"{r['trade_date']} {label}：库里 {a} vs 上游 {b}")
    if not checked:
        return [skip("valuation", f"{symbol} 两边日期对不上", str([r['trade_date'] for r in rows]))]
    if diffs:
        return [bad("valuation", f"{symbol} 估值不一致（{len(diffs)} 处）",
                    "估值允许整段覆盖刷新（pb 分母随新财报重算），但同一天的值必须一致", diffs)]
    return [ok("valuation", f"{symbol} 估值一致（{checked} 个周频点）")]


# ---------------------------------------------------------------- 5. 停牌判定

def probe_suspension(conn, cal: TradingCalendar, symbol: str, *, look_back: int = 60,
                     max_report: int = 3) -> list[Result]:
    """K 线里的洞：**停牌**还是**我们缺数据**？

    两者的 `rows` 都是空，`settle()` 之前完全分不清。判据只有一条：另一家源那天有没有 bar。
    有 → 我们缺数据（要修）；没有 → 停牌（合法状态，settle 成永久洞）。
    """
    rows = conn.execute("SELECT hole_detail FROM coverage "
                        "WHERE domain='kline' AND symbol=?", (symbol,)).fetchone()
    if not rows or not rows[0]:
        return [skip("suspension", f"{symbol} 没有记录在案的洞", "无洞即无需判")]
    try:
        holes = json.loads(rows[0])
    except ValueError:
        return [skip("suspension", f"{symbol} 的 hole_detail 不是合法 JSON", "doctor 会报这条")]
    recent = [h for h in holes if (h.get("to") or "") >= _days_ago(look_back)][-max_report:]
    recent = [h for h in recent if h.get("from") and h.get("to")]
    if not recent:
        return [skip("suspension", f"{symbol} 近 {look_back} 天没有洞",
                     f"共 {len(holes)} 个洞都在更早")]

    d = W.call("data_kline", {"code": symbol, "period": "day",
                              "start": min(h["from"] for h in recent),
                              "end": max(h["to"] for h in recent),
                              "limit": 400})
    nodes = {W._date(x.get("date")) for x in ((W._unwrap(d) or {}).get("nodes") or [])}

    real_gap, suspended = [], []
    for h in recent:
        span = cal.between(h["from"], h["to"])
        hit = [x for x in span if x in nodes]
        (real_gap if hit else suspended).append(
            f"{h['from']}..{h['to']}（{len(span)} 天）"
            + (f"，上游有 {len(hit)} 天：{hit[:3]}" if hit else ""))
    out: list[Result] = []
    if real_gap:
        out.append(bad("suspension", f"{symbol} 有 {len(real_gap)} 个洞是「我们缺数据」",
                       "上游同一天有 bar，说明不是停牌。重跑该票即可补上", real_gap))
    if suspended:
        out.append(ok("suspension", f"{symbol} 有 {len(suspended)} 个洞确认是停牌",
                      "上游同区间也没有 bar → 可以 settle 成永久洞，不必再重试", suspended))
    return out


def _days_ago(n: int) -> str:
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


# ---------------------------------------------------------------- 入口

PROBES = ("calendar", "qfq", "dividend", "valuation", "suspension")


def run(params: dict, *, cfg: Config | None = None) -> list[Result]:
    """跑指定探针。**不抛异常**：探针崩掉等于没探，坏一处只掉一项。

    两条避免"探了一堆空气"的规矩：

    1. **按探针各自挑票**。`qfq` 要 K 线、`valuation` 要估值，两边有数据的往往不是同一批。
       全按 K 线行数挑的话，`--valuation` 会在三只没有估值的票上连报三次"库里没有估值"，
       看起来像探针坏了，其实是从没探到。用户显式给 `--symbols` 时除外——那时照单全跑，
       SKIP 就是他要的答案。
    2. **某条探针一次都没跑起来就明说**，不要"0 通过 0 不一致 0 跳过"这种空账。
    """
    cfg = cfg or Config.from_env()
    names = [p for p in PROBES if params.get(p)]
    if not names:
        names = list(PROBES)
    results: list[Result] = []

    cal = None
    try:
        cal = TradingCalendar.load(cfg.calendar_path)
    except Exception as e:                    # noqa: BLE001
        results.append(skip("calendar", "日历加载不了，依赖日历的探针全部跳过", str(e)[:120]))

    conn = None
    try:
        conn = db.connect(cfg.db_path, readonly=True)
    except Exception as e:                    # noqa: BLE001
        results.append(skip("db", "库打不开，依赖库的探针全部跳过", str(e)[:120]))

    explicit = [s for s in (params.get("symbols") or []) if s]
    picked = _pick_symbols(conn, names)
    symbols = explicit or picked
    ran: dict[str, int] = {n: 0 for n in names}

    try:
        if "calendar" in names and cal is not None:
            got = _guard("calendar", probe_calendar, cal)
            results += got
            ran["calendar"] += len(got)
        for sym in symbols:
            if "qfq" in names and cal is not None and conn is not None:
                if explicit or _has(conn, "kline", sym):
                    got = _guard("qfq", probe_qfq, conn, cal, sym)
                    results += got
                    ran["qfq"] += len(got)
            if "dividend" in names and conn is not None:
                if explicit or _has(conn, "dividend", sym):
                    got = _guard("dividend", probe_dividend, conn, sym)
                    results += got
                    ran["dividend"] += len(got)
            if "valuation" in names and conn is not None:
                if explicit or _has(conn, "valuation", sym):
                    got = _guard("valuation", probe_valuation, conn, sym)
                    results += got
                    ran["valuation"] += len(got)
            if "suspension" in names and cal is not None and conn is not None:
                if explicit or _has_holes(conn, sym):
                    got = _guard("suspension", probe_suspension, conn, cal, sym)
                    results += got
                    ran["suspension"] += len(got)
    finally:
        if conn is not None:
            conn.close()

    for n in names:
        if not ran[n]:
            results.append(skip(n, f"{n} 探针没跑起来", _why_not(n, cal, conn, symbols)))
    return results


def _why_not(name: str, cal, conn, symbols: list[str]) -> str:
    """没跑起来的原因。**不猜**，只报当前知道的事实。"""
    if name in ("calendar", "qfq", "suspension") and cal is None:
        return "日历没加载上"
    if conn is None:
        return "库没打开"
    if name == "calendar":
        return "日历是空的"
    if name == "suspension":
        return "库里没有带洞的 K 线覆盖记录——没洞就没有可判的，这是好状态"
    if not symbols:
        return "库里没有任何可查的票：先 ensure 一次再验"
    return f"挑中的票里没有 {name} 域的数据（{', '.join(symbols[:3])}）"


_TABLE_OF = {"kline": "kline", "dividend": "dividend",
             "valuation": "valuation", "coverage": "coverage"}


def _has(conn, table: str, symbol: str) -> bool:
    try:
        return conn.execute(f"SELECT 1 FROM {_TABLE_OF[table]} WHERE symbol=? LIMIT 1",
                            (symbol,)).fetchone() is not None
    except Exception:                         # noqa: BLE001 - 表都不在就当没有
        return False


def _has_holes(conn, symbol: str) -> bool:
    """有没有**记录在案的 K 线洞**。没洞的票探 `suspension` 等于白打一次上游。"""
    try:
        return conn.execute(
            "SELECT 1 FROM coverage WHERE domain='kline' AND symbol=? AND holes>0 LIMIT 1",
            (symbol,)).fetchone() is not None
    except Exception:                         # noqa: BLE001
        return False


def _guard(name: str, fn, *a, **kw) -> list[Result]:
    """探针内部异常降级成 MISMATCH——探针崩掉等于没探。"""
    try:
        return fn(*a, **kw)
    except Exception as e:                    # noqa: BLE001
        return [bad(name, f"探针自己出错：{type(e).__name__}", str(e)[:200])]


_PICK_SQL = {
    "qfq": "SELECT symbol FROM kline GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT ?",
    "suspension": "SELECT symbol FROM coverage WHERE domain='kline' AND holes>0 "
                  "GROUP BY symbol ORDER BY SUM(holes) DESC LIMIT ?",
    "dividend": "SELECT symbol FROM dividend GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT ?",
    "valuation": "SELECT symbol FROM valuation GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT ?",
}


def _pick_symbols(conn, names, limit: int = 3) -> list[str]:
    """**按探针各自挑票**（见 `run()` 第 1 条），再按出现顺序去重。

    `suspension` 特意只挑"有洞"的票：没洞的票探它等于白跑一趟上游。
    """
    if conn is None:
        return []
    out: list[str] = []
    for n in names:
        sql = _PICK_SQL.get(n)
        if not sql:
            continue
        try:
            rows = [r[0] for r in conn.execute(sql, (limit,))]
        except Exception:                     # noqa: BLE001
            rows = []
        for s in rows:
            if s not in out:
                out.append(s)
    return out


ICON = {"OK": "✅", "MISMATCH": "❌", "SKIP": "⏭ "}


def render(results: list[Result]) -> str:
    out: list[str] = []
    for r in results:
        out.append(f"{ICON[r.status]} [{r.probe}] {r.title}")
        if r.detail:
            out.append(f"      {r.detail}")
        for line in r.rows:
            out.append(f"      - {line}")
    n_bad = sum(1 for r in results if r.status == "MISMATCH")
    n_skip = sum(1 for r in results if r.status == "SKIP")
    n_ok = sum(1 for r in results if r.status == "OK")
    out.append("")
    out.append(f"结论：{n_ok} 通过 / {n_bad} 不一致 / {n_skip} 跳过"
               + ("" if not n_bad else "  ← 有对不上的地方，先查它们"))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="跨源探针（会联网，手动跑）",
        epilog="注意：`--calendar` 是「跑日历探针」，日历文件路径用 `--calendar-file`。"
               "两个都叫 --calendar 会撞车（argparse 报 conflict），也别指望缩写。")
    for p in PROBES:
        ap.add_argument(f"--{p}", action="store_true", help=f"只跑 {p} 探针")
    ap.add_argument("--symbols", default="", help="逗号分隔；默认取库里 K 线最全的 3 只")
    ap.add_argument("--db", default="", help="默认按 STOCKHUB_DB / Config 的库")
    ap.add_argument("--calendar-file", default="",
                    help="日历文件；默认按 STOCKHUB_CALENDAR / Config 的日历")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if args.db:
        cfg.db_path = Path(args.db).expanduser()
    if args.calendar_file:
        cfg.calendar_path = Path(args.calendar_file).expanduser()
    params = {p: getattr(args, p) for p in PROBES}
    params["symbols"] = [s.strip() for s in args.symbols.split(",") if s.strip()]

    results = run(params, cfg=cfg)
    if args.json:
        print(json.dumps([{"probe": r.probe, "status": r.status, "title": r.title,
                           "detail": r.detail, "rows": r.rows} for r in results],
                         ensure_ascii=False, indent=2))
    else:
        print(render(results))
    return 1 if any(r.status == "MISMATCH" for r in results) else 0


if __name__ == "__main__":                    # pragma: no cover
    raise SystemExit(main())
