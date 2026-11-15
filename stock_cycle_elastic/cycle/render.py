"""数据 → HTML 报告（README.md「HTML 报告设计」）。

四块结构：
    1. Hero        名称/代码/行业 + 一行核心读数 + 徽章
    2. 三个判断     一张表三行 + 周期位置三档含义
    3. 指标历史     三张小图（内联 SVG，无 JS、无 CDN，横轴带季度刻度）
    + 脚注          六条口径声明

「原始数据」那一栏原本用来展示合同负债和单季营收，已按用户要求删掉：
报告只回答那三个判断，展示型数据留给原始报表看。

机器可读锚点（§8.5，索引页依赖）：body 上 10 个 data-* 属性。
少一个索引页就会静默填空白 —— test_render 会逐字断言。
"""

import datetime
import math
import os
import re

import config
from cycle import decide as decide_mod
from cycle import metrics as metrics_mod

# ---------------------------------------------------------------------------
# HTML 转义
# ---------------------------------------------------------------------------

_ESCAPES = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("'", "&#39;"),
)


def esc(value):
    """HTML 转义。None 转成空串（不是字符串 "None"）。

    顺序很重要：& 必须最先替换，否则会把后面替换出的 &amp; 再转一次。
    """
    if value is None:
        return ""
    text = str(value)
    for raw, safe in _ESCAPES:
        text = text.replace(raw, safe)
    return text


# ---------------------------------------------------------------------------
# 数值格式化
# ---------------------------------------------------------------------------

DASH = "—"


def fmt_num(value, digits=2, suffix="", lower=None, upper=None):
    """数值格式化。None / 非有限数 → 破折号（不出现 "None"/"nan"）。

    `nan` 和 `inf` 也要挡掉：上游算出一个 0/0 就会漏进来，
    直接 f-string 会把 "nan" 原样打进页面，比显示"缺失"更误导。

    `lower` / `upper` 传判定阈值时，保证显示值不跨线（见 `_round_same_side`）。
    """
    if value is None:
        return DASH
    try:
        if not math.isfinite(value):
            return DASH
        return f"{_round_same_side(value, digits, lower, upper):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return DASH


def fmt_yi(value, digits=1):
    """元 → 亿元。"""
    if value is None:
        return DASH
    try:
        if not math.isfinite(value):
            return DASH
    except TypeError:
        return DASH
    return f"{value / 1e8:.{digits}f} 亿"


def _floor_digits(value, digits):
    """向下截断到 digits 位小数。"""
    factor = 10 ** digits
    return math.floor(value * factor) / factor


def _ceil_digits(value, digits):
    """向上截断到 digits 位小数。"""
    factor = 10 ** digits
    return math.ceil(value * factor) / factor


def _round_same_side(value, digits, lower=None, upper=None):
    """按 digits 位四舍五入，但**不许跨过判定边界**。

    坑（真实发生过）：川发龙蟒 σ = 4.9505，判「非周期」（< 5pp 正确），
    但 1 位小数显示成「5.0 pp」，页面上读起来是「5.0 pp < 5 pp」——自相矛盾。

    修法不是把阈值改宽，也不是四舍五入改成"四舍六入"，
    而是：舍入后如果越过了 lower/upper（原值并没有越过），
    就改为朝原值所在的一侧截断。这样**显示值是判定值的一个单调近似**，
    看得见的数字与看不见的标签永远在同一侧，且不会夸大精度。
    """
    rounded = round(value, digits)
    # 下边界：原值在线上方，舍入后掉到线下方 → 向上截断
    if lower is not None:
        if value >= lower and rounded < lower:
            return _ceil_digits(value, digits)
        if value < lower and rounded >= lower:
            return _floor_digits(value, digits)
    # 上边界：原值在线下方，舍入后冒到线上方 → 向下截断
    if upper is not None:
        if value <= upper and rounded > upper:
            return _floor_digits(value, digits)
        if value > upper and rounded <= upper:
            return _ceil_digits(value, digits)
    return rounded


def fmt_pp(value, digits=2, lower=None, upper=None):
    """百分点。默认 2 位小数：σ 常年贴着 5pp 这条线，1 位小数会把 4.95 显示成 5.0。

    `lower` / `upper` 传判定阈值时，保证显示值不跨线（见 `_round_same_side`）。
    """
    if value is None:
        return DASH
    try:
        if not math.isfinite(value):
            return DASH
    except TypeError:
        return DASH
    return f"{_round_same_side(value, digits, lower, upper):.{digits}f} pp"


def fmt_pct(value, digits=1, lower=None, upper=None):
    """百分比。`lower` / `upper` 同上，用于保证不跨分位边界。"""
    if value is None:
        return DASH
    try:
        if not math.isfinite(value):
            return DASH
    except TypeError:
        return DASH
    return f"{_round_same_side(value, digits, lower, upper):.{digits}f}%"


def attr_value(value):
    """data-* 属性的值：None / 非有限数 → 空串（绝不写 "None" 或 "nan"）。"""
    if value is None:
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


# ---------------------------------------------------------------------------
# 机器可读锚点（§8.5）
# ---------------------------------------------------------------------------

DATA_ATTRS = (
    "data-code",
    "data-name",
    "data-quarters",
    "data-gm-std",
    "data-cycle",
    "data-elastic",
    "data-elastic-ratio",
    "data-position",
    "data-position-pct",
    "data-financial",
    "data-gm-cv",
    "data-cycle-channel",
    "data-capacity-direction",
    "data-elastic-reason",
    "data-latest",
)


