"""因子卡片共用的小工具与「数据不足」模板。"""


def _fmt_px(v):
    return f"{v:.2f}" if v is not None else "—"


def _fmt_yi(v):
    if v is None:
        return "—"
    return f"{v / 1e8:,.2f} 亿元"


def _fmt_vol(v):
    if v is None:
        return "—"
    return f"{v:,.0f}"


def _insufficient(key, title, subtitle, note, calc=None):
    return {
        "key": key, "title": title, "subtitle": subtitle,
        "state": "数据不足", "tone": "muted", "headline": "数据不足，不判断",
        "metrics": [], "calc": calc or [], "note": note, "wide": False,
    }
