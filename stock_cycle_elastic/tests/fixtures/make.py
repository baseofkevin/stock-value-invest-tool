"""fixtures 构造库 —— 造出「接口原样」的假返回，而不是「build 之后」的干净数据。

为什么要自己造而不是录真实接口：
  * 真实返回有 280KB+，进仓库不合适；
  * 要打的边界（毛利率σ 恰好 5.0、比值恰好 2.0、分位恰好 40/60）真实数据里几乎不存在，
    只能精确构造。

关键约定：**生成的是 `data_finance` 那种嵌套结构**（income/balance 行），
而不是 build 的产物 —— 这样 build.py 的解析逻辑也在测试覆盖范围内。
行内字段名用接口真实字段名：`EndDate` / `OperatingRevenue_Q` / `OperatingCost_Q` /
`OperatingProfit_Q` / `TotalFixedAsset` / `TConstruInProcess` / `ContractLiability`。

季度序列生成：从 end_date 往前倒推 n 季，日期为标准季末（03-31 / 06-30 / 09-30 / 12-31）。
"""

import copy
import json
import os

#: 四个季末的 (月, 日)
_QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


def prev_quarter(year, quarter, back=1):
    """往回退 back 个季度。返回 (year, quarter)，quarter 为 1..4。"""
    idx = year * 4 + (quarter - 1) - back
    return idx // 4, idx % 4 + 1


def quarter_dates(last_year, last_quarter, n):
    """生成最近 n 季的日期字符串（升序）。"""
    out = []
    for back in range(n - 1, -1, -1):
        year, quarter = prev_quarter(last_year, last_quarter, back)
        month, day = _QUARTER_ENDS[quarter - 1]
        out.append(f"{year:04d}-{month:02d}-{day:02d}")
    return out


def q_index(date_str):
    """'2026-09-30' -> 3（季度序号 1..4）。"""
    return (int(date_str[5:7]) + 2) // 3


# ---------------------------------------------------------------------------
# 行构造
# ---------------------------------------------------------------------------


def income_row(date, revenue_q=None, cost_q=None, op_profit_q=None,
               revenue_cum=None, cost_cum=None, op_cum=None,
               gross_ratio=None, include_q=True):
    """造一行利润表。

    `*_q`  是单季值（`OperatingRevenue_Q` 等）；
    `*_cum` 是累计值（无 `_Q` 后缀）。

    `include_q=False` 时**不写单季字段** —— 用于测试"单季缺失→回推"和"Q4 回推陷阱"。
    """
    row = {"EndDate": date}
    if revenue_cum is not None:
        row["OperatingRevenue"] = _s(revenue_cum)
    if cost_cum is not None:
        row["OperatingCost"] = _s(cost_cum)
    if op_cum is not None:
        row["OperatingProfit"] = _s(op_cum)
    if include_q:
        if revenue_q is not None:
            row["OperatingRevenue_Q"] = _s(revenue_q)
        if cost_q is not None:
            row["OperatingCost_Q"] = _s(cost_q)
        if op_profit_q is not None:
            row["OperatingProfit_Q"] = _s(op_profit_q)
    if gross_ratio is not None:
        row["GrossIncomeRatio"] = _s(gross_ratio)
    return row


def balance_row(date, fixed_asset=None, cip=None, contract_liab=None):
    """造一行资产负债表。"""
    row = {"EndDate": date}
    if fixed_asset is not None:
        row["TotalFixedAsset"] = _s(fixed_asset)
    if cip is not None:
        row["TConstruInProcess"] = _s(cip)
    if contract_liab is not None:
        row["ContractLiability"] = _s(contract_liab)
    return row


def _s(value):
    """接口把数字都给成字符串。None 保持不写字段。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# 组装成接口返回
# ---------------------------------------------------------------------------


def finance_payload(code, income, balance):
    """包成 data_finance 的完整嵌套返回。

    实测结构：{"ok":true,"data":{"code":0,"msg":"...","data":{"sh600519":{...}}}}
    """
    prefixed = code if code[:2].isalpha() else None
    if prefixed is None:
        from cycle import codes as codes_mod
        prefixed = codes_mod.with_prefix(code)
    return {
        "ok": True,
        "data": {
            "code": 0,
            "msg": "success",
            "data": {
                prefixed: {
                    "income": income,
                    "balance": balance,
                }
            },
        },
    }


def profile_payload(name, industry):
    return {"ok": True, "data": {"name": name, "industry": industry}}


def bundle(code, income, balance, name="", industry="", with_profile=True):
    """--bundle 文件内容。"""
    calls = {"data_finance": finance_payload(code, income, balance)}
    if with_profile:
        calls["data_profile"] = profile_payload(name, industry)
    return {
        "code": code,
        "name": name,
        "industry": industry,
        "calls": calls,
    }


def dump(obj, path):
    """写 fixture 到磁盘（utf-8，缩进 1 —— 280KB 的文件用 2 空格太占地方）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    return path


def deep_copy(obj):
    return copy.deepcopy(obj)


# ---------------------------------------------------------------------------
# 场景构造器：按序列给毛利率 / 营收 / 营业利润
# ---------------------------------------------------------------------------


def build_series(dates, gross_margins, revenues, op_profits,
                 fixed_assets=None, cips=None, contract_liabs=None,
                 with_balance=True):
    """按逐季序列造 (income, balance) 两张表。

    参数都是与 dates 等长的列表；元素为 None 表示该季缺这个字段（整项不写）。
    """
    n = len(dates)
    fixed_assets = fixed_assets if fixed_assets is not None else [1.0e10] * n
    cips = cips if cips is not None else [2.0e9] * n
    contract_liabs = contract_liabs if contract_liabs is not None else [5.0e8] * n

    income = []
    for i, date in enumerate(dates):
        gm = gross_margins[i]
        rev = revenues[i]
        cost = None
        if rev is not None and gm is not None:
            cost = rev * (1.0 - gm)
        income.append(income_row(
            date,
            revenue_q=rev,
            cost_q=cost,
            op_profit_q=op_profits[i],
        ))

    balance = []
    if with_balance:
        for i, date in enumerate(dates):
            balance.append(balance_row(
                date,
                fixed_asset=fixed_assets[i],
                cip=cips[i],
                contract_liab=contract_liabs[i],
            ))
    return income, balance
