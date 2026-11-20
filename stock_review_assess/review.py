"""综合评估：把四个工具对同一只票的结论并排，算一个**赔率分**再排序。

这一页是什么、不是什么
----------------------
它是**候选池排序器**，不是买入清单。排序靠前的票仍然要自己深入研究才决定。

为什么从「逐条必过」改成「加减分」
--------------------------------
早期的规则是「硬门槛 → 逐条差距 → 差一条就降档」。它有一个副作用：软条件
变成了事实上的否决项——市值大、弹性弱、机构没覆盖，任何一条都能把一只
本来值得看的票挡在门外，而这三件事没有一件是"会亏钱"。

现在的分工是：

    硬门槛（一票否决）  —— 只留"会不会亏光"：Q < 45、命中致命风险。
    赔率分（0~15）      —— 其余全部按"赚得多不多"加减分，分低就排后面，
                           **但不消失**。差一条只扣几分，不影响它被看见。

阈值刻意"模糊"
--------------
`BUY_CUT = 5` 是唯一的切点旋钮：调大更严格，调小更宽松。加分项也是分档
（<50 亿 / <100 亿 / <200 亿）而不是精确门槛——这一页给的是**优先级**，
不是"合格 / 不合格"的判决。

四项数据的来源（都由工具算好了，这里只读）
------------------------------------------
    市值        `select_result.market_cap_yi`（选股工具）
    垄断性      `shared.db` 的 `annotation` 表（质量工具的标注，人工维护）
    股价低位    `quality.detail.v.historical` 的 PE/PB/PS 历史分位
                （机构目标价只做加分——它只覆盖一半的票）
    周期底部    `cycle_result.position_label`（产能周转率分位 < 40）

档位
----
    优先研究   赔率分 ≥ BUY_CUT，且工具没有判回避 / 数据不足
    观察       过了质量硬门槛，但分数不够或工具自己没下结论
    排除       质量已否决（Q < 45 / 命中致命风险 / 工具结论就是回避）
    待补数据   这一批没有质量结果（工具没跑或这只票失败）
"""
from __future__ import annotations

from . import config
from .config import (
    BUY_CUT, ELASTIC_CN, ELASTIC_POINTS, ELASTIC_RANK, ELASTIC_TONE,
    MCAP_STEPS, MOAT_NONE, MOAT_UNVERIFIED, NEAR_HIGH, PCT_STEPS,
    PE_PCT_HIGH, Q_HARD, Q_STEPS, SCORE_MAX, TIERS, TREND_PENALTY,
    V_PENALTY, V_WEAK,
)
from . import gates as _gates
from .gates import summary as _gate_summary


def _inner(row: dict) -> dict:
    """工具自己那份原始 payload（原来在 `web/db.py`，同名逻辑）。

    搬过来的原因：`stock_review_assess` 要和 `stock_kline_screen` 平级，就不能反向
    依赖 `web`——`web/__init__.py` 会 import `app`，`app` 又 import 本模块，
    一依赖就成环。逻辑只有五行，复制一份比拉循环导入划算。
    """
    data = row.get("data") or {}
    seen = 0
    while isinstance(data.get("payload"), dict) and seen < 3:
        data = data["payload"]
        seen += 1
    return data


