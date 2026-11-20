"""硬门槛：不亏损、增速、有供需矛盾——逐条给出 过 / 不过 / 未核实。

**只做标识**：结论交给页面显示，**不参与分档、不参与排序、不把谁拿掉**。
"排不排前面"仍然是赔率分的事（`review.py`），两套逻辑刻意不相加。

哪几条算、哪几条显示，都由 `config.GATE_KEYS` 决定。三条都**只吃数据库里
现成的数**，不看任何手写备注：

    ① 不亏损    `shared.db.finance` 里的扣非与归母。
    ② 增速      单季营收同比、单季扣非同比（finance 的 raw_json）。
    ③ 供需      毛利率环比连升两季（营收/成本现算）。

需要人工判断、或者库里没有字段的条件（提价、交期、新产品）**一条都不加**——
加了就只能靠手写备注，等于把判断建立在数据库之外。

这一层不碰文件、不连库，保持可单测（和 `stock_cycle_elastic` 的规则层一样）。

`unknown` 不是"通过"，也不是"不通过"：工具不知道的事不替人做决定，
但也不让它冒充通过——页面上单独列出来。
"""
from __future__ import annotations

from . import config


def _num(value):
    """raw_json 里的数都是字符串（"33.21"），读不出来就算没有。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gate(key: str, state: str, value: str = "", note: str = "",
          detail: str = "") -> dict:
    """一条门槛的结论。`value` 进表格那一格，`note` 悬停看完整解释。"""
    return {
        "key": key,
        "label": config.GATE_LABELS[key],
        "short": config.GATE_SHORT[key],
        "state": state,
        "state_cn": config.GATE_STATE_CN[state],
        "rule": config.GATE_RULES[key],
        "value": value,
        "note": note,
        "detail": detail,
    }


def _pct(x) -> str:
    return "—" if x is None else f"{x:+.0f}%"


# ---------------------------------------------------------------- 第 1 条

def gate_profit(fin: dict) -> dict:
    """不亏损：利润是真的，而且不是靠非经常性损益撑出来的。

    `cut_ttm` 是最近四季扣非之和；四季凑不齐时退回累计扣非（`cut_cum`）——
    Q3 报告下累计值已经是 9 个月，比 TTM 弱一点，但仍比"单季为正"可靠。
    """
    cut_ttm = _num(fin.get("cut_ttm"))
    cut_cum = _num(fin.get("cut_cum"))
    np_cum = _num(fin.get("np_cum"))
    cut_q = _num(fin.get("cut_q"))

    if cut_ttm is None and cut_cum is None and np_cum is None:
        return _gate("profit", "unknown", note="没有财务数据，判断不了盈亏")

    if np_cum is not None and np_cum <= 0:
        return _gate("profit", "fail",
                     value=f"归母 {np_cum / 1e8:.2f} 亿",
                     note="归母净利润为负：亏损公司不买")

    base = cut_ttm if cut_ttm is not None else cut_cum
    basis = "扣非 TTM" if cut_ttm is not None else "累计扣非"
    if base is not None and base <= 0:
        return _gate("profit", "fail",
                     value=f"{basis} {base / 1e8:.2f} 亿",
                     note=f"{basis}为负：主业没有赚钱，亏损公司不买")

    if cut_q is not None and cut_q <= 0:
        return _gate("profit", "fail",
                     value=f"单季扣非 {cut_q / 1e8:.2f} 亿",
                     note="最新单季扣非为负：不管 TTM 多好看，当期在失血")

    if cut_cum is not None and np_cum is not None and np_cum > 0:
        ratio = cut_cum / np_cum
        if ratio < config.GATE1_DEDUCTED_RATIO_MIN:
            return _gate("profit", "fail",
                         value=f"扣非/归母 {ratio:.0%}",
                         note=f"扣非只占归母的 {ratio:.0%}（低于 "
                              f"{config.GATE1_DEDUCTED_RATIO_MIN:.0%}）：利润靠"
                              "政府补助/资产处置/公允价值变动撑着，不是主业赚的")
        value = f"扣非/归母 {ratio:.0%}"
    else:
        value = f"{basis} {base / 1e8:.2f} 亿" if base is not None else "—"

    parts = []
    if base is not None:
        parts.append(f"{basis} {base / 1e8:+.2f} 亿")
    if cut_cum is not None and np_cum:
        parts.append(f"扣非/归母 {cut_cum / np_cum:.0%}")
    return _gate("profit", "pass", value=value, note="；".join(parts))


# ---------------------------------------------------------------- 第 2 条

def gate_growth(fin: dict) -> dict:
    """增速：总量够快，而且利润跑得比收入快。

    两个条件缺一不可——只看营收会选到"增收不增利"的铺货型公司，
    只看利润会被低基数和一次性收益骗（+161% 可能去年同季在亏损）。
    """
    rev = _num(fin.get("rev_yoy_q"))
    cut = _num(fin.get("cut_yoy_q"))
    if rev is None or cut is None:
        return _gate("growth", "unknown", note="缺单季营收/扣非同比，算不了")

    value = f"营收 {_pct(rev)}｜扣非 {_pct(cut)}"
    if rev < config.GATE2_REV_YOY_MIN:
        return _gate("growth", "fail", value=value,
                     note=f"单季营收同比 {rev:+.1f}%，低于 "
                          f"{config.GATE2_REV_YOY_MIN:.0f}% 的底线")
    if cut <= rev:
        return _gate("growth", "fail", value=value,
                     note=f"扣非同比 {cut:+.1f}% 没有跑赢营收 {rev:+.1f}%："
                          "增长靠堆量，利润没跟上（经营杠杆没起来）")
    return _gate("growth", "pass", value=value,
                 note=f"营收 {rev:+.1f}%（≥ {config.GATE2_REV_YOY_MIN:.0f}%），"
                      f"扣非 {cut:+.1f}% 快于营收——量价或结构在改善")


# ---------------------------------------------------------------- 第 3 条：供需

def _yi(v: float) -> str:
    """元 → 亿元，用来显示合同负债这种大数。"""
    return f"{v / 1e8:.1f} 亿" if abs(v) >= 1e8 else f"{v / 1e4:.0f} 万"


def _margin_signal(fin: dict) -> tuple[str, str, str]:
    """供需的第一条证据：毛利率环比。返回 (state, 读数, 理由)。"""
    gm = [x for x in (fin.get("gm_q") or []) if x is not None]
    text = "／".join(f"{x:.1f}%" for x in gm[:3]) if gm else ""
    if len(gm) < 3:
        return ("unknown", "", "缺连续三季的营收/成本，算不出毛利率环比"
                               + (f"（现有 {text}）" if text else ""))
    if gm[0] > gm[1] > gm[2]:
        return ("pass", f"毛利率 {text}", f"毛利率环比连升两季（{text}）")
    if gm[0] < gm[1] < gm[2]:
        return ("fail", f"毛利率 {text}", f"毛利率环比连降两季（{text}）")
    return ("unknown", f"毛利率 {text}",
            f"毛利率 {text} 没有连续两季的方向")


def _cl_signal(fin: dict) -> tuple[str, str, str]:
    """供需的第二条证据：合同负债同比（预收款变多 = 订单在手变厚）。

    三个门槛缺一不可：两期都有数、占营收够重、同比变化够大。
    小基数上翻一倍不作数——中位公司的合同负债只有年化营收的 2.4%。
    """
    cl, prev, rev = fin.get("cl"), fin.get("cl_prev"), fin.get("rev_q")
    if cl is None or prev is None or prev <= 0:
        return ("unknown", "", "缺合同负债的两期数据（当期 / 去年同季）")
    if rev is None or rev <= 0:
        return ("unknown", "", "缺单季营收，算不出合同负债占营收的比重")

    share = cl / (rev * 4.0)
    if share < config.GATE3_CL_SHARE_MIN:
        return ("unknown",
                f"合同负债 {_yi(cl)}（占营收 {share:.1%}）",
                f"合同负债只有年化营收的 {share:.1%}，基数太小，涨跌都不作数")

    yoy = cl / prev - 1.0
    read = f"合同负债 {_yi(cl)}（同比 {yoy:+.0%}，占营收 {share:.1%}）"
    if yoy >= config.GATE3_CL_YOY_MIN:
        return ("pass", read, f"合同负债同比 {yoy:+.0%}：预收款变多，订单在手在变厚")
    if yoy <= config.GATE3_CL_YOY_FAIL:
        return ("fail", read, f"合同负债同比 {yoy:+.0%}：预收款在缩，订单在流失")
    return ("unknown", read, f"合同负债同比 {yoy:+.0%}，落在正常波动区间")


#: 两条证据怎么合成一条结论。思路是"占一条即可"，但**打架时不下结论**：
#: 毛利率在涨、订单却在缩，可能只是原材料降价，交给人工看。
_SUPPLY_COMBINE = {
    ("pass", "pass"): "pass",
    ("pass", "unknown"): "pass",
    ("unknown", "pass"): "pass",
    ("fail", "fail"): "fail",
    ("fail", "unknown"): "fail",
    ("unknown", "fail"): "fail",
    ("pass", "fail"): "unknown",
    ("fail", "pass"): "unknown",
    ("unknown", "unknown"): "unknown",
}


def gate_supply(fin: dict) -> dict:
    """供需：毛利率环比连升两季 **或** 合同负债同比明显变厚。

    两条证据都只用数据库里现成的数：

    * 毛利率 = （`OperatingRevenue_Q` − `OperatingCost_Q`）/ 营收，看三季方向；
      涨价或结构上移会推高它，降价换量会压低它。
    * 合同负债 = `ContractLiability`，当期比**去年同季**；预收款变多说明订单在
      手、客户愿意先付钱，这是"需求强于供给"在报表上的第二个痕迹。

    刻意**不加**"提价公告""交期拉长""产能受限"：共享库里没有这些字段，
    硬加就只能靠手写备注，等于把判断建立在数据库之外。

    金融股没有营业成本、合同负债也常常不适用，两条都判不了 → `unknown`。
    """
    m_state, m_read, m_note = _margin_signal(fin)
    c_state, c_read, c_note = _cl_signal(fin)
    state = _SUPPLY_COMBINE[(m_state, c_state)]
    value = "；".join(x for x in (m_read, c_read) if x)

    if {m_state, c_state} == {"pass", "fail"}:
        note = f"两条证据打架：{m_note}；{c_note}——交给人工看"
    else:
        note = "；".join(x for x in (m_note, c_note) if x)
    return _gate("supply", state, value=value, note=note)


# ---------------------------------------------------------------- 汇总

#: 门槛 key → 怎么调它。`check()` 只跑 `config.GATE_KEYS` 里列出来的，
#: 所以"页面显示哪几条"和"算哪几条"是同一个开关（第 ③ 条就靠它下线）。
_CHECKERS = {
    "profit": gate_profit,
    "growth": gate_growth,
    "supply": gate_supply,
}


def check(fin: dict | None) -> list[dict]:
    """三条门槛一起查（哪几条见 `config.GATE_KEYS`）。

    `fin` 缺省时三条都给"未核实"，不抛异常。

    `fin` 的形状由 `web/db.py: financials()` 组装：
        {period, rev_yoy_q, cut_yoy_q, cut_q, cut_cum, np_cum, cut_ttm,
         gm_q: [最新季, 上季, 上上季]}
    """
    fin = fin or {}
    out = [_CHECKERS[k](fin) for k in config.GATE_KEYS]
    for g in out:
        g["ok"] = g["state"] == "pass"
        g["blocked"] = g["state"] == "fail"
    return out


def summary(gates: list[dict]) -> dict:
    """门槛的总体状态：`fail` 压过 `unknown`，`unknown` 压过 `pass`。"""
    states = [g["state"] for g in gates]
    if "fail" in states:
        overall = "fail"
    elif "unknown" in states:
        overall = "unknown"
    else:
        overall = "pass"
    return {
        "overall": overall,
        "passed": sum(1 for s in states if s == "pass"),
        "failed": sum(1 for s in states if s == "fail"),
        "unknown": sum(1 for s in states if s == "unknown"),
        "total": len(states),
    }
