"""三个判定，纯函数：`list[dict]` 进 → `dict` 出。不碰网络，不碰库。

**这里只有规则，没有结论。** 三路各算各的，谁都不参与谁的判断——
"趋势空头但估值便宜"是合法状态，合成一个总分只会把它抹平。

原文（那份分享对话）里有四个词写不进程序："收不回""拐头""向下发散""放量长阳"。
这里全部换成可复现的算术，且只保留最少的几条：

    趋势  连续 2 周收盘 < MA20            → 偏弱
          MA20 < MA60 且 MA60 低于 4 周前  → 空头条件
          两条同时成立                     → 空头确认
    估值  ERP = 100/PE(TTM) − 10年国债，取近 10 年分位
    情绪  全A破净率近 10 年分位 + 沪深300 周换手率（≤1% 地量 / ≥动态线 天量）

趋势、估值、破净率三条的阈值都是**分位**，不是固定数字——固定数字过上几年必然
失效（1.0%~1.5% 的换手率地量线就是 2014-2019 的化石）。

换手率这头是**非对称**的，两条线各有各的道理：

    地量线  1% 固定不动。换手率下端有结构性地板（ETF 申赎、量化、指数调仓的刚性
            成交），缩不下去——近十年的 10% 分位一直只在 0.96%~1.41% 之间打转，
            钉在 1% 等于钉在分位上，十年没偏过。
    天量线  近 52 周换手中位数 × 1.5，向上取整到 0.5%。上端没有盖子，2015 年能到
            8.67%、2023 年最高才 3% 出头，中间三倍差距全看当年有没有情绪事件。
            写死一个数必然两头落空：4% 在 2016-2023 有六年一次没触发，2026 年却
            触发了三成——"天量"被用成了"常态"。改成跟着近一年的常态水位走，
            市场抬到 3% 它自己变 4.5%，回落到 1.5% 它自己变 2.5%，不需要人校准。

两个输入（近 52 周中位数、当周换手）都在库里，纯函数现算，回填用基准日之前的
52 周，所以"回填 6 月那一周"和"跑本周"走的是同一段代码。

基准日（`as_of`）在这里落地
---------------------------
三个函数都收一个 `as_of`：进来先把序列截到 `date <= as_of`，再照原样算。
这样"回填 6 月那一周"和"跑本周"走的是同一段代码，不需要另写一条历史分支。

不传 `as_of`（None）= 取序列最后一项，就是老行为。
"""

from __future__ import annotations

import datetime as dt
import math

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

TURNOVER_DRY = 1.0         # % 周换手 ≤1% → 地量（**绝对值**，下端有地板，不用动）
TURNOVER_LOW_MULT = 0.9    # 缩量线 = 近 52 周中位数 × 这个倍数（下沿）
TURNOVER_HIGH_MULT = 1.1   # 放量线 = 近 52 周中位数 × 这个倍数（上沿）
TURNOVER_HOT_MULT = 1.5    # 天量线 = 近 52 周中位数 × 这个倍数
TURNOVER_HEAVY_WIN = 52    # 周频，1 年——四条线共用的锚，只看最近这一年
TURNOVER_WIN = 260         # 周频，5 年——只用来给「分位」这个参考值，不参与分档

_EPS = 1e-9                # 取整时抵掉浮点噪声（见 `volume_lines`）


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


def _cut(series: list[dict], as_of: str | None) -> list[dict]:
    """只留 `date <= as_of` 的记录。`as_of` 为空就原样返回（=取最新）。

    日期是 ISO 字符串，字典序即时间序，不用解析成 date。
    """
    if not as_of:
        return series
    return [r for r in series if str(r.get("date") or "") <= as_of]


def _iso_week(day: str) -> tuple[int, int]:
    d = dt.date.fromisoformat(day)
    return d.isocalendar()[:2]


