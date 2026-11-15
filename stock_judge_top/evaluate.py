"""单股评估编排与结果组装。

迁移说明（2026-11-15）：取数入口从 `load_westock_module()` + `fetch_kline(wk, ...)`
换成共享库 hub + `data.fetch(hub, ...)`；`_kline_error` 里的存在性检查从
`data_quote` 换成 profile 域（stock 表）。四因子逻辑、结果结构一字未动。
"""
from .common.values import fnum
from .data import fetch
from .finance import fin_metrics
from .indicators import kline_metrics, market_phase
from .judgement import earnings_judgement, qoq_judgement
from .log import log
from .render.format import fmt_pct, fmt_pct_frac, fmt_pct_point
from .views import build_views


def _log_kline(km):
    log(f"[计算] K线因子：5日 {fmt_pct(km['ret5'])} | 20日 {fmt_pct(km['ret20'])} | "
        f"60日 {fmt_pct(km['ret60'])} | 距250日高点 {fmt_pct(km['pos250'])}")
    if km["av20"] is not None and km["std20"] is not None and km["ratio"] is not None:
        log(f"[计算] 换手因子：20日均 {km['av20']:.2f}% | "
            f"标准差 {km['std20']:.2f} | 1月/1年 {km['ratio']:.2f}")
    else:
        log("[计算] 换手因子：数据不完整，换手历史不足")
    hi5, lo5 = km.get("range5_high"), km.get("range5_low")
    range5 = (hi5 / lo5 - 1) if (hi5 and lo5) else None
    log(f"[位置] {market_phase(km)} | 距60日高点 {fmt_pct(km.get('pos60'))}"
        f"（{km.get('high60_date') or '—'}） | 距250日高点 {fmt_pct(km.get('pos250'))}")
    vr = km.get("pvol_ratio5")
    log(f"[量价] 5日/20日量比 {vr:.2f}" if vr is not None else "[量价] 5日/20日量比 —")
    log(f"       5日涨跌 {fmt_pct(km.get('ret5'))} | "
        f"5日振幅 {fmt_pct(range5)} | 距60日高点 {fmt_pct(km.get('pos60'))}")


def _kline_error(code, nodes, prof):
    """K 线不可用时的报错：说清是「没这只票」「停牌」还是「字段缺失/根数不够」。"""
    if not nodes:
        if not prof:
            raise RuntimeError(
                f"{code} 无行情数据：请检查代码是否存在；"
                "基金/ETF、退市或停牌股票可能不在覆盖范围内。"
                "A股代码示例：sh600519 / sz300750")
        raise RuntimeError(f"{code} 当前无 K 线（可能停牌），无法计算短期因子")
    # 区分「根数不足」与「字段缺失」，否则缺 high 会报成「K线仅 725 根」
    if any(fnum(x.get("high")) is None for x in nodes):
        raise RuntimeError(
            f"{code} 的日K缺少 high 字段（{len(nodes)} 根中有空值），"
            "无法计算 250 日高点与位置分位；请加 --refresh 重拉")
    if any(fnum(x.get("last")) is None for x in nodes):
        raise RuntimeError(
            f"{code} 的日K缺少 last（收盘）字段，无法计算短期因子；"
            "请加 --refresh 重拉")
    raise RuntimeError(
        f"{code} 的 K 线仅 {len(nodes)} 根，至少需要 30 根（约 6 周）"
        "才能计算短期因子；新股/次新股暂不适用")


def evaluate(hub, code, end, refresh, with_quote=True):
    nodes, fin_rows, prof = fetch(hub, code, end, refresh)
    km = kline_metrics(nodes)
    if km is None:
        _kline_error(code, nodes, prof)

    _log_kline(km)

    fm = fin_metrics(fin_rows, end)
    # quote 快照：profile 域的静态行（name/industry/list_date）。
    # 原版这里是 data_quote 的实时快照；共享库里"当期行情"归 quote 域，
    # 但四因子与展示只用到了 name，取 profile 一份就够，省一次请求。
    quote = dict(prof) if with_quote else {}

    # 四因子只做独立观察：不打分、不算加权总分、不产出 verdict/summary/position。
    ej = earnings_judgement(fm)
    qj = qoq_judgement(fm)
    if fm:
        log(f"[业绩] 最新 {fm['latest_end']} | 单季同比 "
            f"{fmt_pct_point(fm['latest_yoy'])} | 上季 "
            f"{fmt_pct_point(fm.get('prev_yoy'))} | 加速度 "
            f"{fmt_pct_frac(fm.get('accel'))} | 状态 {ej['state']}")
        log(f"[业绩] 环比 {fmt_pct_frac(fm.get('qoq'))} | "
            f"上季环比 {fmt_pct_frac(fm.get('qoq_prev'))} | "
            f"环比加速度 {fmt_pct_frac(fm.get('qoq_accel'))} | 状态 {qj['state']}")
    else:
        log("[业绩] 无可用财务数据，业绩卡片不判断")

    warnings = []
    if km["bars"] < 250:
        warnings.append(f"K线仅 {km['bars']} 根，不足 250 日；位置类指标只作参考")
    if nodes and all(n.get("exchange") is None for n in nodes):
        warnings.append("流通市值缺失，换手类指标不可用（av20/ratio/AVG20 显示 —）")
    if not fm:
        warnings.append("无可用财务数据，业绩卡片不判断")
    elif fm.get("accel") is None:
        warnings.append("最近两个季度增速不连续，业绩卡片不判断")
    elif fm.get("stale_days") and fm["stale_days"] > 200:
        warnings.append(f"最新财报已 {fm['stale_days']} 天，业绩背景偏旧")
    if km.get("below_ma20"):
        warnings.append("当前收盘已跌破 MA20（仅作事实提示）")

    log("[完成] 4 个因子独立计算（不合并总分）")

    result = {
        "code": code, "name": quote.get("name") or "",
        "date": end, "close": km["close"],
        "high250": km["high250"], "high250_date": km["high250_date"],
        "days_since_high": km["days_since_high"], "pos250": km["pos250"],
        "pos_basis": km["pos_basis"],
        "phase": market_phase(km),
        "local": {
            "pos20": km.get("pos20"), "pos60": km.get("pos60"),
            "pos120": km.get("pos120"),
            "high20_date": km.get("high20_date"),
            "high60_date": km.get("high60_date"),
            "high120_date": km.get("high120_date"),
        },
        "kline": km, "finance": fm,
        "quote": quote, "warnings": warnings,
        "earnings": {
            "state": ej["state"], "direction": ej["direction"],
            "is_accelerating": ej["is_accelerating"],
            "is_decelerating": ej["is_decelerating"],
            "tag": ej["tag"], "note": ej["note"],
            "qoq": fm.get("qoq") if fm else None,
            "qoq_prev": fm.get("qoq_prev") if fm else None,
            "qoq_accel": fm.get("qoq_accel") if fm else None,
            "qoq_state": qj["state"], "qoq_direction": qj["direction"],
            "qoq_is_accelerating": qj["is_accelerating"],
            "qoq_is_decelerating": qj["is_decelerating"],
            "qoq_tag": qj["tag"], "qoq_note": qj["note"],
        },
    }
    result["views"] = build_views(result)
    return result
