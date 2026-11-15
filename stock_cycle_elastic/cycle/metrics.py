"""三个指标（README.md「metrics：三个纯函数」）—— 纯计算，不含阈值，不含判断。

    gross_margin_std(quarters, cfg)     → float | None       判断① 是不是周期股
    elasticity_ratio(quarters, cfg)     → dict               判断② 弹性大不大
    capacity_percentile(quarters, cfg)  → dict               判断③ 在周期哪个位置

共同约定：
  * 入参是 build() 产出的 quarters 列表（升序，最新在末）
  * **不抛错、不打印、不读 config 的模块级状态**（阈值由参数传入，便于单测）
  * 缺失一律返回 None，绝不返回 0
  * **绝不读 `contract_liab`** —— 它是透传字段（§2.8），
    测试会用"只含 6 个计算字段"的序列来钉住这一点。
"""

from cycle import util


# ---------------------------------------------------------------------------
# 判断① 是不是周期股 → 毛利率 5 年标准差（百分点）
# ---------------------------------------------------------------------------


def gross_margin_std(quarters, cfg):
    """最近 WAVE_WINDOW_QUARTERS 季单季毛利率的样本标准差，单位 pp。

    为什么用标准差而不是极差：
        极差只由两个点决定（一个离群值就能把它拉到天上），标准差用了所有点。

    为什么必须用**单季**毛利率而不是累计：
        累计值天然平滑（Q1 是 1 季、Q4 是 4 季累加），会系统性低估波动。
        测试用同一份数据构造"单季/累计"两种输入做对照。

    返回 None 的情况：
        * 有效样本 < cfg.GM_MIN_SAMPLES
        * 样本数 < 2（标准差无定义）
    """
    if not quarters:
        return None
    recent = quarters[-cfg.WAVE_WINDOW_QUARTERS:] if cfg.WAVE_WINDOW_QUARTERS > 0 else []
    gms = [q.get("gross_margin") for q in recent]
    valid = [v for v in gms if v is not None]
    if len(valid) < cfg.GM_MIN_SAMPLES:
        return None
    sd = util.stdev(valid)
    if sd is None:
        return None
    return sd * 100.0


def gross_margin_cv(quarters, cfg):
    """最近 WAVE_WINDOW_QUARTERS 季毛利率的**变异系数**（%，相对波动）。

        CV = σ / |毛利率中位数| × 100

    为什么需要它（判断① 的第二条通道）：
        σ 是有量纲的绝对百分点。毛利率水平只有十几个点的公司，σ 在数学上就
        到不了 5pp —— 实测毛利率中位 < 10% 的 17 只里只有 35% 过线，而 ≥10%
        的组是 49%。同一把尺子，低毛利公司天然吃亏。
        CV 把"波动幅度"换成"相对自己水平的摆动比例"，
        毛利率 15% 的公司摆动 4pp（CV≈27%）与毛利率 40% 的公司摆动 11pp
        是同一件事。

    返回：
        {"cv": float|None, "median": float|None, "samples": int}
        cv 单位是百分数（20.0 表示 20%），median 单位是百分数（毛利率 %）。

    返回 cv = None 的情况（一律当"这条通道给不出结论"，不当作"否"）：
        * 有效样本 < cfg.GM_MIN_SAMPLES（与 σ 同一条样本门槛）
        * 中位数绝对值 < cfg.GM_MEDIAN_MIN → 分母太小，CV 会爆炸
          （实测振华新材中位 −3.8% → CV=354%，那不是波动，那是噪声）
        * σ < cfg.GM_STD_MIN_FOR_CV → 绝对波动太小，用毛利率量它本就不合适
          （如纯冶炼加工：毛利率 2-6% 之间晃，动的是加工费不是价差）
    """
    result = {"cv": None, "median": None, "samples": 0}
    if not quarters:
        return result

    recent = quarters[-cfg.WAVE_WINDOW_QUARTERS:] if cfg.WAVE_WINDOW_QUARTERS > 0 else []
    gms = [q.get("gross_margin") for q in recent]
    valid = [v for v in gms if v is not None]
    result["samples"] = len(valid)
    if len(valid) < cfg.GM_MIN_SAMPLES:
        return result

    sd = util.stdev(valid)
    median = util.median(valid)
    if sd is None or median is None:
        return result

    sd_pp = sd * 100.0
    med_pp = median * 100.0
    result["median"] = med_pp

    # 守卫 1：中位数接近 0（或为负）→ 分母趋零，比值没有意义
    if abs(med_pp) < cfg.GM_MEDIAN_MIN:
        return result
    # 守卫 2：绝对波动太小 → 这条通道不适用（让它老实走通道 A）
    if sd_pp < cfg.GM_STD_MIN_FOR_CV:
        return result

    result["cv"] = sd_pp / abs(med_pp) * 100.0
    return result


