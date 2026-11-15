# -*- coding: utf-8 -*-
"""测试基座：路径、导入方式、构造数据的辅助函数。

**为什么需要 `sys.path` 这段。**
本工具的模块全部用顶层导入（`import config`、`from screen import codes`），
不是相对导入。这意味着 `stock_quality_screen/` 自己必须在 `sys.path` 上。
好处是 `python3 stock_report.py` 直接能跑、不用 `-m`；
代价是测试不能从上级目录 `import stock_quality_screen.screen`——那样
`import config` 会找不到。

所以这里在导入任何被测模块**之前**把项目根塞进 `sys.path`，与生产的运行方式
对齐。测试文件务必 `from helpers import ...` 放在最前面。

**为什么不用 `/tmp`。**
本机 `/tmp` 是 10MB tmpfs，建几个 SQLite（含 WAL）就会撞墙，表现为进程被
SIGKILL、连报错都没有。临时目录落项目根的 `.testtmp/`（已 gitignore），
可用 `QV_TMP` 环境变量改位置。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # stock_quality_screen/tests/
PROJECT = HERE.parent                           # stock_quality_screen/
HUB = PROJECT.parent                            # stock-value-invest-tool/

# 必须早于任何被测模块的 import：`import config` 要靠这条路径
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def tmp_root() -> Path:
    """测试临时根目录。"""
    root = Path(os.environ.get("QV_TMP") or HUB / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return root


def mkdtemp(prefix: str = "qv-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=tmp_root()))


def cleanup(path: Path | str) -> None:
    """安静地删掉临时目录（失败不抛）。"""
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 构造「年报」数据行
# ---------------------------------------------------------------------------
# score.py 的 `_quality()` / `_risk_checks()` 吃的是 compute.py 加工后的
# `financials.annual` 行。这些行有三十来个字段，且**必须自洽**：
# 毛利率 = (营收−成本)/营收、fcf = ocf − capex、各家比率的分子分母要同源。
# 手写一遍很容易自相矛盾，于是这里做成「默认值 + 覆盖」：
# 默认代表一家「还行」的制造业公司，想测哪项只改哪项。
#
# 字段名一律取自 score.py 实际 `y.get("...")` 的名字，别自己造：
#   ocf（不是 cfo）、deducted（不是 deducted_profit）、debt（不是 interest_debt）……

_BASE = {
    # 规模（元）
    "revenue": 100e8,
    "gross": 30e8,
    "net_profit": 12e8,
    "np_ttm": 12e8,
    "deducted": 11e8,
    "ebit": 15e8,
    # 现金流
    "ocf": 14e8,                    # 经营活动现金流净额
    "ocf_ttm": 14e8,
    "icf": -4e8,                    # 投资活动现金流（资本开支在这里）
    "fcf": 10e8,                    # = ocf − capex
    "fcf_fin": 10e8,
    "fcf_margin": 0.10,             # fcf / revenue，**小数**不是百分数
    # 资产负债
    "total_assets": 200e8,
    "equity": 95e8,                 # 归母净资产
    "debt": 10e8,                   # 有息负债
    "cash": 30e8,                   # 货币资金
    "short_loan": 3e8,
    "goodwill": 2e8,
    "goodwill_imputed": False,
    "ar": 15e8,                     # 应收账款
    "inventory": 18e8,
    "other_ar": 0.5e8,
    "other_ap": 0.5e8,
    "quick_ratio": 1.2,
    # 比率（compute 会算，测试直接给，保持自洽）
    "gross_margin": 30.0,           # %
    "roe": 12.6,                    # %
    "roic": 12.0,                   # %
    "shares": 10e8,
    "finance_expense": 0.5e8,
}


def annual_row(**over) -> dict:
    """造一行年报数据。`over` 覆盖默认值。"""
    row = dict(_BASE)
    row.update(over)
    return row


def annual_series(n: int = 5, *, start_year: int = 2021, growth: float = 0.08,
                  **over) -> list[dict]:
    """造 n 年连续年报，年份递增、`year` 字段填好、营收与净利按 growth 递增。

    `over` 里的值**覆盖**按 growth 推出来的值。所以想造「营收每年涨 8%、
    但现金流一直是负的」只需 `annual_series(5, ocf=-5e8)`。
    """
    rows = []
    for i in range(n):
        year = start_year + i
        k = (1 + growth) ** i
        rev = _BASE["revenue"] * k
        base = {
            "year": year,
            "revenue": rev,
            "gross": rev * (_BASE["gross_margin"] / 100.0),
            "net_profit": _BASE["net_profit"] * k,
            "np_ttm": _BASE["net_profit"] * k,
            "deducted": _BASE["deducted"] * k,
            "ocf": _BASE["ocf"] * k,
            "ocf_ttm": _BASE["ocf"] * k,
            "fcf": _BASE["fcf"] * k,
            "fcf_fin": _BASE["fcf_fin"] * k,
        }
        base.update(over)
        rows.append(annual_row(**base))
    return rows


def data_with_annual(annual: list[dict], **extra) -> dict:
    """把年报列表包成 `compute_scores()` 能吃的最小 data 结构。

    `compute_scores()` 会读取 financials.annual、以及若干可选块。
    只测质量分时给最小结构即可；`extra` 用于补 valuation / risks 等。
    """
    out = {
        "meta": {"code": "sh600519", "name": "测试公司", "industry": "白酒"},
        "financials": {"annual": annual},
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# 构造「已经算完分」的 data（渲染 / CLI 用）
# ---------------------------------------------------------------------------
def render_data(**extra) -> dict:
    """能跑通 `render.render_html()` 的最小 data。

    `meta` 与 `financials` 是渲染器的硬依赖：`_section_business` 里直接
    `data["financials"]`，缺了就是 KeyError。这不是缺陷——`build_report_data()`
    保证这两个键一定存在，渲染器不必为「上游违约」写容错；测试照实供给。
    """
    data = {
        "meta": {
            "code": "600000",
            "name": "测试股份",
            "industry": "白酒",
            "price": 12.34,
            "price_date": "2026-11-13",
            "price_basis": "实时价",
            "market_cap_yi": 1234.5,
            "quote_date": "2026-11-13",
            "latest_date": "2026-09-30",
        },
        "financials": {"annual": [], "structure": {}},
        "quality": {"score": 72.3, "grade": "良好", "coverage": 100.0},
        "valuation_score": {"score": 58.0, "grade": "中性"},
        "decision": {"key": "watch_price", "label": "好公司，等价格",
                     "reason": "质量通过，估值尚未到舒服区间"},
        "data_quality": {"buy_blockers": [], "q_coverage": 100.0,
                         "institution_coverage": 8, "risk_known": 0,
                         "risk_total": 0, "risk_na": 0},
        "risks": {"checks": []},
        "valuation": {"metrics": {}},
        "tracking": {},
    }
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------

def approx(a, b, tol=1e-6) -> bool:
    """两者都在 tol 内（None 与 None 算相等）。"""
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol
