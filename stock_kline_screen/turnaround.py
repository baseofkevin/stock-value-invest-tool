"""业绩转变①毛利率环比改善标注（只标注，不裁决）。

从原版 `kline_screen/metrics/turnaround.py` 搬来，数据源从「westock 三表
（income + balancesheet 的 GrossIncomeRatio_Q + InfoPublDate）」换成共享库的
`finance` 域（一期一行、单季列直接给）。三处口径变化，全部记在 `README.md`：

1. **毛利率一律本地算** `(营收单季−成本单季)/营收单季`。原版优先用上游
   `GrossIncomeRatio_Q`（balancesheet 的现成比率），本地差分只做兜底；
   现在反过来——因为共享库没存 `GrossIncomeRatio_Q`，而 `OperatingRevenue_Q`
   与 `OperatingCost_Q` 实测与累计差分逐分一致（hub README §3），
   两个等价来源里取哪个都是对的，少存一列就少一个口径。
2. **不再按 InfoPublDate（披露日）过滤防未来函数。** 共享库的 finance 是
   append-only、上游只给已披露的报告期，**当期选股**（S = 最近交易日）时
   库里的最新期必然已在 S 前披露。⚠️ 已知限制：**补跑历史基准日**时，
   「S 之后才入库的报告期」会被用上（原版会挡掉）。给 `--date` 补跑加了一条
   stderr 提示，不静默。
3. **金融股判「不适用」用 `stock.is_financial`**（profile 域，词表判定），
   替代原版从 balancesheet 行里读 `EnterpriseType`。
"""

from __future__ import annotations

from .params import TURN_GM_REQUIRE_YOY
from .rules import fnum


def prev_quarter(period: str) -> str | None:
    """报告期 → 上一季末；非标准季末（含脏日期）返回 None。

    月份和日都查：`2025-12-30` 这种差一天的脏日期不该被当成 Q4 季末，
    否则差分兜底会拿它当基准算出错误单季值。
    """
    try:
        y, m = int(period[:4]), int(period[5:7])
        d = int(period[8:10])
    except (TypeError, ValueError):
        return None
    ends = {3: 31, 6: 30, 9: 30, 12: 31}
    if m not in ends or d != ends[m]:
        return None
    if m == 3:
        return f"{y - 1}-12-31"
    if m == 6:
        return f"{y}-03-31"
    if m == 9:
        return f"{y}-06-30"
    return f"{y}-09-30"


