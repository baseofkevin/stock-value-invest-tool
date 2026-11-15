"""百分比与数值格式化。"""


def fmt_pct(x, signed=True):
    if x is None:
        return "—"
    return (f"{x * 100:+.2f}%" if signed else f"{x * 100:.2f}%")


def fmt_pct_point(x):
    return f"{x:+.1f}%" if x is not None else "—"


def fmt_pct_frac(x, limit=1000.0):
    """x 是小数（0.1 = 10%），用于环比等可能极端的增速。"""
    if x is None:
        return "—"
    y = x * 100
    if abs(y) > limit:
        return "极端值（基数异常）"
    return f"{y:+.1f}%"