def _has_moat(moat: str) -> bool:
    return bool(moat) and moat not in (MOAT_NONE, MOAT_UNVERIFIED, "未标注")




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
    """质量行 + 内层 `detail` 里的三样东西：估值分位、致命项、趋势。

    `fatal` 只在内层 payload 里（页面上那一列是 `risks_hit`，含义是"命中几条"，
    分不清哪条是致命的）。买入判断必须看到"哪条"，所以这里往下剥一层。

    `detail` 结构（`stock_quality_screen/screen/detail.py`）：
        v.historical.metrics[] —— [{key: pe/pb/ps, label, percentile}]
        v.parts[]              —— [{label: 机构目标价空间, raw}]
        risks[]                —— [{group, name, status: hit/watch/unknown/clear}]
    """
    q = row.get("quality") or {}
    if not q:
        return {"q": None, "v": None, "q_grade": "", "v_grade": "",
                "conclusion": "", "reason": "", "risks_hit": None,
                "risks_known": None, "fatal": [], "pct_pe": None,
                "pct_pb": None, "pct_ps": None, "pct_avg": None,
                "trend_bad": [], "has_quality": False}
    payload = _inner(q)
    fatal = payload.get("fatal") or []
    if isinstance(fatal, str):
        fatal = [fatal]
    detail = payload.get("detail") or {}
    hist = (detail.get("v") or {}).get("historical") or {}
    pct = {m.get("key"): _num(m.get("percentile"))
           for m in hist.get("metrics") or []}
    have = [x for x in (pct.get("pe"), pct.get("pb"), pct.get("ps")) if x is not None]
    # 趋势：只认"命中"，`watch` 不算坏（工具自己也只是提示）。
    # 这两项是用户观点里"财务要有走好的趋势"的直接对应物。
    trend_bad = [str(x.get("name") or "") for x in detail.get("risks") or []
                 if x.get("status") == "hit"
                 and ("毛利" in str(x.get("name") or "")
                      or "合同负债" in str(x.get("name") or ""))]
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
        "pct_pe": pct.get("pe"), "pct_pb": pct.get("pb"), "pct_ps": pct.get("ps"),
        "pct_avg": (sum(have) / len(have)) if have else None,
        "trend_bad": trend_bad,
        "has_quality": True,
    }


def _cycle(row) -> dict:
    """周期行的字段。

    `elastic_label` 的 `na` 归一成空串：`na` 是"工具跑了但算不出来"，
    与"这一批没跑周期"（整行缺失）是两回事，靠 `has_cycle` 区分。
    """
    c = row.get("cycle") or {}
    label = (c.get("elastic_label") or "").lower()
    if label in ("", "na"):
        label = ""
    ratio = _num(c.get("elasticity"))
    # 表格里那一格：「强 5.3×」。弱/不稳定也照样显示原值
    # （弱的 0.68× 比一个"弱"字有信息量）。
    text = ""
    if label:
        text = ELASTIC_CN.get(label, label)
        if ratio is not None:
            text = f"{text} {ratio:.1f}×"
    # 色调跟着"这一关管不管它"走：非周期股的弱/不稳定与本页无关，
    # 涂成橙色会让人以为它拖了后腿（橙色在这页一直是"有问题"的意思）。
    tone = ELASTIC_TONE.get(label, "muted")
    if label and not c.get("is_cycle") and label != "strong":
        tone = "muted"
    return {
        "is_cycle": bool(c.get("is_cycle")),
        "position": (c.get("position_label") or "").lower(),
        "position_pct": _num(c.get("position_pct")),
        "elastic": label,
        "ratio": ratio,
        "text": text,
        "tone": tone,
        "gm_cv": _num(c.get("gm_cv_pct")),
        "has_cycle": bool(c),
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
        "has_top": bool(t),
    }


def _select(row) -> dict:
    s = row.get("select") or {}
    return {
        "in_select": bool(s),
        "mcap": _num(s.get("market_cap_yi")),
        "turn_rank": _num(s.get("pool_turnover_rank")),
        "stars": s.get("stars") or "",
        "industry": s.get("industry") or "",
        "vol_multiple": _num(s.get("vol_multiple")),
        "gross_margin_up": s.get("gross_margin_up"),
    }


POSITION_CN = {"low": "低位", "high": "高位", "neutral": "中位", "na": "不适用"}


# ---------------------------------------------------------------- 赔率分

def _steps(value, steps) -> int:
    """分档给分：`value` 落在哪一档就给那一档的分。`None` → 0（不知道不加分）。"""
    if value is None:
        return 0
    for limit, points in steps:
        if value < limit:
            return points
    return 0


def _q_points(q) -> int:
    """质量加分。越高质量分越高，与 `_steps` 方向相反，所以单写。"""
    if q is None:
        return 0
    for floor, points in Q_STEPS:
        if q >= floor:
            return points
    return 0


