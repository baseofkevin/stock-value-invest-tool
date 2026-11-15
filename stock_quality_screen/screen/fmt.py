# -*- coding: utf-8 -*-
"""数值格式化：全项目唯一一份实现。

以前三套并存（`score._p/_n/_yi`、`render.n/pct/yi`、`util.pct/yi/ratio/text`），
同一个数字在不同章节可能差一个千分位或一个「亿」，改口径要改三处。现在只留这里，
调用方按需取：

    num / num_sep   两位小数；num 不带千分位，num_sep 带（金额用）
    pct             百分数，默认一位
    signed_pct      带正负号的百分数（增速）
    yi / yi_suffix  元 → 亿；yi 只给数字，yi_suffix 补「 亿」

三条约定：
  1. None 一律输出 `—`（U+2014），不用 0 或空串冒充数据；
  2. **NaN / ±Inf 也输出 `—`**。它们在语义上与 None 相同（都表示「算不出来」），
     但 `float("nan")` 能通过 `float()` 转换，于是旧实现会原样格式化成字符串
     `"nan"` / `"nan%"` / `"inf"` 落到报告页面上 —— 读者会以为整份报告坏了。
     上游 `0/0`、`inf-inf` 这类运算很容易产出 nan，这条守卫必须在最下层。
  3. 传进来不是数字（比如接口直接给了中文）时原样转义输出，不抛错——
     报告缺一格比整份报告生成失败好。
"""
from __future__ import annotations

import html
import math
from typing import Any

DASH = "—"

#: 字符串形态的非有限值。上游有时把 nan 当字符串给出来，这些也要收敛成 `—`。
_NON_FINITE_TEXT = frozenset({"nan", "inf", "-inf", "+inf", "infinity", "-infinity"})


def _raw(value: Any) -> str:
    return html.escape(str("" if value is None else value), quote=True)


def _try_float(value: Any) -> float | None:
    """转成有限浮点数；转不了或非有限一律 None。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fmt(value: Any, spec: str, *, suffix: str = "", scale: float = 1.0) -> str:
    """统一入口：None/NaN/Inf → `—`，非数字原样转义，有限数按 spec 格式化。

    `spec` 是格式串（如 `",.2f"`）；`suffix` 拼在数值后面（`%` / ` 亿`）；
    `scale` 先乘上去（`signed_pct` 要 ×100、`yi` 要 ÷1e8）。

    非数字与「非有限数字」要分开处理：
      * `float("nan")` 能转成 float，但它表示「算不出来」，必须给 `—`；
      * `"abc"` 转不了 float，是上游给了脏值，原样输出比崩掉好（约定 3）。
    """
    if value is None:
        return DASH
    if isinstance(value, bool):
        # bool 是 int 子类，`True` 会被格式化成 "1.00"。财务值不该出现 bool，
        # 出现了就是上游字段错位，宁可显示原值也不显示一个假的数。
        return _raw(value)

    f = _try_float(value)
    if f is None:
        if isinstance(value, (int, float)):
            return DASH                     # nan / inf
        if isinstance(value, str) and value.strip().lower() in _NON_FINITE_TEXT:
            return DASH                     # "nan" / "inf" 这类字符串
        return _raw(value)                  # 真·非数字：原样输出
    return f"{f * scale:{spec}}{suffix}"


def num(value: Any, digits: int = 2) -> str:
    """1.234 → "1.23"；None / nan → "—"。不带千分位。"""
    return _fmt(value, f".{digits}f")


def num_sep(value: Any, digits: int = 2) -> str:
    """1234.5 → "1,234.50"；None / nan → "—"。带千分位，给金额和倍数用。"""
    return _fmt(value, f",.{digits}f")


def pct(value: Any, digits: int = 1) -> str:
    """12.34 → "12.3%"；None / nan → "—"。"""
    return _fmt(value, f".{digits}f", suffix="%")


def signed_pct(value: Any, digits: int = 1) -> str:
    """0.1234（小数）→ "+12.3%"；None / nan → "—"。

    注意入参是**小数**（0.1234），内部乘 100 再格式化。
    """
    return _fmt(value, f"+.{digits}f", suffix="%", scale=100.0)


def yi(value: Any, digits: int = 2) -> str:
    """1e8（元）→ "1.00"，单位由调用方在表头写。"""
    return _fmt(value, f",.{digits}f", scale=1e-8)


def yi_suffix(value: Any, digits: int = 2) -> str:
    """1e8（元）→ "1.00 亿"，单位跟着数值走。"""
    return _fmt(value, f",.{digits}f", suffix=" 亿", scale=1e-8)
