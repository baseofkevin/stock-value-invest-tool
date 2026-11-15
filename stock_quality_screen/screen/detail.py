"""结论之外的「明细」：看板个股页要摆的那一块，纯 JSON。

工具算完一只票手里有一份 70KB 的 `data`（财报、行情序列、风险检查、估值分位…），
而 results.db 原来只留了 8 个汇总字段（Q / V / 结论 / 风险命中数）。看板的个股页
因此只能摆一张 4 行的表，看不出"为什么是 54.4 分"。

这里把页面真正要画的那一小块抽出来（约 5KB），跟结论一起写进 payload：

* `q` / `v`：分数、等级、覆盖度、分项（含每项原始值，如「ROE 中位数 6.2%」）
* `risks`：14 条风险检查逐条（分组、名称、状态、严重度、说明）——这是页面最有
  信息量的部分，"命中 1 项"到底是哪一项必须能点开看
* `valuation`：PE/PB/PS 当前值 + 自身历史分位
* `forecast`：机构目标价 + 三个预测年度
* `tracking`：跟踪清单（指标 / 当前值 / 阈值 / 频率）

只放**展示用的字符串与数**，不放原始序列（那是 454 个周点的数组，页面不画它）。
旧行没有这个键，页面降级成一句话（"这条结果没有明细，重跑一次就有了"）。
"""
from __future__ import annotations

from typing import Any

STATUS_ORDER = {"hit": 0, "watch": 1, "unknown": 2, "clear": 3, "na": 4}
STATUS_LABEL = {"hit": "命中", "watch": "提示", "clear": "通过",
                "unknown": "数据不足", "na": "不适用"}