def split_partial(weeks: list[dict], today: str | None = None) -> tuple[list[dict], dict | None]:
    """把末尾「本周还没走完」的那根周线摘出来，返回 `(完整周, 未完成周 or None)`。

    为什么必须摘：东财周K 在周中会返回一根**当周至今**的 bar（日期 = 最新交易日），
    它的 close 是当天收盘（当价格看没问题），但 volume/turnover 只累计了几天。
    2026-11-23（周一）那根：成交量只有上周的 23%、换手 0.61%，拿它去比"周"的
    四条线，会凭空判出一个「地量」。量能这一档只认**走完的周**。

    判据：这根 bar 不晚于 `today`、和 `today` 落在同一个 ISO 周，且 `today`
    还没到周五（= 这一周还没收盘）。周五盘后跑，它就是完整周；周末跑也一样。
    跨周之后自然就变成完整周了——不需要人工标记，也不查交易日历。

    节假日前最后一个交易日（周三/周四）当天跑，会被保守判成"未走完"，
    退回上一周。代价只是那一天的档位晚一周生效，不会错判——宁可说"没走完"。
    """
    if not weeks:
        return weeks, None
    ref = today or dt.date.today().isoformat()
    last = weeks[-1]
    day = str(last.get("date") or "")
    try:
        running = (day <= ref and _iso_week(day) == _iso_week(ref)
                   and dt.date.fromisoformat(ref).weekday() < 4)
    except ValueError:
        return weeks, None
    if not running:
        return weeks, None
    return weeks[:-1], {"date": day, "turnover": last.get("turnover"),
                        "close": last.get("close"),
                        # 档位实际是按这一周判的（= 上一完整周）
                        "prev_date": (str(weeks[-2].get("date"))
                                      if len(weeks) >= 2 else None)}


def _r(v, digits: int = 2):
    return None if v is None else round(float(v), digits)


def _median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def volume_lines(turns: list[float]) -> dict | None:
    """量能四条线，**全部从同一个水位派生**：近 52 周换手中位数 M。

        M     = 近 52 周中位           ← 基准水位，四条线都从它算
        缩量线 = 0.9M，向下取整到 0.1%  ← 低于这条 = 比平常明显缩
        放量线 = 1.1M，向上取整到 0.1%  ← 高于这条 = 比平常明显放
        天量线 = 1.5M，向上取整到 0.5%

    `turns` 是按日期升序的周换手率（%）。窗口不足半年（26 周）就返回 None，
    让调用方自己决定"没有线"怎么说——宁可不说，也不拿半个窗口硬算。

    取整方向不是随手定的：界值都是闭区间，上限向上取整、下限向下取整，
    让「平稳」这一档尽量宽、两头的标签宁缺勿滥。

    缩量线会被地量线（1% 绝对值）兜住：`max(TURNOVER_DRY, ...)`。市场整体
    缩到 1.1% 上下时，缩量线就贴到 1.0%，**「缩量」这一档会被「地量」吃掉**——
    这是对的：整个市场都蹲在地板上，"比平常再缩一点"没有增量信息。
    """
    win = turns[-TURNOVER_HEAVY_WIN:]
    if len(win) < TURNOVER_HEAVY_WIN // 2:
        return None
    m = _median(win)
    # `_EPS` 抵掉浮点噪声：2.0×1.1×10 在二进制里是 22.000000000000004，
    # 直接 ceil 会得到 2.3 而不是 2.2；同理 floor 侧可能少一格。
    low = max(TURNOVER_DRY,
              math.floor(m * TURNOVER_LOW_MULT * 10 + _EPS) / 10)
    high = max(low, math.ceil(m * TURNOVER_HIGH_MULT * 10 - _EPS) / 10)
    return {"median": m, "low": low, "high": high,
            "heavy": math.ceil(m * TURNOVER_HOT_MULT * 2 - _EPS) / 2}


def heavy_line(turns: list[float]) -> float | None:
    """只要天量线时的简写（老调用方 + 看板漂移测试在用）。"""
    lines = volume_lines(turns)
    return None if lines is None else lines["heavy"]


# ---------------------------------------------------------------- 1. 趋势

def trend(weeks: list[dict], as_of: str | None = None) -> dict | None:
    """沪深300 周线。`weeks` 按日期升序，每项至少含 date / close。"""
    weeks = _cut([w for w in weeks if w.get("close")], as_of)
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

def valuation(perf: list[dict], bonds: list[dict],
              as_of: str | None = None) -> dict | None:
    """ERP = 盈利收益率 − 10 年国债。`perf` 每项含 date / pe_ttm。"""
    perf = _cut([p for p in perf if p.get("pe_ttm") and p.get("date")], as_of)
    bonds = _cut([b for b in bonds if b.get("value") is not None and b.get("date")],
                 as_of)
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

