"""日 K / 财务 / 行情取数：全部走共享库 hub。

迁移说明（2026-11-15，原 stock-judge-top-tool 的 judge/data.py 重写）
--------------------------------------------------------------------
| 原版（westock MCP） | 本版（共享库 hub） |
|---|---|
| `data_kline` fq="qfq"，3 段×1 年 limit=300 | `ensure_many("kline", ...)` 取 **raw**，原样使用（2026-11-15 起与选股工具同口径） |
| 换手率 `exchange` 由上游直接给 | **折算**：`amount ÷ 流通市值 × 100`（市值来自 quote 域，当期一份） |
| `data_finance` fields=all num=60 | `ensure_many("finance", ...)` + `finance_full()["income"]`（raw_json 拆包，字段名与上游一致） |
| `data_quote` 取名称/存在性 | profile 域（`stock` 表：name / list_date / industry） |
| 单股 JSON 缓存 cache/top_*.json | **删除**：缓存归 hub（coverage 闸 + 共享库） |

三个口径决定（都记在 README §2）：

1. **价格一律 raw（不再本地算 qfq）**：与 `stock_kline_screen` 统一。之前这里用
   `factor.adjusted()` 算前复权，同一只票会出现「选股说站上 MA20、这里说跌破」
   （实测 2026-11-13 的 sh601288 农业银行：qfq MA20 = 7.358、raw = 7.371）。
   位置类指标（距高点 %）本来就是同一价格序列内的比值，除权日只会让「前复权」
   与「不复权」差一个当天的除权缺口，取舍一致更重要。
   代价：除权日附近的历史高点会带着除权缺口（例如 10 派 3 元会让 250 日高点
   虚高约 3%），所以 Q 报告里「距 250 日高点」的绝对值在有分红时略偏保守。
2. **换手率折算**：共享库 kline 没有 `exchange` 列。折算式
   `exchange = amount / circ_mv × 100` 与 kline_screen 的 `turnover20` 同款
   （分母用"当期一份"的流通市值，历史日的真实流通市值不在库里——两边接受
   同一个近似，注释见 stock_kline_screen/turnover.py 模块头）。amount 缺失时
   退回 `close(raw) × volume`。
3. **不缺段即不缺洞**：原版分 3 段拉、缺段抛错，是因为"分段请求"可能部分失败
   留下整年空洞。hub 的 kline 是一次连续区间 + coverage 判深，`InsufficientDepth`
   由 need 的 `min_bars` 门槛表达，不再需要人工分段对账。
"""
from __future__ import annotations

from .common.values import fnum
from .log import log

# 原版 3 段×365 天 ≈ 3 年 ≈ 730 根；留余量取 750。门槛 30 根（约 6 周）
# 与原版 `_kline_error` 的下限一致。
KLINE_BARS = 750
KLINE_MIN_BARS = 30
FIN_PERIODS = 60


def _nodes(rows, circ_mv):
    """hub kline 行 → judge 指标层的 node（last/high/exchange/volume）。

    价格一律取 raw（不复权，见模块头第 1 条）。换手率是**折算**的：
    `amount ÷ 流通市值 × 100`，市值只有"当期一份"，所以历史换手率是近似——
    与 stock_kline_screen 的 turnover20 同一个近似。流通市值缺失时
    `exchange` 为 None，指标层会退回用成交额自身的分位（见 indicators）。
    """
    out = []
    for row in rows:
        amt = fnum(row.get("amount"))
        if amt is None or amt <= 0:
            c, v = fnum(row.get("close")), fnum(row.get("volume"))
            amt = c * v if (c is not None and v is not None) else None
        exch = amt / circ_mv * 100 if (amt and circ_mv) else None
        out.append({
            "date": str(row["date"]),
            "open": row.get("open"), "high": row.get("high"),
            "low": row.get("low"), "last": row.get("close"),
            "volume": row.get("volume"),
            "exchange": round(exch, 4) if exch is not None else None,
            "amount": amt,
        })
    return out


def fetch(hub, code, end, refresh=False):
    """返回 (nodes, finance_rows, profile)。

    - nodes：日 K（raw 价）+ 折算换手率，按日期升序；
    - finance_rows：westock income 原始行（含 EndDate / InfoPublDate /
      NPParentCompanyYOY_Q 等判断字段），可能为空列表；
    - profile：stock 表行（name / list_date / industry），取不到为 {}。

    `refresh=True` 透传为 hub `ensure` 的 `force`（清重试标记后重取），
    对应原版 `--refresh` 忽略缓存。
    """
    from stock_data_fetch.need import want

    kw = {"force": refresh} if refresh else {}

    # ---- profile：名称 / 存在性（一次，静态事实） --------------------------
    prof = {}
    batch = hub.ensure_many("profile", [code], want.profile(), **kw)
    if code in batch.ok and batch.ok[code].rows:
        prof = batch.ok[code].rows[0]

    # ---- 日 K：raw → node -------------------------------------------------
    from stock_data_fetch.errors import InsufficientDepth, HoleUnreachable
    need_k = want.kline(hub.cal, end, bars=KLINE_BARS, min_bars=KLINE_MIN_BARS)
    try:
        batch = hub.ensure_many("kline", [code], need_k, **kw)
    except (InsufficientDepth, HoleUnreachable) as e:
        # 新股/长期停牌：上游就这么多。上层按"根数不足"报，不算取数失败。
        log(f"[数据] {code} K线深度不足：{e}")
        batch = None
    rows = sorted(batch.ok[code].rows, key=lambda r: str(r["date"])) \
        if (batch and code in batch.ok) else []

    nodes = []
    if rows:
        # 流通市值：quote 域当期一份（分母口径见模块头第 2 条）
        cap = None
        try:
            qb = hub.ensure_many("quote", [code], want.quote(end), **kw)
            if code in qb.ok and qb.ok[code].rows:
                cap = fnum(qb.ok[code].rows[0].get("circ_mv"))
        except Exception as e:                      # 市值取不到不该拦住 K 线因子
            log(f"[数据] {code} 行情(quote)获取失败，换手率将不可用：{e}")
        nodes = _nodes(rows, cap)
        if not cap:
            log(f"[数据] {code} 无流通市值：换手类指标（av20/ratio/AVG20）将为 —")
        ks = [n["date"] for n in nodes]
        log(f"[数据] 日K完成：{len(nodes)} 根（{ks[0]} ~ {ks[-1]}，raw）")
    else:
        log("[数据] 日K完成：0 根（库里无数据且未能取到）")

    # ---- 财务：income 原始行 ----------------------------------------------
    fin_rows = []
    need_f = want.finance(end, periods=FIN_PERIODS, min_points=1)
    try:
        hub.ensure_many("finance", [code], need_f, **kw)
        fin_rows = hub.finance_full(code, need_f).get("income") or []
    except Exception as e:
        log(f"[数据] {code} 财务获取失败（业绩卡片将不判断）：{e}")
    log(f"[数据] 财务完成：{len(fin_rows)} 期")

    return nodes, fin_rows, prof
