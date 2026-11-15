"""综合评估：把四个工具对同一只票的结论并排，按**显式规则**分档、排序。

为什么要这一层
--------------
四个工具各说各的：选股管"有没有启动"，周期管"是不是周期股、在哪个位置"，
质量管"公司好不好、贵不贵"，顶部管"离一年高点还有多远"。真正要买的时候
得把这几句话放在一起看，而且**顺序**很关键——先排除、再谈便宜、最后看位置。

这里刻意**不做一个加权总分**。加权分的问题是：一个致命风险可以和一堆小优点
互相抵消，而"风险检查命中了"这种事在买入决策里是**一票否决**，不能抵消。
所以规则是：硬门槛（一票否决）→ 逐条差距 → 分档 → 档内按"木桶"排序。

一句话：**能不能买，先看有没有被否决；买不买得下手，看质量和估值是否都够；
什么时候买，看周期位置和离一年高点还有多少空间。**

档位
----
    优先研究   四条都通过，可以直接进你的研究清单
    观察       过了质量硬门槛，但还差 1 条以上——差什么写在「差距」列里
    排除       质量已否决（Q < 45 / 命中致命风险 / 工具结论就是回避）
    待补数据   质量没结论（V 缺、数据缺口太大），先补齐数据再谈

排在同一档里怎么排
------------------
按 `min(Q, V)` 从高到低——**木桶原则**：两者都高的排前面，只有一项高不占便宜。
这也是"不合并总分"的另一种写法：不求和、不加权，只看短板。
"""
from __future__ import annotations

from .db import inner

# ---------------------------------------------------------------- 门槛

#: 质量硬门槛，与 `stock_quality_screen` 的 `Q < 45 一律回避` 同一个数。
#: 这里是**读**它的结论，绝不比工具更宽松。
Q_HARD = 45.0

#: Q 到这个数才算"站得住"。45～55 之间是刚过线，不当成优选。
Q_GOOD = 55.0

#: 质量与估值的互换表：(Q 下限, 需要的 V)。工具本身的口径就是"Q 越低要求 V 越高"，
#: 这里把它写成三条明确的阶梯，而不是一条斜线——阶梯看得懂、也好解释。
V_LADDER = ((70.0, 50.0), (60.0, 60.0), (0.0, 65.0))

#: 离一年高点不到 10%（`pos250 > -0.10`）就不再算"提前"了：买点已经过去一段。
NEAR_HIGH = -0.10

#: 历史 PE 分位高于这个数，就算 V 还行也降一档——V 是机构口径的预测，
#: 而 PE 分位是这只票自己跟自己的历史比，两个都贵才叫真贵。
PE_PCT_HIGH = 90.0


def v_need(q: float | None) -> float:
    """这个 Q 分要求多高的 V 分。Q 为 None 时给最严的一档（宁严勿松）。"""
    for q_floor, need in V_LADDER:
        if (q or 0) >= q_floor:
            return need
    return V_LADDER[-1][1]


TIERS = {
    "buy": {"label": "优先研究", "tone": "ok", "order": 0,
            "hint": "四条都过：质量站得住、估值够便宜、周期不在高位、离一年高点还有空间"},
    "watch": {"label": "观察", "tone": "info", "order": 1,
              "hint": "过了质量硬门槛，但还差一条以上——看「差距」列"},
    "out": {"label": "排除", "tone": "warn", "order": 2,
            "hint": "质量已否决（Q < 45 / 命中致命风险 / 结论就是回避）"},
    "nodata": {"label": "待补数据", "tone": "muted", "order": 3,
               "hint": "质量没给出结论（V 缺、数据缺口太大），先补齐再谈"},
}


# ---------------------------------------------------------------- 取字段

