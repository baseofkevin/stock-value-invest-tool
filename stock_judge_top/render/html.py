"""HTML 报告生成。"""
import datetime as dt
import html
import os
import re

from ..config import OUT_DIR
from .css import HTML_CSS
from .format import fmt_pct


def _h(v):
    return html.escape("—" if v is None else str(v))


def _num(v, nd=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def html_report(r):
    name = r.get("name") or ""
    code = r["code"]
    date = r["date"]
    kline = r.get("kline") or {}
    loc = r.get("local") or {}
    display_name = name if name else code
    code_html = f'<span class="code">{_h(code)}</span>' if name else ""
    views = list(r.get("views") or [])
    views = [v for v in views if not v.get("wide")] + [v for v in views if v.get("wide")]

    lines = []
    add = lines.append
    add("<!DOCTYPE html>")
    add('<html lang="zh-CN">')
    add("<head>")
    add('<meta charset="utf-8">')
    add('<meta name="viewport" content="width=device-width, initial-scale=1">')
    add(f"<title>四因子观察 | {_h(code)} {_h(display_name)} | {_h(date)}</title>")
    add(f"<style>{HTML_CSS}</style>")
    add("</head>")
    add("<body>")
    add('<div class="page">')

    add('<header class="hero">')
    add('<div class="eyebrow">Factor Watch · 四因子独立观察</div>')
    add('<div class="title-row">')
    add(f"<h1>{_h(display_name)}{code_html}</h1>")
    add("</div>")
    add(f'<div class="meta">基准日 <strong>{_h(date)}</strong> · K线最后交易日 '
        f'{_h(kline.get("date"))} · 收盘 <strong>{_h(_num(r.get("close")))}</strong></div>')
    add(f'<div class="meta">位置阶段：<strong>{_h(r.get("phase"))}</strong>'
        '（描述价格在历史区间中的位置，不是顶部结论）</div>')
    add('<div class="notice"><span class="dot"></span>'
        '4 个因子各自独立展示，不合并总分，不自动判断顶部</div>')
    add('<div class="hero-grid">')
    add(f'''<div class="hero-stat"><label>250日高点</label><b>{_h(_num(r.get("high250")))}</b>
      <small>{_h(r.get("high250_date"))} · 距今 {_h(r.get("days_since_high"))} 个交易日</small></div>''')
    pos_cls = "neg" if (r.get("pos250") is not None and r["pos250"] < 0) else ""
    add(f'''<div class="hero-stat"><label>距250日高点</label><b class="{pos_cls}">{_h(fmt_pct(r.get("pos250")))}</b>
      <small>口径：最高价（盘中）· 只作位置背景</small></div>''')
    add(f'''<div class="hero-stat"><label>距60日高点</label><b>{_h(fmt_pct(loc.get("pos60")))}</b>
      <small>{_h(loc.get("high60_date") or "—")}</small></div>''')
    fm = r.get("finance") or {}
    add(f'''<div class="hero-stat"><label>最新报告期</label><b>{_h(fm.get("latest_end") or "—")}</b>
      <small>业绩背景，不参与顶部判断</small></div>''')
    add("</div>")
    add("</header>")

    add('<section class="panel">')
    add('<div class="section-head"><div><h2>四因子独立观察</h2>'
        '<p>每张卡片只描述自己的维度；程序不做加权、不合计、不替你做综合判断。</p></div></div>')
    add('<div class="factor-grid">')
    for v in views:
        wide = " wide" if v.get("wide") else ""
        tone = v.get("tone") or "muted"
        add(f'<article class="factor-card tone-{tone}{wide}">')
        add('<div class="factor-head">')
        add(f'<h3>{_h(v.get("title"))}<small>{_h(v.get("subtitle"))}</small></h3>')
        add(f'<span class="state state-{_h(tone)}">{_h(v.get("state"))}</span>')
        add("</div>")
        add(f'<p class="headline">{_h(v.get("headline"))}</p>')
        if v.get("blocks"):
            add('<div class="earn-blocks">')
            for b in v["blocks"]:
                add(f'<section class="earn-block tone-{_h(b.get("tone", "info"))}">')
                add(f'<h4>{_h(b.get("title"))}</h4>')
                add(f'<div class="earn-sub">{_h(b.get("subtitle"))}</div>')
                add('<div class="earn-results">')
                for label, value in b.get("results", []):
                    add(f'<div class="earn-result"><span>{_h(label)}</span><b>{_h(value)}</b></div>')
                add('</div>')
                if b.get("sources"):
                    add('<div class="earn-title">数据来源</div>')
                    for label, value in b["sources"]:
                        add(f'<div class="earn-source-row"><span>{_h(label)}</span><b>{_h(value)}</b></div>')
                if b.get("formulas"):
                    add('<div class="earn-title">计算</div>')
                    for formula in b["formulas"]:
                        add(f'<div class="earn-formula">{_h(formula)}</div>')
                add('</section>')
            add('</div>')
        if v.get("metrics"):
            add('<div class="metric-grid">')
            for label, value in v["metrics"]:
                add(f'<div class="metric"><label>{_h(label)}</label><b>{_h(value)}</b></div>')
            add("</div>")
        if v.get("calc"):
            add('<div class="calc"><div class="calc-title">计算数据</div><div class="calc-grid">')
            for label, value in v["calc"]:
                add(f'<div class="calc-item"><span>{_h(label)}</span><b>{_h(value)}</b></div>')
            add("</div></div>")
        add(f'<p class="note">{_h(v.get("note"))}</p>')
        add("</article>")
    add("</div>")
    add("</section>")

    add(_evidence_section(r))

    add('<section class="panel">')
    add('<div class="section-head"><div><h2>数据与口径</h2>'
        '<p>每个因子独立判断；数据不足时该卡片显示“数据不足，不判断”。</p></div></div>')
    add(_gaps_table(r))
    if r.get("warnings"):
        add('<ul class="warnings">')
        for w in r["warnings"]:
            add(f"<li>{_h(w)}</li>")
        add("</ul>")
    add('<table class="kv"><tbody>')
    add('<tr><th>价格口径</th><td><b>不复权（raw）</b>，与 stock_kline_screen 一致。'
        '因此除权日附近的历史高点会带着缺口（例：10 派 3 元会让 250 日高点虚高约 3%），'
        '「距 250 日高点」在有分红时略偏保守。位置类指标是同一价格序列内的比值，'
        '不因复权而改变方向。</td></tr>')
    add('<tr><th>位置口径</th><td>最高价（盘中），不是收盘价；'
        '距高点 = 收盘 ÷ 区间最高价 − 1。</td></tr>')
    add('<tr><th>换手口径</th><td>换手率 = 成交额 ÷ 流通市值（市值取当期一份，'
        '历史换手是近似）；流通市值取不到时改用<b>成交额自身的分位</b>，卡片会写明。</td></tr>')
    add('<tr><th>财务口径</th><td>按披露日 ≤ 基准日过滤；单季数据取上游 `_Q` 字段。</td></tr>')
    add('</tbody></table>')
    add("</section>")

    add('<div class="foot"><strong>说明</strong>：本页展示动量、换手、业绩、量价四个因子'
        '（周期股另加一张周期位置）的独立结果与四条旁证，'
        '不合并总分，不输出顶部结论，也不构成买卖建议。<br>'
        '数据来源：腾讯自选股（westock）日 K、财务，东方财富估值序列；'
        '财务按披露日过滤，结果仅供研究参考。</div>')
    add("</div>")
    add("</body>")
    add("</html>")
    return "\n".join(lines) + "\n"


def _evidence_section(r):
    """四条旁证：估值分位 / 机构目标价 / 基本面反证 / 周期状态。

    都是**一句话**，都不给总分、不参与任何结论——它们只是把"贵不贵、机构怎么看、
    基本面撑不撑得住"摆在旁边，供人自己组合。
    """
    ex = r.get("extras") or {}
    rows = []

    val = ex.get("valuation") or {}
    m = val.get("metrics") or {}
    if m:
        parts = []
        for key in ("pe", "pb", "ps"):
            item = m.get(key) or {}
            if item.get("current") is None:
                continue
            cur = item["current"]
            pct = item.get("percentile")
            tag = f"P{pct:.0f}" if pct is not None else "分位不足"
            parts.append(f"{item.get('label')} {cur:.2f}（{tag}）")
        span = f"{val.get('sample_start')} 以来 {val.get('weeks')} 周"
        tone = "warn" if any(((m.get(k) or {}).get("percentile") or 0) >= 85
                             for k in ("pe", "pb", "ps")) else "info"
        rows.append(("估值分位", "·".join(parts) + f"，样本 {span}", tone))
    else:
        rows.append(("估值分位", "—", "muted"))

    con = ex.get("consensus") or {}
    if con.get("target"):
        up = con.get("upside")
        txt = f"{con['target']:.2f}"
        if up is not None:
            txt += f"（距现价 {up * 100:+.1f}%"
            txt += f"，{con['forecasts']} 个预测年度）" if con.get("forecasts") else "）"
        elif con.get("forecasts"):
            txt += f"（{con['forecasts']} 个预测年度）"
        tone = "good" if (up is not None and up >= 0.10) else (
            "warn" if (up is not None and up <= 0) else "info")
        rows.append(("机构目标价", txt, tone))
    else:
        rows.append(("机构目标价", "无机构覆盖（上游一致预期为空表，不是取数故障）", "muted"))

    rk = ex.get("risk") or {}
    if rk.get("items"):
        txt = "；".join(f"{i['label']} {i['text']}" for i in rk["items"])
        tone = "warn" if rk.get("hit") else ("info" if rk.get("watch") else "good")
        rows.append((f"基本面反证（命中 {rk.get('hit', 0)} / 提示 {rk.get('watch', 0)}）", txt, tone))
    else:
        rows.append(("基本面反证", "—", "muted"))

    cy = ex.get("cycle") or {}
    if cy:
        gm_std = cy.get("gm_std")
        txt = (f"毛利率σ {'—' if gm_std is None else format(gm_std, '.2f') + 'pp'}、CV "
               f"{'—' if cy.get('gm_cv') is None else format(cy['gm_cv'], '.0f') + '%'}"
               f" → {'是周期股' if cy.get('is_cycle') else '不是周期股'}"
               f"；弹性{cy.get('elastic_label')}；产能周转率 "
               f"{'—' if cy.get('position_pct') is None else format(cy['position_pct'], '.0f') + '% 分位'}"
               f"（{cy.get('position_label')}）")
        rows.append(("周期状态", txt, "info"))

    out = ['<section class="panel">',
           '<div class="section-head"><div><h2>旁证（各一句话，不参与结论）</h2>'
           '<p>顶部判断的主证据是上面四张卡片；这里只把"贵不贵、机构怎么看、'
           '基本面撑不撑得住"摆出来。</p></div></div>',
           '<table class="evidence"><tbody>']
    for label, text, tone in rows:
        out.append(f'<tr class="tone-{tone}"><th>{_h(label)}</th><td>{_h(text)}</td></tr>')
    out.append("</tbody></table>")
    out.append("</section>")
    return "\n".join(out)


def _gaps_table(r):
    """取数缺口清单：按域汇总，附一句"怎么办"。

    旧版只有 warnings 里一行行文字，读者分不清"缺数据"和"算不出来"，
    也看不出哪一条会真正影响结论。这里给出：域 · 原因 · 影响面。
    """
    gaps = r.get("gaps") or []
    if not gaps:
        return ('<table class="gaps"><tbody><tr><th>取数缺口</th>'
                '<td class="ok">无（全部数据已取到）</td></tr></tbody></table>')
    impact = {
        "valuation": "估值分位为空，旁证第一行不可用；不影响四因子",
        "snapshot": "机构目标价为空；不影响四因子",
        "finance": "基本面反证/周期位置可能缺项；业绩卡片仍按已有数据判断",
    }
    out = ['<table class="gaps"><thead><tr><th>域</th><th>原因</th><th>影响</th></tr></thead><tbody>']
    for g in gaps:
        dom = g.get("domain") or "—"
        out.append(f'<tr><td>{_h(dom)}</td><td>{_h(g.get("why"))}</td>'
                   f'<td>{_h(impact.get(dom, "该旁证为空，不影响四因子"))}</td></tr>')
    out.append("</tbody></table>")
    return "\n".join(out)


def _safe_filename(value):
    """股票名称 → 安全文件名；去掉路径分隔符、Windows 非法字符和空白。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "", str(value or "")).strip(" .")


def html_filename(r, now=None):
    """HTML 文件名：股票名称_生成时间.html；名称缺失时用代码兜底。"""
    name = _safe_filename(r.get("name")) or _safe_filename(r.get("code")) or "unknown"
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{name}_{stamp}.html"


def write_html_file(r, now=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, html_filename(r, now))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_report(r))
    return path
