"""§11.2 的 9 个场景 —— 集中定义，fixtures 生成脚本和测试用例共用。

每个函数返回 `{"code","name","industry","income":[...],"balance":[...]}`，
即 build() 的入参形态；需要接口原样的嵌套结构时用 `make.finance_payload` 再包一层。

日期基准：`LAST = (2026, 3)` 即 2026Q3，与 README.md 记录的实测口径一致。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures import make  # noqa: E402

LAST_YEAR, LAST_QUARTER = 2026, 3


# ---------------------------------------------------------------------------
# full_40q —— 资源股，43 季完整，三个指标都有值
# ---------------------------------------------------------------------------

def full_40q():
    """紫金矿业式的资源股，三个判断都命中：

        判断① 命中  毛利率在 12%~24% 之间波动，σ 明显 > 5pp
        判断② 命中  逐季比值恒为 2.5（利润增速 ÷ 营收增速），> 2.0
        判断③ 命中  产能周转率**逐季下滑**（固定资产扩张快于营收），分位在低位

    刻意这么造是为了有一条"三个判断都非 na、都不是中间态"的黄金路径用例 ——
    渲染、索引、离线快照都靠它。
    """
    n = 43
    dates = make.quarter_dates(LAST_YEAR, LAST_QUARTER, n)
    gross, revs, ops, fa, cip, cl = [], [], [], [], [], []
    prev_rev = None
    prev_op = None
    for i in range(n):
        # 毛利率：±6pp 交替，叠加缓慢下行
        wave = 6.0 * (1 if i % 2 == 0 else -1)
        base = 18.0 - i * 0.05
        gm = (base + wave) / 100.0
        gross.append(gm)
        # 营收：每季 +2.5%，每 4 季一次季节性高点
        rev = 2.0e10 * (1.025 ** i) * (1.04 if i % 4 == 0 else 1.0)
        revs.append(rev)
        # 营业利润：严格造出"利润增速 = 2.5 × 营收增速"
        if prev_rev is None:
            op = rev * gm
        else:
            op = prev_op * (1.0 + 2.5 * (rev / prev_rev - 1.0))
        ops.append(op)
        prev_rev, prev_op = rev, op
        # 固定资产比营收涨得快 → 周转率一路下滑 → 当前分位在低位
        fa.append(6.0e10 * (1.038 ** i))
        cip.append(8.0e9 * (1.02 ** i))
        cl.append(1.5e9 + i * 2.0e7)
    return {
        "code": "601899", "name": "紫金矿业", "industry": "有色金属",
        "income": _income(dates, gross, revs, ops),
        "balance": _balance(dates, fa, cip, cl),
    }


# ---------------------------------------------------------------------------
# short_12q —— 次新股，12 季
# ---------------------------------------------------------------------------

def short_12q():
    """次新股 12 季。

    打哪些分支：
      * 判断① 有效季 12 ≥ GM_MIN_SAMPLES(8)，所以**仍有值**（σ ≈ 1.2pp，不命中）
      * 判断② 比值 ≈ 0.58 → weak
      * 判断③ 有效样本 12 ≥ POSITION_MIN_SAMPLES(8)，**仍有值**
      * `decide` 侧 `insufficient_history = True`（12 < MIN_QUARTERS_REQUIRED 20）

    注意：它**不能**用来测"样本不足 → None"。样本不足的用例在
    test_metrics_* 里用更短的序列单独造（8 季以下）。
    """
    n = 12
    dates = make.quarter_dates(LAST_YEAR, LAST_QUARTER, n)
    # 关键：判断② 的比值 = 利润增速 ÷ 营收增速。若毛利率恒定，
    # 利润就与营收严格同比例增长，比值必然恒为 1.0 —— 无论乘什么常数。
    # 所以要让比值 ≠ 1，必须让**毛利率本身变化**。
    #
    # 这里毛利率每季 -0.4pp（20% → 15.6%），营收 +5%，则
    #   利润增速 ≈ 1.05 × 0.196/0.20 - 1 ≈ +2.9%
    #   比值 ≈ 2.9% / 5% ≈ 0.58 → weak
    # 毛利率 σ ≈ 1.2pp < 5pp → 判断① 不命中（次新股本就不该是周期股）。
    gross = [(0.20 - 0.004 * i) for i in range(n)]
    revs = [5.0e9 * (1.05 ** i) for i in range(n)]
    ops = [r * g for r, g in zip(revs, gross)]
    fa = [2.0e9] * n
    cip = [1.0e8] * n
    cl = [3.0e8] * n
    return {
        "code": "301101", "name": "明月镜片", "industry": "轻工制造",
        "income": _income(dates, gross, revs, ops),
        "balance": _balance(dates, fa, cip, cl),
    }


# ---------------------------------------------------------------------------
# balance_missing —— income 有、balance 自 2023 年起缺失
# ---------------------------------------------------------------------------

def balance_missing():
    n = 40
    dates = make.quarter_dates(LAST_YEAR, LAST_QUARTER, n)
    gross = [0.25 + 0.07 * (1 if i % 2 else -1) for i in range(n)]
    revs = [8.0e9 * (1.02 ** i) for i in range(n)]
    ops = [r * g * 1.05 for r, g in zip(revs, gross)]
    # 前 12 季（2023Q4 及更早）有 balance，后 28 季全部缺失
    keep = 12
    fa = [4.0e9] * keep
    cip = [2.0e8] * keep
    cl = [6.0e8] * keep
    return {
        "code": "002594", "name": "比亚迪", "industry": "汽车",
        "income": _income(dates, gross, revs, ops),
        "balance": _balance(dates[:keep], fa, cip, cl),
    }


# ---------------------------------------------------------------------------
# zero_revenue —— 某季营收为 0
# ---------------------------------------------------------------------------

def zero_revenue():
    """某季营收为 0 → 该季 gross_margin / capacity_turnover 必须为 None，不是 0。

    毛利率不造全等（否则 σ 恰好 0，会掩盖"σ=0 是否合理"的断言）。
    """
    n = 24
    dates = make.quarter_dates(LAST_YEAR, LAST_QUARTER, n)
    gross = [0.28 + 0.02 * (1 if i % 2 else -1) for i in range(n)]
    revs = [1.0e9 * (1.01 ** i) for i in range(n)]
    revs[10] = 0.0                      # 某一季营收为 0（停工/数据错）
    ops = [r * 0.3 for r in revs]
    fa = [5.0e8] * n
    cip = [1.0e8] * n
    cl = [1.0e8] * n
    return {
        "code": "600001", "name": "零营收测试", "industry": "测试",
        "income": _income(dates, gross, revs, ops),
        "balance": _balance(dates, fa, cip, cl),
    }


# ---------------------------------------------------------------------------
# neg_profit —— 连续亏损、营业利润为负
# ---------------------------------------------------------------------------

def neg_profit():
    n = 24
    dates = make.quarter_dates(LAST_YEAR, LAST_QUARTER, n)
    gross = [0.10, 0.12, 0.08, 0.14] * 6
    revs = [3.0e9 * (1.0 + 0.03 * (1 if i % 2 else -1)) for i in range(n)]
    # 营业利润：前 12 季 -1e8 左右，后 12 季亏损收窄到 -0.5e8
    ops = [-1.0e8 if i < 12 else -5.0e7 for i in range(n)]
    fa = [1.0e9] * n
    cip = [1.0e8] * n
    cl = [2.0e8] * n
    return {
        "code": "600002", "name": "亏损测试", "industry": "测试",
        "income": _income(dates, gross, revs, ops),
        "balance": _balance(dates, fa, cip, cl),
    }


# ---------------------------------------------------------------------------
# huge_ratio —— 某季 op 增速 3000%、rev 增速 0.2%
# ---------------------------------------------------------------------------

def huge_ratio():
    """比值荒值 + 营收增速趋零，两条保护都命中。

    实测逐对结果（窗口 = 最近 8 季，即 i=32..39）：

        i=34  营收 +0.2%（< 0.5%）→ **skip 命中**
              比值 250（> 50）    → **clip 同时命中**（说明两条保护同时成立时先 skip）
        i=35  营收 +10%、利润 +3100% → 比值 310（> 50）→ **clip 命中**
        i=36  营收 +0.3%            → **skip 命中**
        其余 5 对正常参与           → samples = 5

    `ratio` 中位数 = -0.683（受 i=37 的 -6.87 拖低），`samples=5 ≥ 4` 所以有值。

    这个 fixture 的价值在于**一次打中两条独立保护路径**；
    "有效样本不足 → ratio=None" 的用例在 test_metrics_elastic 里单独构造
    （一个 fixture 只负责一条主分支更好排查）。

    注意毛利率必须**变化**：恒定时利润与营收严格同比例，比值永远 ±1.0 附近，
    上面那些人为的爆炸值会被"同比例"前提吞掉，什么也测不出来。
    """
    n = 40
    dates = make.quarter_dates(LAST_YEAR, LAST_QUARTER, n)
    gross = [0.30 + 0.015 * (1 if i % 2 else -1) for i in range(n)]
    revs = [1.0e10 * (1.06 ** i) for i in range(n)]
    ops = [r * g for r, g in zip(revs, gross)]

    # i=34：营收增速 0.2% → 跳过（同时比值也超界，验证 skip 优先）
    revs[34] = revs[33] * 1.002
    ops[34] = ops[33] * 1.5
    # i=35：营收 +10%，营业利润 +3100% → 比值 310 > 50 → 剔除
    revs[35] = revs[34] * 1.10
    ops[35] = ops[34] * 32.0
    # i=36：营收增速 0.3% → 再跳过
    revs[36] = revs[35] * 1.003
    ops[36] = ops[35] * 1.1
    # i=37 起恢复正常

    fa = [2.0e9] * n
    cip = [5.0e8] * n
    cl = [1.0e9] * n
    return {
        "code": "600003", "name": "巨幅比值测试", "industry": "测试",
        "income": _income(dates, gross, revs, ops),
        "balance": _balance(dates, fa, cip, cl),
    }


# ---------------------------------------------------------------------------
# flat_margin —— 40 季毛利率恒定 32%
# ---------------------------------------------------------------------------

def flat_margin():
    n = 40
    dates = make.quarter_dates(LAST_YEAR, LAST_QUARTER, n)
    gross = [0.32] * n
    revs = [1.0e10 * (1.03 ** i) for i in range(n)]
    ops = [r * 0.32 for r in revs]
    # 固定资产 + 在建工程 恒定，净利波动小 → 产能周转率单调上升
    fa = [8.0e9] * n
    cip = [1.0e9] * n
    cl = [2.0e9] * n
    return {
        "code": "600004", "name": "恒定毛利测试", "industry": "测试",
        "income": _income(dates, gross, revs, ops),
        "balance": _balance(dates, fa, cip, cl),
    }


def flat_margin_equal_turnover():
    """毛利率恒定 32% 且**产能周转率也全等** —— 用于断言分位恰好 50.0。"""
    n = 40
    dates = make.quarter_dates(LAST_YEAR, LAST_QUARTER, n)
    gross = [0.32] * n
    revs = [1.0e10] * n                  # 营收不变
    ops = [r * 0.32 for r in revs]
    fa = [8.0e9] * n
    cip = [1.0e9] * n
    cl = [2.0e9] * n
    return {
        "code": "600014", "name": "全等周转测试", "industry": "测试",
        "income": _income(dates, gross, revs, ops),
        "balance": _balance(dates, fa, cip, cl),
    }


# ---------------------------------------------------------------------------
# financial —— 银行股
# ---------------------------------------------------------------------------

def financial():
    """银行：没有 OperatingCost（毛利率算不出来），产能周转率无意义。"""
    n = 40
    dates = make.quarter_dates(LAST_YEAR, LAST_QUARTER, n)
    income = []
    for i, date in enumerate(dates):
        rev = 7.0e10 * (1.008 ** i)
        row = {"EndDate": date, "OperatingRevenue_Q": f"{rev:.2f}"}
        row["OperatingProfit_Q"] = f"{rev * 0.42:.2f}"
        income.append(row)
    balance = [make.balance_row(d, fixed_asset=1.0e11, cip=2.0e9, contract_liab=None)
               for d in dates]
    return {
        "code": "600036", "name": "招商银行", "industry": "银行",
        "income": income, "balance": balance,
    }


# ---------------------------------------------------------------------------
# no_cl —— ContractLiability 全空（其余字段完好）
# ---------------------------------------------------------------------------

def no_cl():
    base = full_40q()
    base["code"] = "600005"
    base["name"] = "无合同负债测试"
    for row in base["balance"]:
        row.pop("ContractLiability", None)
    return base


# ---------------------------------------------------------------------------
# 额外：Q4 回推陷阱 & 累计口径对照（不落盘，测试内直接造）
# ---------------------------------------------------------------------------

def q4_trap():
    """三季报缺失 → 年报累计无法回推 → Q4 单季必须为 None。

    2025 年报 100 亿累计，2025 三季报整行缺失。若实现拿年报累计当单季，
    revenue_q 会变成 100 亿（应为 25 亿左右），判断② 立刻崩。
    """
    income = [
        # 2025Q1/Q2 正常，Q3 缺失，Q4 只有累计
        make.income_row("2025-03-31", revenue_q=2.0e9, cost_q=1.4e9, op_profit_q=3.0e8,
                        revenue_cum=2.0e9, cost_cum=1.4e9, op_cum=3.0e8),
        make.income_row("2025-06-30", revenue_q=2.2e9, cost_q=1.5e9, op_profit_q=3.2e8,
                        revenue_cum=4.2e9, cost_cum=2.9e9, op_cum=6.2e8),
        # 2025-09-30 整行缺失
        make.income_row("2025-12-31", revenue_cum=9.0e9, cost_cum=6.2e9, op_cum=1.3e9,
                        include_q=False),
    ]
    balance = [make.balance_row(d, fixed_asset=5.0e9, cip=1.0e9, contract_liab=4.0e8)
               for d in ("2025-03-31", "2025-06-30", "2025-12-31")]
    return {
        "code": "600006", "name": "Q4回推陷阱", "industry": "测试",
        "income": income, "balance": balance,
    }


def derive_from_cumulative():
    """单季字段全缺，但累计字段齐全且相邻 —— 应能正确回推。"""
    cum = [("2025-03-31", 2.0e9, 1.4e9), ("2025-06-30", 4.2e9, 2.9e9),
           ("2025-09-30", 6.4e9, 4.4e9), ("2025-12-31", 8.8e9, 6.0e9)]
    income = [make.income_row(d, revenue_cum=r, cost_cum=c, include_q=False)
              for d, r, c in cum]
    balance = [make.balance_row(d, fixed_asset=5.0e9, cip=1.0e9, contract_liab=4.0e8)
               for d, _r, _c in cum]
    return {
        "code": "600007", "name": "累计回推", "industry": "测试",
        "income": income, "balance": balance,
    }


# ---------------------------------------------------------------------------
# 内部：按序列造两张表
# ---------------------------------------------------------------------------

def _income(dates, gross, revs, ops):
    rows = []
    for date, gm, rev, op in zip(dates, gross, revs, ops):
        if rev is None:
            rows.append(make.income_row(date, revenue_q=None, cost_q=None, op_profit_q=op))
            continue
        cost = rev * (1.0 - gm) if gm is not None else None
        rows.append(make.income_row(date, revenue_q=rev, cost_q=cost, op_profit_q=op))
    return rows


def _balance(dates, fa, cip, cl):
    return [make.balance_row(d, fixed_asset=f, cip=c, contract_liab=l)
            for d, f, c, l in zip(dates, fa, cip, cl)]


#: 场景名 → 构造函数（生成脚本按这个顺序落盘）
SCENARIOS = {
    "full_40q": full_40q,
    "short_12q": short_12q,
    "balance_missing": balance_missing,
    "zero_revenue": zero_revenue,
    "neg_profit": neg_profit,
    "huge_ratio": huge_ratio,
    "flat_margin": flat_margin,
    "flat_margin_equal_turnover": flat_margin_equal_turnover,
    "financial": financial,
    "no_cl": no_cl,
}
