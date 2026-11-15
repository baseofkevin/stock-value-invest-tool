"""规则层测试：`find_start` 与 `judge`。

跑法：

    cd /home/kevin/MyData/stock-value-invest-tool && \
    python3 -m stock_kline_screen.tests.test_rules

全部离线：bar 是手工构造的 dict（hub `kline` 行形状），横截面行是
`{"symbol","pct_chg","name",...}`（hub `scan` 行形状）。

这组用例从原工具 tests/test_screen.py 的思路重建（数值断言沿用原版
口径，保证迁移不漂移），容器形状换成 hub 的 dict。
"""


from __future__ import annotations

# 结果库指到临时目录：本文件被直接跑（不经 tests 包）时也要生效。
try:
    from . import _env  # noqa: F401  （作为包导入）
except ImportError:  # pragma: no cover - 直接跑文件
    import _env  # noqa: F401

import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent          # stock-value-invest-tool/
sys.path.insert(0, str(PROJECT))

from stock_kline_screen.params import C2_TOL, C3_RATIO, MIN_HISTORY  # noqa: E402
from stock_kline_screen.rules import (find_start, judge, reason_cat,  # noqa: E402
                                      reason_label)

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


S = "2026-11-13"


def mkbars(closes, vols):
    """构造 hub kline 形状的 bar 序列（日期升序、volume 是股、amount 是元）。"""
    n = len(closes)
    base = dt.date.fromisoformat(S) - dt.timedelta(days=n - 1)
    out = []
    for i, (c, v) in enumerate(zip(closes, vols)):
        d = (base + dt.timedelta(days=i)).isoformat()
        out.append({"date": d, "open": c, "high": c, "low": c, "close": c,
                    "volume": float(v), "amount": c * v})
    return out


def launch(total=120, n_up=30, flat=10.0, brk=11.0, vflat=1000.0, vbrk=3000.0):
    """横盘 `total-n_up` 根后放量上穿，再连涨 `n_up` 根（含 S）。

    d* = 第 (total-n_up) 根（0 基），age = n_up - 1，i = total-1。
    上穿日成交 3000 / 前 5 日均 1000 → vcross = 3.0。

    上穿段是**线性上升**而不是平台：`find_start` 从 S 往回扫时要求
    `cl[j] > MA20(j)` 一直成立，平台段的 MA20 会追平价格
    （实测踩过：`(19×10+11)/20` 只在窗口跨过断点那几根成立，
    一旦整窗都在平台内，MA20 = 价格，回扫第一步就 break）。
    """
    n_flat = total - n_up
    closes = [flat] * n_flat + [brk + i * 0.01 for i in range(n_up)]
    vols = [vflat] * n_flat + [vbrk] + [vflat * 2] * (n_up - 1)
    return mkbars(closes, vols)


def full_up(total=120, vflat=1000.0):
    """整条序列都在线性上升 → 回扫触边界 → 「非真上穿」。"""
    closes = [10.0 + i * 0.01 for i in range(total)]
    return mkbars(closes, [vflat] * total)


def cross_row(chg):
    """hub scan 行的最小形状（judge 只读 `pct_chg`）。"""
    return {"pct_chg": chg, "name": "测试股"}


# ================================================================ find_start

