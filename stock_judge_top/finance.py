"""财务指标与季度口径。"""
import datetime as dt

from .common.values import fnum, pdate

REVENUE_FIELDS = ("OperatingRevenue_Q", "TotalOperatingRevenue_Q",
                  "OperatingRevenue", "TotalOperatingRevenue")


def prev_quarter(end_date):
    y, m = int(end_date[:4]), int(end_date[5:7])
    if m == 3:
        return f"{y - 1}-12-31"
    if m == 6:
        return f"{y}-03-31"
    if m == 9:
        return f"{y}-06-30"
    if m == 12:
        return f"{y}-09-30"
    return None


def year_ago(end_date):
    y, m = int(end_date[:4]), int(end_date[5:7])
    return f"{y - 1}-{m:02d}-{end_date[8:10]}"


def relative_accel(cur, prev, min_abs_base):
    """相对加速度 = (本期增速 - 上期增速) / |上期增速|。

    用绝对值作分母，避免上期负增长时方向反转；上期增速接近 0 时不可比。
    返回 (值, 说明)；值用小数表示，0.5 = 50%。
    """
    if cur is None or prev is None:
        return None, "缺少上一期增速，无法计算相对加速度"
    if abs(prev) < min_abs_base:
        return None, "上期增速接近 0，相对加速度不可比"
    note = "上期增速为负，相对加速度只作参考" if prev < 0 else ""
    return (cur - prev) / abs(prev), note


def _first_num(row, keys):
    """按顺序取第一个「有值」的字段。

    不能用 `a or b or c`：字段值可能真的是 0，`0` 会被跳过并落到累计口径上。
    """
    for k in keys:
        v = fnum(row.get(k))
        if v is not None:
            return v
    return None


def _dedup_by_end(rows):
    """同一报告期可能有多条（快报/更正/重述），保留披露日最新的那条。"""
    best = {}
    for r in rows:
        end = pdate(r["EndDate"])
        pub = pdate(r.get("InfoPublDate")) or ""
        if end not in best or pub > best[end][0]:
            best[end] = (pub, r)
    return [best[e][1] for e in sorted(best)]


