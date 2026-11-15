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
    pos250 = km.get("pos250")

    # 二维（2026-11-15）：**"涨得多"和"位置高"是两件事**。
    # 旧版只看 20 日涨幅，于是「从深坑里 20 日反弹 40%、离 60 日高点还有 -14%」
    # 也会被标成"过热"——那不是顶部的过热，是下跌后的剧烈反抽。
    # 实测 40 只里 4 只被这样误标（阿尔特 +40.7%/距高 -14.0%、海量数据
    # +20.6%/-14.0%、日海智能 +17.1%/-16.6%、通力科技 +14.8%/-11.4%）。
    if ret20 >= 0.20:
        speed, tone_s = "过热", "warn"
    elif ret20 >= 0.10:
        speed, tone_s = "偏热", "warn"
    elif ret20 >= 0:
        speed, tone_s = "走平", "info"
    else:
        speed, tone_s = "偏弱", "muted"

    # 位置用 250 日（顶部判断的时间尺度是年线级别）；不足 250 根时退回 60 日，
    # 并在副标题里写明口径。
    if pos250 is not None:
        pos, pos_basis = pos250, "250日"
    else:
        pos, pos_basis = pos60, "60日"
    if pos >= -0.05:
        where, tone_p = "高位", "warn"
    elif pos >= -0.20:
        where, tone_p = "中位", "info"
    else:
        where, tone_p = "低位", "muted"

    state = f"{where}·{speed}"
    # 色调取"更值得看"的那一侧：高位永远要先看，其次才是涨速
    tone = tone_p if tone_p == "warn" else tone_s

    if where == "高位" and speed in ("过热", "偏热"):
        headline = f"20日 {fmt_pct(ret20)}，距{pos_basis}高点 {fmt_pct(pos)}——高位加速"
    elif where != "高位" and speed in ("过热", "偏热"):
        # 涨得急但没到高位：这是反抽，不是见顶。把"离高点还有多远"顶到句首，
        # 否则读者只看见"过热"两个字就当成顶部信号。
        headline = (f"距{pos_basis}高点仍有 {fmt_pct(pos)}，20日 {fmt_pct(ret20)}"
                    f"——{'低位反抽' if where == '低位' else '尚未到高位'}，不是高位过热")
    else:
        headline = f"20日 {fmt_pct(ret20)}，距{pos_basis}高点 {fmt_pct(pos)}"
    return {
        "key": "momentum", "title": "动量",
        "subtitle": f"涨幅（20日）× 位置（距{pos_basis}高点）两个维度",
        "state": state, "tone": tone,
        "headline": headline,
        "metrics": [
            ("20日涨幅", fmt_pct(ret20)),
            ("5日涨幅", fmt_pct(km.get("ret5"))),
            ("60日涨幅", fmt_pct(ret60)),
            (f"距{pos_basis}高点", fmt_pct(pos)),
        ],
        "calc": mom_calc,
        "note": (
            "动量分两个维度：涨了多少（20 日涨幅）和现在在哪个位置（距 250 日高点），"
            "两者分开看才有意义——低位大反弹不是过热，高位继续加速才值得警惕。"
            "但两者都不等于见顶：高位强动量既可能延续，也可能是加速赶顶。"
        ),
        "wide": False,
    }
