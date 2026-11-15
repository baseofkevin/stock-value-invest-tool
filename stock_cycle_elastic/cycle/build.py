"""raw → 季度序列（README.md「build：两表对齐」）。

职责只有一个：把 `income` 与 `balance` 按日期对齐成**干净、升序、无空行**的季度序列。
不做任何指标计算（那是 metrics.py 的事），不做任何判断（那是 decide.py 的事）。

输出结构（字段白名单，test_build 会逐字断言）：

    {
      "code": "601899", "name": "紫金矿业", "industry": "有色金属",
      "quarters": [
        {
          "date": "2016-03-31",
          "revenue_q": ...,          # 单季营收（元）
          "cost_q": ...,             # 单季成本（元）
          "op_profit_q": ...,        # 单季营业利润（元）
          "fixed_asset": ...,        # 时点值
          "cip": ...,                # 在建工程，时点值
          "contract_liab": ...,      # 时点值，仅透传给报告展示（§2.8）
          "gross_margin": ...,       # 派生
          "capacity_turnover": ...,  # 派生：TTM 营收 ÷ (固资+在建)，前 3 季为 None
        }, ...
      ],
      "meta": {"quarters": 43, "data_gaps": [...]},
    }

**6 个参与计算的原始字段 + 1 个透传字段 + 2 个派生值 = 9 个 key**，一个不多。
（`_capacity_base` 是循环内的临时字段，回填完周转率后立即删除，不进最终结构。）
`net_margin` / `cfo_q` / `short_debt` / `total_profit_q` / `fin_expense_q` 一律不得出现。

Q4 回推陷阱（最容易写错的地方）：
    接口的 `OperatingRevenue_Q` 是单季值。若某期缺这个字段，可以用
    "本期累计 − 上期累计" 回推。但 Q4 **不能**用"年报累计 − 三季报累计"以外的路子，
    而三季报缺失时这条也断了 —— 此时必须让 revenue_q = None，
    **绝不能拿年报累计值冒充单季**（会把 Q4 营收放大 4 倍，判断②直接崩）。
"""

import re

from cycle import util

#: 季度字典的**精确** key 集合（顺序即构造顺序）
QUARTER_KEYS = (
    "date",
    "revenue_q",
    "cost_q",
    "op_profit_q",
    "fixed_asset",
    "cip",
    "contract_liab",
    "gross_margin",
    "capacity_turnover",
)

#: 允许出现的数据缺失原因（写进 meta.data_gaps，便于排障）
GAP_BAD_DATE = "日期格式异常"
GAP_DUP_DATE = "同日期重复，取首条"
GAP_EMPTY_ROW = "整行无数据，已剔除"
GAP_REVENUE_MISSING = "缺单季营收且无法回推"
GAP_BALANCE_MISSING = "缺资产负债表"
GAP_GROSS_RATIO = "自算毛利率与接口预算偏差超 1pp"

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


# ---------------------------------------------------------------------------
# 日期工具
# ---------------------------------------------------------------------------


