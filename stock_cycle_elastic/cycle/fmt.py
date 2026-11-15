"""数值 / 属性格式化。

从 `render.py` 里拆出来（2026-11-15 移除 HTML 报告时）：这些函数不是"画页面"，
而是**边界一致性**的一部分——σ 判 4.95 是"非周期"，终端就不能显示成 5.0 pp。
只要还有一处打印读数，它就得跟着走，所以单独成文件、单独测。

`lower` / `upper` 传判定阈值时，`_round_same_side` 保证四舍五入不跨越判定线。
"""

import math

DASH = "—"


def fmt_num(value, digits=2, suffix="", lower=None, upper=None):
    """数值格式化。None / 非有限数 → 破折号（不出现 "None"/"nan"）。

    `nan` 和 `inf` 也要挡掉：上游算出一个 0/0 就会漏进来，
    直接 f-string 会把 "nan" 原样打进页面，比显示"缺失"更误导。

    `lower` / `upper` 传判定阈值时，保证显示值不跨线（见 `_round_same_side`）。
    """
    if value is None:
        return DASH
    try:
        if not math.isfinite(value):
            return DASH
        return f"{_round_same_side(value, digits, lower, upper):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return DASH


def fmt_yi(value, digits=1):
    """元 → 亿元。"""
    if value is None:
        return DASH
    try:
        if not math.isfinite(value):
            return DASH
    except TypeError:
        return DASH
    return f"{value / 1e8:.{digits}f} 亿"


def _floor_digits(value, digits):
    """向下截断到 digits 位小数。"""
    factor = 10 ** digits
    return math.floor(value * factor) / factor


def _ceil_digits(value, digits):
    """向上截断到 digits 位小数。"""
    factor = 10 ** digits
    return math.ceil(value * factor) / factor


def _round_same_side(value, digits, lower=None, upper=None):
    """按 digits 位四舍五入，但**不许跨过判定边界**。

    坑（真实发生过）：川发龙蟒 σ = 4.9505，判「非周期」（< 5pp 正确），
    但 1 位小数显示成「5.0 pp」，页面上读起来是「5.0 pp < 5 pp」——自相矛盾。

    修法不是把阈值改宽，也不是四舍五入改成"四舍六入"，
    而是：舍入后如果越过了 lower/upper（原值并没有越过），
    就改为朝原值所在的一侧截断。这样**显示值是判定值的一个单调近似**，
    看得见的数字与看不见的标签永远在同一侧，且不会夸大精度。
    """
    rounded = round(value, digits)
    # 下边界：原值在线上方，舍入后掉到线下方 → 向上截断
    if lower is not None:
        if value >= lower and rounded < lower:
            return _ceil_digits(value, digits)
        if value < lower and rounded >= lower:
            return _floor_digits(value, digits)
    # 上边界：原值在线下方，舍入后冒到线上方 → 向下截断
    if upper is not None:
        if value <= upper and rounded > upper:
            return _floor_digits(value, digits)
        if value > upper and rounded <= upper:
            return _ceil_digits(value, digits)
    return rounded


def fmt_pp(value, digits=2, lower=None, upper=None):
    """百分点。默认 2 位小数：σ 常年贴着 5pp 这条线，1 位小数会把 4.95 显示成 5.0。

    `lower` / `upper` 传判定阈值时，保证显示值不跨线（见 `_round_same_side`）。
    """
    if value is None:
        return DASH
    try:
        if not math.isfinite(value):
            return DASH
    except TypeError:
        return DASH
    return f"{_round_same_side(value, digits, lower, upper):.{digits}f} pp"


def fmt_pct(value, digits=1, lower=None, upper=None):
    """百分比。`lower` / `upper` 同上，用于保证不跨分位边界。"""
    if value is None:
        return DASH
    try:
        if not math.isfinite(value):
            return DASH
    except TypeError:
        return DASH
    return f"{_round_same_side(value, digits, lower, upper):.{digits}f}%"