def test_find_start() -> None:
    print("\nfind_start")
    check("空 bars → 无K线数据",
          find_start("sh600000", "x", [], S) == (None, "无K线数据"))
    check("S 不在 bars 里",
          find_start("sh600000", "x", mkbars([10] * 5, [1] * 5), "2026-01-01")[1]
          == "K线无 2026-01-01 数据")
    bars = mkbars([10.0] * 70, [100] * 70)          # 只有 70 根，i=69 < 70
    check("历史不足报根数（70 根 < 71）",
          find_start("sh600000", "x", bars, S)[1] == f"K线历史不足(70根，需≥{MIN_HISTORY + 1})")

    # 全程横盘：从没站上 MA20
    flat = mkbars([10.0] * 120, [100] * 120)
    check("横盘 → 未站上 MA20",
          find_start("sh600000", "x", flat, S) == (None, "未站上 MA20"))

    # 正常上穿
    bars = launch(total=120, n_up=30)
    det, why = find_start("sh600000", "测试股", bars, S)
    check("正常上穿能定位", det is not None, why)
    if det:
        d_star = bars[90]["date"]
        check("d* 是第一个上穿日", det["d_star"] == d_star, f"{det['d_star']} vs {d_star}")
        check("age = i - ci", det["age"] == 29, str(det["age"]))
        check("vcross = 3000/1000 = 3.0", det["vcross"] == 3.0, str(det["vcross"]))
        check("name 原样带出", det["name"] == "测试股")

    # 一直站在 MA20 上（回扫触及边界索引 20）→ 非真上穿
    long_up = full_up(total=120)
    det2, why2 = find_start("sh600000", "x", long_up, S)
    check("连续站上超过回扫上限 → 非真上穿",
          det2 is None and "非真上穿" in why2, why2)

    # 上穿后小幅回踩（跌破 MA20 的那天之后才再站上）：
    # 构造 10→11（30 根）→回 10.0（1 根）→11（…）不算——那会改变 d*。
    # 直接测 judge 的 ⑥ 分支不触发（定义性条件），见 test_judge。


# ================================================================ judge

def test_judge() -> None:
    print("\njudge（七条）")
    # N=4 是每周口径：启动日必须在最近 5 个交易日内 → n_up=5（age=4）。
    bars = launch(total=120, n_up=5)
    det, _ = find_start("sh600000", "测试股", bars, S)
    d0 = det["d_star"]
    cross_by_day = {S: {}, d0: {"sh600000": cross_row(1.77)}}
    st_by_day, new_by_day = {}, {}

    def go(N=4, k_vol=1.5, gap=0.0, c2=C2_TOL, c3=C3_RATIO, **kw):
        return judge(det, bars, S, N, cross_by_day, st_by_day, new_by_day,
                     k_vol, gap, c2, c3, **kw)

    ok, why = go()
    check("基准场景入选", ok and why == "", f"{ok} {why}")

    ok, why = go(N=3)
    check("① age(4) > N=3 → 剔除", not ok and "距启动" in why, why)

    ok, why = go(k_vol=5.0)
    check("② vcross(3.0) < 5 → 剔除", not ok and "放量不足" in why, why)

    # ③ 启动日不在窗口（该股停牌）
    cross2 = {S: {}, d0: {"sh600000": cross_row(1.77)}}
    ok, why = judge(det, bars, "2026-11-13", 4, cross2, {}, {}, 1.5, 0.0, C2_TOL, C3_RATIO)
    # S 不在 cross2 → 不影响（judge 只对 d0 查 cross）；这条直接测 d0 缺失：
    ok, why = judge(det, bars, S, 4, {S: {}}, {}, {}, 1.5, 0.0, C2_TOL, C3_RATIO)
    check("③ 启动日不在覆盖窗口 → 剔除（停牌）", not ok and "覆盖窗口" in why, why)

    cross3 = {S: {}, d0: {"sh600000": cross_row(0.2)}}     # 涨幅 0.2 < 0.5
    ok, why = judge(det, bars, S, 4, cross3, {}, {}, 1.5, 0.0, C2_TOL, C3_RATIO)
    check("③ 启动日涨幅 0.2 不在 [0.5,20.5] → 剔除", not ok and "涨幅" in why, why)

    cross4 = {S: {}, d0: {"sh600000": {"pct_chg": None}}}  # 字段缺失
    ok, why = judge(det, bars, S, 4, cross4, {}, {}, 1.5, 0.0, C2_TOL, C3_RATIO)
    check("③ 涨幅字段缺失 → 剔除", not ok and "字段缺失" in why, why)

    # ④ MA20 < MA60×0.98：阴跌后上穿（上穿段线性上升，理由见 launch 的 docstring）
    closes = [12.0 - i * 0.02 for i in range(90)] + [10.5 + i * 0.01 for i in range(30)]
    vols4 = [100.0] * 90 + [3000.0] + [200.0] * 29
    bars4 = mkbars(closes, vols4)
    det4, why4 = find_start("sh600000", "x", bars4, S)
    check("阴跌后上穿能定位", det4 is not None, why4)
    if det4:                                   # 能定位就测 ④ 的比例剔除
        cross5 = {S: {}, det4["d_star"]: {"sh600000": cross_row(1.0)}}
        ok, why = judge(det4, bars4, S, 29, cross5, {}, {}, 1.5, 0.0, C2_TOL, 0.98)
        check("④ MA20 < MA60×0.98 → 剔除", not ok and "MA20 < MA60" in why, why)

    # ④ 距 MA60 太远
    ok, why = go(gap=0.5)
    check("④ close@S / MA60 ≥ 0.5 → 剔除（距MA60）",
          not ok and "距MA60" in why, why)

    # ⑤ ST（d* 日与 S 日两处都查）
    st_by_day = {d0: {"sh600000"}}
    ok, why = judge(det, bars, S, 4, cross_by_day, st_by_day, {}, 1.5, 0.0, C2_TOL, C3_RATIO)
    check("⑤ d* 日 ST → 剔除", not ok and why == "⑤ ST", why)
    st_by_day = {S: {"sh600000"}}
    ok, why = judge(det, bars, S, 4, cross_by_day, st_by_day, {}, 1.5, 0.0, C2_TOL, C3_RATIO)
    check("⑤ S 日 ST → 剔除", not ok and why == "⑤ S日 ST", why)

    new_by_day = {S: {"sh600000"}}
    ok, why = judge(det, bars, S, 4, cross_by_day, {}, new_by_day, 1.5, 0.0, C2_TOL, C3_RATIO)
    check("⑤ S 日次新 → 剔除", not ok and "次新" in why, why)

    # ⑥ 定义性条件恒不触发（本用例即证明：上穿后从未跌破 MA20）。
    # 用干净的空集合显式调一次——st_by_day 在上面 ⑤ 的用例里已被改写。
    ok6, _ = judge(det, bars, S, 4, cross_by_day, {}, {}, 1.5, 0.0, C2_TOL, C3_RATIO)
    check("⑥ 正常场景不触发（上穿后未跌破）", ok6 is True)


