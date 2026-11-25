"""业绩转变标注测试：毛利率环比改善。

跑法：cd ~/MyData/stock_value_invest_tool && python3 -m stock_kline_screen.tests.test_turnaround

数据形状是 hub `finance` 行（一期一行、单季列直接给）。三条口径变化
（本地算毛利率 / 无披露日过滤 / is_financial 判金融）各有对应用例。
"""


from __future__ import annotations

# 结果库指到临时目录：本文件被直接跑（不经 tests 包）时也要生效。
try:
    from . import _env  # noqa: F401  （作为包导入）
except ImportError:  # pragma: no cover - 直接跑文件
    import _env  # noqa: F401

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

from stock_kline_screen.turnaround import (compute_turnaround,     # noqa: E402
                                           prev_quarter,
                                           turnaround_metrics)

PASS = 0
FAIL: list[str] = []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def fin(period, *, rev_q=None, cost_q=None, rev=None, cost=None):
    """一期 hub finance 行。`rev_q/cost_q` 是单季列，`rev/cost` 是累计列。"""
    row = {"period": period}
    if rev_q is not None:
        row["OperatingRevenue_Q"] = rev_q
    if cost_q is not None:
        row["OperatingCost_Q"] = cost_q
    if rev is not None:
        row["OperatingRevenue"] = rev
    if cost is not None:
        row["OperatingCost"] = cost
    return row


def test_prev_quarter() -> None:
    print("\nprev_quarter")
    check("Q1 → 上年 Q4", prev_quarter("2026-03-31") == "2025-12-31")
    check("Q2 → Q1", prev_quarter("2026-06-30") == "2026-03-31")
    check("Q3 → Q2", prev_quarter("2026-09-30") == "2026-06-30")
    check("Q4 → Q3", prev_quarter("2026-12-31") == "2026-09-30")
    check("非季末 → None", prev_quarter("2026-05-31") is None)


def test_quarters() -> None:
    """单季列齐全 → 毛利率全部本地算，不经差分。"""
    print("\nturnaround_metrics（单季列直接给）")
    rows = [
        fin("2025-09-30", rev_q=100, cost_q=40),      # 毛利率 60%
        fin("2025-12-31", rev_q=100, cost_q=45),      # 55%
        fin("2026-03-31", rev_q=100, cost_q=50),      # 50%
        fin("2026-06-30", rev_q=110, cost_q=55),      # 50%
        fin("2026-09-30", rev_q=120, cost_q=48),      # 60%
    ]
    m = turnaround_metrics(rows)
    check("最新期取最后一行", m["period"] == "2026-09-30", m["period"])
    # 近 2 季：Q2 50% → Q3 60%，环比改善 ✓；营收 110 → 120 环比不为负 ✓；
    # 同比：上年同季（2025-09-30）60%，最新 60% **未高于** → 护栏挡下。
    check("毛利率环比改善但同比未超过上年同季 → 否",
          m["p1_label"] == "否" and "未高于上年同季" in m["gm_desc"], m["gm_desc"])

    # 改成同比略升：上年同季 59 → 今年 60 → 全部条件成立
    rows2 = [
        fin("2025-09-30", rev_q=100, cost_q=41),      # 59%
        fin("2025-12-31", rev_q=100, cost_q=45),
        fin("2026-03-31", rev_q=100, cost_q=50),
        fin("2026-06-30", rev_q=110, cost_q=55),
        fin("2026-09-30", rev_q=120, cost_q=48),      # 60% > 59% ✓
    ]
    m2 = turnaround_metrics(rows2)
    check("环比改善 + 营收不降 + 同比更高 → 是",
          m2["p1_label"] == "是" and m2["p1"] is True, m2["gm_desc"])

    # 营收环比为负：挡下
    rows3 = [
        fin("2025-09-30", rev_q=100, cost_q=41),
        fin("2025-12-31", rev_q=100, cost_q=45),
        fin("2026-03-31", rev_q=100, cost_q=50),
        fin("2026-06-30", rev_q=120, cost_q=48),
        fin("2026-09-30", rev_q=110, cost_q=40),      # 毛利率 63.6% 但营收环比降
    ]
    m3 = turnaround_metrics(rows3)
    check("毛利率改善但营收环比为负 → 否",
          m3["p1_label"] == "否" and "营收环比为负" in m3["gm_desc"], m3["gm_desc"])

    # 只有最新一期：近 2 季数据不足
    m4 = turnaround_metrics([fin("2026-09-30", rev_q=100, cost_q=40)])
    check("只有一期 → 近2季数据不足（无数据）", m4["p1_label"] == "无数据",
          m4["gm_desc"])

    check("一行都没有 → None", turnaround_metrics([]) is None)


def test_fallback() -> None:
    """单季列缺失 → 累计列差分兜底（差分要求相邻期齐全）。"""
    print("\nturnaround_metrics（累计差分兜底）")
    rows = [
        fin("2025-09-30", rev=100, cost=40),          # 无前一期，差不出
        fin("2025-12-30", rev=200, cost=90),           # 脏日期：差一天不是季末
        fin("2025-12-31", rev=200, cost=90),           # Q4 单季 = (200-100)/(90-40) → 50%
        fin("2026-03-31", rev=130, cost=50),           # Q1：累计值即单季值 → 61.5%
    ]
    m = turnaround_metrics(rows)
    # 最新期 2026-03-31 毛利率 61.5%（Q1 直取），上一季 2025-12-31 差分 50%
    # → 环比改善；但营收同比（无上年同季）→ 无数据
    check("差分兜底：Q4 差分 50%、Q1 直取 61.5%、同比缺失 → 无数据",
          m["p1_label"] == "无数据" and abs(m["gm"][0] - 50) < 0.1
          and abs(m["gm"][1] - 61.5) < 0.1, str(m["gm"]))
    check("脏日期 2025-12-30 不被差分用（不在毛利率里）",
          "脏日期不进 gm 需要直接看 _gm_and_revq",
          "")   # 行为由下面 prev_quarter 用例钉住
    check("prev_quarter 认不出脏日期",
          prev_quarter("2025-12-30") is None,
          str(prev_quarter("2025-12-30")))
    check("prev_quarter 认得出 12-31", prev_quarter("2025-12-31") == "2025-09-30")


def test_financial() -> None:
    print("\ncompute_turnaround（金融股「不适用」）")
    rows = [
        fin("2026-06-30", rev_q=100, cost_q=50),
        fin("2026-09-30", rev_q=120, cost_q=48),
    ]
    info = compute_turnaround({"sh600000": rows, "sz000001": rows},
                              {"sh600000": False, "sz000001": True})
    check("非金融股正常判", info["sh600000"]["p1_label"] in ("是", "否", "无数据"),
          str(info.get("sh600000")))
    check("金融股 → 不适用（没有毛利率语义）",
          info["sz000001"]["p1_label"] == "不适用", str(info.get("sz000001")))
    check("空行列表 → 不出现在结果里",
          compute_turnaround({"x": []}, {}) == {})


def main() -> int:
    test_prev_quarter()
    test_quarters()
    test_fallback()
    test_financial()
    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