def fin_metrics(rows, end):
    valid = [r for r in rows
             if pdate(r.get("InfoPublDate")) and pdate(r.get("EndDate"))
             and pdate(r.get("InfoPublDate")) <= end]
    valid = _dedup_by_end(valid)

    seq = []
    for r in valid:
        y = fnum(r.get("NPParentCompanyYOY_Q"))
        if y is None:
            continue
        profit_q = fnum(r.get("NPParentCompanyOwners_Q"))
        seq.append({
            "end": pdate(r["EndDate"]),
            "pub": pdate(r.get("InfoPublDate")),
            "yoy": y,
            "cut": fnum(r.get("NPParentCompanyCutYOY_Q")),
            "profit_q": profit_q,
            "profit": profit_q if profit_q is not None else fnum(r.get("NPParentCompanyOwners")),
            "revenue_q": _first_num(r, REVENUE_FIELDS),
            "revenue_yoy": fnum(r.get("OperatingRevenueGrowRate_Q")),
        })
    if not seq:
        return None

    latest = seq[-1]
    prev = None
    pq = prev_quarter(latest["end"])
    if pq:
        prev = next((x for x in seq if x["end"] == pq), None)

    prev2 = None
    if prev:
        pq2 = prev_quarter(prev["end"])
        prev2 = next((x for x in seq if x["end"] == pq2), None) if pq2 else None

    yoy_base_end = year_ago(latest["end"])
    yoy_base = next((x for x in seq if x["end"] == yoy_base_end), None)
    prev_yoy_base_end = year_ago(prev["end"]) if prev else None
    prev_yoy_base = (next((x for x in seq if x["end"] == prev_yoy_base_end), None)
                     if prev_yoy_base_end else None)

    accel_pp = (latest["yoy"] - prev["yoy"]) if prev else None
    accel, accel_note = relative_accel(
        latest["yoy"], prev["yoy"] if prev else None, 1.0)

    accel_cut = None
    if prev and latest.get("cut") is not None and prev.get("cut") is not None:
        accel_cut, _ = relative_accel(latest["cut"], prev["cut"], 1.0)

    stale_days = None
    if latest.get("pub"):
        try:
            stale_days = (dt.date.fromisoformat(end)
                          - dt.date.fromisoformat(latest["pub"])).days
        except ValueError:
            pass

    qoq = qoq_prev = qoq_accel = qoq_accel_pp = None
    qoq_note = ""
    qoq_accel_note = ""
    if prev is None:
        qoq_note = "缺少上一季度数据，环比不可比"
    elif latest.get("profit_q") is None or prev.get("profit_q") is None:
        qoq_note = "单季利润缺失，环比不可比"
    elif latest["profit_q"] <= 0 or prev["profit_q"] <= 0:
        qoq_note = "本季或上季利润≤0，环比不可比"
    else:
        qoq = latest["profit_q"] / prev["profit_q"] - 1
        if prev2 and prev2.get("profit_q") is not None and prev2["profit_q"] > 0:
            qoq_prev = prev["profit_q"] / prev2["profit_q"] - 1
            qoq_accel_pp = (qoq - qoq_prev) * 100
            qoq_accel, qoq_accel_note = relative_accel(qoq, qoq_prev, 0.01)
            if qoq_accel is None:
                qoq_note = qoq_accel_note
        else:
            qoq_note = "缺少上上季度数据，无法判断环比是否加速"

    revenue_qoq = None
    if (latest.get("revenue_q") is not None and prev
            and prev.get("revenue_q") not in (None, 0)):
        revenue_qoq = latest["revenue_q"] / prev["revenue_q"] - 1

    history = []
    for i, x in enumerate(seq):
        a = None
        if i > 0 and seq[i - 1]["end"] == prev_quarter(x["end"]):
            a = x["yoy"] - seq[i - 1]["yoy"]
        q = None
        if i > 0 and seq[i - 1]["end"] == prev_quarter(x["end"]):
            p0, p1 = seq[i - 1].get("profit_q"), x.get("profit_q")
            if p0 is not None and p1 is not None and p0 > 0 and p1 > 0:
                q = p1 / p0 - 1
        qa = None
        if q is not None and history and history[-1].get("qoq") is not None:
            qa = (q - history[-1]["qoq"]) * 100
        history.append({
            "end": x["end"], "pub": x["pub"], "yoy": x["yoy"],
            "cut": x["cut"], "accel": a, "qoq": q, "qoq_accel": qa,
        })

    return {
        "latest_end": latest["end"], "latest_pub": latest["pub"],
        "latest_yoy": latest["yoy"], "prev_yoy": prev["yoy"] if prev else None,
        "accel": accel, "accel_pp": accel_pp, "accel_note": accel_note,
        "accel_cut": accel_cut,
        # 单季口径缺失时回退到累计口径，只用于「利润是否为负」这类粗判断
        "profit": latest.get("profit"),
        "profit_q": latest.get("profit_q"),
        "prev_profit_q": prev.get("profit_q") if prev else None,
        "prev2_profit_q": prev2.get("profit_q") if prev2 else None,
        "prev_end": prev["end"] if prev else None,
        "yoy_base_end": yoy_base_end,
        "yoy_base_profit_q": yoy_base.get("profit_q") if yoy_base else None,
        "prev_yoy_base_end": prev_yoy_base_end,
        "prev_yoy_base_profit_q": (prev_yoy_base.get("profit_q")
                                   if prev_yoy_base else None),
        "latest_revenue": latest.get("revenue_q"),
        "latest_revenue_yoy": latest.get("revenue_yoy"),
        "prev_revenue": prev.get("revenue_q") if prev else None,
        "yoy_base_revenue": yoy_base.get("revenue_q") if yoy_base else None,
        "revenue_qoq": revenue_qoq,
        "qoq": qoq, "qoq_prev": qoq_prev,
        "qoq_accel": qoq_accel, "qoq_accel_pp": qoq_accel_pp,
        "qoq_note": qoq_note, "qoq_accel_note": qoq_accel_note,
        "stale_days": stale_days, "n_reports": len(seq),
        "history": history[-6:],
    }
