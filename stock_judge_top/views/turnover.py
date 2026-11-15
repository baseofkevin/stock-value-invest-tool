"""换手卡片。

口径（2026-11-15）：优先用换手率，**流通市值缺失时退回成交额分位**。

换手率 = 成交额 ÷ 流通市值。市值只有"当期一份"（quote 域），取不到时原版会
直接判「数据不足」，整张卡片作废（实测 41 只里 2 只：胜利股份、好利科技）。
成交额是市场真实数据、不依赖市值，缺市值时改看它自身的分位——回答的仍是
「这段时间的交易是不是比过去一年更拥挤」，但口径不同，卡片标题与副标题必须
写明是「成交额分位」，不能顶着"换手"的名字说成交额。
"""

from .common import _insufficient


def _fmt_amount(yuan):
    """成交额（元）→ 亿元，两位小数。"""
    if yuan is None:
        return "—"
    return f"{yuan / 1e8:,.2f} 亿"


def _calc_lines(km):
    basis = km.get("turnover_basis") or "turnover"
    if basis == "amount":
        aav20, aav250 = km.get("amt_av20"), km.get("amt_av250")
        ratio = km.get("amt_ratio")
        apct = km.get("amt_pct")
        n = km.get("amt_pct_n") or 0
        ratio_line = "—"
        if aav20 is not None and aav250 and ratio is not None:
            ratio_line = (f"20日均 {_fmt_amount(aav20)} ÷ 250日均 "
                          f"{_fmt_amount(aav250)} = {ratio:.2f}")
        return [
            ("口径", "成交额（无流通市值，换手率算不出来）"),
            ("20日均成交额", _fmt_amount(aav20)),
            ("250日均成交额", _fmt_amount(aav250)),
            ("1月/1年成交额比", f"{ratio:.2f}" if ratio is not None else "—"),
            ("自身分位", f"P{apct:.0f}" if apct is not None else "—"),
            ("分位样本", f"{n} 个交易日"),
            ("成交额比计算", ratio_line),
        ]
    av20, av250 = km.get("av20"), km.get("av250")
    ratio = km.get("ratio")
    pct_av20 = (km.get("pct") or {}).get("AVG20")
    n_av20 = (km.get("pct_n") or {}).get("AVG20") or 0
    ratio_line = "—"
    if av20 is not None and av250 and ratio is not None:
        ratio_line = f"20日均 {av20:.2f}% ÷ 250日均 {av250:.2f}% = {ratio:.2f}"
    return [
        ("20日均换手", f"{av20:.2f}%" if av20 is not None else "—"),
        ("250日均换手", f"{av250:.2f}%" if av250 is not None else "—"),
        ("1月/1年换手比", f"{ratio:.2f}" if ratio is not None else "—"),
        ("自身分位", f"P{pct_av20:.0f}" if pct_av20 is not None else "—"),
        ("分位样本", f"{n_av20} 个交易日"),
        ("换手比计算", ratio_line),
    ]


def _amount_view(km, turn_calc, bars):
    """换手率不可用时的替身卡片：成交额自身的分位。"""
    av20, av250 = km.get("amt_av20"), km.get("amt_av250")
    ratio = km.get("amt_ratio")
    apct = km.get("amt_pct")
    if bars < 60 or av20 is None or av250 is None or apct is None:
        return _insufficient(
            "turnover", "交易拥挤度", "顶部区域交易通常更拥挤",
            "换手率与成交额历史都不足 60 日，本轮不判断。", turn_calc)
    if apct >= 75 and ratio is not None and ratio >= 1.2:
        state, tone = "偏高", "warn"
    elif apct >= 50:
        state, tone = "中性", "info"
    else:
        state, tone = "偏低", "muted"
    return {
        "key": "turnover", "title": "交易拥挤度",
        "subtitle": "无流通市值 → 用成交额自身的分位代替换手率",
        "state": state, "tone": tone,
        "headline": f"20日均成交额 {_fmt_amount(av20)}，自身 {apct:.0f} 分位",
        "metrics": [
            ("20日均成交额", _fmt_amount(av20)),
            ("自身分位", f"P{apct:.0f}"),
            ("1月/1年成交额比", f"{ratio:.2f}" if ratio is not None else "—"),
            ("250日均成交额", _fmt_amount(av250)),
        ],
        "calc": turn_calc,
        "note": (
            "流通市值取不到，换手率算不出来，这一张改用成交额自身的分位："
            "它不含股本变化，只看每天的成交金额在自身历史里算高还是低，"
            "所以绝对成交额小的票也可能命中高分位。"
            "它同样只描述交易拥挤度，不能单独当作顶部信号。"
        ),
        "wide": False,
    }


def turnover_view(r):
    km = r.get("kline") or {}
    bars = km.get("bars") or 0
    turn_calc = _calc_lines(km)

    if (km.get("turnover_basis") or "turnover") == "amount":
        return _amount_view(km, turn_calc, bars)

    av20, av250 = km.get("av20"), km.get("av250")
    ratio = km.get("ratio")
    pct_av20 = (km.get("pct") or {}).get("AVG20")
    apct = km.get("amt_pct")

    # 换手：只描述交易拥挤度，不判断顶部
    if bars < 60 or av20 is None or av250 is None or pct_av20 is None:
        return _insufficient(
            "turnover", "换手", "顶部区域换手通常偏高",
            "换手历史不足 60 日或换手数据缺失，本轮不判断。", turn_calc)

    if pct_av20 >= 75 and ratio is not None and ratio >= 1.2:
        state, tone = "偏高", "warn"
    elif pct_av20 >= 50:
        state, tone = "中性", "info"
    else:
        state, tone = "偏低", "muted"
    metrics = [
        ("20日均换手", f"{av20:.2f}%"),
        ("自身分位", f"P{pct_av20:.0f}"),
        ("1月/1年换手比", f"{ratio:.2f}" if ratio is not None else "—"),
        ("250日均换手", f"{av250:.2f}%"),
    ]
    note = ("换手只描述交易是否拥挤。高换手可能是顶部派发，也可能是突破放量；"
            "不能单独当作顶部信号。")
    if apct is not None:
        # 成交额分位也一并给出，但**不当主判据**：成交额随股价水平一起涨，
        # 一只票翻倍后同样的成交量会多出一倍成交额，分位会被"涨过了"本身推高。
        # 换手率 = 成交额÷流通市值，已经把这层价格效应除掉，所以拥挤度以它为准。
        metrics.append(("参考·成交额分位", f"P{apct:.0f}"))
        note += ("参考的成交额分位只作对照：它含价格水平效应（涨了一倍，"
                 "同样的成交量成交额就多一倍），在「已经涨了很多」的票上会"
                 "系统性偏高，所以主判据仍用换手率。")
    return {
        "key": "turnover", "title": "换手",
        "subtitle": "顶部区域换手通常偏高",
        "state": state, "tone": tone,
        "headline": f"20日均换手 {av20:.2f}%，自身 {pct_av20:.0f} 分位",
        "metrics": metrics,
        "calc": turn_calc,
        "note": note,
        "wide": False,
    }
