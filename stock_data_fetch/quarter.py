"""单季值的兜底推导。

库里的 `finance` 表**直接存上游给的 `_Q`**（单季），因为上游给了就不该自己算。
但上游偶尔漏：某些期 `_Q` 是空的，而累计值在。这时才轮到本模块，优先级：

| 顺序 | 条件 | 做法 | `src` |
|---|---|---|---|
| 1 | 该期 `_Q` 非空 | 直接用 | `stored` |
| 2 | 报告期是 Q1（`-03-31`） | 累计值**就是**单季值（年初至今 = 一个季度） | `q1_cum` |
| 3 | 上一期存在且相邻 | `本期累计 − 上期累计` | `derived` |
| 4 | 上一期缺失 | **记 `missing`，值为 `None`** | `missing` |

第 4 条是这个模块存在的唯一理由。填 0 的话下游会发生什么：

- "同比 +∞%"、"环比 −100%" 这种一眼假的还能发现；
- 真正危险的是**看起来合理**的那类：某季营收本来是 300 亿，填 0 后全年营收少 300 亿，
  ROE 从 18% 变成 12%，`quality` 判"不达标"，这只股票被静默踢出候选池——
  报告不报错、日志不告警，只是名单里少了一只。

所以宁可让 `value is None` 一路上浮，让消费侧显式决定"这期不用"或"这只跳过"。

**不做的事**：不推导 TTM。TTM 需要"连续四个单季"，而上游 `_TTM` 也是直接给的；
真到了 `_TTM` 缺失的时候，缺的往往是一整段，硬凑出来的 TTM 比 `None` 更难发现。
那件事的取舍留给 `cycle`（它知道自己的 40 期门槛），不在这里替它决定。
"""

from __future__ import annotations

import sqlite3

__all__ = ["rows", "value", "qfield", "prev_period", "QFIELDS"]

QFIELDS: dict[str, str] = {
    "OperatingRevenue": "OperatingRevenue_Q",
    "OperatingCost": "OperatingCost_Q",
    "OperatingProfit": "OperatingProfit_Q",
    "NPParentCompanyOwners": "NPParentCompanyOwners_Q",
}
"""累计列名 → 单季列名。不在这张表里的字段按 `<原名>_Q` 猜，猜不到就查库报错。"""

_QUARTER_ENDS = ("03-31", "06-30", "09-30", "12-31")


def qfield(field: str) -> str:
    return QFIELDS.get(field, f"{field}_Q")


def prev_period(period: str) -> str:
    """上一个报告期。`2026-06-30` → `2026-03-31`；`2026-03-31` → `2025-12-31`。

    **严格相邻**：只退一个季度。差分期要的是"相邻两期相减"，跨过一期（比如直接去找
    "上一个存在的期"）会把两个季度的差算成一个季度，数值偏大一倍且看不出异常。
    """
    y, tail = period[:4], period[5:]
    try:
        i = _QUARTER_ENDS.index(tail)
    except ValueError:
        return ""
    if i == 0:
        return f"{int(y) - 1}-12-31"
    return f"{y}-{_QUARTER_ENDS[i - 1]}"


def rows(conn: sqlite3.Connection, symbol: str, field: str) -> list[dict]:
    """整段单季序列，按报告期升序。`field` 传累计口径的原名。"""
    qcol = qfield(field)
    recs = conn.execute(
        f"SELECT period, {field} AS cum, {qcol} AS q FROM finance "
        "WHERE symbol=? ORDER BY period", (symbol,)).fetchall()
    cum = {r["period"]: r["cum"] for r in recs}

    out: list[dict] = []
    for r in recs:
        period = r["period"]
        if r["q"] is not None:
            out.append({"period": period, "value": float(r["q"]), "src": "stored"})
            continue
        if r["cum"] is None:                       # 连累计值都没有，等于这一期没取到
            out.append({"period": period, "value": None, "src": "missing"})
            continue
        if period.endswith("-03-31"):
            out.append({"period": period, "value": float(r["cum"]), "src": "q1_cum"})
            continue
        prev = cum.get(prev_period(period))
        if prev is None:
            out.append({"period": period, "value": None, "src": "missing"})
            continue
        out.append({"period": period, "value": float(r["cum"]) - float(prev), "src": "derived"})
    return out


def value(conn: sqlite3.Connection, symbol: str, field: str,
          period: str | None = None) -> dict:
    """单期的单季值。`period` 省略 = 最新一期。查不到就给 `missing`，不抛。"""
    series = rows(conn, symbol, field)
    if period is None:
        return series[-1] if series else {"period": None, "value": None, "src": "missing"}
    for r in series:
        if r["period"] == period:
            return r
    return {"period": period, "value": None, "src": "missing"}