def _gm_and_revq(rows: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    """两套字典：`{报告期: 单季毛利率%}` 与 `{报告期: 单季营收}`。

    优先用上游直接给的单季列（`_Q`）；缺哪期再拿累计列差分兜底——
    差分要求相邻期齐全，缺一期该期就算不出，宁缺毋猜。
    """
    by_period = {r["period"]: r for r in rows if r.get("period")}
    gm: dict[str, float] = {}
    revq: dict[str, float] = {}

    for p, r in by_period.items():
        rq = fnum(r.get("OperatingRevenue_Q"))
        cq = fnum(r.get("OperatingCost_Q"))
        if rq is not None and rq > 0 and cq is not None:
            gm[p] = (rq - cq) / rq * 100
        if rq is not None:
            revq[p] = rq

    for p in by_period:
        if p in gm and p in revq:
            continue
        q = prev_quarter(p)
        if not q or q not in by_period:
            continue
        rev = fnum(by_period[p].get("OperatingRevenue"))
        cost = fnum(by_period[p].get("OperatingCost"))
        prev_rev = fnum(by_period[q].get("OperatingRevenue"))
        prev_cost = fnum(by_period[q].get("OperatingCost"))
        if rev is None or cost is None or prev_rev is None or prev_cost is None:
            continue
        if int(p[5:7]) == 3:            # 一季报的累计值就是单季值
            rq, cq = rev, cost
        else:
            rq, cq = rev - prev_rev, cost - prev_cost
        if p not in gm and rq > 0:
            gm[p] = (rq - cq) / rq * 100
        if p not in revq:
            revq[p] = rq
    return gm, revq


def turnaround_metrics(rows: list[dict]) -> dict | None:
    """「①毛利率环比改善」。`rows` 是该股的 finance 全部期（升序）。

    返回 None 表示一期可用报告期都没有（→「无数据」）。

    口径（三项同时成立才算「是」）：
      ① 最近两个单季毛利率严格递增；
      ② 最新单季营收环比不为负；
      ③ 最新单季毛利率高于上年同季（TURN_GM_REQUIRE_YOY 开关，防季节性误判）。
    """
    periods = sorted({r["period"] for r in rows if r.get("period")})
    if not periods:
        return None
    t = periods[-1]
    gm, revq = _gm_and_revq(rows)

    q2 = prev_quarter(t)
    g2, g3 = gm.get(q2), gm.get(t)
    gm_ok = g2 is not None and g3 is not None and g2 < g3
    yoy_dt = f"{int(t[:4]) - 1}{t[4:]}"
    gm_yoy = gm.get(yoy_dt)
    gm_yoy_ok = gm_yoy is not None and g3 is not None and g3 > gm_yoy

    r2, r3 = revq.get(q2), revq.get(t)
    rev_ok = r2 is not None and r3 is not None and r3 >= r2
    if r2 is not None and r3 is not None:
        if r2 > 0:
            rev_txt = f"营收环比 {(r3 / r2 - 1) * 100:+.1f}%"
        else:
            rev_txt = "营收环比 不可比（基期为0）"
        if not rev_ok:
            rev_txt += "（最新一季营收环比为负）"
    else:
        rev_txt = "营收环比 数据不足"

    gm_text = "→".join("-" if v is None else f"{v:.2f}" for v in (g2, g3))
    if not gm:
        p1_label, gm_desc = "无数据", "无毛利率数据"
    elif g2 is None or g3 is None:
        p1_label = "无数据"
        gm_desc = f"{gm_text}；数据不足（需最近2个单季）"
    else:
        if not gm_ok:
            p1_label, state = "否", "环比未改善"
        elif not rev_ok:
            if r2 is None or r3 is None:
                p1_label, state = "无数据", "毛利率改善但营收环比数据不足"
            else:
                p1_label, state = "否", "毛利率改善但营收环比不达标"
        elif TURN_GM_REQUIRE_YOY and gm_yoy is None:
            p1_label, state = "无数据", "毛利率与营收达标，但同比不可比"
        elif TURN_GM_REQUIRE_YOY and not gm_yoy_ok:
            p1_label, state = "否", "毛利率与营收达标，但未高于上年同季"
        else:
            p1_label, state = "是", "环比改善"
        yoy_txt = ("同比 无数据" if gm_yoy is None else
                   f"同比 {g3 - gm_yoy:+.2f}pct（上年同季 {gm_yoy:.2f}%）")
        gm_desc = f"{gm_text}（{state}）；{rev_txt}；{yoy_txt}"

    p1 = gm_ok and rev_ok and (not TURN_GM_REQUIRE_YOY or gm_yoy_ok)
    return {"period": t,
            "gm": [g2, g3], "gm_text": gm_text, "gm_desc": gm_desc,
            "gm_yoy": round(gm_yoy, 2) if gm_yoy is not None else None,
            "rev_q": [r2, r3], "rev_ok": bool(rev_ok),
            "p1": bool(p1), "p1_label": p1_label}


def compute_turnaround(fin_by_code: dict[str, list[dict]],
                       is_financial: dict[str, bool]) -> dict[str, dict]:
    """`{code: metrics}`。金融股（is_financial=True）标「不适用」——
    银行/保险/券商没有毛利率语义，硬算出来的"毛利率改善"是噪音。"""
    info: dict[str, dict] = {}
    for code, rows in fin_by_code.items():
        m = turnaround_metrics(rows or [])
        if m is None:
            continue
        if is_financial.get(code):
            m["p1_label"] = "不适用"
        info[code] = m
    return info
