"""索引页 `<CSV主名>.html`（README.md「CLI 参考」）。

8 列：代码 / 名称 / 毛利率σ / 弹性 / 周期位置 / 判定 / 报告 / 行情
5 个统计块：周期股 / 弹性强 / 低位 / 高位 / 失败缺数据

三条必须做的坑（参考页实测踩过）：
  1. `href` 必须 `urllib.parse.quote()` 编码 —— 中文名 + 空格 + `#` 不编码在
     `file://` 下必断链（`#` 会被当成锚点，后面的部分全丢）。
  2. 每行要 `data-search` 且**包含可见文本**，否则搜索框搜不到。
  3. 失败行也要在表里，否则行数与 CSV 对不上。
"""

import datetime
import os
from urllib.parse import quote

import config
from cycle import codes as codes_mod
from cycle import decide as decide_mod
from cycle import render as render_mod

#: 统计块（5 项，§6.4）
STAT_KEYS = ("cycle_yes", "elastic_strong", "low", "high", "failed")

#: 表格列（8 列）。`行情` 是东方财富跳转，方便看完初筛直接翻 K 线。
COLUMNS = ("代码", "名称", "毛利率σ", "弹性", "周期位置", "判定", "报告", "行情")


def _esc(value):
    if value is None:
        return ""
    text = str(value)
    for raw, safe in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                      ('"', "&quot;"), ("'", "&#39;")):
        text = text.replace(raw, safe)
    return text


def _fmt(value, digits=2, suffix="", lower=None, upper=None):
    """与报告页同一套格式化：精度与"不跨判定阈值"的舍入都由 cycle.render 提供。

    索引页原先自己写了一份 `f"{value:.1f}"`，导致川发龙蟒的 σ=4.9505
    在索引页显示成「5.0 pp」而徽章写「非周期」—— 两处必须共用一个实现。
    """
    if value is None:
        return ""
    return render_mod.fmt_num(value, digits, suffix, lower=lower, upper=upper)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def compute_stats(rows):
    """5 个统计数与成功/失败数。

    `失败/缺数据` = 无报告的行 + 报告中任一指标为 `na` 的行。
    **只统计一次**（重复行不重复计）。
    """
    stats = {k: 0 for k in STAT_KEYS}
    success = 0
    failed = 0
    for item in rows:
        if getattr(item.row, "duplicate", False):
            continue
        if not item.ok or item.decision is None:
            failed += 1
            stats["failed"] += 1
            continue
        success += 1
        d = item.decision
        if (d.get("cycle") or {}).get("label") == decide_mod.CYCLE_YES:
            stats["cycle_yes"] += 1
        if (d.get("elastic") or {}).get("label") == decide_mod.ELASTIC_STRONG:
            stats["elastic_strong"] += 1
        pos = (d.get("position") or {}).get("label")
        if pos == decide_mod.POSITION_LOW:
            stats["low"] += 1
        elif pos == decide_mod.POSITION_HIGH:
            stats["high"] += 1
        if decide_mod.has_any_na(d):
            stats["failed"] += 1
    return {
        "counts": stats,
        "success": success,
        "failed": failed,
        "total": len([r for r in rows if not getattr(r.row, "duplicate", False)]),
    }


def render_stats(counts):
    labels = (
        ("cycle_yes", "周期股"),
        ("elastic_strong", "弹性强"),
        ("low", "低位"),
        ("high", "高位"),
        ("failed", "失败/缺数据"),
    )
    return "\n      ".join(
        f'<span class="stat">{_esc(text)} {counts.get(key, 0)}</span>'
        for key, text in labels
    )


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------


