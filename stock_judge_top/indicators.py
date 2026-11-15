"""K 线指标与市场阶段。

口径（与 README「数据口径」一致）
----------------------------------
`high*` / `pos*` 用日 K 的**最高价**（字段 high），不是收盘价：
「距高点回撤」的通行算法是对最高价算，盘中冲高也应计入。
README 旧版写的是「最高收盘」，与代码不符，已在文档里改成「最高价」。
"""
import statistics as st

from .common.values import fnum

POS_BASIS = "high"      # 位置类指标的价格口径：high（最高价）


def pct_rank(values, x, min_n=60):
    """x 在 values 中的百分位（0~100，含自身）；样本不足返回 None。"""
    if x is None:
        return None
    vals = [v for v in values if v is not None]
    if len(vals) < min_n:
        return None
    return 100.0 * sum(1 for v in vals if v <= x) / len(vals)


def roll_mean(vals, k):
    out = [None] * len(vals)
    s = 0.0
    cnt = 0
    for i, v in enumerate(vals):
        if v is not None:
            s += v
            cnt += 1
        if i >= k:
            old = vals[i - k]
            if old is not None:
                s -= old
                cnt -= 1
        if i >= k - 1 and cnt == k:
            out[i] = s / k
    return out


def kline_metrics(nodes):
    if len(nodes) < 30:
        return None
    cl = [fnum(x.get("last")) for x in nodes]
    hi = [fnum(x.get("high")) for x in nodes]
    if any(v is None for v in cl):
        return None
    # hi 与 cl 必须同样防 None：缺 high 时 high_stats() 的 max(key=...) 会抛 TypeError。
    if any(v is None for v in hi):
        return None
    tv = [fnum(x.get("exchange")) for x in nodes]
    pv = [fnum(x.get("volume")) for x in nodes]
    t = len(nodes) - 1

    def ret(k):
        return cl[t] / cl[t - k] - 1 if t >= k and cl[t - k] else None

    def avg_tv(k):
        if t + 1 < k:
            return None
        vals = [v for v in tv[t - k + 1:t + 1] if v is not None]
        return sum(vals) / len(vals) if vals else None

    std20 = None
    v20 = [v for v in tv[t - 19:t + 1] if v is not None]
    if len(v20) == 20:
        std20 = st.pstdev(v20)

    av20, av250 = avg_tv(20), avg_tv(250)

    def high_stats(k, require=True):
        if require and len(hi) < k:
            return None, None, None, None
        start = max(0, len(hi) - k)
        idx = max(range(start, len(hi)), key=lambda i: hi[i])
        h = hi[idx]
        return (h, nodes[idx]["date"], t - idx,
                cl[t] / h - 1 if h else None)

    high20, high20_date, days_since_high20, pos20 = high_stats(20)
    high60, high60_date, days_since_high60, pos60 = high_stats(60)
    high120, high120_date, days_since_high120, pos120 = high_stats(120)
    high250, high250_date, days_since_high, pos250 = high_stats(250, require=False)

    av5 = avg_tv(5)
    vol_ratio5 = (av5 / av20) if (av5 is not None and av20) else None

    def avg_pv(k):
        if t + 1 < k:
            return None
        vals = [v for v in pv[t - k + 1:t + 1] if v is not None]
        return sum(vals) / len(vals) if vals else None

    pav5, pav20 = avg_pv(5), avg_pv(20)
    pvol_ratio5 = (pav5 / pav20) if (pav5 is not None and pav20) else None

    ma20 = sum(cl[-20:]) / 20 if len(cl) >= 20 else None
    ma60 = sum(cl[-60:]) / 60 if len(cl) >= 60 else None

    # 换手自身分位：只用过去最多 250 个 20 日均换手值。
    # 旧版还算了 RET5/RET20/RET60/POS250/STD20/RATIO 六组分位，
    # 但没有任何视图消费它们（纯计算开销），已删除。
    avg20_hist = roll_mean(tv, 20)
    pct = {"AVG20": pct_rank(avg20_hist[-250:], av20)}
    pct_n = {"AVG20": min(len([v for v in avg20_hist if v is not None]), 250)}

    return {
        "bars": len(nodes), "date": nodes[-1]["date"], "close": cl[t],
        "pos_basis": POS_BASIS,
        "ret5": ret(5), "ret20": ret(20), "ret60": ret(60),
        "av5": av5, "av20": av20, "av60": avg_tv(60), "av250": av250,
        "std20": std20,
        "ratio": (av20 / av250) if (av20 is not None and av250) else None,
        "high20": high20, "high20_date": high20_date,
        "days_since_high20": days_since_high20, "pos20": pos20,
        "high60": high60, "high60_date": high60_date,
        "days_since_high60": days_since_high60, "pos60": pos60,
        "high120": high120, "high120_date": high120_date,
        "days_since_high120": days_since_high120, "pos120": pos120,
        "high250": high250, "high250_date": high250_date,
        "days_since_high": days_since_high, "pos250": pos250,
        "vol_ratio5": vol_ratio5,
        "pav5": pav5, "pav20": pav20, "pvol_ratio5": pvol_ratio5,
        "pct": pct, "pct_n": pct_n,
        "close_5_ago": cl[t - 5] if t >= 5 else None,
        "close_20_ago": cl[t - 20] if t >= 20 else None,
        "close_60_ago": cl[t - 60] if t >= 60 else None,
        "range5_high": max(cl[-5:]) if len(cl) >= 5 else None,
        "range5_low": min(cl[-5:]) if len(cl) >= 5 else None,
        "ma20": ma20, "ma60": ma60,
        "below_ma20": (cl[t] < ma20) if ma20 else None,
    }


def market_phase(km):
    """区分「历史顶已过」和「当前反弹/局部高位」，避免一句话混淆。"""
    pos250, pos60, ret20 = km.get("pos250"), km.get("pos60"), km.get("ret20")
    if pos250 is None and pos60 is None:
        # 没有位置数据时不能输出「未处于明显高位」，那会被读成「确认不在高位」
        return "位置数据不足，不判断"
    if pos250 is not None and pos250 <= -0.20:
        if pos60 is not None and pos60 >= -0.05:
            if ret20 is not None and ret20 > 0:
                return "历史顶已过，当前反弹接近局部高点"
            return "历史顶已过，当前在局部高位附近"
        return "历史顶已过，当前处于回撤/震荡"
    if pos250 is not None and pos250 >= -0.05:
        return "接近250日高点"
    if pos60 is not None and pos60 >= -0.03:
        return "接近60日局部高点"
    if pos250 is not None and pos250 >= -0.10:
        return "高位小幅回落"
    if pos250 is not None and pos250 >= -0.20:
        return "中高位回落"
    return "未处于明显高位"