def body_attrs(stock, decision):
    """生成 15 个 data-* 属性（顺序固定，便于正则解析与测试断言）。

    第 10 个 `data-financial` 是后补的（"1"/"0"）：索引页靠它区分
    「金融企业本来就不适用产能周转率」和「这只股票数据缺失」——
    两者在报告里都表现为 data-position="na"，光看这一个值分不出来。

    第 11/12 个 `data-gm-cv` / `data-cycle-channel` 同理：判断① 改成双通道后，
    光看 data-gm-std 已经推不出标签（σ<5 也可能是「周期股」），
    索引页续跑时必须能读到"是哪条通道过的"，否则会把相对波动通道
    判出来的周期股当成脏数据。

    第 13/14 个 `data-capacity-direction` / `data-elastic-reason` 同理：
    前者让索引页能显示「扩产中/产能在退」，后者让"为什么不稳定"在
    续跑后仍然分得清（是营收不动还是贴近盈亏平衡）。
    """
    cycle = decision.get("cycle") or {}
    elastic = decision.get("elastic") or {}
    position = decision.get("position") or {}
    meta = decision.get("meta") or {}
    flags = decision.get("flags") or {}

    pairs = (
        ("data-code", stock.get("code")),
        ("data-name", stock.get("name")),
        ("data-quarters", meta.get("quarters")),
        ("data-gm-std", cycle.get("gm_std")),
        ("data-cycle", cycle.get("label")),
        ("data-elastic", elastic.get("label")),
        ("data-elastic-ratio", elastic.get("ratio")),
        ("data-position", position.get("label")),
        ("data-position-pct", position.get("pct")),
        ("data-financial", "1" if flags.get("financial") else "0"),
        ("data-gm-cv", cycle.get("gm_cv")),
        ("data-cycle-channel", cycle.get("channel")),
        ("data-capacity-direction", position.get("capacity_direction")),
        ("data-elastic-reason", elastic.get("reason")),
        # 最近报告期：结果库要靠它当基准日（各股未必同一期），续跑读回也得有
        ("data-latest", meta.get("latest")),
    )
    parts = []
    for key, value in pairs:
        parts.append(f'{key}="{esc(attr_value(value))}"')
    return " " + " ".join(parts)


# ---------------------------------------------------------------------------
# 第一块：Hero
# ---------------------------------------------------------------------------


def build_headline(decision, cfg=None):
    """Hero 里那一行核心读数。

    有任一判断算不出来时，末尾补一句「数据不完整，结论仅供参考」——
    否则用户看到两个有结论、一个破折号，容易以为只是那一项不重要。
    次新股必然缺 10 年数据，这句提示是它的常客。

    σ 与分位都带上了判定边界（`cfg.GM_STD_CYCLE` / `POSITION_*_PCT`），
    保证显示的读数不会跟同一行的徽章打架。
    """
    cfg = cfg or config
    cycle = decision.get("cycle") or {}
    elastic = decision.get("elastic") or {}
    position = decision.get("position") or {}

    sigma_text = fmt_pp(cycle.get("gm_std"), lower=cfg.GM_STD_CYCLE)
    ratio_text = fmt_num(elastic.get("ratio"), 2, lower=cfg.ELASTIC_RATIO_MIN)
    pct_text = fmt_pct(position.get("pct"), lower=cfg.POSITION_LOW_PCT,
                       upper=cfg.POSITION_HIGH_PCT)

    bits = [
        f"毛利率σ <b>{sigma_text}</b>",
        f"弹性 <b>{ratio_text}</b>",
        f"产能分位 <b>{pct_text}</b>",
    ]
    line = " · ".join(bits)
    if decide_mod.has_any_na(decision):
        line += ' <span class="na-hint">（数据不完整，结论仅供参考）</span>'
    return line


def render_badges(decision):
    items = decide_mod.badges(decision)
    if not items:
        return '<span class="badge badge-muted">数据不足</span>'
    return "".join(f'<span class="badge {cls}">{esc(text)}</span>'
                   for cls, text in items)


# ---------------------------------------------------------------------------
# 第二块：三个判断表
# ---------------------------------------------------------------------------


def render_judge_rows(stock, decision, metrics, cfg=None):
    """三行判断 + 单元格里的一行说明（为什么是 na）。"""
    cfg = cfg or config
    quarters = stock.get("quarters") or []
    cycle = decision.get("cycle") or {}
    elastic = decision.get("elastic") or {}
    position = decision.get("position") or {}
    flags = decision.get("flags") or {}

    rows = []

    # ---- 判断① ----
    label = cycle.get("label")
    css, text = _cycle_cell(label, flags)
    channel = cycle.get("channel")
    gm_cv = cycle.get("gm_cv")
    gm_median = cycle.get("gm_median")
    note = ""
    if label == decide_mod.NA:
        note = f'<span class="j-note">需 {cfg.GM_MIN_SAMPLES} 季毛利率，现有 {_gm_valid(quarters, cfg)} 季</span>'
    elif channel == "cv":
        # 走的是相对波动通道：必须把 CV 展示出来，否则用户看到 σ 不到 5 却判「是」会以为算错
        note = (f'<span class="j-note">σ 未到 {cfg.GM_STD_CYCLE:g} pp，'
                f'但相对波动 {gm_cv:.0f}% ≥ {cfg.GM_CV_MIN:g}%（毛利率中位 {gm_median:.1f}%）</span>')
    elif label == decide_mod.CYCLE_YES:
        note = f'<span class="j-note">≥ {cfg.GM_STD_CYCLE:g} pp</span>'
    else:
        note = f'<span class="j-note">&lt; {cfg.GM_STD_CYCLE:g} pp</span>'
        if gm_cv is not None:
            note += f'<span class="j-note">，相对波动 {gm_cv:.0f}% &lt; {cfg.GM_CV_MIN:g}%</span>'
    rows.append(_row("是不是周期股", css, text,
                     f"最近 {cfg.WAVE_WINDOW_QUARTERS} 季毛利率标准差",
                     fmt_pp(cycle.get("gm_std"), lower=cfg.GM_STD_CYCLE), note))

    # ---- 判断② ----
    elabel = elastic.get("label")
    css, text = _elastic_cell(elabel)
    if elabel == decide_mod.NA:
        samples = (metrics.get("elasticity") or {}).get("samples", 0)
        if elastic.get("ratio") is None:
            note = f'<span class="j-note">有效样本 {samples} 季，需 ≥ {cfg.RATIO_MIN_SAMPLES}</span>'
        else:
            note = '<span class="j-note">营收增速数据不足</span>'
    elif elabel == decide_mod.ELASTIC_UNSTABLE:
        reason = elastic.get("reason")
        if reason == "near_breakeven":
            zc = (metrics.get("elasticity") or {}).get("zero_cross", 0)
            total = (metrics.get("elasticity") or {}).get("samples", 0)
            note = (f'<span class="j-note">样本里 {zc}/{total} 季营业利润跨零，'
                    f'贴近盈亏平衡点，DOL 趋于无穷，比值不可信</span>')
        else:
            note = (f'<span class="j-note">营收增速中位数 &lt; '
                    f'{cfg.REV_MEDIAN_STABLE_MIN:g}%，比值不可信</span>')
    elif elabel == decide_mod.ELASTIC_STRONG:
        note = f'<span class="j-note">≥ {cfg.ELASTIC_RATIO_MIN:g}</span>'
    else:
        note = f'<span class="j-note">&lt; {cfg.ELASTIC_RATIO_MIN:g}</span>'
    rows.append(_row("弹性", css, text,
                     f"最近 {cfg.ELASTIC_WINDOW_QUARTERS} 季「营业利润增速÷营收增速」中位",
                     fmt_num(elastic.get("ratio"), 2,
                             lower=cfg.ELASTIC_RATIO_MIN), note))

    # ---- 判断③ ----
    plabel = position.get("label")
    css, text = _position_cell(plabel, flags)
    if flags.get("financial"):
        note = '<span class="j-note">金融企业，产能周转率无意义</span>'
    elif plabel == decide_mod.NA:
        samples = (metrics.get("position") or {}).get("samples", 0)
        if samples < cfg.POSITION_MIN_SAMPLES:
            note = f'<span class="j-note">有效样本 {samples} 季，需 ≥ {cfg.POSITION_MIN_SAMPLES}</span>'
        else:
            note = '<span class="j-note">最新一季缺资产负债表</span>'
    elif plabel == decide_mod.POSITION_LOW:
        note = f'<span class="j-note">&lt; {cfg.POSITION_LOW_PCT:g}%</span>'
    elif plabel == decide_mod.POSITION_HIGH:
        note = f'<span class="j-note">&gt; {cfg.POSITION_HIGH_PCT:g}%</span>'
    else:
        note = (f'<span class="j-note">{cfg.POSITION_LOW_PCT:g}%~{cfg.POSITION_HIGH_PCT:g}%'
                '，中间地带不给标签</span>')
    # 名义窗口是 10 年，但上市不足 10 年、或 TTM 口径下最早 3 季凑不满 4 季，
    # 都会让实际有效季数变少。实测 19% 的股票不足 40 季（最少 15 季），
    # 所以这里显示**实际**季数，而不是无脑写「10 年」。
    actual = metrics_mod.position_window_actual(quarters, cfg)
    if actual and actual < cfg.POSITION_WINDOW_QUARTERS:
        span = f"{actual} 季（不足 {cfg.POSITION_WINDOW_QUARTERS // 4} 年）"
    else:
        span = f"{cfg.POSITION_WINDOW_QUARTERS // 4} 年"
    rows.append(_row("周期位置", css, text,
                     f"产能周转率的 {span}分位",
                     fmt_pct(position.get("pct"),
                             lower=cfg.POSITION_LOW_PCT,
                             upper=cfg.POSITION_HIGH_PCT), note))

    return "\n        ".join(rows)