def _num(value):
    """payload 里的数可能被写成字符串（老记录），读不出来就算没有。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quality(row) -> dict:
    """质量行 + 它内部的 `fatal`（致命风险清单）。

    `fatal` 只在内层 payload 里（页面上那一列是 `risks_hit`，含义是"命中几条"，
    分不清哪条是致命的）。买入判断必须看到"哪条"，所以这里往下剥一层。
    """
    q = row.get("quality") or {}
    if not q:
        return {"q": None, "v": None, "q_grade": "", "v_grade": "",
                "conclusion": "", "reason": "", "risks_hit": None,
                "risks_known": None, "fatal": []}
    inner_payload = inner(q)
    fatal = inner_payload.get("fatal") or []
    if isinstance(fatal, str):
        fatal = [fatal]
    return {
        "q": _num(q.get("q")),
        "v": _num(q.get("v")),
        "q_grade": q.get("q_grade") or "",
        "v_grade": q.get("v_grade") or "",
        "conclusion": q.get("conclusion") or "",
        "reason": q.get("reason") or "",
        "risks_hit": q.get("risks_hit"),
        "risks_known": q.get("risks_known"),
        "fatal": [str(x) for x in fatal if x],
    }


def _cycle(row) -> dict:
    c = row.get("cycle") or {}
    return {
        "is_cycle": bool(c.get("is_cycle")),
        "position": (c.get("position_label") or "").lower(),
        "position_pct": _num(c.get("position_pct")),
        "elastic": c.get("elastic_label") or "",
        "gm_cv": _num(c.get("gm_cv_pct")),
    }


def _top(row) -> dict:
    t = row.get("top") or {}
    return {
        "pos250": _num(t.get("pos250")),
        "momentum": t.get("momentum_state") or "",
        "turnover": t.get("turnover_state") or "",
        "earnings": t.get("earnings_state") or "",
        "volume": t.get("volume_state") or "",
        "cycle_state": t.get("cycle_state") or "",
        "pe_pct": _num(t.get("pe_pct")),
        "upside": _num(t.get("target_upside")),
    }


def _select(row) -> dict:
    s = row.get("select") or {}
    return {
        "in_select": bool(s),
        "turn_rank": _num(s.get("pool_turnover_rank")),
        "stars": s.get("stars") or "",
        "industry": s.get("industry") or "",
        "vol_multiple": _num(s.get("vol_multiple")),
        "gross_margin_up": s.get("gross_margin_up"),
    }


POSITION_CN = {"low": "低位", "high": "高位", "neutral": "中位", "na": "不适用"}


# ---------------------------------------------------------------- 规则

def _excluded(qua: dict) -> dict | None:
    """质量这一关的否决理由；没被否决给 None。

    顺序有讲究：先看"工具已经说回避"，再说分数——工具可能因为致命风险
    直接回避，这时分数可能并不低，只看分数会漏。

    返回 `{"label", "text", "detail"}`：label 是表里那个短胶囊，
    detail 是短说明（致命项的名字 / 分数对比），text 是完整一句话（悬停看）。
    """
    if not qua["conclusion"]:
        return None
    # 致命项的两条来源：内层 payload 的 `fatal` 列表（新记录），或者
    # 工具把致命项直接写进了 reason（`出现致命风险：利息保障 < 1`，老记录）。
    # 不认第二种的话，30 只回避里绝大多数只会显示一句没信息量的「结论回避」。
    fatal = list(qua["fatal"])
    reason = qua["reason"]
    if not fatal and "致命风险" in reason:
        fatal = [reason.split("致命风险：", 1)[-1]]
    if fatal:
        names = "；".join(fatal[:3])
        return {"label": "致命风险", "detail": names,
                "text": f"命中致命风险：{names}"}
    # 先看分数：Q < 45 是工具里写死的硬线，比"结论回避"这个笼统说法更有信息量。
    if qua["q"] is not None and qua["q"] < Q_HARD:
        return {"label": "Q 不足",
                "detail": f"{qua['q']:.1f} < {Q_HARD:.0f}",
                "text": f"质量分 {qua['q']:.1f} 低于 {Q_HARD:.0f}，禁止买入"}
    if qua["conclusion"].startswith("回避"):
        return {"label": "结论回避", "detail": reason,
                "text": reason or "质量结论回避"}
    return None


def _gaps(qua: dict, cyc: dict, top: dict, sel: dict) -> list[dict]:
    """距"优先研究"还差哪几条。每条 {key,label,text}。

    label 是给图表/图例用的短词，text 是给人看的整句话。全部可解释——
    这里出的每一句，用户在个股页都能找到对应的原始结论。
    """
    out: list[dict] = []
    q, v = qua["q"], qua["v"]
    need = v_need(q)

    if v is None:
        out.append({"key": "v", "label": "缺 V 分",
                    "text": "估值分 V 取不到（多为没有机构覆盖），便宜不便宜算不出来"})
    elif q is not None and v < need:
        out.append({"key": "v", "label": "估值不够便宜",
                    "text": f"V {v:.1f} < {need:.0f}：Q {q:.1f} 要求 V 至少 {need:.0f}"})

    if q is not None and q < Q_GOOD:
        out.append({"key": "q", "label": "质量刚过线",
                    "text": f"Q {q:.1f} 只比 45 高一点（到 {Q_GOOD:.0f} 才算站得住）"})

    if cyc["is_cycle"] and cyc["position"] == "high":
        out.append({"key": "cycle", "label": "周期高位",
                    "text": "周期股且已在高位：周期的钱主要在低位→高位那一段赚"})

    if not top:
        out.append({"key": "top", "label": "缺顶部判断",
                    "text": "这一批没跑顶部判断，离一年高点还有多少空间不知道"})
    else:
        if top["pos250"] is not None and top["pos250"] > NEAR_HIGH:
            out.append({"key": "pos", "label": "离高点太近",
                        "text": f"距 250 日高点 {top['pos250'] * 100:+.1f}%："
                                f"不到 -10% 就不算「提前」了"})
        if "高位" in top["momentum"]:
            out.append({"key": "momentum", "label": "动量高位",
                        "text": f"动量 {top['momentum']}：已经涨在前头，追高风险"})
        if top["earnings"] == "盈利为负":
            out.append({"key": "earnings", "label": "业绩亏损",
                        "text": "业绩因子是「盈利为负」，亏损期不谈估值便宜"})
        if top["pe_pct"] is not None and top["pe_pct"] >= PE_PCT_HIGH:
            out.append({"key": "pe", "label": "PE 历史偏贵",
                        "text": f"历史 PE 分位 {top['pe_pct']:.0f}%"
                                f"（高于 {PE_PCT_HIGH:.0f}% 就算贵）"})

    if not sel["in_select"]:
        out.append({"key": "select", "label": "不在选股名单",
                    "text": "这只票不是这一批选股选出来的，只是别的工具算过它"})
    return out


def _highlights(qua: dict, cyc: dict, top: dict, sel: dict) -> list[dict]:
    """加分项（只做展示，不参与分档）：让「为什么值得看」一眼可见。"""
    out: list[dict] = []
    if top.get("upside") is not None and top["upside"] >= 0.15:
        out.append({"key": "upside", "tone": "good",
                    "text": f"目标价空间 {top['upside'] * 100:+.0f}%"})
    if top.get("pe_pct") is not None and top["pe_pct"] <= 20:
        out.append({"key": "pe", "tone": "good",
                    "text": f"PE 分位 {top['pe_pct']:.0f}%（历史低位）"})
    if top.get("earnings") == "加速":
        out.append({"key": "earnings", "tone": "good", "text": "业绩加速"})
    if cyc["is_cycle"] and cyc["position"] == "low":
        out.append({"key": "cycle", "tone": "good", "text": "周期低位"})
    if top.get("volume") == "放量（非高位）":
        out.append({"key": "volume", "tone": "info", "text": "放量（非高位）"})
    if "第二节" in qua["conclusion"]:
        out.append({"key": "sec2", "tone": "info", "text": "第二节有信号"})
    if sel.get("stars"):
        out.append({"key": "stars", "tone": "muted", "text": f"选股 {sel['stars']}"})
    return out[:4]


def evaluate(row: dict) -> dict:
    """一只票的综合评估结果（纯函数：只吃数据，不碰数据库）。"""
    qua, cyc, top, sel = (_quality(row), _cycle(row), _top(row), _select(row))

    item = {
        "symbol": row["symbol"], "name": row.get("name") or "",
        "q": qua["q"], "v": qua["v"],
        "q_grade": qua["q_grade"], "v_grade": qua["v_grade"],
        "conclusion": qua["conclusion"], "reason": qua["reason"],
        "fatal": qua["fatal"],
        "cycle": cyc, "top": top, "select": sel,
        "position_cn": "" if not cyc["is_cycle"]
                       else POSITION_CN.get(cyc["position"], "—"),
        "gaps": [], "highlights": [],
    }

    why = _excluded(qua)
    if not qua["conclusion"]:
        tier = "nodata"
        item["gaps"] = [{"key": "q", "label": "缺质量结论",
                         "text": "这一批没有质量结果（工具没跑或这只票失败）"}]
    elif qua["conclusion"].startswith("数据不足"):
        tier = "nodata"
        item["gaps"] = [{"key": "q", "label": "质量没结论",
                         "text": qua["reason"] or "质量分或估值分算不出来"}]
    elif why:
        tier = "out"
        item["gaps"] = [{"key": "out", **why}]
    else:
        item["gaps"] = _gaps(qua, cyc, top, sel)
        tier = "buy" if not item["gaps"] else "watch"

    item["tier"] = tier
    item["tier_label"] = TIERS[tier]["label"]
    item["tier_tone"] = TIERS[tier]["tone"]
    item["tier_hint"] = TIERS[tier]["hint"]
    item["highlights"] = _highlights(qua, cyc, top, sel)
    # 木桶：两个分数里偏低的那一个。缺 V 时按 0 算——缺数据的票不该排到
    # 前面去（"不知道便不便宜"和"便宜"是两回事）。
    q, v = item["q"], item["v"]
    if q is None:
        item["bucket"] = None
    elif v is None:
        item["bucket"] = 0.0
    else:
        item["bucket"] = min(q, v)
    return item


def build(rows: list[dict]) -> dict:
    """整批评估：分档 + 排序 + 统计。返回 {'rows', 'tiers', 'counts', 'buy'}。"""
    items = [evaluate(r) for r in rows]

    def key(it):
        tier = TIERS[it["tier"]]["order"]
        if it["tier"] in ("buy", "watch"):
            # 档内按木桶分从高到低；同分再按 Q，最后按代码（保证顺序稳定）
            return (tier, -(it["bucket"] or 0.0), -(it["q"] or 0.0), it["symbol"])
        return (tier, 0.0, 0.0, it["symbol"])

    items.sort(key=key)
    counts = {k: 0 for k in TIERS}
    for it in items:
        counts[it["tier"]] += 1
    for i, it in enumerate(items, 1):
        it["rank"] = i

    return {
        "rows": items,
        "counts": counts,
        "tiers": [{"key": k, **v, "count": counts[k]} for k, v in
                  sorted(TIERS.items(), key=lambda kv: kv[1]["order"])],
        "buy": [it for it in items if it["tier"] == "buy"],
    }
