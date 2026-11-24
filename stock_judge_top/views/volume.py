"""量价卡片。"""
from ..render.format import fmt_pct
from .common import _fmt_px, _fmt_vol, _insufficient


def volume_view(r):
    km = r.get("kline") or {}
    bars = km.get("bars") or 0
    # 4. 量价：放量盘整只作观察，不单独触发
    hi5, lo5 = km.get("range5_high"), km.get("range5_low")
    range5 = (hi5 / lo5 - 1) if (hi5 and lo5) else None
    vr, ret5, pos60 = km.get("pvol_ratio5"), km.get("ret5"), km.get("pos60")
    # 「高位」用 250 日（2026-11-15）：顶部判断的时间尺度是年线级别。
    # 旧版用 pos60（距 60 日高点 5% 内即算高位），一只从两年高点腰斩、
    # 现在刚回到 60 日高点的票会被当成"高位放量"。不足 250 根时退回 60 日。
    pos = km.get("pos250") if km.get("pos250") is not None else pos60
    pos_basis = "250日" if km.get("pos250") is not None else "60日"
    vol_calc = [
        ("5日均量（原始）", _fmt_vol(km.get("pav5"))),
        ("20日均量（原始）", _fmt_vol(km.get("pav20"))),
        ("5日/20日量比", f"{vr:.2f}" if vr is not None else "—"),
        ("量比计算", (f"5日均量 {_fmt_vol(km.get('pav5'))} ÷ 20日均量 "
                    f"{_fmt_vol(km.get('pav20'))}")),
        ("5日收盘区间", f"{_fmt_px(lo5)} ~ {_fmt_px(hi5)}"),
        ("5日振幅", fmt_pct(range5, signed=False)),
        ("5日涨跌", fmt_pct(ret5)),
        (f"距{pos_basis}高点", fmt_pct(pos)),
    ]
    if bars < 60 or vr is None or range5 is None or pos is None:
        return _insufficient(
            "volume", "量价", "放量但价格盘整",
            "K线不足 60 日或量价数据缺失，本轮不判断。", vol_calc)
    else:
        if pos >= -0.05 and vr >= 1.5 and range5 <= 0.03:
            state, tone = "放量盘整", "warn"
            note = (f"距 {pos_basis} 高点 {fmt_pct(pos)}、量比 {vr:.2f}、5 日振幅 "
                    f"{fmt_pct(range5, signed=False)}：高位放量而价格窄幅盘整。"
                    "历史样本显示它的增量有限，只能作为观察项，不能单独触发。")
        elif pos >= -0.05 and vr >= 1.5 and ret5 >= 0.02:
            state, tone = "放量上涨", "warn"
            note = "高位放量且价格继续上涨，更像加速赶顶，但同样不能单独确认顶部。"
        elif vr >= 1.5:
            state, tone = "放量（非高位）", "info"
            note = f"成交量放大，但价格不在 {pos_basis} 高位附近；不作为高位量价信号。"
        else:
            state, tone = "未触发", "ok"
            note = "没有出现明显的放量盘整或放量上涨，量价项不构成观察信号。"
        return {
            "key": "volume", "title": "量价",
            "subtitle": f"放量但价格盘整（高位=距{pos_basis}高点 5% 内）",
            "state": state, "tone": tone,
            "headline": f"量比 {vr:.2f}，5日振幅 {fmt_pct(range5, signed=False)}，"
                       f"5日涨跌 {fmt_pct(ret5)}",
            "metrics": [
                ("5日/20日量比", f"{vr:.2f}"),
                ("5日振幅", fmt_pct(range5, signed=False)),
                ("5日涨跌", fmt_pct(ret5)),
                (f"距{pos_basis}高点", fmt_pct(pos)),
            ],
            "calc": vol_calc,
            "note": note,
            "wide": False,
        }