#: 判断① 的**准确含义**（§2.1 边界）—— 两档：是 / 否。
#:
#: 这一段比②③ 更不能省。表格里写的是「最近 20 季毛利率标准差」，
#: 但"标准差是什么、怎么算出来的、5pp 这个线凭什么"三件事一个都没交代。
#: 用户看到"是"只会得到两个错误印象：
#:   ① 「是」= 确定是周期股 —— 错。只是通过了三条判据中的一条（见下）。
#:   ② 「否」= 这公司不是周期股 —— 错。它可能只是**这一项**没波动；
#:      一家营收随油价剧烈波动、但成本同步联动的公司，毛利率平稳但仍是周期股。
#: 这两点必须写在报告上，因为索引页的徽章「周期股」会被直接当成结论扫过去。
CYCLE_MEANING = {
    decide_mod.CYCLE_YES: (
        "近 20 季毛利率波动大，像「价差型生意」",
        "毛利率在 5 年里上下摆动超过 5 个百分点，或相对自身水平摆动超过 20%"
        "（低毛利生意用相对口径衡量），说明卖价与原料成本之间那道缝"
        "**经常张开又收窄** —— 这正是周期股赚钱与亏钱的同一来源。"
        "但**「是」不等于「确定是周期股」**：文献给了三条判据"
        "（毛利率波动、营收行业同步性、利润增速波动），本工具只做了第一条。"
        "还有一层：5pp 是按**年度**数据标定的，本工具用**单季**数据，"
        "季节性天然抬高季度标准差，所以这条线对季度口径**偏松**——"
        "有意为之，初筛宁宽勿严，宁可多放进来看，也不要漏掉。",
    ),
    decide_mod.CYCLE_NO: (
        "近 20 季毛利率波动不大，看不到「价差型生意」的指纹",
        "**不代表**这家公司不是周期股，只代表**毛利率这一项**没波动。"
        "绝对与相对两条通道都没命中，所以这个「否」是把两把尺子都用过了。"
        "三种常见情形会让周期股被判成「否」：一是成本与售价同步联动"
        "（营收大跌、毛利率却稳），波动全落在量上而不在价差上；"
        "二是公司有长协、套保、牌照或品牌把价差焊住了；"
        "三是毛利率绝对水平太低（中位不到 5%），相对口径也失去分辨率。"
        "反过来说，「否」也不代表公司差——恰恰相反，毛利率平滑本身就是护城河的证据。"
        "要确认周期性，得自己再看营收增速的波动与行业景气的关系。",
    ),
    decide_mod.NA: (
        "算不出标准差",
        "近 20 季里有效的单季毛利率不足 8 季（新股、长期停牌、"
        "或接口缺成本科目），样本太少算出来的标准差没有意义，"
        "所以**不给结论**，而不是勉强给一个「否」。",
    ),
}

#: 「标准差是怎么算的」—— 单独一小块，紧跟两档卡片。
#:
#: 为什么要写公式而不是只写结果：这一列的名字（"标准差"）本身就是门槛。
#: 不写清楚"样本标准差、单季、剔缺失不补 0"，用户无法自己复核，
#: 也就无法判断这个「是/否」该信几分。三个口径细节都影响结果大小，必须写明。
CYCLE_FORMULA_NOTE = (
    "毛利率_q = (单季营业收入 − 单季营业成本) ÷ 单季营业收入；"
    "σ = 近 {window} 季毛利率的**样本标准差**（分母 n−1，单位：百分点）；"
    "CV = σ ÷ |毛利率中位数| × 100（相对波动，单位：%）。"
    "**用单季值**：接口的累计口径（三季度=前三季合计）已把前两季平均掉，"
    "用它会系统性抹平波动、直接漏判。**缺失季整季剔除、不补 0**："
    "补 0 会凭空造出「毛利率归零」的极端值，把 σ 抬高。"
    "判断线：**σ ≥ {thr:g}pp**，或 **CV ≥ {cv_thr:g}%**"
    "（后者要求 |毛利率中位| ≥ {med_min:g}% 且 σ ≥ {std_min:g}pp，"
    "防止分母趋零与绝对波动过小）。两条都是闭区间；"
    "有效季 < {min_samples} 季则不给结论。"
)