def _score(qua: dict, cyc: dict, top: dict, sel: dict, moat: str) -> dict:
    """赔率分：0~`SCORE_MAX`。返回 {'total', 'terms', 'penalties'}。

    `terms` 的每一项都是 {key,label,points,note}——页面上悬停能看到
    「为什么是 11 分」，不用去猜权重。
    """
    terms = [
        {"key": "mcap", "label": "市值小",
         "points": _steps(sel["mcap"], MCAP_STEPS),
         "note": "—" if sel["mcap"] is None else f"{sel['mcap']:.0f} 亿"},
        {"key": "value", "label": "估值低位",
         "points": _steps(qua["pct_avg"], PCT_STEPS),
         "note": ("—" if qua["pct_avg"] is None
                  else f"PE/PB/PS 分位均值 {qua['pct_avg']:.0f}%")},
        {"key": "cycle", "label": "周期低位",
         "points": 2 if cyc["is_cycle"] and cyc["position"] == "low" else 0,
         "note": (POSITION_CN.get(cyc["position"], "—") if cyc["is_cycle"]
                  else "非周期股" if cyc["has_cycle"] else "缺周期数据")},
        {"key": "elastic", "label": "盈利弹性",
         "points": ELASTIC_POINTS.get(cyc["elastic"], 0),
         "note": cyc["text"] or "—"},
        {"key": "moat", "label": "垄断性",
         "points": 2 if _has_moat(moat) else 0,
         "note": moat or "未标注"},
        {"key": "q", "label": "质量",
         "points": _q_points(qua["q"]),
         "note": "—" if qua["q"] is None else f"Q {qua['q']:.1f}"},
        {"key": "upside", "label": "目标价空间",
         "points": 1 if (top["upside"] is not None and top["upside"] >= 0.20) else 0,
         "note": "—" if top["upside"] is None else f"{top['upside'] * 100:+.0f}%"},
    ]

    penalties = []
    if qua["trend_bad"]:
        penalties.append({"key": "trend", "label": "趋势走弱",
                          "points": TREND_PENALTY,
                          "note": "；".join(qua["trend_bad"])})
    if qua["v"] is not None and qua["v"] < V_WEAK:
        penalties.append({"key": "v", "label": "估值分偏低",
                          "points": V_PENALTY, "note": f"V {qua['v']:.1f} < {V_WEAK:.0f}"})

    total = sum(t["points"] for t in terms) + sum(p["points"] for p in penalties)
    return {"total": total, "terms": terms, "penalties": penalties}


# ---------------------------------------------------------------- 提示

