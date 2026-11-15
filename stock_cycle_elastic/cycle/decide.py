"""阈值 → 标签（README.md「decide：八个阈值」）。

输出结构（**恰好 5 个 key**）：

    {
      "cycle":    {"label": 1 | 0 | "na",        "gm_std": float|None,
                   "gm_cv": float|None,          "channel": "std"|"cv"|None},
      "elastic":  {"label": "strong"|"weak"|"unstable"|"na", "ratio": float|None},
      "position": {"label": "low"|"neutral"|"high"|"na",     "pct": float|None},
      "flags":    {"financial": bool, "insufficient_history": bool},
      "meta":     {"quarters": int},
    }

三个 label 都是**显式枚举**，从不出现 True/False。
理由：`na` 没有布尔对应值，混用会让 `if not label` 把"数据缺失"当成"否"。

判定顺序只有三步：
    1. quarters < MIN_QUARTERS_REQUIRED → insufficient_history = True
    2. industry 命中金融关键词          → financial = True，判断③ 强制 na
    3. 依次套阈值（判断① 是双通道：绝对 σ 或 相对 CV）

没有第四步。上一版的"四池归类"、"降级通道"、"诚信折扣"全部不存在。
判断① 的 `channel` 记录**是哪条通道过的**，供报告解释用 ——
它不是第四个判断，只是同一判断的归因。
"""

import config as default_config

#: 判断① 的标签值
CYCLE_YES = 1
CYCLE_NO = 0
NA = "na"

#: 判断② 的标签值
ELASTIC_STRONG = "strong"
ELASTIC_WEAK = "weak"
ELASTIC_UNSTABLE = "unstable"

#: 判断③ 的标签值
POSITION_LOW = "low"
POSITION_NEUTRAL = "neutral"
POSITION_HIGH = "high"

#: 合法值域（测试用来做枚举完备性断言）
CYCLE_LABELS = (CYCLE_YES, CYCLE_NO, NA)
ELASTIC_LABELS = (ELASTIC_STRONG, ELASTIC_WEAK, ELASTIC_UNSTABLE, NA)
POSITION_LABELS = (POSITION_LOW, POSITION_NEUTRAL, POSITION_HIGH, NA)

#: 输出 dict 的精确 key 集合
DECIDE_KEYS = ("cycle", "elastic", "position", "flags", "meta")


def is_financial(industry, cfg=None):
    """行业字符串是否命中金融关键词。

    用"包含"而不是"等于"：接口给的行业名可能是"银行"、"股份制银行"、
    "保险及其他"、"证券Ⅱ"等，精确匹配必然漏。
    """
    cfg = cfg or default_config
    if not industry:
        return False
    text = str(industry)
    return any(keyword in text for keyword in cfg.FINANCIAL_INDUSTRY_KEYWORDS)


def decide(quarters, metrics, industry="", cfg=None):
    """把指标转成三个标签。

    `metrics` 是 metrics.compute_all() 的返回：
        {"gm_std": float|None,
         "gm_cv": {"cv","median","samples"},
         "elasticity": {"ratio","reliable","samples","zero_cross_ratio"},
         "position": {"pct","current","samples"},
         "capacity_direction": {"direction","asset_growth","revenue_growth"}}
    """
    cfg = cfg or default_config
    n_quarters = len(quarters or [])

    # 产能方向只对判断③ 有意义，且只作为限定语挂在 position 上。
    direction = (metrics.get("capacity_direction") or {}).get("direction")
    # 金融企业不适用产能周转率，方向标记也没有意义
    financial = is_financial(industry, cfg)
    if financial:
        direction = None

    flags = {
        "insufficient_history": n_quarters < cfg.MIN_QUARTERS_REQUIRED,
        "financial": financial,
    }

    cycle = _decide_cycle(metrics.get("gm_std"), metrics.get("gm_cv"), cfg)
    elastic = _decide_elastic(metrics.get("elasticity") or {}, cfg)
    position = _decide_position(
        metrics.get("position") or {},
        financial=financial,
        cfg=cfg,
    )
    position["capacity_direction"] = direction
    dir_metrics = metrics.get("capacity_direction") or {}
    position["asset_growth"] = dir_metrics.get("asset_growth")
    position["revenue_growth"] = dir_metrics.get("revenue_growth")

    return {
        "cycle": cycle,
        "elastic": elastic,
        "position": position,
        "flags": flags,
        "meta": {"quarters": n_quarters},
    }


# ---------------------------------------------------------------------------
# 三个判断
# ---------------------------------------------------------------------------


