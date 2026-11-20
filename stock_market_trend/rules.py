"""三个判定，纯函数：`list[dict]` 进 → `dict` 出。不碰网络，不碰库。

**这里只有规则，没有结论。** 三路各算各的，谁都不参与谁的判断——
"趋势空头但估值便宜"是合法状态，合成一个总分只会把它抹平。

原文（那份分享对话）里有四个词写不进程序："收不回""拐头""向下发散""放量长阳"。
这里全部换成可复现的算术，且只保留最少的几条：

    趋势  连续 2 周收盘 < MA20            → 偏弱
          MA20 < MA60 且 MA60 低于 4 周前  → 空头条件
          两条同时成立                     → 空头确认
    估值  ERP = 100/PE(TTM) − 10年国债，取近 10 年分位
    情绪  全A破净率近 10 年分位 + 沪深300 周换手率近 3 年 10% 分位

所有阈值都是**分位**，不是固定数字——固定数字过上几年必然失效（1.0%~1.5% 的
换手率地量线就是 2014-2019 的化石）。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 阈值

MA_FAST = 20
MA_SLOW = 60
CONFIRM_WEEKS = 2          # "连续 2 周"才算数，单周插针不算
SLOW_SLOPE_WEEKS = 4       # MA60 跟 4 周前比，下行即算

ERP_WIN = 2400             # 日频，约 10 年
ERP_CHEAP = 80
ERP_EXTREME = 95
ERP_EXPENSIVE = 20

NA_WIN = 2400              # 日频，约 10 年
NA_PANIC = 90
NA_COLD = 70

TURNOVER_WIN = 156         # 周频，3 年
TURNOVER_DRY = 10          # 3 年 10% 分位


# ---------------------------------------------------------------- 小工具

def _sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def _pct(values: list[float], v: float) -> int | None:
    """分位：比 v 小的占多少。用分位不用 σ——ERP 分布厚尾，σ 和分位对不上。"""
    vals = [x for x in values if x is not None]
    if not vals:
        return None
    return round(sum(1 for x in vals if x < v) / len(vals) * 100)


def _tail(series: list[dict], n: int) -> list[dict]:
    return series[-n:] if n and len(series) > n else series


def _r(v, digits: int = 2):
    return None if v is None else round(float(v), digits)


# ---------------------------------------------------------------- 1. 趋势

def trend(weeks: list[dict]) -> dict | None:
    """沪深300 周线。`weeks` 按日期升序，每项至少含 date / close。"""
    weeks = [w for w in weeks if w.get("close")]
    if len(weeks) < MA_SLOW + SLOW_SLOPE_WEEKS:
        return None

    closes = [float(w["close"]) for w in weeks]
    ma20 = _sma(closes, MA_FAST)
    ma60 = _sma(closes, MA_SLOW)

    i = len(closes) - 1
    if ma60[i] is None or ma60[i - SLOW_SLOPE_WEEKS] is None:
        return None

    below20 = closes[i] < ma20[i] and closes[i - 1] < ma20[i - 1]
    bear = ma20[i] < ma60[i] and ma60[i] < ma60[i - SLOW_SLOPE_WEEKS]
    score = int(below20) + int(bear)

    if score >= 2:
        state = "空头确认"
    elif score == 1:
        state = "偏弱"
    elif ma20[i] > ma60[i] and closes[i] > ma20[i]:
        state = "偏强"
    else:
        state = "中性"

    def _streak(limit: list[float | None]) -> int:
        n = 0
        for k in range(i, -1, -1):
            if limit[k] is not None and closes[k] < limit[k]:
                n += 1
            else:
                break
        return n

    return {
        "state": state,
        "score": score,
        "date": weeks[i]["date"],
        "close": _r(closes[i]),
        "ma20": _r(ma20[i]),
        "ma60": _r(ma60[i]),
        "ma60_slope": _r((ma60[i] / ma60[i - SLOW_SLOPE_WEEKS] - 1) * 100),
        "weeks_below_ma20": _streak(ma20),
        "weeks_below_ma60": _streak(ma60),
    }


# ---------------------------------------------------------------- 2. 赔率

def valuation(perf: list[dict], bonds: list[dict]) -> dict | None:
    """ERP = 盈利收益率 − 10 年国债。`perf` 每项含 date / pe_ttm。"""
    perf = [p for p in perf if p.get("pe_ttm") and p.get("date")]
    bonds = [b for b in bonds if b.get("value") is not None and b.get("date")]
    if not perf or not bonds:
        return None

    # 按日期把国债收益率贴到每个交易日上（国债只到 T-1 时沿用前值，这是对的：
    # 周五那天的 ERP 用的就是当天已公布的最新收益率）
    bond_by_date = [b["date"] for b in bonds]
    j = 0
    series = []
    for p in perf:
        while j + 1 < len(bonds) and bonds[j + 1]["date"] <= p["date"]:
            j += 1
        if bonds[j]["date"] > p["date"]:
            continue            # 这一天的 PE 比最早的国债还早，算不了
        series.append({
            "date": p["date"],
            "pe": float(p["pe_ttm"]),
            "bond": float(bonds[j]["value"]),
            "erp": 100.0 / float(p["pe_ttm"]) - float(bonds[j]["value"]),
        })
    if not series:
        return None

    last = series[-1]
    window = [s["erp"] for s in _tail(series, ERP_WIN)]
    pct = _pct(window, last["erp"])
    if pct is None:
        return None
    mean = sum(window) / len(window)

    if pct >= ERP_EXTREME:
        state = "极值"
    elif pct >= ERP_CHEAP:
        state = "便宜"
    elif pct <= ERP_EXPENSIVE:
        state = "偏贵"
    else:
        state = "中性"

    return {
        "state": state,
        "date": last["date"],
        "pe_ttm": _r(last["pe"]),
        "bond10y": _r(last["bond"]),
        "erp": _r(last["erp"]),
        "pct": pct,
        "mean": _r(mean),
        "n": len(window),
    }


# ---------------------------------------------------------------- 3. 情绪

def sentiment(below: list[dict], weeks: list[dict]) -> dict | None:
    """破净率分位 + 周换手地量。`below` 每项含 date / value。"""
    below = [b for b in below if b.get("value") is not None and b.get("date")]
    if not below:
        return None

    last = below[-1]
    pct = _pct([b["value"] for b in _tail(below, NA_WIN)], last["value"])
    if pct is None:
        return None

    if pct >= NA_PANIC:
        state = "恐慌"
    elif pct >= NA_COLD:
        state = "偏冷"
    else:
        state = "正常"

    out = {
        "state": state,
        "date": last["date"],
        "below_na": _r(last["value"]),
        "below_na_pct": pct,
        "below_count": last.get("below"),
        "total_count": last.get("total"),
        "turnover": None,
        "turnover_q10": None,
        "volume_dry": None,
    }

    turns = [w for w in weeks if w.get("turnover") is not None]
    if len(turns) >= 60:
        window = [float(w["turnover"]) for w in turns[-TURNOVER_WIN:]]
        q10 = sorted(window)[max(0, int(len(window) * TURNOVER_DRY / 100) - 1)]
        out.update({
            "turnover": _r(turns[-1]["turnover"]),
            "turnover_q10": _r(q10),
            "volume_dry": bool(float(turns[-1]["turnover"]) <= q10),
        })
    return out


# ---------------------------------------------------------------- 合并

def judge(*, weeks: list[dict], perf: list[dict], bonds: list[dict],
          below: list[dict]) -> dict:
    """三路各算各的，只把它们装进一个 dict——**不合并、不加权、不排序**。"""
    t = trend(weeks)
    v = valuation(perf, bonds)
    s = sentiment(below, weeks)

    dates = [x["date"] for x in (t, v, s) if x]
    data_asof = min(dates) if dates else None

    return {
        "trend": t,
        "valuation": v,
        "sentiment": s,
        "data_asof": data_asof,
        "summary": _line(t, v, s),
    }


def _line(t: dict | None, v: dict | None, s: dict | None) -> str:
    """页面上那一行摘要。缺哪一路就写"—"，不猜。"""
    parts = [
        f"趋势 {t['state']}" if t else "趋势 —",
        f"估值 {v['state']} {v['pct']}%" if v else "估值 —",
        f"情绪 {s['state']}" if s else "情绪 —",
    ]
    return " · ".join(parts)
