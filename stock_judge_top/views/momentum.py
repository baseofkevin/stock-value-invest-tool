"""动量卡片。"""
from ..render.format import fmt_pct
from .common import _fmt_px, _insufficient


def momentum_view(r):
    km = r.get("kline") or {}
    bars = km.get("bars") or 0
    mom_calc = [
        ("当前收盘", _fmt_px(km.get("close"))),
        ("5日前收盘", _fmt_px(km.get("close_5_ago"))),
        ("20日前收盘", _fmt_px(km.get("close_20_ago"))),
        ("60日前收盘", _fmt_px(km.get("close_60_ago"))),
        # 位置类指标用最高价口径，标签必须写「最高价」，不能写「最高收盘」
        ("60日最高价", f"{_fmt_px(km.get('high60'))}（{km.get('high60_date') or '—'}）"),
        ("5日涨幅计算", f"收盘 {_fmt_px(km.get('close'))} ÷ 5日前收盘 "
                     f"{_fmt_px(km.get('close_5_ago'))} − 1 = {fmt_pct(km.get('ret5'))}"),
        ("20日涨幅计算", f"收盘 {_fmt_px(km.get('close'))} ÷ 20日前收盘 "
                      f"{_fmt_px(km.get('close_20_ago'))} − 1 = {fmt_pct(km.get('ret20'))}"),
        ("60日涨幅计算", f"收盘 {_fmt_px(km.get('close'))} ÷ 60日前收盘 "
                      f"{_fmt_px(km.get('close_60_ago'))} − 1 = {fmt_pct(km.get('ret60'))}"),
    ]
    if bars < 60 or km.get("ret20") is None or km.get("pos60") is None:
        return _insufficient(
            "momentum", "动量", "股价走势的延续性",
            "K线不足 60 日或动量数据缺失，本轮不判断。", mom_calc)

    ret20, ret60, pos60 = km["ret20"], km.get("ret60"), km["pos60"]
    if ret20 >= 0.20:
        state, tone = "过热", "warn"
    elif ret20 >= 0.10:
        state, tone = "偏热", "warn"
    elif ret20 >= 0:
        state, tone = "中性", "info"
    else:
        state, tone = "偏弱", "muted"
    return {
        "key": "momentum", "title": "动量",
        "subtitle": "股价走势的延续性",
        "state": state, "tone": tone,
        "headline": f"20日 {fmt_pct(ret20)}，距60日高点 {fmt_pct(pos60)}",
        "metrics": [
            ("20日涨幅", fmt_pct(ret20)),
            ("5日涨幅", fmt_pct(km.get("ret5"))),
            ("60日涨幅", fmt_pct(ret60)),
            ("距60日高点", fmt_pct(pos60)),
        ],
        "calc": mom_calc,
        "note": (
            "动量只描述“已经涨了多少、是否还在高位”，不等于见顶。"
            "高位强动量既可能延续，也可能是加速赶顶，需要你结合其他因子判断。"
        ),
        "wide": False,
    }
