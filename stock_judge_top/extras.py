"""四因子之外的旁证：估值分位 / 机构目标价 / 基本面反证 / 周期位置。

为什么不 import 另外两个工具
--------------------------------
`stock_quality_screen` 与 `stock_cycle_elastic` 都是**独立包**（顶层 `import config`，
能整个目录拷走单独跑）。从 judge_top 直接 import 会把两个包焊在一起，拷走就断。
所以这里按"少量代码复制"的约定重写计算部分：只复制**判定规则**（阈值与档位），
不复制取数编排和渲染。规则若在那边改了，这里要跟着改——每个函数头都写了
对应源文件与阈值出处。

四个函数都返回 `(payload, gap)`：
    payload 是给报告用的一行结论（字段见各函数），失败时为空 dict；
    gap 是"取数缺口"的一句人话（失败时非空），交给报告统一列表展示。
每张旁证失败**都不该拦住四因子**——顶部判断的主证据仍然是 K 线与财务。
"""
from __future__ import annotations

from .common.values import fnum
from .log import log

#: 估值分位只看这么多年的周线（与 quality 的 VALUATION_YEARS 同值）
VALUATION_YEARS = 10
#: 少于这么多周就不给分位（样本太少的百分位没有意义）
VALUATION_MIN_WEEKS = 100


def _pct_rank(values, x):
    """x 在 values 中的百分位（0~100，中位取半）。与 quality `_valuation` 同式。"""
    if x is None or not values:
        return None
    n = len(values)
    return (sum(1 for v in values if v < x) + 0.5 * sum(1 for v in values if v == x)) / n * 100.0


def _short(text: object, limit: int = 60) -> str:
    """把上游的整段报错压成一句人话。

    取数层（`stock_data_fetch`）的异常信息很长，带参数提示和
    `STOCKHUB_LOG=DEBUG` 的排查建议——那些该进日志，不该占满报告里的一行表格。
    这里只留第一句（到句号/分号/括号为止），剩下的交给 log。
    """
    head = str(text or "").strip().split("\n")[0]
    for sep in ("。", "；", "（", ";"):
        if sep in head:
            head = head.split(sep)[0]
    return head[:limit] + ("…" if len(head) > limit else "")


def _gap(domain: str, code: str, why: str) -> dict:
    return {"domain": domain, "symbol": code, "why": why}


# ------------------------------------------------------------------ 估值

def valuation(hub, code: str, end: str, force: bool = False) -> tuple[dict, dict | None]:
    """PE/PB/PS 的当前值与历史分位（周频，全历史）。

    阈值出处：`stock_quality_screen/screen/compute.py:_valuation`。
    「贵」是顶部的第一定义，所以这张旁证只在有历史时给结论；不足
    `VALUATION_MIN_WEEKS` 周就只报样本数，不给分位。
    """
    from stock_data_fetch.need import want

    kw = {"force": force} if force else {}
    try:
        hub.ensure("valuation", code,
                   want.valuation(hub.cal, end, years=VALUATION_YEARS), **kw)
        rng = hub.read("valuation", code,
                       want.valuation(hub.cal, end, years=VALUATION_YEARS))
    except Exception as e:                          # noqa: BLE001
        log(f"[缺口] {code} valuation 全文：{e}")
        return {}, _gap("valuation", code, f"估值序列取不到：{_short(e)}")

    rows = [r for r in rng.rows if r.get("trade_date") and str(r["trade_date"]) <= end]
    if not rows:
        return {}, _gap("valuation", code, "估值序列为空（东财无此股的 RPT_VALUEANALYSIS_DET）")

    out = {"sample_start": str(rows[0]["trade_date"]), "sample_end": str(rows[-1]["trade_date"]),
           "weeks": len(rows), "metrics": {}}
    for key, col, label in (("pe", "pe_ttm", "PE(TTM)"), ("pb", "pb_mrq", "PB"),
                            ("ps", "ps_ttm", "PS(TTM)")):
        cur = fnum(rows[-1].get(col))
        series = [v for v in (fnum(r.get(col)) for r in rows) if v is not None and v > 0]
        item = {"label": label, "current": cur, "percentile": None, "min": None, "max": None}
        if cur is not None and cur > 0 and len(series) >= VALUATION_MIN_WEEKS:
            item["percentile"] = round(_pct_rank(series, cur), 1)
            item["min"] = round(min(series), 2)
            item["max"] = round(max(series), 2)
        out["metrics"][key] = item
    return out, None