def _badge_group(decision):
    """『判定』列：周期性 + 弹性，外加两个辅助标记。

    「相对波动」= 判断① 走的是 CV 通道（结论强度与绝对口径相同）；
    「①否②强·存疑」= 矛盾提示，见 decide.BADGE_CONTRADICTION。
    两个标记都不改变三个判断的取值，只解释"为什么"和"该不该多看一页"。
    """
    if not decision:
        return ""
    out = []
    cycle_obj = decision.get("cycle") or {}
    cycle = cycle_obj.get("label")
    if cycle == decide_mod.CYCLE_YES:
        out.append('<span class="badge b-cycle-yes">周期股</span>')
        if cycle_obj.get("channel") == "cv":
            out.append('<span class="badge b-cv">相对波动</span>')
    elif cycle == decide_mod.CYCLE_NO:
        out.append('<span class="badge b-cycle-no">非周期</span>')

    elastic = (decision.get("elastic") or {}).get("label")
    cls = {
        decide_mod.ELASTIC_STRONG: ("b-strong", "弹性强"),
        decide_mod.ELASTIC_WEAK: ("b-weak", "弹性弱"),
        decide_mod.ELASTIC_UNSTABLE: ("b-unstable", "比值不稳定"),
    }.get(elastic)
    if cls:
        out.append(f'<span class="badge {cls[0]}">{cls[1]}</span>')

    if cycle == decide_mod.CYCLE_NO and elastic == decide_mod.ELASTIC_STRONG:
        out.append(f'<span class="badge b-contra">{decide_mod.BADGE_CONTRADICTION}</span>')

    # 产能方向：给「低位/高位」加限定语的辅助标记
    direction = (decision.get("position") or {}).get("capacity_direction")
    pos = (decision.get("position") or {}).get("label")
    dir_text = decide_mod.CAPACITY_DIRECTION_TEXT.get(direction)
    if dir_text and pos in (decide_mod.POSITION_LOW, decide_mod.POSITION_HIGH):
        out.append(f'<span class="badge b-capacity">{dir_text}</span>')
    return "".join(out)


def _position_cell(decision):
    """『周期位置』列。

    金融企业要特别区分：它们的产能周转率**本来就不适用**，
    和"数据缺失"是两件事，都显示 `—` 会让用户以为这只股票数据不全。
    """
    if not decision:
        return '<span class="badge b-muted">—</span>'
    if (decision.get("flags") or {}).get("financial"):
        return '<span class="badge b-muted">不适用</span>'
    pos = (decision.get("position") or {}).get("label")
    mapping = {
        decide_mod.POSITION_LOW: ("b-low", "低位"),
        decide_mod.POSITION_NEUTRAL: ("b-neutral", "中性"),
        decide_mod.POSITION_HIGH: ("b-high", "高位"),
    }
    if pos in mapping:
        cls, text = mapping[pos]
        return f'<span class="badge {cls}">{text}</span>'
    return '<span class="badge b-muted">—</span>'


def render_row(item):
    """一行 tr。失败行、重复行都要渲染。"""
    row = item.row
    code = row.code or row.code_raw or ""
    name = row.name_raw or ""
    if item.decision:
        name = name or (item.decision.get("_name") or "")

    badges_html = ""
    if getattr(row, "duplicate", False):
        badges_html = '<span class="badge b-dup">重复</span>'

    if item.ok and item.path:
        href = quote(os.path.basename(item.path))
        # 新标签页打开：索引页是"目录"，点进报告后要能随时回来接着看下一只。
        # 直接跳转会把索引页顶掉，用户得一直按后退 —— 扫 100 多只时很难受。
        # `noopener` 是必须的：不让报告页通过 window.opener 反向控制索引页
        # （本地 file:// 场景下这一条尤其重要）。
        report_cell = (
            f'<a class="btn" href="{_esc(href)}" '
            f'target="_blank" rel="noopener">报告链接</a>'
        )
    else:
        reason = item.error or "未生成"
        report_cell = f'<span class="btn disabled">无报告</span><div class="reason">{_esc(reason)}</div>'

    quotes_cell = _quotes_cell(code)

    d = item.decision
    gm_std = (d.get("cycle") or {}).get("gm_std") if d else None
    ratio = (d.get("elastic") or {}).get("ratio") if d else None

    if gm_std is not None:
        gm_cell = (f'<td class="num">{_fmt(gm_std, 2, " pp", lower=config.GM_STD_CYCLE)}'
                   '</td>')
    else:
        gm_cell = '<td class="num null">—</td>'
    if ratio is not None:
        ratio_cell = (f'<td class="num">{_fmt(ratio, 2, lower=config.ELASTIC_RATIO_MIN)}'
                      '</td>')
    else:
        ratio_cell = '<td class="num null">—</td>'

    search = " ".join(filter(None, [
        code, name,
        _plain_badges(d) if d else "",
        item.error if not item.ok else "",
    ]))

    return (
        f'<tr data-search="{_esc(search)}">'
        f'<td class="code">{_esc(code)}</td>'
        f'<td class="name" title="{_esc(name)}">{_esc(name)}{badges_html}</td>'
        f'{gm_cell}{ratio_cell}'
        f'<td>{_position_cell(d)}</td>'
        f'<td>{_badge_group(d)}</td>'
        f'<td>{report_cell}</td>'
        f'{quotes_cell}'
        f'</tr>'
    )