def _norm_date(value):
    """把接口日期归一成 'YYYY-MM-DD'。无法识别返回 None。

    接口正常给 '2026-09-30'；历史上出现过 '2026/09/30'、带时间的写法，一律容错。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # 只取前 10 个字符再匹配，顺手兼容 '2026-09-30 00:00:00 +0800 CST'
    head = text[:10]
    if _DATE_RE.match(head):
        return head if _valid_month_day(head) else None
    # '2026/09/30' 这类斜杠写法
    alt = head.replace("/", "-")
    if _DATE_RE.match(alt):
        return alt if _valid_month_day(alt) else None
    return None


def _valid_month_day(date_str):
    """`\\d{2}` 会把 `2025-13-45` 放过去 —— 月份/日必须落在真实范围。

    Q4 才能拿到年报，所以第 4 季的日期还必须是 12 月；
    这里只做粗校验（月 1-12、日 1-31），够挡住明显的脏数据，
    又不至于因为"2 月 30 日"这种边缘把正常数据拒了。
    """
    month = int(date_str[5:7])
    day = int(date_str[8:10])
    return 1 <= month <= 12 and 1 <= day <= 31


def _row_date(row):
    """从一行里挖日期。接口字段名有过 EndDate / date 两种。"""
    if not isinstance(row, dict):
        return None
    for key in ("EndDate", "date", "REPORT_DATE", "EndDateStr"):
        got = _norm_date(row.get(key))
        if got:
            return got
    return None


def _quarter_of(date_str):
    """'2016-03-31' → 4（季度序号 1-4）。用于 Q4 回推时判断上一期是什么。"""
    month = int(date_str[5:7])
    return (month + 2) // 3


# ---------------------------------------------------------------------------
# 单季值提取（含回推）
# ---------------------------------------------------------------------------


def _cumulative(row, base):
    """取累计值：`OperatingRevenue` 这类无 _Q 后缀的字段。"""
    return util.num(row.get(base)) if isinstance(row, dict) else None


def _single_q(row, base):
    """取单季值：`OperatingRevenue_Q`。"""
    return util.num(row.get(base + "_Q")) if isinstance(row, dict) else None


def _single_or_derive(rows_by_date, dates, idx, base, gaps):
    """取单季值，缺失时用"本期累计 − 上期累计"回推。

    `rows_by_date` 是 date -> 累计行；`dates` 升序；`idx` 是当前下标。
    返回 (值, 是否回推)。
    """
    date = dates[idx]
    row = rows_by_date.get(date) or {}
    direct = _single_q(row, base)
    if direct is not None:
        return direct, False

    cum_now = _cumulative(row, base)
    if cum_now is None:
        return None, False

    # 一季报的累计 = 单季，可以直接用（这是唯一安全的"回推"）
    if _quarter_of(date) == 1:
        return cum_now, True

    # 需要上一期累计。两条都必须满足：
    #   1. 同一年度 —— 跨年累计口径不同（年报 vs 一季报），相减无意义
    #   2. **相邻季度** —— 这是最容易漏的一条。三季报缺失时，上一条能找到的
    #      记录是半年报，"年报 − 半年报" 得到的是 Q3+Q4 两季合计，
    #      当成 Q4 单季会把营收放大近 2 倍（Q4 陷阱的另一种形态）。
    if idx == 0:
        gaps.append(f"{date} {base}：首期无上期累计，无法回推")
        return None, False
    prev_date = dates[idx - 1]
    if not prev_date.startswith(date[:4]):
        gaps.append(f"{date} {base}：上期跨年（{prev_date}），不跨年相减")
        return None, False
    if _quarter_of(prev_date) != _quarter_of(date) - 1:
        gaps.append(
            f"{date} {base}：上期不相邻（{prev_date}），"
            f"相减得到的是多季合计，不拿年报/半年报冒充单季"
        )
        return None, False
    prev = rows_by_date.get(prev_date) or {}
    cum_prev = _cumulative(prev, base)
    if cum_prev is None:
        # Q4 回推陷阱：三季报缺失 → 年报 − 无 = 无法回推。
        # 绝不能用年报累计冒充单季。
        gaps.append(f"{date} {base}：上期累计缺失，无法回推（不拿年报冒充单季）")
        return None, False

    return cum_now - cum_prev, True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build(raw):
    """把 fetch 的原始返回整理成季度序列。

    `raw` 形如：
        {"code","name","industry","income":[...],"balance":[...],"data_gaps":[...]}
    返回结构见模块 docstring。**不抛错**：任何异常数据都跳过并记 data_gaps。
    """
    income_rows = [r for r in (raw.get("income") or []) if isinstance(r, dict)]
    balance_rows = [r for r in (raw.get("balance") or []) if isinstance(r, dict)]
    gaps = list(raw.get("data_gaps") or [])

    income_by_date = _index_by_date(income_rows, gaps, "income")
    balance_by_date = _index_by_date(balance_rows, gaps, "balance")

    dates = sorted(income_by_date.keys())
    if not dates:
        return _result(raw, [], gaps)

    quarters = []
    for idx, date in enumerate(dates):
        row = income_by_date[date]

        revenue = _single_or_derive(income_by_date, dates, idx, "OperatingRevenue", gaps)
        cost = _single_or_derive(income_by_date, dates, idx, "OperatingCost", gaps)
        revenue_q, revenue_derived = revenue
        cost_q, cost_derived = cost

        op_profit_q = _single_q(row, "OperatingProfit")
        if op_profit_q is None:
            # 营业利润同样支持回推（Q4 陷阱一致）
            op_profit_q, _ = _single_or_derive(
                income_by_date, dates, idx, "OperatingProfit", gaps)

        if revenue_q is None:
            gaps.append(f"{date} {GAP_REVENUE_MISSING}，该季剔除")

        bal = balance_by_date.get(date) or {}
        fixed_asset = util.num(bal.get("TotalFixedAsset"))
        cip = util.num(bal.get("TConstruInProcess"))
        contract_liab = util.num(bal.get("ContractLiability"))
        if date not in balance_by_date:
            gaps.append(f"{date} {GAP_BALANCE_MISSING}")

        gross_margin = util.safe_div(
            (revenue_q - cost_q) if (revenue_q is not None and cost_q is not None) else None,
            revenue_q,
        )
        # 产能周转率：年化营收 / (固定资产 + 在建工程)
        base_assets = None
        if fixed_asset is not None and cip is not None:
            base_assets = fixed_asset + cip
        elif fixed_asset is not None:
            base_assets = fixed_asset
        elif cip is not None:
            base_assets = cip
        if base_assets is not None and base_assets <= 0:
            base_assets = None   # 脏数据（负债科目录成正数之类），当作不可用
        # 营收为 0 的季度（停工/数据错）不该产出"周转率 0" —— 那是伪数据，
        # 会被分位算法当成一个真实的低点，把判断③ 拉偏。当缺失处理。
        if revenue_q is not None and revenue_q <= 0:
            base_assets = None
        # 产能周转率留到循环后算：它要「最近 4 季营收之和」（TTM），
        # 逐季循环里拿不到未来数据，只能先记下分母。
        capacity_base = base_assets

        # 交叉校验：接口预算的毛利率（累计口径）与自算值的偏差。
        # 只报偏差，不参与计算 —— 它口径不同，偏差大不一定是错。
        _cross_check_gross(income_by_date, date, gross_margin, gaps)

        # 整行全空的季度直接剔除（不制造 9 个 None 的空壳）
        if _all_none(revenue_q, cost_q, op_profit_q, fixed_asset, cip, contract_liab):
            gaps.append(f"{date} {GAP_EMPTY_ROW}")
            continue

        quarters.append({
            "date": date,
            "revenue_q": revenue_q,
            "cost_q": cost_q,
            "op_profit_q": op_profit_q,
            "fixed_asset": fixed_asset,
            "cip": cip,
            "contract_liab": contract_liab,
            "gross_margin": gross_margin,
            "capacity_turnover": None,      # 下面 _fill_capacity_turnover 回填
            "_capacity_base": capacity_base,
        })

    _fill_capacity_turnover(quarters, gaps)
    for q in quarters:
        q.pop("_capacity_base", None)

    return _result(raw, quarters, gaps)


def _fill_capacity_turnover(quarters, gaps):
    """产能周转率 = 最近 4 季营收之和 / (固定资产 + 在建工程)。**就地回填**。

    为什么是 TTM 而不是「单季营收 × 4」：
        ×4 只是把单季的季节性等比例放大，没有消除它。若最新一季恰好是淡季
        （Q1），周转率会系统性偏低 → 分位偏低 → 误判「低位」。
        实测两种口径的分位差异：中位 5.0pp、P90 22.5pp、最大 40pp，
        15% 的股票标签会因此翻转。
        更重要的是：卖方原始口径（兴证策略）写的就是「周转率 **TTM** 的
        近十年分位数」，×4 偏离了引用来源。

    已知代价：最早 3 季拿不到 4 季滚动和，周转率为 None。
    所以名义 40 季窗口最多产出 37 个分位样本（见 POSITION_MIN_SAMPLES 的说明）。
    """
    n = len(quarters)
    if n < 4:
        return
    for i in range(n):
        if i < 3:
            continue                      # 凑不满 4 季，留 None
        base = quarters[i].get("_capacity_base")
        if base is None:
            continue
        window = [quarters[j].get("revenue_q") for j in range(i - 3, i + 1)]
        if any(v is None or v <= 0 for v in window):
            # 有缺失或非正营收 → 凑不出有意义的 TTM，当缺失处理。
            # 不用「部分求和」：那会让某一季的 TTM 只含 2-3 季营收，偏低。
            continue
        quarters[i]["capacity_turnover"] = sum(window) / base


def _result(raw, quarters, gaps):
    return {
        "code": raw.get("code") or "",
        "name": raw.get("name") or "",
        "industry": raw.get("industry") or "",
        "quarters": quarters,
        "meta": {
            "quarters": len(quarters),
            "data_gaps": _dedupe(gaps),
        },
    }


def _index_by_date(rows, gaps, table):
    """把行列表按日期索引。日期异常的跳过，重复的取首条。"""
    out = {}
    for row in rows:
        date = _row_date(row)
        if not date:
            gaps.append(f"{table} 某行{GAP_BAD_DATE}，已跳过")
            continue
        if date in out:
            gaps.append(f"{table} {date} {GAP_DUP_DATE}")
            continue
        out[date] = row
    return out


def _cross_check_gross(rows_by_date, date, computed, gaps):
    """自算毛利率 vs 接口 `GrossIncomeRatio`（累计口径 %）。

    两者口径不同（单季 vs 累计），所以只在**一季度报**上比较才有意义 ——
    一季报的累计就是单季。偏差 > 1pp 就记一条，提示可能取错了字段。
    """
    if computed is None or _quarter_of(date) != 1:
        return
    row = rows_by_date.get(date) or {}
    official = util.num(row.get("GrossIncomeRatio"))
    if official is None:
        return
    if abs(official / 100.0 - computed) > 0.01:
        gaps.append(f"{date} {GAP_GROSS_RATIO}（自算 {computed*100:.2f}% / 接口 {official:.2f}%）")


def _all_none(*values):
    return all(v is None for v in values)


def _dedupe(items):
    """去重但保序（data_gaps 会重复很多条，重复的没信息量）。"""
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 便捷访问器（供报告展示，不含判断）
# ---------------------------------------------------------------------------


def latest(quarters):
    """最新一季。空序列返回 None。"""
    return quarters[-1] if quarters else None


def window(quarters, n):
    """取最近 n 季（升序保持）。n <= 0 返回空列表。"""
    if n <= 0:
        return []
    return quarters[-n:]
