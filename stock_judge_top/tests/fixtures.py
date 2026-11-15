"""测试夹具：合成日 K 与财务数据。

所有测试都用这里造的数据，不联网、不读 cache/、不依赖任何真实行情，
所以「程序算错」和「数据变了」能分开看。
"""
import datetime as dt


def trading_days(n, end="2026-10-23"):
    """生成 n 个连续工作日（只跳过周末，不做节假日判断）。"""
    d = dt.date.fromisoformat(end)
    days = []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return list(reversed(days))


def kline(n=250, base=10.0, step=0.1, wick=0.05,
          exchange="0.50", volume=1000.0, end="2026-10-23"):
    """等差上涨的日 K：close[i] = base + step*i，high = close + wick。

    用等差序列是为了让期望值能手算：
    n=250 时 close 从 10.00 涨到 34.90，high 最高 34.95，换手恒为 0.50%。
    """
    out = []
    for i, day in enumerate(trading_days(n, end)):
        close = round(base + step * i, 4)
        out.append({
            "date": day,
            "open": close,
            "last": close,
            "high": round(close + wick, 4),
            "low": round(close - wick, 4),
            "volume": volume,
            "amount": volume * close,
            "exchange": exchange,        # 接口返回的是字符串，fnum 负责转换
        })
    return out


def fin_row(end, pub, yoy, profit_q, revenue_q=None, rev_yoy=None, cut=None):
    """一条财务记录，字段名与 data_finance 返回一致（值多为字符串）。"""
    def s(v):
        return None if v is None else f"{v}"
    return {
        "EndDate": f"{end} 00:00:00 +0800 CST",
        "InfoPublDate": f"{pub} 00:00:00 +0800 CST",
        "NPParentCompanyYOY_Q": s(yoy),
        "NPParentCompanyCutYOY_Q": s(cut),
        "NPParentCompanyOwners": s(None if profit_q is None else profit_q * 2),
        "NPParentCompanyOwners_Q": s(profit_q),
        "OperatingRevenue_Q": s(revenue_q),
        "OperatingRevenueGrowRate_Q": s(rev_yoy),
    }


def quarter_rows():
    """六个季度，用来验证同比/环比/加速度：最新 2026-06-30 同比 20%、上季 10%。"""
    return [
        fin_row("2025-03-31", "2025-04-25", 10, 70, 700, 8),
        fin_row("2025-06-30", "2025-08-20", 12, 80, 800, 9),
        fin_row("2025-09-30", "2025-10-25", 14, 90, 900, 10),
        fin_row("2025-12-31", "2026-03-25", 16, 100, 1000, 11),
        fin_row("2026-03-31", "2026-04-25", 10, 80, 800, 12),
        fin_row("2026-06-30", "2026-08-15", 20, 100, 900, 13),
    ]


def kline_dict(**over):
    """kline_metrics() 的产物形态，给视图/渲染测试直接构造用。"""
    d = {
        "bars": 250, "date": "2026-10-23", "close": 1335.0, "pos_basis": "high",
        "ret5": 0.0045, "ret20": 0.0537, "ret60": 0.0333,
        "av5": 0.33, "av20": 0.2705, "av60": 0.2705, "av250": 0.31972,
        "std20": 0.0771, "ratio": 0.846,
        "high20": 1348.6, "high20_date": "2026-10-19",
        "days_since_high20": 4, "pos20": -0.0101,
        "high60": 1363.35, "high60_date": "2026-08-03",
        "days_since_high60": 53, "pos60": -0.0208,
        "high120": 1391.98, "high120_date": "2026-04-27",
        "days_since_high120": 119, "pos120": -0.0409,
        "high250": 1539.98, "high250_date": "2026-02-06",
        "days_since_high": 168, "pos250": -0.1331,
        "vol_ratio5": 1.2274, "pav5": 41511.2, "pav20": 33820.5, "pvol_ratio5": 1.2274,
        "pct": {"AVG20": 61.0}, "pct_n": {"AVG20": 231},
        "close_5_ago": 1329.0, "close_20_ago": 1266.98, "close_60_ago": 1292.01,
        "range5_high": 1340.0, "range5_low": 1320.0,
        "ma20": 1320.0, "ma60": 1300.0, "below_ma20": False,
    }
    d.update(over)
    return d


def finance_dict(**over):
    """fin_metrics() 的产物形态。"""
    d = {
        "latest_end": "2026-06-30", "latest_pub": "2026-08-15",
        "latest_yoy": 20.0, "prev_yoy": 10.0,
        "accel": 1.0, "accel_pp": 10.0, "accel_note": "", "accel_cut": None,
        "profit": 100e8, "profit_q": 100e8,
        "prev_profit_q": 80e8, "prev2_profit_q": 90e8, "prev_end": "2026-03-31",
        "yoy_base_end": "2025-06-30", "yoy_base_profit_q": 80e8,
        "prev_yoy_base_end": "2025-03-31", "prev_yoy_base_profit_q": 70e8,
        "latest_revenue": 900e8, "latest_revenue_yoy": 13.0,
        "prev_revenue": 800e8, "yoy_base_revenue": 800e8,
        "revenue_qoq": 0.125,
        "qoq": 0.25, "qoq_prev": -0.2, "qoq_accel": 2.25, "qoq_accel_pp": 45.0,
        "qoq_note": "", "qoq_accel_note": "",
        "stale_days": 69, "n_reports": 6, "history": [],
    }
    d.update(over)
    return d


def earnings_dict(**over):
    d = {
        "state": "强加速", "direction": "加速",
        "is_accelerating": True, "is_decelerating": False,
        "tag": "【强加速】", "note": "相对加速度 +100.0%（≥+50%）",
        "qoq": 0.25, "qoq_prev": -0.2, "qoq_accel": 2.25,
        "qoq_state": "强加速", "qoq_direction": "加速",
        "qoq_is_accelerating": True, "qoq_is_decelerating": False,
        "qoq_tag": "【强加速】", "qoq_note": "",
    }
    d.update(over)
    return d


def result(**over):
    """evaluate() 的返回形态，带四个视图。"""
    from stock_judge_top.views import build_views
    r = {
        "code": "sh600519", "name": "贵州茅台", "date": "2026-10-23",
        "close": 1335.0, "high250": 1539.98, "high250_date": "2026-02-06",
        "days_since_high": 168, "pos250": -0.1331, "pos_basis": "high",
        "phase": "中高位回落",
        "local": {"pos20": -0.0101, "pos60": -0.0208, "pos120": -0.0409,
                  "high20_date": "2026-10-19", "high60_date": "2026-08-03",
                  "high120_date": "2026-04-27"},
        "kline": kline_dict(), "finance": finance_dict(),
        "quote": {"name": "贵州茅台"}, "warnings": [],
        "earnings": earnings_dict(),
    }
    r.update(over)
    r["views"] = build_views(r)
    return r