# ------------------------------------------------------------- 机构目标价

def consensus(hub, code: str, end: str, price: float | None,
              force: bool = False) -> tuple[dict, dict | None]:
    """机构一致预期的一句话：目标价、距现价空间、覆盖家数。

    阈值出处：`stock_quality_screen/screen/compute.py:_forecast`。
    `snapshot` 的 `consensus` 子域在没有覆盖时返回空表（实测 38 只里 18 只），
    那不是错误、是这家公司没有分析师覆盖——缺口文案要写清这一点，否则会被
    当成取数故障去排查。
    """
    from stock_data_fetch.need import want

    kw = {"force": force} if force else {}
    need = want.snapshot("consensus", end)
    try:
        hub.ensure("snapshot", code, need, **kw)
    except Exception as e:                          # noqa: BLE001
        log(f"[缺口] {code} snapshot 全文：{e}")
        return {}, _gap("snapshot", code, f"一致预期取不到：{_short(e)}")
    rng = hub.read("snapshot", code, need)
    if not rng.rows:
        return {}, _gap("snapshot", code,
                        "上游对该股的一致性预期返回空表（多半是这家公司没有分析师覆盖，"
                        "不是取数故障）")
    payload = rng.rows[0].get("payload")
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        return {}, _gap("snapshot", code, "一致预期 payload 不是对象")

    target = fnum(payload.get("targetPrice"))
    forecasts = [f for f in (payload.get("forecasts") or []) if isinstance(f, dict)]
    n = len(forecasts)
    upside = (target / price - 1) if (target and price) else None
    if target is None and not forecasts:
        return {}, _gap("snapshot", code, "一致预期里既没有目标价也没有预测明细")
    return {"target": target, "upside": upside, "forecasts": n,
            "as_of": str(rng.rows[0].get("as_of") or "")[:10]}, None


# --------------------------------------------------------- 基本面反证

def risk(full: dict) -> tuple[dict, dict | None]:
    """三项"这波涨有没有基本面"的反证：应收、存货、经营现金流。

    阈值出处：`stock_quality_screen/config.py`（RECEIVABLE_GROWTH_* / CFO_NP_MIN）。

    **商誉查不了**：westock 的 balance 没有商誉列（qualify 那边是从东财
    `gbalance` 子域取 GOODWILL 的）。这里不为一项反证多打一个上游，改用
    存货——高位放量 + 存货增速远超营收，同样是"这波涨没有基本面"的证据。

    入参是 `finance_full()` 的三张表（已按报告期对齐），不额外取数。
    """
    income = {str(r.get("period") or "")[:10]: r for r in full.get("income") or []}
    balance = {str(r.get("period") or "")[:10]: r for r in full.get("balance") or []}
    cash = {str(r.get("period") or "")[:10]: r for r in full.get("cashflow") or []}
    ends = sorted(income)
    if not ends:
        return {}, _gap("finance", "", "财务明细为空，无法做基本面反证")

    def last(rows):
        return rows[-1] if rows else ({})

    def pair(col, table, n=5):
        out = []
        for e in ends[-n:]:
            v = fnum((table.get(e) or {}).get(col))
            if v is not None:
                out.append(v)
        return out

    items = []
    rev = pair("OperatingRevenue", income)
    ar = pair("BillAccReceivable", balance)
    inv = pair("Inventories", balance)
    if len(rev) >= 2 and len(ar) >= 2 and rev[0] > 0 and ar[0] > 0:
        rev_g = rev[-1] / rev[0] - 1
        ar_g = ar[-1] / ar[0] - 1
        d = (ar_g - rev_g) * 100
        items.append({"label": "应收增速 − 营收增速", "value": d,
                      "state": "hit" if d >= 30 else ("watch" if d >= 15 else "clear"),
                      "text": f"应收 {ar_g * 100:+.0f}% vs 营收 {rev_g * 100:+.0f}%（差 {d:+.0f}pp）"})
    if len(rev) >= 2 and len(inv) >= 2 and rev[0] > 0 and inv[0] > 0:
        rev_g = rev[-1] / rev[0] - 1
        iv_g = inv[-1] / inv[0] - 1
        d = (iv_g - rev_g) * 100
        items.append({"label": "存货增速 − 营收增速", "value": d,
                      "state": "hit" if d >= 30 else ("watch" if d >= 15 else "clear"),
                      "text": f"存货 {iv_g * 100:+.0f}% vs 营收 {rev_g * 100:+.0f}%（差 {d:+.0f}pp）"})
    cfo = fnum(last([cash.get(e) or {} for e in ends]).get("NetOperateCashFlow"))
    np_ = fnum(last([income.get(e) or {} for e in ends]).get("NPParentCompanyOwners"))
    if cfo is not None and np_ is not None and np_ > 0:
        r = cfo / np_
        items.append({"label": "经营现金流 / 净利润", "value": r,
                      "state": "hit" if r < 0 else ("watch" if r < 0.7 else "clear"),
                      "text": f"{r:.2f}"})
    if not items:
        return {}, _gap("finance", "", "财务行里缺应收/存货/现金流字段")
    return {"items": items,
            "hit": sum(1 for i in items if i["state"] == "hit"),
            "watch": sum(1 for i in items if i["state"] == "watch")}, None


