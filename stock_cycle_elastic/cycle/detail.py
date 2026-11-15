"""存进 results.db 的「明细」：看板个股页要摆的三个判断 + 三条指标序列。

原来这些只活在终端报告里（`cycle_report.py -_print_decision` 的几行文字，
以及旧版 HTML 报告里的 SVG 图）。库里的 payload 只有 7 个字段，看板只能摆
一张三行表，看不到"为什么是高位"。

这里把页面要画的那一块抽出来（约 4KB）：

* `judge`：三个判断各一行（结果 / 指标名 / 数值 / 一句话说明）
* `notes`：限定语（走相对波动通道、金融企业、历史不足、产能方向…）
* `series`：`metrics.series_for_chart()` 的三条序列，供页面画折线
* `badges`：标签（周期股 · 相对波动 · 弹性弱 · 高位）

只放展示用的字符串与数；序列最多 40 个季度点，不存原始财报。
"""
from __future__ import annotations

import config as _default_config
from cycle import decide, fmt, metrics

CYCLE_TEXT = {1: "是", 0: "否", "na": "不适用"}
ELASTIC_TEXT = {"strong": "强", "weak": "弱", "unstable": "不稳定", "na": "不适用"}
POSITION_TEXT = {"low": "低位", "neutral": "中位", "high": "高位", "na": "不适用"}


def _num(value, digits: int = 2):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if value != value:                      # NaN
        return None
    return round(float(value), digits)


def build(stock: dict, m: dict, decision: dict, cfg=None) -> dict:
    """`stock`（含 quarters）/ `m`（compute_all）/ `decision`（decide）→ 明细。"""
    cfg = cfg or _default_config
    cycle = decision.get("cycle") or {}
    elastic = decision.get("elastic") or {}
    position = decision.get("position") or {}
    flags = decision.get("flags") or {}

    notes = []
    if flags.get("financial"):
        notes.append("金融企业：产能周转率不适用")
    if flags.get("insufficient_history"):
        notes.append(f"历史不足：{len(stock.get('quarters') or [])} 季")
    if cycle.get("channel") == "cv":
        notes.append(
            f"判断① 走相对波动通道：σ {cycle['gm_std']:.2f}pp 未到 "
            f"{cfg.GM_STD_CYCLE:g}pp，但 CV {cycle['gm_cv']:.0f}% ≥ "
            f"{cfg.GM_CV_MIN:g}%，毛利率中位 {cycle['gm_median']:.1f}%")
    if elastic.get("reason") == "near_breakeven":
        el = m.get("elasticity") or {}
        notes.append(f"弹性不可信：{el.get('zero_cross', 0)}/{el.get('samples', 0)} "
                     "季营业利润跨零，贴近盈亏平衡点")
    if position.get("capacity_direction") in ("expand", "shrink"):
        notes.append(
            f"产能方向：{decide.CAPACITY_DIRECTION_TEXT[position['capacity_direction']]}，"
            f"近 {cfg.CAPACITY_TREND_QUARTERS} 季资产 "
            f"{(position.get('asset_growth') or 0):+.0f}%、营收 "
            f"{(position.get('revenue_growth') or 0):+.0f}%")

    judge = [
        {
            "name": "判断① 是不是周期股",
            "result": CYCLE_TEXT.get(cycle.get("label"), "—"),
            "metric": "毛利率标准差 σ",
            "value": fmt.fmt_pp(cycle.get("gm_std"), lower=cfg.GM_STD_CYCLE),
            "detail": (f"CV {cycle['gm_cv']:.1f}% · 中位 "
                       f"{cycle['gm_median']:.1f}%"
                       if cycle.get("gm_cv") is not None
                       and cycle.get("gm_median") is not None else ""),
            "tone": "warn" if cycle.get("label") == 1 else "muted",
        },
        {
            "name": "判断② 弹性",
            "result": ELASTIC_TEXT.get(elastic.get("label"), "—"),
            "metric": "营业利润/营收 增速比值",
            "value": fmt.fmt_num(elastic.get("ratio"), 2,
                                 lower=cfg.ELASTIC_RATIO_MIN),
            "detail": "利润对收入变化的敏感度；不含财务杠杆",
            "tone": "good" if elastic.get("label") == "strong" else "muted",
        },
        {
            "name": "判断③ 周期位置",
            "result": POSITION_TEXT.get(position.get("label"), "—"),
            "metric": "产能周转率分位",
            "value": fmt.fmt_pct(position.get("pct"),
                                 lower=cfg.POSITION_LOW_PCT,
                                 upper=cfg.POSITION_HIGH_PCT),
            "detail": "只看供给端；分位低 = 产能利用处自身历史偏低区",
            "tone": {"low": "good", "high": "warn"}.get(position.get("label"), "muted"),
        },
    ]

    return {
        "industry": stock.get("industry") or "",
        "judge": judge,
        "notes": notes,
        "badges": [text for _cls, text in (decide.badges(decision) or [])],
        "series": metrics.series_for_chart(stock.get("quarters") or [], cfg),
        "meta": {
            "quarters": len(stock.get("quarters") or []),
            "latest": (decision.get("meta") or {}).get("latest") or "",
        },
        "thresholds": {
            "gm_std": _num(cfg.GM_STD_CYCLE, 2),
            "gm_cv": _num(cfg.GM_CV_MIN, 2),
            "elastic": _num(cfg.ELASTIC_RATIO_MIN, 2),
            "low": _num(cfg.POSITION_LOW_PCT, 2),
            "high": _num(cfg.POSITION_HIGH_PCT, 2),
        },
    }