def _notes(qua: dict, cyc: dict, top: dict, sel: dict) -> list[dict]:
    """研究时的提示（**不影响分档**）。每条 {key,label,text,tone}。

    label 是表里那个短胶囊，text 是完整一句话（悬停看）。全部可解释——
    这里出的每一句，用户在个股页都能找到对应的原始结论。
    """
    out: list[dict] = []
    concl = qua["conclusion"]

    if concl.startswith("回避"):
        out.append({"key": "verdict", "label": "工具判回避", "tone": "warn",
                    "text": qua["reason"] or "质量结论是「回避」，只降档不排除——"
                                             "自己确认它回避的理由是否已经过去"})
    elif concl.startswith("数据不足"):
        out.append({"key": "verdict", "label": "数据不足", "tone": "muted",
                    "text": qua["reason"] or "质量分或估值分算不出来，工具没下结论"})

    if qua["trend_bad"]:
        out.append({"key": "trend", "label": "趋势走弱", "tone": "warn",
                    "text": "命中：" + "；".join(qua["trend_bad"])
                            + "。用户口径里「财务要有走好的趋势」正是这一条，扣 2 分"})

    if qua["v"] is None:
        out.append({"key": "v", "label": "缺 V 分", "tone": "muted",
                    "text": "机构覆盖不到 3 家，估值分算不出来；"
                            "本批 63 只如此，多为小市值——所以不扣分，看 PB/PS 分位代替"})

    if cyc["is_cycle"] and cyc["position"] == "high":
        out.append({"key": "cycle", "label": "周期高位", "tone": "warn",
                    "text": "产能周转率已在 60 分位以上：周期的钱主要在低位→高位那一段赚"})
    if not cyc["has_cycle"]:
        out.append({"key": "cycle", "label": "缺周期数据", "tone": "muted",
                    "text": "这一批没跑周期弹性，判断不了它是不是周期股、在周期哪个位置"})
    elif cyc["is_cycle"] and cyc["elastic"] in ("weak", "unstable"):
        if cyc["elastic"] == "unstable":
            out.append({"key": "elastic", "label": "弹性不可信", "tone": "warn",
                        "text": "营收长期走平或季度利润频繁跨零，比值没有意义"
                                "——不是弹性差，是算不出来"})
        else:
            ratio = cyc["ratio"]
            out.append({"key": "elastic", "label": "弹性偏弱", "tone": "muted",
                        "text": f"营业利润对营收的敏感度 "
                                f"{f'{ratio:.1f}×' if ratio is not None else '不到 2×'}"
                                "：营收起来了利润跟不上"})

    if not top["has_top"]:
        out.append({"key": "top", "label": "缺顶部判断", "tone": "muted",
                    "text": "这一批没跑顶部判断，离一年高点还有多少空间不知道"})
    else:
        if top["pos250"] is not None and top["pos250"] > NEAR_HIGH:
            out.append({"key": "pos", "label": "离高点太近", "tone": "muted",
                        "text": f"距 250 日高点 {top['pos250'] * 100:+.1f}%："
                                "不到 -10% 就不算「提前」了"})
        if "高位" in top["momentum"]:
            out.append({"key": "momentum", "label": "动量高位", "tone": "muted",
                        "text": f"动量 {top['momentum']}：已经涨在前头，追高风险"})
        if top["earnings"] == "盈利为负":
            out.append({"key": "earnings", "label": "业绩亏损", "tone": "warn",
                        "text": "业绩因子是「盈利为负」，亏损期不谈估值便宜"})
        if top["pe_pct"] is not None and top["pe_pct"] >= PE_PCT_HIGH:
            out.append({"key": "pe", "label": "PE 历史偏贵", "tone": "muted",
                        "text": f"历史 PE 分位 {top['pe_pct']:.0f}%"
                                f"（高于 {PE_PCT_HIGH:.0f}% 就算贵）"})

    if not sel["in_select"]:
        out.append({"key": "select", "label": "不在选股名单", "tone": "muted",
                    "text": "这只票不是这一批选股选出来的，只是别的工具算过它"})
    return out


def _highlights(qua: dict, cyc: dict, top: dict, sel: dict, moat: str) -> list[dict]:
    """加分项（只做展示，让「为什么值得看」一眼可见）。"""
    out: list[dict] = []
    if moat and _has_moat(moat):
        out.append({"key": "moat", "tone": "good", "text": moat})
    if sel["mcap"] is not None and sel["mcap"] < 100:
        out.append({"key": "mcap", "tone": "good", "text": f"市值 {sel['mcap']:.0f} 亿"})
    if qua["pct_avg"] is not None and qua["pct_avg"] <= 25:
        out.append({"key": "value", "tone": "good",
                    "text": f"估值分位 {qua['pct_avg']:.0f}%（历史低位）"})
    if cyc["is_cycle"] and cyc["position"] == "low":
        out.append({"key": "cycle", "tone": "good", "text": "周期低位"})
    if cyc["elastic"] == "strong":
        # 非周期股的强 DOL 工具自己就标「①否②强·存疑」（成本与售价联动），
        # 这里照抄它的态度：是好事，但别当成周期弹性来理解。
        out.append({"key": "elastic",
                    "tone": "good" if cyc["is_cycle"] else "info",
                    "text": (f"盈利弹性 {cyc['ratio']:.1f}×" if cyc["ratio"] is not None
                             else "盈利弹性强")
                            + ("" if cyc["is_cycle"] else "·存疑")})
    if top.get("upside") is not None and top["upside"] >= 0.20:
        out.append({"key": "upside", "tone": "good",
                    "text": f"目标价空间 {top['upside'] * 100:+.0f}%"})
    if top.get("earnings") == "加速":
        out.append({"key": "earnings", "tone": "good", "text": "业绩加速"})
    if top.get("volume") == "放量（非高位）":
        out.append({"key": "volume", "tone": "info", "text": "放量（非高位）"})
    if "第二节" in qua["conclusion"]:
        out.append({"key": "sec2", "tone": "info", "text": "第二节有信号"})
    if sel.get("stars"):
        out.append({"key": "stars", "tone": "muted", "text": f"选股 {sel['stars']}"})
    return out[:6]