# ------------------------------------------------------------ 周期位置

#: 与 stock_cycle_elastic/config.py 同值（改那边也要改这里）
CYCLE = {
    "gm_window": 20, "gm_min_samples": 8, "gm_std_cycle": 5.0,
    "gm_cv_min": 20.0, "gm_median_min": 5.0, "gm_std_min_for_cv": 2.0,
    "elastic_window": 8, "elastic_ratio_min": 2.0,
    "elastic_zero_cross_max": 0.3333, "rev_growth_min_abs": 0.5,
    "ratio_clip_abs": 50.0, "ratio_min_samples": 4,
    "position_window": 40, "position_min_samples": 8,
    "position_low": 40.0, "position_high": 60.0,
}


def _median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def cycle_position(full: dict) -> tuple[dict, dict | None]:
    """判断① 是不是周期股 → 判断② 弹性 → 判断③ 在周期哪个位置。

    阈值与判定口径照抄 `stock_cycle_elastic/cycle/decide.py`：
      · 判断① 毛利率 σ ≥ 5.0pp **或** CV ≥ 20%（CV 要求中位 ≥5% 且 σ ≥2pp）
      · 判断② 逐季 营业利润环比 ÷ 营收环比 的 8 季中位数，≥2.0 为强
      · 判断③ 产能周转率（TTM 营收 ÷（固定资产+在建工程））的 40 季分位，
        <40 低位、>60 高位，之间中位
    与那边唯一的差别：这里直接用上游的 `_Q`（单季）字段，不再从累计值推导——
    westock 的 income 行本身就带 `OperatingRevenue_Q` / `OperatingProfit_Q` /
    `OperatingCost_Q`，推导反而多一层出错机会。
    """
    income = {str(r.get("period") or "")[:10]: r for r in full.get("income") or []}
    balance = {str(r.get("period") or "")[:10]: r for r in full.get("balance") or []}
    if not income:
        return {}, _gap("finance", "", "财务行为空，算不出周期位置")

    quarters = []
    for end in sorted(income):
        inc = income[end]
        bal = balance.get(end) or {}
        rev = fnum(inc.get("OperatingRevenue_Q"))
        cost = fnum(inc.get("OperatingCost_Q"))
        op = fnum(inc.get("OperatingProfit_Q"))
        fa = fnum(bal.get("TotalFixedAsset"))
        cip = fnum(bal.get("TConstruInProcess"))
        gm = (1 - cost / rev) * 100 if (rev and cost is not None and rev > 0) else None
        # 在建工程在很多公司是空值：有其中一个就算产能资产（另一个按 0 计）
        cap = ((fa or 0.0) + (cip or 0.0)) if (fa or cip) else None
        quarters.append({"end": end, "rev": rev, "op": op, "gm": gm, "cap": cap})
    if len(quarters) < CYCLE["gm_min_samples"]:
        return {}, _gap("finance", "", f"季度数只有 {len(quarters)}，不够算周期档位")

    # 判断① 毛利率 σ / CV
    recent = quarters[-CYCLE["gm_window"]:]
    gms = [q["gm"] for q in recent if q["gm"] is not None]
    gm_std = gm_cv = gm_median = None
    if len(gms) >= CYCLE["gm_min_samples"]:
        mean = sum(gms) / len(gms)
        gm_std = (sum((x - mean) ** 2 for x in gms) / len(gms)) ** 0.5
        gm_median = _median(gms)
        if (gm_std >= CYCLE["gm_std_min_for_cv"] and gm_median is not None
                and abs(gm_median) >= CYCLE["gm_median_min"]):
            gm_cv = gm_std / abs(gm_median) * 100.0
    is_cycle = ((gm_std is not None and gm_std >= CYCLE["gm_std_cycle"])
                or (gm_cv is not None and gm_cv >= CYCLE["gm_cv_min"]))

    # 判断② 弹性：逐季环比比值的中位数
    ratios, zero_cross, pairs = [], 0, 0
    win = quarters[-CYCLE["elastic_window"] - 1:]
    for prev, cur in zip(win, win[1:]):
        if None in (prev["rev"], cur["rev"], prev["op"], cur["op"]):
            continue
        if not prev["rev"] or not prev["op"]:
            continue
        rev_g = cur["rev"] / prev["rev"] - 1
        op_g = cur["op"] / prev["op"] - 1
        pairs += 1
        if prev["op"] * cur["op"] < 0:
            zero_cross += 1
        if abs(rev_g) * 100 < CYCLE["rev_growth_min_abs"]:
            continue
        r = op_g / rev_g
        if abs(r) > CYCLE["ratio_clip_abs"]:
            continue
        ratios.append(r)
    ratio = _median(ratios) if len(ratios) >= CYCLE["ratio_min_samples"] else None
    zero_cross_ratio = (zero_cross / pairs) if pairs else None
    if ratio is None:
        elastic_label = "数据不足"
    elif (zero_cross_ratio is not None
          and zero_cross_ratio > CYCLE["elastic_zero_cross_max"]):
        elastic_label = "不稳定"
    elif ratio >= CYCLE["elastic_ratio_min"]:
        elastic_label = "强"
    else:
        elastic_label = "弱"

    # 判断③ 产能周转率分位
    cap_series = []
    for i, q in enumerate(quarters):
        if q["cap"] in (None, 0):
            continue
        ttm = sum(x["rev"] for x in quarters[max(0, i - 3):i + 1]
                  if x["rev"] is not None)
        if ttm:
            cap_series.append(ttm / q["cap"])
    pos_pct = None
    if len(cap_series) >= CYCLE["position_min_samples"]:
        window = cap_series[-CYCLE["position_window"]:]
        cur = window[-1]
        pos_pct = 100.0 * sum(1 for v in window if v <= cur) / len(window)
    if pos_pct is None:
        pos_label = "数据不足"
    elif pos_pct < CYCLE["position_low"]:
        pos_label = "低位"
    elif pos_pct > CYCLE["position_high"]:
        pos_label = "高位"
    else:
        pos_label = "中位"

    return {"is_cycle": is_cycle, "gm_std": gm_std, "gm_cv": gm_cv,
            "gm_median": gm_median, "elastic": ratio, "elastic_label": elastic_label,
            "position_pct": pos_pct, "position_label": pos_label,
            "quarters": len(quarters)}, None