# ---------------------------------------------------------------------------
# 判断② 弹性大不大 → 营业利润增速 ÷ 营收增速 的中位数
# ---------------------------------------------------------------------------


def elasticity_ratio(quarters, cfg):
    """最近 ELASTIC_WINDOW_QUARTERS 季的逐季比值中位数。

        比值 = %Δ营业利润 / %Δ营收        （逐季环比）

    返回：
        {"ratio": float|None, "reliable": bool|None, "samples": int,
         "zero_cross": int, "zero_cross_ratio": float|None}

    `reliable` 的判定有**两条**，任一命中即不可信：
        1. 近 8 季营收增速中位数绝对值 < cfg.REV_MEDIAN_STABLE_MIN
           营收长期不动的公司，分母趋零，比值会爆炸成几十倍，那些值不是"弹性"。
        2. 已进入样本的季度里，「营业利润跨零」的占比 > cfg.ELASTIC_ZERO_CROSS_MAX
           DOL 在盈亏平衡点趋于无穷（README 有推导），跨零季度的比值是噪声。
           实测 904 个逐季对里 135 个（15%）跨零；按股票算 46 只（41%）至少一季跨零。

    **为什么不直接剔除跨零季度**（曾经的首选方案）：
        剔了之后 33 只标签会变、约 15 只会因样本不足掉成 na。用户选择保留信息量，
        改为「照算但标不稳定」—— 数值仍然显示，只是不给强弱结论。
        这与 `REV_MEDIAN_STABLE_MIN` 的处理方式完全一致（unstable 不取代比值）。

    三个保护（都不是判定阈值，是防脏数据）：
        1. |营收增速| < REV_GROWTH_MIN_ABS  → 跳过该季（分母趋零）
        2. |比值| > RATIO_CLIP_ABS          → 剔除（荒值）
        3. 有效样本 < RATIO_MIN_SAMPLES     → ratio = None

    **负比值必须参与中位数**：营收涨 10%、营业利润跌 10% → 比值 -1。
    如果因为"负的不像弹性"就丢掉，会系统性高估弹性（只留下暴涨的季度）。

    为什么用中位数而不是均值/回归：
        DOL 的局部值在盈亏平衡点附近会趋于无穷，均值会被单季异常彻底带偏；
        中位数对离群值稳健。代价是没有标准误 —— 这是已知边界（§13 第 4 条）。
    """
    window = cfg.ELASTIC_WINDOW_QUARTERS
    n = len(quarters)
    result = {"ratio": None, "reliable": None, "samples": 0,
              "zero_cross": 0, "zero_cross_ratio": None}
    if n < 2 or window < 2:
        return result

    ratios = []
    zero_cross = 0
    # 逐季环比需要 i-1，所以从 max(1, n-window) 开始；窗口内的第一季因缺上期被跳过
    start = max(1, n - window)
    for i in range(start, n):
        cur = quarters[i]
        prev = quarters[i - 1]
        rev_now = cur.get("revenue_q")
        rev_prev = prev.get("revenue_q")
        op_now = cur.get("op_profit_q")
        op_prev = prev.get("op_profit_q")
        if None in (rev_now, rev_prev, op_now, op_prev):
            continue
        if rev_prev == 0:
            continue

        g_rev = util.pct_change(rev_now, rev_prev)
        g_op = util.pct_change(op_now, op_prev)
        if g_rev is None or g_op is None:
            continue
        if abs(g_rev) < cfg.REV_GROWTH_MIN_ABS:
            continue                      # 分母趋零，跳过
        ratio = g_op / g_rev
        if abs(ratio) > cfg.RATIO_CLIP_ABS:
            continue                      # 荒值剔除
        ratios.append(ratio)
        # 只统计**已进入样本**的季度里有多少跨零 —— 被前面几条过滤掉的季度
        # 本来就不参与中位数，算进分母会把比例稀释得失去意义。
        if (op_now >= 0) != (op_prev >= 0):
            zero_cross += 1

    result["samples"] = len(ratios)
    result["zero_cross"] = zero_cross
    if ratios:
        result["zero_cross_ratio"] = zero_cross / len(ratios)
    if len(ratios) < cfg.RATIO_MIN_SAMPLES:
        return result

    result["ratio"] = util.median(ratios)

    # 稳定性：近 8 季营收同比（对 4 季前）的中位数绝对值
    rev_growth = _recent_revenue_growth(quarters, cfg)
    if not rev_growth:
        result["reliable"] = None
    else:
        med = util.median(rev_growth)
        result["reliable"] = abs(med) >= cfg.REV_MEDIAN_STABLE_MIN
    return result