def sentiment(below: list[dict], weeks: list[dict],
              as_of: str | None = None, today: str | None = None) -> dict | None:
    """破净率分位 + 周换手量能分档。`below` 每项含 date / value。

    `today` 只用来判「最后一根周线是不是本周还没走完」（见 `split_partial`），
    默认取真实今天；测试里传死日期，免得跑到哪个星期几结果不一样。
    """
    below = _cut([b for b in below if b.get("value") is not None and b.get("date")],
                 as_of)
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
        "turnover_pct": None,
        "turnover_q10": None,
        "turnover_q90": None,
        "dry_line": TURNOVER_DRY,
        "low_line": None,
        "high_line": None,
        "heavy_line": None,
        "median52": None,
        "volume_dry": None,
        "volume_state": None,
        "partial_week": None,
    }

    turns = _cut([w for w in weeks if w.get("turnover") is not None], as_of)
    turns, partial = split_partial(turns, today)
    out["partial_week"] = partial
    if len(turns) >= 60:
        series = [float(w["turnover"]) for w in turns]
        window = series[-TURNOVER_WIN:]
        latest = series[-1]
        lines = volume_lines(series)
        volume_state = _volume_state(latest, lines)
        out.update({
            "turnover": _r(latest),
            # 分位只当参考值（"这周在近 5 年里排第几"），不参与分档
            "turnover_pct": _pct(window, latest),
            "turnover_q10": _r(_quantile(window, 10)),
            "turnover_q90": _r(_quantile(window, 90)),
            "dry_line": TURNOVER_DRY,
            "low_line": _r(None if lines is None else lines["low"]),
            "high_line": _r(None if lines is None else lines["high"]),
            "heavy_line": _r(None if lines is None else lines["heavy"]),
            "median52": _r(None if lines is None else lines["median"]),
            # `volume_dry` 是旧字段（看板与 payload 里都有人读），跟着分档走，
            # 免得同一个换手率在"地量"和"未地量"之间出现两套说法。
            "volume_dry": bool(volume_state == "地量"),
            "volume_state": volume_state,
        })
    return out


def _quantile(values: list[float], pct: float) -> float:
    """窗口内的 pct 分位值。与看板上那条虚线的取法完全一致。"""
    v = sorted(values)
    return v[max(0, min(len(v) - 1, int(len(v) * pct / 100) - 1))]


def _volume_state(turnover: float | None, lines: dict | None = None) -> str | None:
    """周换手率 → 五档说法。**下限看绝对值，上限看相对水位。**

        地量  ≤ 1.0%（固定）          ← 市场的地板，十年没动过
        缩量  < 缩量线 0.9M
        平稳  缩量线 ~ 放量线之间
        放量  ≥ 放量线 1.1M
        天量  ≥ 天量线 1.5M

    `lines` 由 `volume_lines()` 逐周现算（看板上是三条随水位起伏的折线）。
    给 None 时退回"只判地量"，不硬造剩下四档。

    判定顺序有讲究：先地量（绝对地板压过一切），再天量，最后才在中间三档里走。
    缩量线被地量线兜住的市场（中位 ≈1.1%）里，「缩量」自然缺席——
    那不是漏判，是这一档本来就没有空间了。
    """
    if turnover is None:
        return None
    if turnover <= TURNOVER_DRY:
        return "地量"
    if not lines:
        return "正常"
    if turnover >= lines["heavy"]:
        return "天量"
    if turnover < lines["low"]:
        return "缩量"
    if turnover >= lines["high"]:
        return "放量"
    return "平稳"


# ---------------------------------------------------------------- 合并

def judge(*, weeks: list[dict], perf: list[dict], bonds: list[dict],
          below: list[dict], as_of: str | None = None) -> dict:
    """三路各算各的，只把它们装进一个 dict——**不合并、不加权、不排序**。

    `as_of` 是基准日：三路都只用这一天及以前的数据算，给旧日期就是回填那一周。
    """
    t = trend(weeks, as_of)
    v = valuation(perf, bonds, as_of)
    s = sentiment(below, weeks, as_of)

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