def enrich(hub, code: str, end: str, price: float | None,
           refresh: bool = False) -> tuple[dict, list[dict]]:
    """四张旁证一次取完，返回 (extras, gaps)。任何一张失败都只记缺口。"""
    from stock_data_fetch.need import want

    gaps: list[dict] = []
    out: dict = {}

    val, g = valuation(hub, code, end, refresh)
    out["valuation"] = val
    if g:
        gaps.append(g)

    con, g = consensus(hub, code, end, price, refresh)
    out["consensus"] = con
    if g:
        gaps.append(g)

    # 财务明细只取一次（40 期 ≈ 10 年，周期位置的分位窗口需要这么长），
    # 基本面反证和周期位置共用同一份，避免两处各自 ensure 一遍。
    full: dict = {}
    kw = {"force": refresh} if refresh else {}
    try:
        need = want.finance(end, periods=40, min_points=1)
        hub.ensure("finance", code, need, **kw)
        full = hub.finance_full(code, need)
    except Exception as e:                          # noqa: BLE001
        log(f"[缺口] {code} finance 全文：{e}")
        gaps.append(_gap("finance", code, f"财务明细（40 期）取不到：{_short(e)}"))

    rk, g = risk(full)
    out["risk"] = rk
    if g:
        gaps.append(g)

    cyc, g = cycle_position(full)
    out["cycle"] = cyc
    if g:
        gaps.append(g)

    for g in gaps:
        g.setdefault("symbol", code)
        log(f"[缺口] {g['domain']}: {g['why']}")
    return out, gaps