def render_cycle_meaning(decision, cfg=None):
    """判断① 下面的一小段：两档含义 + 标准差算法，当前档高亮。

    与 `render_elastic_meaning` / `render_position_meaning` 同构，
    只是档位是两档而不是三档 —— 判断① 本来就只有「是 / 否」。
    """
    cfg = cfg or config
    cycle = decision.get("cycle") or {}
    label = cycle.get("label")
    gm_std = cycle.get("gm_std")

    cards = []
    for key, css in ((decide_mod.CYCLE_YES, "pm-strong"),
                     (decide_mod.CYCLE_NO, "pm-weak")):
        head, body = CYCLE_MEANING[key]
        active = " pm-active" if key == label else ""
        mark = '<span class="pm-now">当前</span>' if key == label else ""
        cards.append(
            f'<li class="{css}{active}">'
            f'<b>{esc(_cycle_cell(key, {})[1])}</b>'
            f'{mark}'
            f'<span class="pm-head">{esc(head)}</span>'
            f'<span class="pm-body">{_emph(body)}</span>'
            '</li>'
        )

    # 样本不够时「是 / 否」两档都不成立，得说清楚为什么，否则那个「—」像漏了数据
    if label == decide_mod.NA:
        head, body = CYCLE_MEANING[decide_mod.NA]
        cards.append(
            '<li class="pm-na pm-active">'
            f'<b>{DASH}</b>'
            '<span class="pm-now">当前</span>'
            f'<span class="pm-head">{esc(head)}</span>'
            f'<span class="pm-body">{_emph(body)}</span>'
            '</li>'
        )

    lead = (
        f'<p class="pm-lead"><b>「是不是周期股」看的是毛利率波动，'
        f'不是股价、不是营收、也不是行业标签。</b>'
        f'毛利率 = 卖价与原料成本之间那道缝，含<code>（收入−成本）÷收入</code>；'
        f'周期股的钱就来自这道缝的张开与收窄，非周期股的缝被护城河焊住了。'
        f'所以本工具用<b>近 {cfg.WAVE_WINDOW_QUARTERS} 季'
        f'（{cfg.WAVE_WINDOW_QUARTERS // 4} 年）单季毛利率的波动</b>当指纹，'
        f'两条通道任一命中即判「是」：'
        f'<b>绝对</b> σ ≥ {cfg.GM_STD_CYCLE:g}pp；'
        f'<b>相对</b> CV = σ÷|毛利率中位| ≥ {cfg.GM_CV_MIN:g}%'
        f'（且中位 ≥ {cfg.GM_MEDIAN_MIN:g}%、σ ≥ {cfg.GM_STD_MIN_FOR_CV:g}pp）。'
        f'相对通道是为低毛利生意准备的 —— 毛利率只有十几点的公司，'
        f'σ 在数学上就到不了 {cfg.GM_STD_CYCLE:g}pp，但它摆动的<b>比例</b>可能远超 20%。'
        f'两档具体含义如下：</p>'
    )

    note = (
        '<p class="pm-formula">'
        + _emph(CYCLE_FORMULA_NOTE.format(
            window=cfg.WAVE_WINDOW_QUARTERS,
            thr=cfg.GM_STD_CYCLE,
            cv_thr=cfg.GM_CV_MIN,
            med_min=cfg.GM_MEDIAN_MIN,
            std_min=cfg.GM_STD_MIN_FOR_CV,
            min_samples=cfg.GM_MIN_SAMPLES,
        ))
        + '</p>'
    )

    return lead + '<ul class="pm-list">' + "".join(cards) + "</ul>" + note


#: 判断② 三档的**准确含义**（§2.2 边界）。
#:
#: 和判断③ 一样，必须写清楚 —— 「弹性弱」最容易被误读成三件不同的事：
#:   ① 公司基本面差 / 生意不行  —— 错。弹性衡量的是**放大倍数**不是好坏。
#:      茅台弹性很弱，因为毛利率稳定、成本刚性小，这恰恰是护城河的证据。
#:   ② 营收涨得少               —— 错。比值是**放大的倍数**，跟营收绝对增速无关。
#:      营收涨 1%、利润涨 2.5% 是强（2.5 倍）；营收涨 50%、利润涨 55% 是弱（1.1 倍）。
#:   ③ 收入动 1% 时利润动几倍    —— 对，这是它唯一想表达的意思。
#: 不写清楚的后果：用户看到"弱"以为这家公司不行，实际它可能只是价格稳定；
#: 或者看到"强"就以为业绩好，实际它可能处在亏损边缘（EBIT→0 时 DOL 天然爆表）。
#:
#: **「弱」里还藏着一半是负数**：实测 113 只自选股里，65 只 weak 有 28 只比值为负
#: （占比 43%），中位数落在 -0.2 ~ -8.4。负比值意味着**那几季营收和利润方向相反**，
#: 不是"放大得少"，而是"根本不放大甚至反向"。这两个意思差得远，
#: 所以 weak 的说明里必须把负值这一半也讲明白，不能只说"成本随收入同比例变动"。
ELASTIC_MEANING = {
    decide_mod.ELASTIC_STRONG: (
        "收入每变动 1%，营业利润约变动 2 倍以上",
        "即典型的经营杠杆放大：成本刚性大（固定成本占比高），"
        "所以收入的小幅波动会被放大成利润的大幅波动。**涨跌对称** ——"
        "周期上行放得大，下行也摔得重，不是只赚不赔。"
        "注意 DOL 在盈亏平衡点附近天然趋于无穷，**不等于**这家公司质地好。",
    ),
    decide_mod.ELASTIC_WEAK: (
        "收入每变动 1%，营业利润的变动不到 2 倍（也可能方向相反）",
        "分母为正时是**放大效应有限**：成本随收入同比例变动（固定成本占比低），"
        "周期来了利润也没多少反应，常说的「赚了吆喝不赚钱」。"
        "但中位数为**负数**时是另一回事 —— 它表示这 8 季里有一半以上的季度"
        "营收和利润**方向相反**（增收不增利，或减产反而增利），"
        "根源通常是原料/产品价差在收紧，而不是杠杆小。"
        "**两种情况都不代表**公司差 —— 价格稳定、毛利率平滑本身就是护城河的证据，"
        "只是它不适合作为「赚周期弹性」的标的。但具体是哪一种，要自己翻季度报表确认。",
    ),
    decide_mod.ELASTIC_UNSTABLE: (
        "比值算得出来，但不可信",
        "近 8 季营收增速的中位数绝对值不到 3%，分母趋近于零，"
        "比值会被微小波动放大成几十倍甚至上百倍，那些数字不是「弹性」而是噪声。"
        "所以**既不说强也不说弱**，数值照常显示供参考。",
    ),
    decide_mod.NA: (
        "算不出弹性",
        "有效样本不足，或缺少营业利润 / 营收的逐季数据。",
    ),
}