def _num(value, digits: int = 1):
    """四舍五入到 digits 位；None/NaN 原样给 None（页面上显示 —）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if value != value:                     # NaN
        return None
    return round(float(value), digits)


def _items(parts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for p in parts or []:
        out.append({
            "label": p.get("label") or "",
            "score": _num(p.get("score")),
            "max": _num(p.get("max"), 2),
            "raw": p.get("raw") or "",
        })
    return out


def _v_parts(parts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """V 的分项跟 Q 的**量纲不同**：V 的 `score` 是 0–100 的百分制，`max` 是权重
    （V 总分 = Σ score×权重/100）。所以这里拆成 score/weight 两个键，别让页面
    拿 score 去比 max（会画出 84.8/40 这种超过满分的条）。"""
    out = []
    for p in parts or []:
        out.append({
            "label": p.get("label") or "",
            "score": _num(p.get("score")),
            "weight": _num(p.get("max"), 2),
            "raw": p.get("raw") or "",
        })
    return out


def _groups(groups: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for g in groups or []:
        out.append({
            "label": g.get("label") or "",
            "score": _num(g.get("score")),
            "max": _num(g.get("max"), 2),
            "coverage": _num(g.get("coverage"), 0),
            "subs": _items(g.get("items")),
        })
    return out


def _risks(risks: dict[str, Any] | None) -> list[dict[str, Any]]:
    checks = list((risks or {}).get("checks") or [])
    # 命中>提示>数据不足>通过：页面上要人先看到坏的
    checks.sort(key=lambda c: STATUS_ORDER.get(c.get("status") or "", 9))
    return [{
        "group": c.get("group") or "",
        "name": c.get("name") or "",
        "status": c.get("status") or "",
        "status_label": STATUS_LABEL.get(c.get("status") or "", c.get("status") or ""),
        "severity": c.get("severity") or "",
        "detail": c.get("detail") or "",
    } for c in checks]


def _valuation(val: dict[str, Any] | None) -> dict[str, Any]:
    val = val or {}
    metrics = val.get("metrics") or {}
    rows = []
    for key in ("pe", "pb", "ps"):
        m = metrics.get(key) or {}
        if m.get("current") is None:
            continue
        rows.append({
            "label": m.get("label") or key.upper(),
            "current": _num(m.get("current"), 2),
            "percentile": _num(m.get("percentile"), 0),
            "min": _num(m.get("min"), 2),
            "max": _num(m.get("max"), 2),
        })
    sample = val.get("sample") or {}
    return {
        "rows": rows,
        "sample": f"{sample.get('start') or '—'} 至 {sample.get('end') or '—'}"
                  f" · {sample.get('points') or 0} 个周点",
    }


def _forecast(fc: dict[str, Any] | None) -> dict[str, Any]:
    fc = fc or {}
    rows = []
    for f in (fc.get("forecasts") or [])[:3]:
        rows.append({
            "year": f.get("year"),
            "eps": _num(f.get("eps"), 2),
            "pe": _num(f.get("pe"), 2),
            "pb": _num(f.get("pb"), 2),
            "profit_yi": _num(f.get("net_profit_yi"), 1),
            "profit_yoy": _num(f.get("net_profit_yoy"), 1),
        })
    return {"available": bool(fc.get("available")), "target_price": _num(fc.get("target_price"), 2),
            "rows": rows}


def _historical(h: dict[str, Any] | None) -> dict[str, Any]:
    """历史分位参考分只给页面展示，不进 score/decision。"""
    h = h or {}
    return {
        "available": bool(h.get("available")),
        "score": _num(h.get("score")),
        "grade": h.get("grade") or "",
        "verdict": h.get("verdict") or "",
        "median_percentile": _num(h.get("median_percentile")),
        "sample_weeks": h.get("sample_weeks") or 0,
        "spread": _num(h.get("spread")),
        "metrics": [{
            "key": m.get("key") or "",
            "label": m.get("label") or "",
            "percentile": _num(m.get("percentile")),
        } for m in (h.get("metrics") or [])],
        "reason": h.get("reason") or "",
    }


def build_detail(data: dict[str, Any] | None) -> dict[str, Any]:
    """`data`（compute_scores 的输出）→ 页面用的明细 dict。"""
    if not data:
        return {}
    meta = data.get("meta") or {}
    q = data.get("quality") or {}
    v = data.get("valuation_score") or {}
    risks = data.get("risks") or {}
    dq = data.get("data_quality") or {}
    dec = data.get("decision") or {}
    counts: dict[str, int] = {}
    for c in risks.get("checks") or []:
        key = c.get("status") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return {
        "meta": {
            "industry": meta.get("industry") or "",
            "business": meta.get("business") or "",
            "price": _num(meta.get("price"), 2),
            "price_basis": meta.get("price_basis") or "",
            "mcap_yi": _num(meta.get("market_cap_yi"), 0),
        },
        "q": {
            "score": _num(q.get("score")),
            "grade": q.get("grade") or "",
            "coverage": _num(q.get("coverage"), 0),
            "groups": _groups(q.get("groups")),
        },
        "v": {
            "score": _num(v.get("score")),
            "grade": v.get("grade") or "",
            "coverage": _num(v.get("coverage"), 0),
            "parts": _v_parts(v.get("parts")),
            "historical": _historical(v.get("historical")),
        },
        "risks": _risks(risks),
        "risk_counts": counts,
        "fatal": list(risks.get("fatal") or data.get("fatal") or []),
        "valuation": _valuation(data.get("valuation")),
        "forecast": _forecast(data.get("forecast")),
        "tracking": [{
            "metric": t.get("metric") or "",
            "current": t.get("current") or "",
            "threshold": t.get("threshold") or "",
            "frequency": t.get("frequency") or "",
        } for t in (data.get("tracking") or [])],
        "data_quality": {
            "risks_known": dq.get("risk_known"),
            "risks_total": dq.get("risk_total"),
            "coverage": _num(dq.get("q_coverage"), 0),
            "institution": dq.get("institution_coverage"),
            "blockers": list(dq.get("buy_blockers") or []),
        },
        "decision": {"label": dec.get("label") or "",
                     "reason": dec.get("reason") or ""},
    }
