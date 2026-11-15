"""换手卡片。"""

from .common import _insufficient


def _calc_lines(km):
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


def turnover_view(r):
    km = r.get("kline") or {}
    bars = km.get("bars") or 0
    av20, av250 = km.get("av20"), km.get("av250")
    ratio = km.get("ratio")
    pct_av20 = (km.get("pct") or {}).get("AVG20")
    turn_calc = _calc_lines(km)

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
    return {
        "key": "turnover", "title": "换手",
        "subtitle": "顶部区域换手通常偏高",
        "state": state, "tone": tone,
        "headline": f"20日均换手 {av20:.2f}%，自身 {pct_av20:.0f} 分位",
        "metrics": [
            ("20日均换手", f"{av20:.2f}%"),
            ("自身分位", f"P{pct_av20:.0f}"),
            ("1月/1年换手比", f"{ratio:.2f}" if ratio is not None else "—"),
            ("250日均换手", f"{av250:.2f}%"),
        ],
        "calc": turn_calc,
        "note": (
            "换手只描述交易是否拥挤。高换手可能是顶部派发，也可能是突破放量；"
            "不能单独当作顶部信号。"
        ),
        "wide": False,
    }