def evaluate(row: dict, moat: str = "", gates: list[dict] | None = None) -> dict:
    """一只票的综合评估结果（纯函数：只吃数据，不碰数据库）。

    `moat` 是这只票的垄断性标注（`shared.db` 的 `annotation`），由 `build()`
    查好传进来——这一层不自己去连库，保持可单测。

    `gates` 是 `gates.check()` 算好的四条硬门槛。它**只做标识**：
    写进 `gates` / `gate_state` 供页面显示，**不改变分档，也不改变排序**。
    这一页原来的名次一动都不动——门槛是给人看的，不是替人下结论的。
    """
    qua, cyc, top, sel = (_quality(row), _cycle(row), _top(row), _select(row))
    scored = _score(qua, cyc, top, sel, moat)
    gates = list(gates or [])
    gsum = _gate_summary(gates) if gates else None

    item = {
        "symbol": row["symbol"], "name": row.get("name") or "",
        "q": qua["q"], "v": qua["v"],
        "q_grade": qua["q_grade"], "v_grade": qua["v_grade"],
        "conclusion": qua["conclusion"], "reason": qua["reason"],
        "fatal": qua["fatal"],
        "moat": moat or "未标注",
        "has_moat": _has_moat(moat),
        "mcap": sel["mcap"],
        "pct_avg": qua["pct_avg"],
        "pct_pe": qua["pct_pe"], "pct_pb": qua["pct_pb"], "pct_ps": qua["pct_ps"],
        "cycle": cyc, "top": top, "select": sel,
        "position_cn": "" if not cyc["is_cycle"]
                       else POSITION_CN.get(cyc["position"], "—"),
        "score": scored["total"], "terms": scored["terms"],
        "penalties": scored["penalties"],
        "gates": gates,
        "gate_state": gsum["overall"] if gsum else "",
        "gate_summary": gsum,
        "notes": [], "highlights": [],
    }
    # 悬停看「这 11 分是怎么来的」：加分项按分数降序，后面接扣分项。
    parts = [f"{t['label']} {t['points']:+d}（{t['note']}）"
             for t in sorted(scored["terms"], key=lambda x: -x["points"]) if t["points"]]
    parts += [f"{p['label']} {p['points']:+d}（{p['note']}）"
              for p in scored["penalties"]]
    item["score_text"] = " ｜ ".join(parts) or "没有拿到任何加分项"

    if not qua["has_quality"]:
        tier = "nodata"
        item["notes"] = [{"key": "q", "label": "缺质量结论", "tone": "muted",
                          "text": "这一批没有质量结果（工具没跑或这只票失败）"}]
    elif qua["fatal"]:
        tier = "out"
        names = "；".join(qua["fatal"][:3])
        item["notes"] = [{"key": "out", "label": "致命风险", "tone": "warn",
                          "detail": names, "text": f"命中致命风险：{names}"}]
    elif qua["q"] is not None and qua["q"] < Q_HARD:
        tier = "out"
        item["notes"] = [{"key": "out", "label": "Q 不足", "tone": "warn",
                          "detail": f"{qua['q']:.1f} < {Q_HARD:.0f}",
                          "text": f"质量分 {qua['q']:.1f} 低于 {Q_HARD:.0f}，禁止买入"}]
    elif qua["conclusion"].startswith("回避"):
        # 工具判回避但 Q 没有低于硬线、也没有致命项：降为观察而不是排除。
        # 用户会在个股页看到回避的理由，自己判断它是否已经过去。
        tier = "watch"
        item["notes"] = _notes(qua, cyc, top, sel)
    elif qua["conclusion"].startswith("数据不足"):
        tier = "watch"
        item["notes"] = _notes(qua, cyc, top, sel)
    else:
        item["notes"] = _notes(qua, cyc, top, sel)
        # 未核实**不挡**买入：工具查不到的事不该替人否决（否则第 3 条
        # 永远是 unknown，整页一只都进不来）。核实过、并明确不过的才挡。
        tier = "buy" if item["score"] >= BUY_CUT else "watch"

    item["tier"] = tier
    item["tier_label"] = TIERS[tier]["label"]
    item["tier_tone"] = TIERS[tier]["tone"]
    item["tier_hint"] = TIERS[tier]["hint"]
    item["highlights"] = _highlights(qua, cyc, top, sel, moat)
    return item


