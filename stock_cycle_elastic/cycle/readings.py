"""图形解读：三张指标历史图下面那段"趋势 + 业绩"的话。

纯函数、不 import config——报告渲染（render.py）和看板明细视图（web）
共用同一份阈值逻辑，避免两处口径漂移。阈值由调用方从各自 config 传入。
"""
from __future__ import annotations


def _trend_of(values):
    """前 1/3 与后 1/3 的均值比较 → 趋势短语。样本太少返回 None。"""
    vals = [v for v in values if v is not None]
    if len(vals) < 6:
        return None
    k = max(2, len(vals) // 3)
    head = sum(vals[:k]) / k
    tail = sum(vals[-k:]) / k
    if head == 0:
        return None
    chg = (tail - head) / abs(head)
    if chg >= 0.10:
        return "整体上行"
    if chg <= -0.10:
        return "整体下行"
    return "基本持平"


def _pct_of_last(values):
    """当前值在整段序列里的分位（0–100）。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    cur = vals[-1]
    return 100.0 * (sum(1 for v in vals if v < cur)
                    + 0.5 * sum(1 for v in vals if v == cur)) / len(vals)


def _std(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 3:
        return None
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return var ** 0.5


def gm_read(values, *, gm_threshold, n_quarters=None):
    """毛利率图解读：波动幅度 → 是不是周期特征；当前毛利率水平。

    `gm_threshold` 是周期股的 σ 门槛（config.GM_STD_CYCLE）。
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    cur = vals[-1]
    n = n_quarters or len(vals)
    sd = _std(values)
    parts = []
    trend = _trend_of(values)
    if trend:
        parts.append(f"近 {n} 季毛利率{trend}，当前 {cur:.1f}%")
    if sd is not None:
        wave = "波动明显（超过周期股 σ 门槛）" if sd >= gm_threshold else "波动很小（远低于周期股 σ 门槛）"
        parts.append(f"σ {sd:.2f}pp，{wave}")
        if sd >= gm_threshold:
            parts.append("说明公司盈利水平随行业景气大起大落，毛利率本身是周期温度计。")
        else:
            parts.append("说明公司盈利稳定，毛利率不随景气大起大落（判断①看波动幅度，不看高低）。")
    pct = _pct_of_last(values)
    if pct is not None:
        pos = "偏高位" if pct >= 70 else ("偏低位" if pct <= 30 else "中位")
        parts.append(f"当前毛利率处于自身历史的{pos}（P{pct:.0f}）。")
    return " ".join(parts) if parts else None


def ratio_read(values):
    """弹性比值图解读：利润对收入的放大/挤压；直说业绩含义。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    above = sum(1 for v in vals if v >= 1.0)
    cur = vals[-1]
    parts = [f"近 {len(vals)} 季里有 {above} 季利润增速跑赢营收增速（比值≥1），最近一季比值 {cur:.2f}。"]
    if above > len(vals) / 2:
        parts.append("说明收入向上时利润放得更大（经营杠杆正向），收入一旦减速利润同样被放大收缩。")
    else:
        parts.append("说明利润增速长期跟不上收入，收入增长未必带来同幅利润增长，弹性偏弱。")
    return " ".join(parts)


def turnover_read(values, *, pos_window, pos_min_samples):
    """产能周转率图解读：趋势 + 尾窗分位 → 产能利用与景气冷热。

    `pos_window` / `pos_min_samples` 对应 config 的 POSITION_WINDOW_QUARTERS /
    POSITION_MIN_SAMPLES，分位口径与判断③一致。
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    cur = vals[-1]
    parts = []
    trend = _trend_of(values)
    if trend:
        parts.append(f"近 {len(vals)} 季产能周转率{trend}，当前 {cur:.2f} 次/年")
    window = vals[-pos_window:] if pos_window else vals
    if len(window) >= pos_min_samples:
        pct = 100.0 * sum(1 for v in window if v <= cur) / len(window)
        if pct >= 70:
            parts.append(f"处尾窗 {pct:.0f}% 分位（高位）——产能利用充分、景气偏热，业绩弹性最大但也最接近顶部。")
        elif pct <= 30:
            parts.append(f"处尾窗 {pct:.0f}% 分位（低位）——产能闲置、景气偏冷，业绩在底部区域。")
        else:
            parts.append(f"处尾窗 {pct:.0f}% 分位（中位）——产能利用正常，景气中性。")
    return " ".join(parts) if parts else None