def _decide_cycle(gm_std, gm_cv, cfg):
    """判断①：双通道，任一条通过就判「是周期股」。

        通道 A（绝对）：σ >= GM_STD_CYCLE                     —— 5.0 pp
        通道 B（相对）：CV >= GM_CV_MIN（且 metrics 层已守住两个守卫）

    为什么是"或"而不是加权合成：
        两个通道回答的是同一个问题——"毛利率在剧烈摆动吗"。绝对口径适合
        毛利率水平正常的公司，相对口径适合低毛利公司；用 OR 就是让两家
        各自用自己那把握得住的尺子，最后判同一件事。加权反而要引入权重，
        而权重没有客观依据（README「为什么不排序、不打总分」同一理由）。

    边界都是**闭区间**（`>=`）：恰好等于 5.0pp 或 20% 都判为周期股。

    `gm_cv` 结构见 metrics.gross_margin_cv()；cv 为 None 表示这条通道
    给不出结论（样本不足 / 中位数太小 / 绝对波动太小），此时只看通道 A。

    返回的 `channel` 说明是哪条通道过的：两条都过记 "std"（绝对口径更权威）。
    """
    cv_value = (gm_cv or {}).get("cv")
    cv_median = (gm_cv or {}).get("median")

    if gm_std is None:
        # σ 算不出来（样本不足）→ 两条通道都没有可靠输入，给 na
        return {"label": NA, "gm_std": None, "gm_cv": cv_value,
                "gm_median": cv_median, "channel": None}

    by_std = gm_std >= cfg.GM_STD_CYCLE
    by_cv = cv_value is not None and cv_value >= cfg.GM_CV_MIN

    if by_std:
        channel = "std"
    elif by_cv:
        channel = "cv"
    else:
        channel = None

    label = CYCLE_YES if (by_std or by_cv) else CYCLE_NO
    return {"label": label, "gm_std": gm_std, "gm_cv": cv_value,
            "gm_median": cv_median, "channel": channel}


def _decide_elastic(elastic_metrics, cfg):
    """判断②：比值 >= ELASTIC_RATIO_MIN → strong，否则 weak。

    **unstable 优先于强弱**：比值不可信时，既不能说强也不能说弱。
    这是本模块最容易写错的地方 —— 先判 reliability，再比阈值。

    不可信有两条独立原因，任一命中即标 unstable：
      1. 营收长期不动（metrics 里的 `reliable is False`）
      2. 盈亏平衡点附近的季度太多（这里判 `zero_cross_ratio`）
    第 2 条不能放进 metrics 的 `reliable` —— 那个字段的名字是"营收是否够动"，
    语义不同；混用会让"为什么不稳定"没法解释、也没法分别显示文案。
    """
    ratio = elastic_metrics.get("ratio")
    reliable = elastic_metrics.get("reliable")
    zero_cross_ratio = elastic_metrics.get("zero_cross_ratio")

    if ratio is None:
        return {"label": NA, "ratio": None, "reason": "insufficient"}

    # DOL 在盈亏平衡点趋于无穷：跨零季度占比过高时，中位数被噪声主导。
    # 放在 reliable 之前判，因为它的证据更直接（就是算不出来），
    # 而且它跟营收是否够动无关 —— 一家营收波动剧烈的公司照样可能长期亏损。
    if (zero_cross_ratio is not None
            and zero_cross_ratio > cfg.ELASTIC_ZERO_CROSS_MAX):
        return {"label": ELASTIC_UNSTABLE, "ratio": ratio, "reason": "near_breakeven"}

    if reliable is False:
        # 营收长期不动 → 比值算得出来但没有意义
        return {"label": ELASTIC_UNSTABLE, "ratio": ratio, "reason": "flat_revenue"}

    if reliable is None:
        # 拿不到营收增速（数据不足），不敢给结论
        return {"label": NA, "ratio": ratio, "reason": "unknown"}

    label = ELASTIC_STRONG if ratio >= cfg.ELASTIC_RATIO_MIN else ELASTIC_WEAK
    return {"label": label, "ratio": ratio, "reason": "ok"}


def _decide_position(position_metrics, financial, cfg):
    """判断③：分位 < 40 → low；> 60 → high；中间 neutral。

    **两个边界都是开区间**：
        pct = 40.0 → neutral（不算低位）
        pct = 60.0 → neutral（不算高位）
    中间地带不给标签是刻意的 —— 40~60 这个区间里本来就没有信息。

    金融股强制 na：产能周转率对银行/保险/证券无意义
    （它们的"营收"是利息净收入，固定资产只有办公楼）。
    """
    pct = position_metrics.get("pct")
    if financial:
        return {"label": NA, "pct": pct}
    if pct is None:
        return {"label": NA, "pct": None}
    if pct < cfg.POSITION_LOW_PCT:
        return {"label": POSITION_LOW, "pct": pct}
    if pct > cfg.POSITION_HIGH_PCT:
        return {"label": POSITION_HIGH, "pct": pct}
    return {"label": POSITION_NEUTRAL, "pct": pct}


#: 产能方向 → 徽章文案。只在「低位」/「高位」时出，且用中性色 ——
#: 它是给位置标签加的一句**限定语**，不是第四个判断。
CAPACITY_DIRECTION_TEXT = {
    "expand": "扩产中",
    "shrink": "产能在退",
}


# ---------------------------------------------------------------------------
# 展示用映射（中文文案集中在这里，避免模板里散落硬编码）
# ---------------------------------------------------------------------------