def _recent_revenue_growth(quarters, cfg):
    """近 ELASTIC_WINDOW_QUARTERS 季的单季营收**同比**增速（%）。"""
    yoy = cfg.YOY_PERIODS
    n = len(quarters)
    window = cfg.ELASTIC_WINDOW_QUARTERS
    out = []
    for i in range(max(yoy, n - window), n):
        cur = quarters[i].get("revenue_q")
        base = quarters[i - yoy].get("revenue_q")
        if cur is None or base is None:
            continue
        g = util.pct_change(cur, base)
        if g is not None:
            out.append(g)
    return out


# ---------------------------------------------------------------------------
# 判断③ 在周期哪个位置 → 产能周转率的 10 年分位
# ---------------------------------------------------------------------------


def capacity_percentile(quarters, cfg):
    """产能周转率在当前窗口内的分位（0~100）。

        产能周转率 = 最近 4 季营收之和（TTM） / (固定资产 + 在建工程)

        注：口径是 TTM 而非「单季 × 4」，实现在 build._fill_capacity_turnover；
        ×4 会把淡旺季等比例放大，导致分位系统性偏移。

    返回：
        {"pct": float|None, "current": float|None, "samples": int}

    为什么用"跟自己过去比"的分位而不是绝对值：
        周转率跨公司完全不可比 —— 重资产 2 次/年、轻资产 8 次/年都可能是正常。
        只有看自身历史，才能回答"现在算高还是算低"。

    已知偏差（写进报告脚注，§13 第 6 条）：
        分子是**营收**不是产量，所以产品涨价会让周转率虚高（误判"高位"），
        跌价会让它虚低（误判"低位"）。

    关键边界：
        * 当前季的 capacity_turnover 为 None → pct = None
          （不能用倒数第二季冒充当前，否则"最新数据缺失"会被静默当成"还是上季的位置"）
        * None 的季度不进分母（分母是**有效个数**）
        * 有效样本 < POSITION_MIN_SAMPLES → None
    """
    result = {"pct": None, "current": None, "samples": 0}
    if not quarters:
        return result

    window = quarters[-cfg.POSITION_WINDOW_QUARTERS:] if cfg.POSITION_WINDOW_QUARTERS > 0 else []
    series = [q.get("capacity_turnover") for q in window]
    valid = [v for v in series if v is not None]
    result["samples"] = len(valid)

    if len(valid) < cfg.POSITION_MIN_SAMPLES:
        return result

    # 当前值必须来自**最新一季本身**，不能从 valid 的最后一个取
    current = quarters[-1].get("capacity_turnover")
    if current is None:
        return result

    result["current"] = current
    result["pct"] = util.percentile_rank(valid, current)
    return result


def capacity_direction(quarters, cfg):
    """产能方向：近 CAPACITY_TREND_QUARTERS 季「固定资产 + 在建工程」的变化方向。

    返回：
        {"direction": "expand"|"shrink"|"flat"|None,
         "asset_growth": float|None,      # % ，正为扩张
         "revenue_growth": float|None}    # % ，同期营收变化

    为什么判断③ 需要这个标记：
        产能周转率 = 营收 ÷ 产能。比值走低有**两种语义相反**的成因：
          A 需求崩塌（营收↓、产能不变）→ 供给端出清，与「低位」的文档定义一致
          B 大举扩产（营收不变、产能↑↑）→ 产能即将增加，与「低位」的定义相反
        而工具只看周转率，把两种都标成「低位」。实测 49 只低位股里 9 只（18%）
        属于 B（营收在涨、资产涨得更快），如中色股份营收 +89% 却被判「低位」。

    判定（阈值都在 config，都属于"判定阈值"）：
        expand : asset_growth >= CAPACITY_EXPAND_GROWTH 且 revenue_growth >= 0
        shrink : asset_growth <= CAPACITY_SHRINK_GROWTH
        flat   : 其余

    `expand` 额外要求营收不降，是因为「营收跌 + 资产涨」的组合更可能是
    需求下滑时仍在硬着头皮投（B 与 A 的混合），此时说"扩产中"会误导。

    **它不参与判断③ 的取值** —— 只是给「低位」加一句限定语，告诉用户
    这个低位是"需求走了"还是"产能来了"。
    """
    result = {"direction": None, "asset_growth": None, "revenue_growth": None}
    n = len(quarters)
    window = cfg.CAPACITY_TREND_QUARTERS
    if n < window or window < 2:
        return result

    first = quarters[n - window]
    last = quarters[-1]

    def _assets(q):
        fa, cip = q.get("fixed_asset"), q.get("cip")
        if fa is None and cip is None:
            return None
        return (fa or 0.0) + (cip or 0.0)

    a0, a1 = _assets(first), _assets(last)
    r0, r1 = first.get("revenue_q"), last.get("revenue_q")

    if a0 and a0 > 0 and a1 is not None:
        result["asset_growth"] = (a1 / a0 - 1.0) * 100.0
    if r0 and r0 > 0 and r1 is not None:
        result["revenue_growth"] = (r1 / r0 - 1.0) * 100.0

    ag = result["asset_growth"]
    if ag is None:
        return result

    # 浮点边界：1.15e9/1.0e9 - 1 算出来是 0.14999999999999991，
    # 直接跟 15.0 比会判成 flat。加一个极小的容差把边界钉成闭区间，
    # 与其余阈值「恰好等于也算」的语义保持一致。
    eps = 1e-9
    rg = result["revenue_growth"]
    if ag >= cfg.CAPACITY_EXPAND_GROWTH - eps and (rg is None or rg >= -eps):
        result["direction"] = "expand"
    elif ag <= cfg.CAPACITY_SHRINK_GROWTH + eps:
        result["direction"] = "shrink"
    else:
        result["direction"] = "flat"
    return result