#: 本股比值为负时，「弱」那一档换成的说明。
#:
#: 为什么值得单列一段：实测 113 只自选股里 28 只（占 weak 的 43%）比值为负。
#: 负中位数不是"放大得少"，而是"这 8 季里过半的季度营收与利润方向相反"——
#: 两者对投资的含义完全不同，混为一谈等于没解释。
ELASTIC_MEANING_NEGATIVE = (
    "这 8 季里过半季度营收与利润方向相反",
    "中位数为负，说明「营收增、利润跌」或「营收跌、利润升」占了多数，"
    "**不是**放大得少，而是根本没在放大 —— 通常意味着"
    "原料与产品之间的价差在收紧（增收不增利），或者成本端出现了一次性冲击。"
    "要紧的是它**方向不稳定**：没有可靠的弹性可用，"
    "拿它做「周期来了放大几倍」的推演会得到反的结论。",
)


def render_elastic_meaning(decision, cfg=None):
    """判断② 下面的一小段：把三档含义摊开写，当前档高亮。

    与 `render_position_meaning` 同构 —— 两个判断用同一种说明样式，
    用户扫 113 只股票时不用重新理解版式。

    **负比值要单独说明**：`weak` 覆盖了 [0, 2) 和 (-∞, 0) 两大段，
    前者是"放大得少"、后者是"方向相反"，混在一句话里讲会误导。
    所以当本股比值 < 0 时，弱档那句会换成负值专属解释。
    """
    cfg = cfg or config
    elastic = decision.get("elastic") or {}
    label = elastic.get("label")
    ratio = elastic.get("ratio")

    cards = []
    for key, css in ((decide_mod.ELASTIC_STRONG, "pm-strong"),
                     (decide_mod.ELASTIC_WEAK, "pm-weak"),
                     (decide_mod.ELASTIC_UNSTABLE, "pm-unstable")):
        head, body = ELASTIC_MEANING[key]
        # 本股是 weak 且比值为负 → 把"弱"那句换成负值专属说法
        if key == decide_mod.ELASTIC_WEAK and label == key \
                and ratio is not None and ratio < 0:
            head, body = ELASTIC_MEANING_NEGATIVE
        active = " pm-active" if key == label else ""
        mark = '<span class="pm-now">当前</span>' if key == label else ""
        cards.append(
            f'<li class="{css}{active}">'
            f'<b>{esc(_elastic_cell(key)[1])}</b>'
            f'{mark}'
            f'<span class="pm-head">{esc(head)}</span>'
            f'<span class="pm-body">{_emph(body)}</span>'
            '</li>'
        )

    lead = (
        f'<p class="pm-lead"><b>「弹性」是经营杠杆，衡量收入波动被放大成利润波动的倍数，'
        f'不衡量公司好坏。</b>指标是<b>营业利润增速 ÷ 营业收入增速</b>'
        f'（逐季环比，取近 {cfg.ELASTIC_WINDOW_QUARTERS} 季中位数，抗单季异常）：'
        f'≥ {cfg.ELASTIC_RATIO_MIN:g} 为强、&lt; {cfg.ELASTIC_RATIO_MIN:g} 为弱；'
        f'营收长期不动的公司比值不可信，单独标「不稳定」。三档具体含义如下：</p>'
    )
    return lead + '<ul class="pm-list">' + "".join(cards) + "</ul>"


#: 判断③ 三档的**准确含义**（§2.3 边界）。
#:
#: 必须写清楚，因为「低位」最容易被误读成三件不同的事：
#:   ① 营收处在低位        —— 错，分子是营收但比的是"周转率"，营收涨了产能涨更多也是低位
#:   ② 需求正在回暖        —— 错，本工具不看需求（订单线已删）
#:   ③ 产能正在出清 / 已出清 —— 对，这是它唯一想表达的意思
#: 不写清楚的后果：用户看到"低位"以为该买了，其实只是"供给端不紧张"，
#: 需求没起来的时候，低位的产能利用率可以维持很久。
POSITION_MEANING = {
    decide_mod.POSITION_LOW: (
        "产能利用率处于自身近 10 年偏低区间",
        "即产能相对需求是宽松的、供给端可能已经出清。"
        "**不代表**营收处在低位，也**不代表**需求正在回暖——"
        "本工具只看供给端，需求拐头要自己确认。"
        "**但「低位」有两种成因，方向相反，必须看旁边的方向标记**："
        "需求走了（营收跌、产能不变）→ 供给端在出清；"
        "产能来了（营收没跌、资产猛涨）→ 分母变大压低了周转率，"
        "公司其实在扩产、未来供给还会增加。实测 49 只低位股里 9 只属于后者。",
    ),
    decide_mod.POSITION_NEUTRAL: (
        "产能利用率处于自身近 10 年中间区间",
        "既谈不上紧张也谈不上宽松，供需大致匹配。"
        "中间地带不给方向性标签，因为这里本来就没有信息。",
    ),
    decide_mod.POSITION_HIGH: (
        "产能利用率处于自身近 10 年偏高区间",
        "即产能相对需求是紧张的、供给端偏紧。"
        "**不代表**业绩见顶或股价见顶——紧张也可能因为需求旺。"
        "但此时扩产冲动最强，要留意在建工程转为未来供给。"
        "反过来，若旁边标着「**产能在退**」，说明周转率高是被动的："
        "产能退出或折旧、营收还在，这个「高位」含金量要打折。",
    ),
    decide_mod.NA: (
        "算不出位置",
        "数据不足或该指标对本行业无意义（金融企业）。",
    ),
}


def _emph(text):
    """把 `**粗体**` 转成 `<b>`，其余一律转义。

    为什么不直接 `esc()`：这段文案里"**不代表**"是最关键的两个词，
    在视觉上必须跳出来。原先写成 Markdown 星号，HTML 里就原样显示了
    `**不代表**` —— 实机截图才发现（见 test_markdown_stars_never_leak）。

    做法是先切分再转义：只有被 `**` 包裹的片段才放行 `<b>`，
    其余文字照常过 `esc()`。这样即使文案里哪天混进别的标签也漏不出去。
    """
    parts = str(text or "").split("**")
    out = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            out.append(f"<b>{esc(part)}</b>")
        else:
            out.append(esc(part))
    return "".join(out)


