"""业绩卡片（同比 / 环比 / 营收）。"""
from ..finance import prev_quarter
from ..render.format import fmt_pct_frac, fmt_pct_point
from .common import _fmt_yi, _insufficient


def earnings_view(r):
    fm = r.get("finance")
    ej = r.get("earnings") or {}
    # 3. 业绩：同比、环比、营收三块分开，原始数据与结果放在一起
    earn_calc = []
    earn_blocks = []
    if fm:
        latest_end = fm.get("latest_end") or "—"
        latest_pub = fm.get("latest_pub") or "披露日未知"
        prev_end = fm.get("prev_end") or "—"
        prev2_end = None
        # 上上季报告期：从上一季往前推一个季度
        if fm.get("prev_end"):
            prev2_end = prev_quarter(fm["prev_end"]) or "—"
        yoy_base_end = fm.get("yoy_base_end") or "—"
        prev_base_end = fm.get("prev_yoy_base_end") or "—"

        earn_blocks = [
            {
                "title": "同比", "subtitle": "单季归母净利润",
                "tone": "warn" if (fm.get("accel") is not None and fm["accel"] <= -0.10) else (
                    "good" if (fm.get("accel") is not None and fm["accel"] >= 0.10) else "info"),
                "results": [
                    ("本季同比", fmt_pct_point(fm.get("latest_yoy"))),
                    ("上季同比", fmt_pct_point(fm.get("prev_yoy"))),
                    ("同比加速度", fmt_pct_frac(fm.get("accel"))),
                ],
                "sources": [
                    (f"本季 {latest_end}", _fmt_yi(fm.get("profit_q"))),
                    (f"上年同期 {yoy_base_end}", _fmt_yi(fm.get("yoy_base_profit_q"))),
                    (f"上季 {prev_end}", _fmt_yi(fm.get("prev_profit_q"))),
                    (f"上季上年同期 {prev_base_end}", _fmt_yi(fm.get("prev_yoy_base_profit_q"))),
                ],
                "formulas": [
                    (f"本季 {_fmt_yi(fm.get('profit_q'))} ÷ 上年同期 "
                     f"{_fmt_yi(fm.get('yoy_base_profit_q'))} − 1 = "
                     f"{fmt_pct_point(fm.get('latest_yoy'))}"),
                    (f"上季 {_fmt_yi(fm.get('prev_profit_q'))} ÷ 上年同期 "
                     f"{_fmt_yi(fm.get('prev_yoy_base_profit_q'))} − 1 = "
                     f"{fmt_pct_point(fm.get('prev_yoy'))}"),
                    (f"({fmt_pct_point(fm.get('latest_yoy'))} − "
                     f"{fmt_pct_point(fm.get('prev_yoy'))}) ÷ "
                     f"|{fmt_pct_point(fm.get('prev_yoy'))}| = "
                     f"{fmt_pct_frac(fm.get('accel'))}"),
                ],
            },
            {
                "title": "环比", "subtitle": "单季归母净利润",
                "tone": "warn" if (fm.get("qoq_accel") is not None and fm["qoq_accel"] <= -0.10) else (
                    "good" if (fm.get("qoq_accel") is not None and fm["qoq_accel"] >= 0.10) else "info"),
                "results": [
                    ("本季环比", fmt_pct_frac(fm.get("qoq"))),
                    ("上季环比", fmt_pct_frac(fm.get("qoq_prev"))),
                    ("环比加速度", fmt_pct_frac(fm.get("qoq_accel"))),
                ],
                "sources": [
                    (f"本季 {latest_end}", _fmt_yi(fm.get("profit_q"))),
                    (f"上季 {prev_end}", _fmt_yi(fm.get("prev_profit_q"))),
                    (f"上上季 {prev2_end}", _fmt_yi(fm.get("prev2_profit_q"))),
                ],
                "formulas": [
                    (f"本季 {_fmt_yi(fm.get('profit_q'))} ÷ 上季 "
                     f"{_fmt_yi(fm.get('prev_profit_q'))} − 1 = "
                     f"{fmt_pct_frac(fm.get('qoq'))}"),
                    (f"上季 {_fmt_yi(fm.get('prev_profit_q'))} ÷ 上上季 "
                     f"{_fmt_yi(fm.get('prev2_profit_q'))} − 1 = "
                     f"{fmt_pct_frac(fm.get('qoq_prev'))}"),
                    (f"({fmt_pct_frac(fm.get('qoq'))} − "
                     f"{fmt_pct_frac(fm.get('qoq_prev'))}) ÷ "
                     f"|{fmt_pct_frac(fm.get('qoq_prev'))}| = "
                     f"{fmt_pct_frac(fm.get('qoq_accel'))}"),
                ],
            },
            {
                "title": "营收", "subtitle": "单季营业收入（不是累计）",
                "tone": "info",
                "results": [
                    ("本季营收", _fmt_yi(fm.get("latest_revenue"))),
                    ("营收同比", fmt_pct_point(fm.get("latest_revenue_yoy"))),
                    ("营收环比", fmt_pct_frac(fm.get("revenue_qoq"))),
                ],
                "sources": [
                    (f"本季 {latest_end}", _fmt_yi(fm.get("latest_revenue"))),
                    (f"上年同期 {yoy_base_end}", _fmt_yi(fm.get("yoy_base_revenue"))),
                    (f"上季 {prev_end}", _fmt_yi(fm.get("prev_revenue"))),
                ],
                "formulas": [
                    (f"本季 {_fmt_yi(fm.get('latest_revenue'))} ÷ 上年同期 "
                     f"{_fmt_yi(fm.get('yoy_base_revenue'))} − 1 = "
                     f"{fmt_pct_point(fm.get('latest_revenue_yoy'))}"),
                    (f"本季 {_fmt_yi(fm.get('latest_revenue'))} ÷ 上季 "
                     f"{_fmt_yi(fm.get('prev_revenue'))} − 1 = "
                     f"{fmt_pct_frac(fm.get('revenue_qoq'))}"),
                ],
            },
        ]

        # 给 JSON/命令行保留一份扁平数据，但 HTML 优先使用 earn_blocks
        earn_calc = [
            ("报告期", f"{latest_end}（{latest_pub} 披露）"),
            ("加速口径", "单季归母净利润（不是营业收入）"),
        ]

    if not fm:
        return _insufficient(
            "earnings", "业绩", "同比 / 环比 / 营收",
            "无可用财务数据，业绩背景不判断。", earn_calc)
    else:
        yoy_d = ej.get("direction") or "未知"
        qoq_d = ej.get("qoq_direction") or "未知"
        if fm.get("profit_q") is not None and fm["profit_q"] <= 0:
            state, tone = "盈利为负", "muted"
            note = "单季归母净利润为负，同比/环比加速度参考意义弱；只作背景展示。"
        elif any("加速" in str(x) for x in (yoy_d, qoq_d)):
            state, tone = "加速", "good"
            note = "同比或环比在加速。业绩加速是基本面背景，不是顶部信号，也不计入顶部判断。"
        elif any("减速" in str(x) for x in (yoy_d, qoq_d)):
            state, tone = "减速", "warn"
            note = "同比或环比在减速。它说明基本面动能走弱，但不等于股价已经见顶。"
        elif fm.get("accel") is None and fm.get("qoq_accel") is None:
            state, tone = "数据不足", "muted"
            note = "最近季度不连续或缺少基数，加速度不可用；只展示原始数据。"
        else:
            state, tone = "持平", "info"
            note = "同比/环比变化不大。业绩背景中性，不构成顶部判断。"
        detail = []
        if fm.get("accel_note"):
            detail.append(fm["accel_note"])
        if fm.get("qoq_accel_note"):
            detail.append(fm["qoq_accel_note"])
        if fm.get("accel") is not None and abs(fm["accel"]) > 5.0:
            detail.append("上期增速基数较小，相对加速度百分比会被放大，需结合原始利润看")
        if detail:
            note += " " + "；".join(dict.fromkeys(detail)) + "。"
        return {
            "key": "earnings", "title": "业绩",
            "subtitle": "同比 / 环比 / 营收（背景）",
            "state": state, "tone": tone,
            "headline": (
                f"最新报告期 {fm.get('latest_end') or '—'}，"
                f"单季归母净利润同比 {fmt_pct_point(fm.get('latest_yoy'))}"
            ),
            "metrics": [],
            "calc": [],
            "blocks": earn_blocks,
            "note": note,
            "wide": True,
        }