def position_window_actual(quarters, cfg):
    """判断③ 实际用到的有效季数。

    名义窗口是 POSITION_WINDOW_QUARTERS（40 季 = 10 年），但两种原因会让实际更短：
      1. 上市不足 10 年（实测 22/113 = 19% 的股票不足 40 季，最少只有 15 季）
      2. 改 TTM 口径后，最早 3 季凑不满 4 季滚动和，必然为 None
    报告里必须显示**实际**季数，否则「10 年分位」这句话与事实不符。
    """
    if not quarters:
        return 0
    window = quarters[-cfg.POSITION_WINDOW_QUARTERS:] if cfg.POSITION_WINDOW_QUARTERS > 0 else []
    return sum(1 for q in window if q.get("capacity_turnover") is not None)


# ---------------------------------------------------------------------------
# 汇总（渲染层用，不含判断）
# ---------------------------------------------------------------------------


def compute_all(quarters, cfg):
    """一次算完所有指标，返回紧凑 dict。

    `gm_cv` 是判断① 第二条通道用的相对波动。它跟 `gm_std` 共享同一个窗口
    与同一条样本门槛 —— 两者永远描述同一批季度，不会出现"σ 用 20 季、
    CV 用 15 季"这种自相矛盾。

    `capacity_direction` 不参与判断③ 的取值，只是给「低位/高位」加限定语
    （这个低位是"需求走了"还是"产能来了"）。
    """
    return {
        "gm_std": gross_margin_std(quarters, cfg),
        "gm_cv": gross_margin_cv(quarters, cfg),
        "elasticity": elasticity_ratio(quarters, cfg),
        "position": capacity_percentile(quarters, cfg),
        "capacity_direction": capacity_direction(quarters, cfg),
    }


def series_for_chart(quarters, cfg):
    """给报告画图用的三个序列（不含任何结论）。

        gross_margin_20q  —— 最近 20 季毛利率（%）
        ratio_8q          —— 最近 8 季逐季比值
        turnover_40q      —— 最近 40 季产能周转率
    """
    gm_window = quarters[-cfg.WAVE_WINDOW_QUARTERS:]
    gross = [
        {"date": q["date"],
         "value": None if q.get("gross_margin") is None else q["gross_margin"] * 100.0}
        for q in gm_window
    ]

    ratios = []
    n = len(quarters)
    start = max(1, n - cfg.ELASTIC_WINDOW_QUARTERS)
    for i in range(start, n):
        cur, prev = quarters[i], quarters[i - 1]
        rev_now, rev_prev = cur.get("revenue_q"), prev.get("revenue_q")
        op_now, op_prev = cur.get("op_profit_q"), prev.get("op_profit_q")
        if None in (rev_now, rev_prev, op_now, op_prev) or rev_prev == 0:
            ratios.append({"date": cur["date"], "value": None})
            continue
        g_rev = util.pct_change(rev_now, rev_prev)
        g_op = util.pct_change(op_now, op_prev)
        if g_rev is None or g_op is None or abs(g_rev) < cfg.REV_GROWTH_MIN_ABS:
            ratios.append({"date": cur["date"], "value": None})
            continue
        ratio = g_op / g_rev
        ratios.append({
            "date": cur["date"],
            "value": None if abs(ratio) > cfg.RATIO_CLIP_ABS else ratio,
        })

    turnover_window = quarters[-cfg.POSITION_WINDOW_QUARTERS:]
    turnover = [{"date": q["date"], "value": q.get("capacity_turnover")}
                for q in turnover_window]

    return {"gross_margin": gross, "ratio": ratios, "turnover": turnover}
