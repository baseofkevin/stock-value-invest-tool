"""业绩同比/环比判断与展示辅助。

同比、环比用的是同一套「相对加速度」阈值，抽成 _accel_state()，
避免两份 if/elif 各写一遍后走偏（旧版里两处对 ±5 倍基数的处理就不一致）。
"""

ACCEL_STRONG, ACCEL_WEAK = 0.50, 0.10      # 强加速 / 加速 的分界
OUTLIER = 5.0                              # 相对加速度超过 5 倍视为基数异常


def _accel_state(a):
    """相对加速度（小数）→ (state, tag, note)。"""
    if a >= ACCEL_STRONG:
        return "强加速", "【强加速】", f"相对加速度 {a * 100:+.1f}%（≥+50%）"
    if a >= ACCEL_WEAK:
        return "加速", "【加速】", f"相对加速度 {a * 100:+.1f}%（≥+10%）"
    if a > -ACCEL_WEAK:
        return "基本持平", "【基本持平】", f"相对加速度 {a * 100:+.1f}%，在 ±10% 内"
    if a > -ACCEL_STRONG:
        return "减速", "【减速】", f"相对加速度 {a * 100:+.1f}%（≤−10%）"
    return "强减速", "【强减速】", f"相对加速度 {a * 100:+.1f}%（≤−50%）"


def _direction(a):
    return "加速" if a >= ACCEL_WEAK else ("减速" if a <= -ACCEL_WEAK else "持平")


def _result(state, tag, note, a):
    return {"state": state, "direction": _direction(a),
            "is_accelerating": a >= ACCEL_WEAK,
            "is_decelerating": a <= -ACCEL_WEAK,
            "tag": tag, "note": note}


def _unknown(state, tag, note):
    return {"state": state, "direction": "未知",
            "is_accelerating": False, "is_decelerating": False,
            "tag": tag, "note": note}


def earnings_judgement(fm):
    """同比加速度：增长率的相对变化。

    相对加速度 = (本季同比 - 上季同比) / |上季同比|；
    >= +10% 为加速，>= +50% 为强加速；对称判断减速。
    """
    if not fm:
        return _unknown("数据不足", "【数据不足】", "无可用财务数据")
    if fm.get("accel") is None:
        return _unknown("数据不足", "【数据不足】",
                        fm.get("accel_note") or "最近两个季度增速不连续，无法计算")
    if fm.get("profit") is not None and fm["profit"] <= 0:
        return _unknown("盈利为负", "【盈利为负，仅参考】",
                        "单季利润为负，增速差的参考意义弱")

    a = fm["accel"]
    state, tag, note = _accel_state(a)
    if fm.get("accel_note"):
        note += "；" + fm["accel_note"]
    if abs(a) > OUTLIER:
        note += "（基数异常，谨慎解读）"
    return _result(state, tag, note, a)


def qoq_judgement(fm):
    """环比提示：单季净利环比增速的相对变化。

    环比受季节性影响很大，只作辅助；同比加速度仍是主口径。
    """
    if not fm or fm.get("qoq") is None:
        return _unknown("不可比", "【不可比】",
                        (fm or {}).get("qoq_note") or "缺少环比数据")
    a = fm.get("qoq_accel")
    if a is None:
        return _unknown("不可比", "【不可比】",
                        fm.get("qoq_note") or "缺少上上季度数据")
    # 环比本身极端（低基数/负基数）时不给方向，避免把噪声读成加速
    if abs(fm["qoq"]) > OUTLIER or abs(a) > OUTLIER:
        return _unknown("基数异常", "【基数异常】",
                        "环比增速极端，通常由低基数/负基数造成，不判断加速")

    state, tag, note = _accel_state(a)
    if fm.get("qoq_accel_note"):
        note += "；" + fm["qoq_accel_note"]
    return _result(state, tag, note, a)


def trend_arrow(a):
    if a is None:
        return "—"
    return "↑ 加速" if a >= 5 else ("↓ 减速" if a <= -5 else "→ 持平")


def consecutive_qoq(hist, limit=4):
    """从最新季度往前取连续可比的环比记录。"""
    out = []
    for h in reversed(hist or []):
        if h.get("qoq") is None:
            break
        out.append(h)
    return list(reversed(out))[-limit:]


def earnings_combo(ej):
    """把同比、环比两个方向合并成一句结论。"""
    yoy_dir = ej.get("direction")
    qoq_dir = ej.get("qoq_direction")
    if yoy_dir in ("加速", "减速") and qoq_dir == yoy_dir:
        return f"同比与环比同向（{yoy_dir}）"
    if yoy_dir in ("加速", "减速") and qoq_dir in ("加速", "减速"):
        return f"同比与环比背离（同比{yoy_dir}、环比{qoq_dir}）"
    if yoy_dir in ("加速", "减速"):
        return f"以同比为主（{yoy_dir}），环比{ej.get('qoq_state', '不可比')}"
    if yoy_dir == "持平" and qoq_dir in ("加速", "减速"):
        return f"同比基本持平，环比{qoq_dir}；以环比为辅，注意季节性"
    if yoy_dir in ("加速", "减速") and qoq_dir == "持平":
        return f"同比{yoy_dir}，环比基本持平"
    if yoy_dir == "持平" and qoq_dir == "持平":
        return "同比与环比均基本持平"
    return "数据不足，无法判断"
