"""日 K / 财务 / 行情取数：全部走共享库 hub。

迁移说明（2026-11-15，原 stock-judge-top-tool 的 judge/data.py 重写）
--------------------------------------------------------------------
| 原版（westock MCP） | 本版（共享库 hub） |
|---|---|
| `data_kline` fq="qfq"，3 段×1 年 limit=300 | `ensure_many("kline", ...)` 取 **raw**，再用 `factor.adjusted()` 本地算 qfq |
| 换手率 `exchange` 由上游直接给 | **折算**：`amount ÷ 流通市值 × 100`（市值来自 quote 域，当期一份） |
| `data_finance` fields=all num=60 | `ensure_many("finance", ...)` + `finance_full()["income"]`（raw_json 拆包，字段名与上游一致） |
| `data_quote` 取名称/存在性 | profile 域（`stock` 表：name / list_date / industry） |
| 单股 JSON 缓存 cache/top_*.json | **删除**：缓存归 hub（coverage 闸 + 共享库） |

三个口径决定（都记在 README §2）：

1. **qfq 本地算**：共享库只存 raw（`fp=fq=raw`），复权是"同一份 raw × 不同因子"，
   不该为某个工具再存一份复权序列。锚点 = 基准日 `end`（原版 westock 的 qfq
   也是以查询日为锚）。
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


def _nodes(rows_raw, rows_qfq, circ_mv):
    """hub kline 行 → judge 指标层的 node（last/high/exchange/volume）。

    `last` / `high` / `low` / `open` 取 **qfq** 值（价格类指标必须复权）；
    `volume` / `exchange` 用 **raw** 口径（量在除权日不缩放；换手率是
    未复权量配流通市值，两个都 raw 才对得上——见 factor.PRICE_COLS 注释）。
    """
    out = []
    for raw, adj in zip(rows_raw, rows_qfq):
        amt = fnum(raw.get("amount"))
        if amt is None or amt <= 0:
            c, v = fnum(raw.get("close")), fnum(raw.get("volume"))
            amt = c * v if (c is not None and v is not None) else None
        exch = amt / circ_mv * 100 if (amt and circ_mv) else None
        out.append({
            "date": str(raw["date"]),
            "open": adj.get("open"), "high": adj.get("high"),
            "low": adj.get("low"), "last": adj.get("close"),
            "volume": raw.get("volume"),
            "exchange": round(exch, 4) if exch is not None else None,
            "amount": amt,
        })
    return out


def fetch(hub, code, end, refresh=False):
    """返回 (nodes, finance_rows, profile)。

    - nodes：qfq 日 K + 折算换手率，按日期升序；
    - finance_rows：westock income 原始行（含 EndDate / InfoPublDate /
      NPParentCompanyYOY_Q 等判断字段），可能为空列表；
    - profile：stock 表行（name / list_date / industry），取不到为 {}。

    `refresh=True` 透传为 hub `ensure` 的 `force`（清重试标记后重取），
    对应原版 `--refresh` 忽略缓存。
    """
    from stock_data_fetch import factor
    from stock_data_fetch.need import want

    kw = {"force": refresh} if refresh else {}

    # ---- profile：名称 / 存在性（一次，静态事实） --------------------------
    prof = {}
    batch = hub.ensure_many("profile", [code], want.profile(), **kw)
    if code in batch.ok and batch.ok[code].rows:
        prof = batch.ok[code].rows[0]

    # ---- 日 K：raw → qfq → node ------------------------------------------
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
        start = str(rows[0]["date"])
        # qfq 依赖 dividend 表；没取过就全 1.0（静默错），必须先 ensure。
        hub.ensure_many("dividend", [code], want.dividend(start), **kw)
        adj = factor.adjusted(hub.conn, hub.cal, code, rows, anchor=end)
        # 流通市值：quote 域当期一份（分母口径见模块头第 2 条）
        cap = None
        try:
            qb = hub.ensure_many("quote", [code], want.quote(end), **kw)
            if code in qb.ok and qb.ok[code].rows:
                cap = fnum(qb.ok[code].rows[0].get("circ_mv"))
        except Exception as e:                      # 市值取不到不该拦住 K 线因子
            log(f"[数据] {code} 行情(quote)获取失败，换手率将不可用：{e}")
        nodes = _nodes(rows, adj, cap)
        if not cap:
            log(f"[数据] {code} 无流通市值：换手类指标（av20/ratio/AVG20）将为 —")
        ks = [n["date"] for n in nodes]
        log(f"[数据] 日K完成：{len(nodes)} 根（{ks[0]} ~ {ks[-1]}，qfq，锚点 {end}）")
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
