"""数值与日期小工具。"""


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pdate(v):
    return str(v)[:10] if v else None