def build(rows: list[dict], moats: dict[str, str] | None = None,
          financials: dict[str, dict] | None = None) -> dict:
    """整批评估：门槛 + 分档 + 排序 + 统计。

    `moats` 是 `{symbol: moat_type}`（`web/db.py: moats()` 读 `shared.db`）。
    `financials` 是 `{symbol: fin}`（`web/db.py: financials()` 读 `finance` 表）。
    两个都可以不传：不传就退回原来的行为（只有质量硬门槛），单测据此稳定。
    """
    known = {str(k).lower(): v for k, v in (moats or {}).items()}
    fins = {str(k).lower(): v for k, v in (financials or {}).items()} \
        if financials is not None else None

    items = []
    for r in rows:
        sym = str(r.get("symbol") or "").lower()
        gs = _gates.check(fins.get(sym)) if fins is not None else None
        items.append(evaluate(r, known.get(sym, ""), gs))

    def key(it):
        tier = TIERS[it["tier"]]["order"]
        if it["tier"] in ("buy", "watch"):
            # 档内按赔率分从高到低；同分看弹性（强的靠前），然后 Q，最后代码。
            # 四条门槛**不参与排序**：它只是标识，不改变这一页原来的名次。
            return (tier, -it["score"],
                    ELASTIC_RANK.get(it["cycle"]["elastic"], 3),
                    -(it["q"] or 0.0), it["symbol"])
        return (tier, 0, 0, 0.0, it["symbol"])

    items.sort(key=key)
    counts = {k: 0 for k in TIERS}
    for it in items:
        counts[it["tier"]] += 1
    for i, it in enumerate(items, 1):
        it["rank"] = i

    return {
        "rows": items,
        "counts": counts,
        "cut": BUY_CUT,
        "score_max": SCORE_MAX,
        "tiers": [{"key": k, **v, "count": counts[k]} for k, v in
                  sorted(TIERS.items(), key=lambda kv: kv[1]["order"])],
        "buy": [it for it in items if it["tier"] == "buy"],
        "gate_stats": _gate_stats(items) if fins is not None else [],
        "has_gates": fins is not None,
        "gate_all_pass": sum(1 for it in items if it["gate_state"] == "pass"),
        # 门槛全过的是哪几只——页面上要把名字写出来，光有一个数字没法用。
        "gate_pass_rows": [{"symbol": it["symbol"], "name": it.get("name") or
                            it["symbol"]}
                           for it in items if it["gate_state"] == "pass"],
        "gate_blocked": sum(1 for it in items if it["gate_state"] == "fail"),
        # 没有明确不过、但还有门槛没核实的票，也就是"值得先去看财报的那批"。
        "gate_pending": [it for it in items if it["gate_state"] == "unknown"],
    }


def _gate_stats(items: list[dict]) -> list[dict]:
    """每条门槛各自的通过/不过/未核实只数（页面顶部那几张卡）。"""
    out = []
    for key in config.GATE_KEYS:
        cell = {"pass": 0, "fail": 0, "unknown": 0}
        for it in items:
            for g in it["gates"]:
                if g["key"] == key:
                    cell[g["state"]] += 1
        out.append({
            "key": key,
            "label": config.GATE_LABELS[key],
            "short": config.GATE_SHORT[key],
            "rule": config.GATE_RULES[key],
            "pass": cell["pass"], "fail": cell["fail"],
            "unknown": cell["unknown"],
            "total": cell["pass"] + cell["fail"] + cell["unknown"],
        })
    return out