def _quotes_cell(code):
    """『行情』列：跳东方财富个股页。

    为什么放在索引页而不是报告页：初筛的动线是「表格里扫一眼 → 挑出想看的
    → 去翻 K 线」。链接在这条动线上省一次手工搜索。

    代码非法（理论上有，因为失败行也会渲染）时退化成破折号，不能崩。
    必须 `target="_blank" rel="noopener"`：`noopener` 是为了不让对方页面
    拿到 `window.opener` 反向控制这个本地 `file://` 页面。
    """
    url = codes_mod.eastmoney_url(code)
    if not url:
        return '<td class="act"><span class="btn disabled">—</span></td>'
    return (
        f'<td class="act"><a class="btn alt" href="{_esc(url)}" '
        f'target="_blank" rel="noopener">东方财富</a></td>'
    )


def _plain_badges(decision):
    """给 data-search 用的纯文本标签，保证搜索能命中"低位""弹性强"等词。"""
    words = []
    if (decision.get("cycle") or {}).get("label") == decide_mod.CYCLE_YES:
        words.append("周期股")
    elif (decision.get("cycle") or {}).get("label") == decide_mod.CYCLE_NO:
        words.append("非周期")
    if ((decision.get("cycle") or {}).get("channel") == "cv"):
        words.append("相对波动")
    elastic = (decision.get("elastic") or {}).get("label")
    words.append({
        decide_mod.ELASTIC_STRONG: "弹性强",
        decide_mod.ELASTIC_WEAK: "弹性弱",
        decide_mod.ELASTIC_UNSTABLE: "比值不稳定",
    }.get(elastic, ""))
    if ((decision.get("cycle") or {}).get("label") == decide_mod.CYCLE_NO
            and elastic == decide_mod.ELASTIC_STRONG):
        words.append(decide_mod.BADGE_CONTRADICTION)
    # 方向标记只在低位/高位出现，搜索词也必须用同一条判据，
    # 否则「中性 + 扩产中」在表格里看不见、却能被搜出来 —— 两处口径不一致。
    position_obj = decision.get("position") or {}
    direction = position_obj.get("capacity_direction")
    pos_label = position_obj.get("label")
    if (direction in decide_mod.CAPACITY_DIRECTION_TEXT
            and pos_label in (decide_mod.POSITION_LOW, decide_mod.POSITION_HIGH)):
        words.append(decide_mod.CAPACITY_DIRECTION_TEXT[direction])
    pos = (decision.get("position") or {}).get("label")
    words.append({
        decide_mod.POSITION_LOW: "低位",
        decide_mod.POSITION_NEUTRAL: "中性",
        decide_mod.POSITION_HIGH: "高位",
    }.get(pos, ""))
    return " ".join(w for w in words if w)


def render_table(rows):
    if not rows:
        return ('<div class="empty">CSV 里没有数据行。</div>')

    head = "".join(f"<th>{_esc(c)}</th>" for c in COLUMNS)
    body = "\n      ".join(render_row(item) for item in rows)
    return (
        '<div class="tablecard">'
        '<table>'
        f'<thead><tr>{head}</tr></thead>'
        f'<tbody id="rows">\n      {body}\n      </tbody>'
        '</table>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def build_index(rows, csv_path, out_dir, generated_at=None):
    """生成索引页，返回其路径。"""
    os.makedirs(out_dir, exist_ok=True)

    from cycle import batch as batch_mod

    fname = batch_mod.index_filename(csv_path)
    path = os.path.join(out_dir, fname)

    summary = compute_stats(rows)
    stem = os.path.splitext(os.path.basename(csv_path))[0]
    title = f"周期弹性初筛 · {stem}"
    subtitle = (
        f"{os.path.basename(csv_path)} · 共 {summary['total']} 只 · "
        f"成功 {summary['success']} · 失败 {summary['failed']} · "
        "点击「报告链接」直接打开 HTML"
    )

    template = _load_template()
    html = (template
            .replace("{{TITLE}}", _esc(title))
            .replace("{{SUBTITLE}}", _esc(subtitle))
            .replace("{{STATS}}", render_stats(summary["counts"]))
            .replace("{{TABLE}}", render_table(rows))
            .replace("{{GENERATED_AT}}",
                     _esc(generated_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))))

    batch_mod.write_atomic(path, html)
    return path


_CACHE = {}


def _load_template():
    if "index" in _CACHE:
        return _CACHE["index"]
    path = os.path.join(config.PROJECT_ROOT, "templates", "index.html")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    _CACHE["index"] = text
    return text


def clear_template_cache():
    _CACHE.clear()