def render_position_meaning(decision, cfg=None):
    """判断③ 下面的一小段：把三档含义摊开写，当前档高亮。

    为什么不做成 hover 提示：报告要能打印、要能离线看，
    隐藏式说明在最需要它的场景（打印稿、截图转发）里恰好消失。
    """
    cfg = cfg or config
    flags = decision.get("flags") or {}
    label = (decision.get("position") or {}).get("label")

    if flags.get("financial"):
        label = decide_mod.NA

    cards = []
    for key, css in ((decide_mod.POSITION_LOW, "pm-low"),
                     (decide_mod.POSITION_NEUTRAL, "pm-neutral"),
                     (decide_mod.POSITION_HIGH, "pm-high")):
        head, body = POSITION_MEANING[key]
        active = " pm-active" if key == label else ""
        mark = '<span class="pm-now">当前</span>' if key == label else ""
        cards.append(
            f'<li class="{css}{active}">'
            f'<b>{esc(_position_cell(key, flags)[1])}</b>'
            f'{mark}'
            f'<span class="pm-head">{esc(head)}</span>'
            f'<span class="pm-body">{_emph(body)}</span>'
            '</li>'
        )

    lead = (
        f'<p class="pm-lead"><b>「周期位置」看的是供给端，不是需求，也不是营收高低。</b>'
        f'指标是<b>产能周转率</b>（最近 4 季营收之和〔TTM〕÷ 固定资产+在建工程）'
        f'在自身近 {cfg.POSITION_WINDOW_QUARTERS // 4} 年里的排位：'
        f'&lt;{cfg.POSITION_LOW_PCT:g}% 为低位、&gt;{cfg.POSITION_HIGH_PCT:g}% 为高位，'
        f'夹在中间不给方向。三档具体含义如下：</p>'
    )
    return lead + '<ul class="pm-list">' + "".join(cards) + "</ul>"


def _row(name, css, result, metric_name, value, note):
    return (
        f'<tr><td>{esc(name)}</td>'
        f'<td class="j-res {css}">{esc(result)}</td>'
        f'<td>{esc(metric_name)}</td>'
        f'<td class="j-num">{esc(value)} {note}</td></tr>'
    )


def _cycle_cell(label, flags):
    if label == decide_mod.CYCLE_YES:
        return "j-yes", "是"
    if label == decide_mod.CYCLE_NO:
        return "j-no", "否"
    if flags.get("insufficient_history"):
        return "j-na", DASH
    return "j-na", DASH


def _elastic_cell(label):
    if label == decide_mod.ELASTIC_STRONG:
        return "j-yes", "强"
    if label == decide_mod.ELASTIC_WEAK:
        return "j-no", "弱"
    if label == decide_mod.ELASTIC_UNSTABLE:
        return "j-neutral", "不稳定"
    return "j-na", DASH


def _position_cell(label, flags):
    if label == decide_mod.POSITION_LOW:
        return "j-low", "低位"
    if label == decide_mod.POSITION_HIGH:
        return "j-high", "高位"
    if label == decide_mod.POSITION_NEUTRAL:
        return "j-neutral", "中性"
    return "j-na", DASH


def _gm_valid(quarters, cfg):
    window = quarters[-cfg.WAVE_WINDOW_QUARTERS:] if cfg.WAVE_WINDOW_QUARTERS > 0 else []
    return sum(1 for q in window if q.get("gross_margin") is not None)


# ---------------------------------------------------------------------------
# 第三块：三张小图（内联 SVG）
# ---------------------------------------------------------------------------

SVG_W = 640
SVG_H = 182
PAD_L = 46
PAD_R = 12
PAD_T = 14
#: 底边留白要装下两行：轴线 + 季度刻度文字（"22Q1" 约 10px 高）
PAD_B = 40


def _x(i, n):
    if n <= 1:
        return PAD_L + (SVG_W - PAD_L - PAD_R) / 2.0
    span = SVG_W - PAD_L - PAD_R
    return PAD_L + span * i / (n - 1)


def _x_bar(i, n):
    """柱状图的柱子中心（按格子中心排，不是首尾顶满）。"""
    slot = (SVG_W - PAD_L - PAD_R) / max(n, 1)
    return PAD_L + slot * (i + 0.5), slot


_QUARTER_RE = re.compile(r"^\d{2}Q[1-4]$")


def _quarter_label(date_str):
    """'2026-09-30' → '26Q3'；认不出就返回空串（宁可不标，不标错）。

    两种输入都认：原始日期串，以及已经被 `_short_date` 转过一次的 '26Q3'。
    图表的 `points` 走的是后者（元素是 `(_short_date(date), value)`），
    早期版本只认原始日期串，接进来时 `26Q3` 全部判空、刻度一个都画不出来。
    """
    text = str(date_str or "")
    if _QUARTER_RE.match(text):
        return text
    if len(text) < 7:
        return ""
    try:
        year = text[2:4]
        month = int(text[5:7])
    except ValueError:
        return ""
    if not 1 <= month <= 12:
        return ""
    return f"{year}Q{(month + 2) // 3}"


#: 季度刻度文字的实测占宽。"22Q1" 是 4 个等宽数字/字母，10px 字号下约 34px，
#: 再加 4px 呼吸空间。这个值定小了刻度就会首尾相接 —— 截图里一度就是这样：
#: `21Q422Q122Q2...` 连成一串，比不标还难读。
TICK_LABEL_PX = 38.0


