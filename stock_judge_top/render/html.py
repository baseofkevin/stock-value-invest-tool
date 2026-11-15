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

    add('<section class="panel">')
    add('<div class="section-head"><div><h2>数据与口径</h2>'
        '<p>每个因子独立判断；数据不足时该卡片显示“数据不足，不判断”。</p></div></div>')
    if r.get("warnings"):
        add('<ul class="warnings">')
        for w in r["warnings"]:
            add(f"<li>{_h(w)}</li>")
        add("</ul>")
    else:
        add('<div class="empty">无额外数据提示。</div>')
    add("</section>")

    add('<div class="foot"><strong>说明</strong>：本页只展示动量、换手、业绩、量价四个因子的独立结果，'
        '不合并总分，不输出顶部结论，也不构成买卖建议。<br>'
        '数据来源：腾讯自选股（westock）日 K、财务数据；财务按披露日过滤，结果仅供研究参考。</div>')
    add("</div>")
    add("</body>")
    add("</html>")
    return "\n".join(lines) + "\n"


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