# ================================================================ reason_cat

def test_reason_cat() -> None:
    print("\nreason_cat")
    check("数字归一成 #",
          reason_cat("⑦ 距启动4日 > N=4") == "⑦ 距启动#日 > N=#")
    check("MA20/MA60 周期保留",
          reason_cat("未站上 MA20") == "未站上 MA20")
    check("量能比归一", reason_cat("② 启动日放量不足(1.20<1.75)")
          == "② 启动日放量不足(#<#)")
    check("百分比归一", reason_cat("③ 启动日涨幅0.20%不在[0.5,20.5]")
          == "③ 启动日涨幅#%不在[#,#]")


def test_reason_label() -> None:
    """聚合键是给程序分组的，印给用户看必须是人话：`#` 不能出现在屏幕上。"""
    print("\nreason_label")
    cases = ["⑦ 距启动4日 > N=4", "② 启动日放量不足(1.20<1.75)",
             "④ 启动日 MA20 < MA60×0.97",
             "④ 启动日 MA20 近3日跌逾2.0%",
             "⑤ 次新（2024-05-06 上市，未满3年）",
             "④ 启动日 距MA60 ≥ 0.1"]
    for raw in cases:
        label = reason_label(reason_cat(raw))
        check(f"无 # 占位符：{label}", "#" not in label)
    check("没匹配上的也不留 #",
          "#" not in reason_label("⑨ 某条新规则#日"))
    check("原因原文照旧", reason_label("未站上 MA20") == "未站上 MA20")


def main() -> int:
    test_find_start()
    test_judge()
    test_reason_cat()
    test_reason_label()
    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
