"""20 日换手、池内分位与硬过滤。

从原版 `kline_screen/metrics/turnover.py` 搬来。**删掉了整套 volume 单位换算**
（`unit_from_prefix` / `infer_unit` / `volume_to_shares`）：那是给「上游给的是手、
科创板给的是股」打的两可补丁；共享库的 `kline.volume` 入库前已统一成**股**，
这层换算在新版里没有任何工作要做，留着反而误导后来人以为还要换。

另一个口径变化（有意，记在 `README.md`）：换手率的分母从「d* 日流通市值」
改成「S 日流通市值」。原因：共享库的 `quote` 域是「当期一份」，历史某日的
流通市值不在库里，而 20 日均成交额对几天的市值漂移不敏感（原版注释自己也是
这么论证「用 d* 里最近一天」的，这里只是再往右挪了最多 N 个交易日）。
"""

from __future__ import annotations

from .params import TURN_MIN_COVER, TURN_WINDOW
from .rules import fnum


def turnover20(code, bars, cal20, cap_yuan):
    """最近 20 个交易日的日均成交额 ÷ 流通市值（%）。

    返回 (值, 覆盖天数)。覆盖 < TURN_MIN_COVER 天（停牌/缺数据）视为数据不足，
    返回 (None, n)，由调用方决定保留并标注，绝不因为缺数据静默剔除。

    `bars` 是 hub `kline` 行（dict，volume 已是股、amount 已是元）。
    """
    if not bars or not cap_yuan:
        return None, 0
    by_day = {b["date"]: b for b in bars if b and b.get("date")}
    amts = []
    for d in cal20:
        b = by_day.get(d)
        if b is None:
            continue
        a = fnum(b.get("amount"))
        if a is not None and a > 0:
            amts.append(a)
        else:
            # amount 缺失时退回 close×volume（hub 的 kline 两列都有），
            # 比原版「3 元组旧缓存没有 amount」的兜底分支覆盖面小得多。
            close, vol = fnum(b.get("close")), fnum(b.get("volume"))
            if close is not None and vol is not None:
                amts.append(close * vol)
    if len(amts) < TURN_MIN_COVER:
        return None, len(amts)
    return sum(amts) / len(amts) / cap_yuan * 100, len(amts)


def annotate_turnover(hits, pool_codes, km, cap_snapshot, cal20):
    """给入选股标注 20 日换手及其在当天粗筛池内的分位，返回 {code: 换手%}。

    分位 = 池内严格小于该值的比例（0 = 池内最冷，100 = 最热）。只标注、
    不剔除；数据不足（覆盖 <15 天）的记为 None 且分位为 None，绝不静默丢弃。
    """
    def cap_of(c):
        v = cap_snapshot.get(c)
        return fnum(v)

    pool_t = {}
    for c in pool_codes:
        t, _ = turnover20(c, km.get(c) or [], cal20, cap_of(c))
        if t is not None:
            pool_t[c] = t
    vals = sorted(pool_t.values())
    m = len(vals)
    for x in hits:
        t, n = turnover20(x["code"], km.get(x["code"]) or [], cal20, cap_of(x["code"]))
        x["turn20"] = round(t, 3) if t is not None else None
        x["turn20_days"] = n
        if t is not None and m:
            x["turn20_rank"] = round(sum(1 for v in vals if v < t) / m * 100, 1)
        else:
            x["turn20_rank"] = None
    return pool_t


def apply_turnover(hits, pool_codes, km, cap_snapshot, cal20,
                   pct_base, min_n, max_n):
    """按 20 日换手的池内分位处理名单，返回 (保留名单, 元信息, 剔除列表)。

    pct_base <= 0 或 >= 1：只排序标注、不剔除（默认，保持「上穿+放量」的本意）。
    0 < pct_base < 1：硬过滤，保留分位最低的 pct_base 段；保留数 < min_n
    放宽一档到 0.60，> max_n 收紧一档到 0.20（若 0.20 档会清空则保持原档）。
    池内可算换手的样本 <20 只时分位不稳定，退化为只标注。
    """
    pool_t = annotate_turnover(hits, pool_codes, km, cap_snapshot, cal20)
    m = len(pool_t)
    if not (0 < pct_base < 1):
        return hits, {"启用": False, "模式": "仅排序/标注", "窗口": TURN_WINDOW,
                      "池内样本": m,
                      "原因": f"--turn-pct={pct_base:g}，换手只参与排序，不剔除"}, []
    if m < 20:
        return hits, {"启用": False, "模式": "仅排序/标注", "窗口": TURN_WINDOW,
                      "池内样本": m,
                      "原因": f"池内可算换手的样本仅 {m} 只（<20），分位不稳定"}, []

    vals = sorted(pool_t.values())

    def cut(p):
        return vals[min(m - 1, int(m * p))]

    def keep_n(p):
        c = cut(p)
        return sum(1 for x in hits if x["turn20"] is not None and x["turn20"] <= c)

    pct, note = pct_base, ""
    n_base = keep_n(pct)
    if n_base < min_n and pct < 0.6:
        pct = 0.6
        note = f"按最低{int(pct_base*100)}%仅 {n_base} 只（<{min_n}），放宽到最低 60%"
    elif n_base > max_n and pct > 0.2 and keep_n(0.2) > 0:
        pct = 0.2
        note = f"按最低{int(pct_base*100)}%有 {n_base} 只（>{max_n}），收紧到最低 20%"
    elif n_base > max_n:
        note = f"按最低{int(pct_base*100)}%有 {n_base} 只（>{max_n}），但 20% 档会清空，保持最低 {int(pct*100)}%"

    c = cut(pct)
    keep, dropped = [], []
    for x in hits:
        if x["turn20"] is None:
            keep.append(x)
            continue
        if x["turn20"] <= c:
            keep.append(x)
        else:
            dropped.append((x["code"],
                            f"⑥ 20日换手分位 > 池内最低{int(pct*100)}%"
                            f"（{x['turn20']:.2f}% > {c:.2f}%）"))
    meta = {"启用": True, "模式": "硬过滤", "窗口": TURN_WINDOW, "池内样本": m,
            "保留档位": f"最低{int(pct*100)}%", "剔除线%": round(c, 3),
            "基数档位": f"最低{int(pct_base*100)}%", "自适应": note or "未触发",
            "剔除数": len(dropped)}
    return keep, meta, dropped