CYCLE_TEXT = {CYCLE_YES: "是", CYCLE_NO: "否", NA: "—"}
ELASTIC_TEXT = {
    ELASTIC_STRONG: "强",
    ELASTIC_WEAK: "弱",
    ELASTIC_UNSTABLE: "不稳定",
    NA: "—",
}
POSITION_TEXT = {
    POSITION_LOW: "低位",
    POSITION_NEUTRAL: "中性",
    POSITION_HIGH: "高位",
    NA: "—",
}

#: 徽章文案（正面/负面徽章 + 三种辅助标记，见 §8.3）
BADGE_TEXT = {
    ("cycle", CYCLE_YES): "周期股",
    ("cycle", CYCLE_NO): "非周期",
    ("elastic", ELASTIC_STRONG): "弹性强",
    ("elastic", ELASTIC_WEAK): "弹性弱",
    ("elastic", ELASTIC_UNSTABLE): "比值不稳定",
    ("position", POSITION_LOW): "低位",
    ("position", POSITION_HIGH): "高位",
}

#: 通道 B 通过的标记文案。**不用「周期股（相对口径）」当主徽章** ——
#: 两者的结论强度一样，只是归因不同，所以主徽章仍是「周期股」，
#: 这里只补一个小标记告诉用户"是相对波动这条通道过线的"。
BADGE_CV_CHANNEL = "相对波动"

#: 「①否 且 ②强」的矛盾标记。
#:
#: 为什么要有它：毛利率不波动、利润却对营收高度敏感，最常见的原因是
#: **成本与售价同步联动**（波动全落在量上、不在价差上）—— 这正是判断①
#: 已知会漏掉的那类周期股。实测 113 只里这种组合有 19 只，其中 15 只
#: 属于典型周期行业（79%），而全样本该比例只有 63%。
#:
#: 它是一句**提示**，不是结论：不改任何阈值，也不改变三个判断的取值。
BADGE_CONTRADICTION = "①否②强·存疑"


def badges(decision):
    """从决策结果生成徽章列表 [(css_class, text), ...]。

    规则（§8.3）：
      * `unstable` **取代** strong/weak 徽章，三者互斥
      * 金融股 → 加"产能不适用"，不再出低位/高位
      * 历史不足 → 加"历史不足"，但不取代判断① 的徽章
      * 通道 B 过的周期股 → 追加"相对波动"小标记
      * ①否 且 ②强 → 追加"①否②强·存疑"（提示，不是第四个判断）
      * 最终展示顺序固定：周期性 → 弹性 → 位置 → 辅助标记
    """
    out = []
    flags = decision.get("flags") or {}

    cycle = decision.get("cycle") or {}
    cycle_label = cycle.get("label")
    if cycle_label in (CYCLE_YES, CYCLE_NO):
        out.append(("badge-cycle-yes" if cycle_label == CYCLE_YES else "badge-cycle-no",
                    BADGE_TEXT[("cycle", cycle_label)]))
        if cycle_label == CYCLE_YES and cycle.get("channel") == "cv":
            out.append(("badge-cv-channel", BADGE_CV_CHANNEL))

    elastic_label = (decision.get("elastic") or {}).get("label")
    if elastic_label in (ELASTIC_STRONG, ELASTIC_WEAK, ELASTIC_UNSTABLE):
        cls = {
            ELASTIC_STRONG: "badge-elastic-strong",
            ELASTIC_WEAK: "badge-elastic-weak",
            ELASTIC_UNSTABLE: "badge-unstable",
        }[elastic_label]
        out.append((cls, BADGE_TEXT[("elastic", elastic_label)]))

    if flags.get("financial"):
        out.append(("badge-muted", "产能不适用"))
    else:
        position = decision.get("position") or {}
        pos_label = position.get("label")
        if pos_label in (POSITION_LOW, POSITION_HIGH):
            cls = "badge-low" if pos_label == POSITION_LOW else "badge-high"
            out.append((cls, BADGE_TEXT[("position", pos_label)]))
            # 方向标记只在出了位置标签时才加：中性区本来就没方向，
            # 再加一句「扩产中」会让人以为那是第四个结论。
            direction = position.get("capacity_direction")
            text = CAPACITY_DIRECTION_TEXT.get(direction)
            if text:
                out.append(("badge-capacity", text))

    # 矛盾标记：①判否 但 ②判强。放最后，因为它是"提示"不是"判断"。
    if cycle_label == CYCLE_NO and elastic_label == ELASTIC_STRONG:
        out.append(("badge-contradiction", BADGE_CONTRADICTION))

    if flags.get("insufficient_history"):
        out.append(("badge-muted", "历史不足"))

    return out


def has_any_na(decision):
    """是否存在任何 `na`（渲染层据此加"数据不完整"提示）。"""
    return (
        (decision.get("cycle") or {}).get("label") == NA
        or (decision.get("elastic") or {}).get("label") == NA
        or (decision.get("position") or {}).get("label") == NA
    )