def _pick_ticks(n, room_px, label_px=TICK_LABEL_PX):
    """挑出要标刻度的下标，保证文字不挤在一起。

    做法：从最右（最新）往左走，每隔 k 个标一个，k 取"每格像素 × k >= 标签宽"。
    从右往左而不是从左往右，是为了让**最新一季永远有刻度**——
    看报告时最想知道"现在到哪了"，而不是"最早是哪季"。
    """
    if n <= 0:
        return []
    if n == 1:
        return [0]
    slot_px = (SVG_W - PAD_L - PAD_R) / (n - 1)
    step = max(1, int(-(-label_px // slot_px)))   # ceil
    picks = list(range(n - 1, -1, -step))
    return sorted(picks)


def _bar_ticks(n, detail_px, label_px=TICK_LABEL_PX):
    """柱状图的刻度下标（按格宽算，柱子是居中排的）。"""
    if n <= 0:
        return []
    if n == 1:
        return [0]
    step = max(1, int(-(-label_px // detail_px)))
    picks = list(range(n - 1, -1, -step))
    return sorted(picks)


def _x_axis_at(labels, tick_idx, base_y, bar_mode=False, n=None):
    """横轴：基线 + 季度刻度文字。base_y 是绘图区底边（y 坐标）。"""
    n = n if n is not None else len(labels)
    out = [f'<line class="xaxis" x1="{PAD_L}" y1="{base_y:.1f}" '
           f'x2="{SVG_W - PAD_R}" y2="{base_y:.1f}"/>']
    for i in tick_idx:
        text = _quarter_label(labels[i])
        if not text:
            continue
        if bar_mode:
            x, _slot = _x_bar(i, n)
        else:
            x = _x(i, n)
        # 首个/末个刻度贴边，避免文字被裁掉
        anchor = "middle"
        if not bar_mode and n > 1:
            if i == 0:
                anchor = "start"
            elif i == n - 1:
                anchor = "end"
        out.append(f'<text class="xtick" x="{x:.1f}" y="{base_y + 14:.1f}" '
                   f'text-anchor="{anchor}">{esc(text)}</text>')
    return "".join(out)


def _y(value, lo, hi):
    if hi == lo:
        return (PAD_T + SVG_H - PAD_B) / 2.0
    span = SVG_H - PAD_T - PAD_B
    return PAD_T + span * (1 - (value - lo) / (hi - lo))


def _value_range(values, extra=()):
    """算纵轴范围，把参考线也纳入，避免线跑到图外。"""
    clean = [v for v in values if v is not None] + [v for v in extra if v is not None]
    if not clean:
        return 0.0, 1.0
    lo, hi = min(clean), max(clean)
    if hi == lo:
        pad = abs(hi) * 0.1 or 1.0
        return lo - pad, hi + pad
    pad = (hi - lo) * 0.12
    return lo - pad, hi + pad


def _empty_svg(message):
    return (
        f'<svg viewBox="0 0 {SVG_W} {SVG_H}" role="img" aria-label="{esc(message)}">'
        f'<text class="empty" x="{SVG_W/2}" y="{SVG_H/2}" text-anchor="middle">{esc(message)}</text>'
        '</svg>'
    )


def _svg_open(title):
    return (f'<svg viewBox="0 0 {SVG_W} {SVG_H}" role="img" aria-label="{esc(title)}" '
            'preserveAspectRatio="none">')


def _line_chart(points, title, threshold=None, threshold_text="", bands=()):
    """折线图。points 是 [(label, value), ...]，None 值断线。"""
    if not points or all(v is None for _, v in points):
        return _empty_svg("无可用数据")

    values = [v for _, v in points]
    extras = []
    if threshold is not None:
        extras.append(threshold)
    for lo, hi in bands:
        extras.extend([lo, hi])
    lo, hi = _value_range(values, extras)

    n = len(points)
    out = [_svg_open(title)]

    # 背景带（如 40~60 分位区间）
    for band_lo, band_hi in bands:
        y1 = _y(band_hi, lo, hi)
        y2 = _y(band_lo, lo, hi)
        out.append(f'<rect class="{"band-low" if band_lo < 50 else "band-high"}" '
                   f'x="{PAD_L}" y="{y1:.1f}" width="{SVG_W-PAD_L-PAD_R}" '
                   f'height="{abs(y2-y1):.1f}"/>')

    # 横向网格 + 刻度
    for frac in (0.0, 0.5, 1.0):
        val = lo + (hi - lo) * frac
        y = _y(val, lo, hi)
        out.append(f'<line class="gridline" x1="{PAD_L}" y1="{y:.1f}" '
                   f'x2="{SVG_W-PAD_R}" y2="{y:.1f}"/>')
        out.append(f'<text class="tick" x="{PAD_L-6}" y="{y+3:.1f}" '
                   f'text-anchor="end">{val:.1f}</text>')

    # 阈值线
    if threshold is not None:
        ty = _y(threshold, lo, hi)
        out.append(f'<line class="threshold" x1="{PAD_L}" y1="{ty:.1f}" '
                   f'x2="{SVG_W-PAD_R}" y2="{ty:.1f}"/>')
        if threshold_text:
            out.append(f'<text class="threshold-label" x="{SVG_W-PAD_R}" y="{ty-4:.1f}" '
                       f'text-anchor="end">{esc(threshold_text)}</text>')

    # 折线（分段，遇到 None 断开）
    segments = []
    current = []
    for i, (_, v) in enumerate(points):
        if v is None:
            if len(current) > 1:
                segments.append(current)
            current = []
        else:
            current.append((_x(i, n), _y(v, lo, hi)))
    if len(current) > 1:
        segments.append(current)

    for seg in segments:
        d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in seg)
        out.append(f'<path d="{d}" fill="none" stroke="#2f6152" stroke-width="2" '
                   'stroke-linejoin="round" stroke-linecap="round"/>')

    # 端点圆点（最后一个有效值）
    last_idx = max((i for i, (_, v) in enumerate(points) if v is not None), default=None)
    if last_idx is not None:
        lx = _x(last_idx, n)
        ly = _y(points[last_idx][1], lo, hi)
        out.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="#2f6152"/>')

    # 横轴 + 季度刻度（放最后画，压住折线尾巴，视觉上像轴而不是线穿过去）
    base_y = SVG_H - PAD_B
    labels = [label for label, _ in points]
    out.append(_x_axis_at(labels, _pick_ticks(n, (SVG_W - PAD_L - PAD_R) / max(n - 1, 1)),
                          base_y, n=n))

    out.append("</svg>")
    return "".join(out)


def _bar_chart(points, title, threshold=None):
    """柱状图，用于逐季比值（有正有负，需要零轴）。"""
    if not points or all(v is None for _, v in points):
        return _empty_svg("无可用数据")

    values = [v for _, v in points]
    extras = [0.0]
    if threshold is not None:
        extras.append(threshold)
    lo, hi = _value_range(values, extras)

    n = len(points)
    out = [_svg_open(title)]

    for frac in (0.0, 0.5, 1.0):
        val = lo + (hi - lo) * frac
        y = _y(val, lo, hi)
        out.append(f'<line class="gridline" x1="{PAD_L}" y1="{y:.1f}" '
                   f'x2="{SVG_W-PAD_R}" y2="{y:.1f}"/>')
        out.append(f'<text class="tick" x="{PAD_L-6}" y="{y+3:.1f}" '
                   f'text-anchor="end">{val:.1f}</text>')

    zero_y = _y(0.0, lo, hi)
    out.append(f'<line class="axis" x1="{PAD_L}" y1="{zero_y:.1f}" '
               f'x2="{SVG_W-PAD_R}" y2="{zero_y:.1f}"/>')

    if threshold is not None:
        ty = _y(threshold, lo, hi)
        out.append(f'<line class="threshold" x1="{PAD_L}" y1="{ty:.1f}" '
                   f'x2="{SVG_W-PAD_R}" y2="{ty:.1f}"/>')
        out.append(f'<text class="threshold-label" x="{SVG_W-PAD_R}" y="{ty-4:.1f}" '
                   f'text-anchor="end">{esc(f"门槛 {threshold:g}")}</text>')

    span = SVG_W - PAD_L - PAD_R
    slot = span / max(n, 1)
    bar_w = max(3.0, slot * 0.58)
    for i, (_, v) in enumerate(points):
        if v is None:
            continue
        cx = PAD_L + slot * (i + 0.5)
        y = _y(v, lo, hi)
        top = min(y, zero_y)
        height = abs(y - zero_y)
        if height < 0.8:
            height = 0.8
        color = "#2f6152" if v >= 0 else "#b3261e"
        out.append(f'<rect x="{cx-bar_w/2:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
                   f'height="{height:.1f}" fill="{color}" rx="1.5"/>')

    # 横轴 + 季度刻度（柱子是第一根也算首个刻度，但仍用居中对齐）
    base_y = SVG_H - PAD_B
    labels = [label for label, _ in points]
    out.append(_x_axis_at(labels, _bar_ticks(n, slot), base_y, bar_mode=True, n=n))

    out.append("</svg>")
    return "".join(out)


def render_charts(charts, cfg=None):
    """三张图，各自带标题与一句读法。"""
    cfg = cfg or config

    gross = charts.get("gross_margin") or []
    gm_points = [(_short_date(p["date"]), p["value"]) for p in gross]
    gm_svg = _line_chart(
        gm_points,
        "毛利率（%）",
        threshold=cfg.GM_STD_CYCLE,
        threshold_text=f"σ 门槛 {cfg.GM_STD_CYCLE:g}pp（示意）",
    )
    gm_block = _chart_block(
        "毛利率",
        f"最近 {cfg.WAVE_WINDOW_QUARTERS} 季单季毛利率",
        gm_svg,
    )

    ratio = charts.get("ratio") or []
    ratio_points = [(_short_date(p["date"]), p["value"]) for p in ratio]
    ratio_svg = _bar_chart(ratio_points, "营业利润增速÷营收增速", threshold=cfg.ELASTIC_RATIO_MIN)
    ratio_block = _chart_block(
        "弹性比值",
        f"最近 {cfg.ELASTIC_WINDOW_QUARTERS} 季逐季比值",
        ratio_svg,
    )

    turnover = charts.get("turnover") or []
    to_points = [(_short_date(p["date"]), p["value"]) for p in turnover]
    to_values = [v for _, v in to_points if v is not None]
    to_svg = _line_chart(to_points, "产能周转率（次/年）")
    to_block = _chart_block(
        "产能周转率",
        f"最近 {len(turnover)} 季（TTM 营收 ÷ 固定资产+在建工程）",
        to_svg,
    )

    return gm_block + ratio_block + to_block


def _chart_block(title, subtitle, svg):
    return (
        '<div class="chart">'
        f'<h3>{esc(title)}<span class="sub">{esc(subtitle)}</span></h3>'
        f'{svg}'
        '</div>'
    )


def _short_date(date_str):
    """'2026-09-30' → '26Q3'。"""
    if not date_str or len(date_str) < 7:
        return str(date_str or "")
    year = date_str[2:4]
    month = int(date_str[5:7])
    quarter = (month + 2) // 3
    return f"{year}Q{quarter}"


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def render(stock, decision, metrics, charts, generated_at=None):
    """生成完整 HTML 文本。

    `stock` 是 build() 的返回；`decision` 是 decide() 的返回。
    """
    cfg = config
    quarters = stock.get("quarters") or []

    title = f"{stock.get('name') or stock.get('code')} {stock.get('code')} · 周期弹性初筛"
    industry = stock.get("industry") or ""
    industry_line = f"{industry} · {len(quarters)} 个季度" if industry else f"{len(quarters)} 个季度"

    date_from = quarters[0]["date"] if quarters else DASH
    date_to = quarters[-1]["date"] if quarters else DASH

    gaps = (stock.get("meta") or {}).get("data_gaps") or []
    if gaps:
        shown = gaps[:8]
        items = "".join(f"<li>{esc(g)}</li>" for g in shown)
        more = f"<li>…另有 {len(gaps) - len(shown)} 条</li>" if len(gaps) > len(shown) else ""
        gaps_block = f'<p style="margin-top:10px"><b>数据说明</b></p><ol>{items}{more}</ol>'
    else:
        gaps_block = ""

    replacements = {
        "{{TITLE}}": esc(title),
        "{{ATTRS}}": body_attrs(stock, decision),
        "{{NAME}}": esc(stock.get("name") or stock.get("code") or ""),
        "{{CODE}}": esc(stock.get("code") or ""),
        "{{INDUSTRY_LINE}}": esc(industry_line),
        "{{HEADLINE}}": build_headline(decision, cfg),
        "{{BADGES}}": render_badges(decision),
        "{{JUDGE_ROWS}}": render_judge_rows(stock, decision, metrics, cfg),
        "{{CYCLE_MEANING}}": render_cycle_meaning(decision, cfg),
        "{{ELASTIC_MEANING}}": render_elastic_meaning(decision, cfg),
        "{{POSITION_MEANING}}": render_position_meaning(decision, cfg),
        "{{CHARTS}}": render_charts(charts, cfg),
        "{{GM_WINDOW}}": str(cfg.WAVE_WINDOW_QUARTERS),
        "{{QUARTERS}}": str(len(quarters)),
        "{{DATE_FROM}}": esc(date_from),
        "{{DATE_TO}}": esc(date_to),
        "{{GENERATED_AT}}": esc(generated_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
        "{{GAPS_BLOCK}}": gaps_block,
    }

    template = _load_template()
    html = template
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


_TEMPLATE_CACHE = {}


def _load_template():
    if "report" in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE["report"]
    path = os.path.join(config.PROJECT_ROOT, "templates", "report.html")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    _TEMPLATE_CACHE["report"] = text
    return text


def clear_template_cache():
    """测试用：模板改了之后需要重新读。"""
    _TEMPLATE_CACHE.clear()
