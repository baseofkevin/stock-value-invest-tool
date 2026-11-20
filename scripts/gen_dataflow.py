# -*- coding: utf-8 -*-
"""生成仓库根目录的 dataflow.svg / dataflow.html（数据流转与筛选条件全景图）。

为什么要写成脚本，而不是一张手画图
----------------------------------
图里写的几乎全是**当前代码里的常量**：18 张表、9 张表、六道闸、K=5、
`KLINE_KEEP_BARS=400`、ERP 2400 日分位 95/80/20、BUY_CUT=5、GATE2 的 25%……
代码一动，图就开始说谎；而手改一张 1720×2754 的 SVG 是件没人愿意干的事，
改完也说不清哪句还对、哪句已经过期。所以这里把图拆成「排版引擎 + 内容」：

* 排版引擎只管三件事：量字宽、折行、按行画盒子（自动高度，不用手算 y）。
* 内容是九个 `s_*` 函数，一个章节一个函数，改哪节翻哪节。
* 画布固定 1720 宽；每行等宽排（末行不足一排时用 `fixed_w` 对齐，不拉宽）。

改图的时候只改内容，别改坐标——坐标是算出来的。

产出
----
    dataflow.svg   纯图，可单独嵌到别处（/dataflow.svg 也读这一份）
    dataflow.html  套了一层吸顶条 + 纸张边框的版本；看板的「本项目代码执行流程」
                   入口（`GET /dataflow`）读的就是它，只在 `</body>` 前补返回浮标

用法
----
    python3 scripts/gen_dataflow.py                      # 写回仓库根目录（默认）
    python3 scripts/gen_dataflow.py --out /tmp/flow      # 写到别处比对
    python3 scripts/gen_dataflow.py --check              # 只比对：仓库那份是不是最新的

`--check` 是给「改完代码忘了重画图」用的：它把图重新渲染一遍和磁盘上的比，
不一样就退出码 1，并列出先改的那个文件。CI 或提交前顺手跑一下即可。

依赖：只用标准库（不要求装浏览器、不要求联网）。图里的字体是系统 CJK 字体，
量宽是估算（`_cw` 那张表），所以只要文字内容不变，两次生成的产物逐字节相同。
"""
from __future__ import annotations

import argparse
from pathlib import Path

#: 图右下角那个日期：内容核对到哪一天。改了内容记得一起改。
VER = "2026-11-20"

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 画布常量
W = 1720
M = 40
CW = W - 2 * M           # 1640
FONT = ("Noto Sans CJK SC, Source Han Sans SC, WenQuanYi Micro Hei, "
        "Microsoft YaHei, PingFang SC, sans-serif")
MONO = "ui-monospace, SFMono-Regular, Menlo, monospace"

INK = "#22303c"
BODY = "#1f2328"
MUTED = "#5b6570"
FAINT = "#8a949e"
LINE = "#e3e6e8"
PAPER = "#fdfcfa"

#: 盒子配色：底色 / 描边 / 标题字 / monospace 副标题 / bullet 圆点。
#: 一套颜色对应一类东西（上游 / 数据层 / 表 / 工具 / 旁路 / 看板），
#: 换色就是换「这类东西长什么样」，九个章节共用，别在一节里单独调。
PAL = {
    "up":     dict(bg="#f6f1e7", bd="#c3ae8b", title="#6b5636", mono="#9a6700", dot="#c3ae8b"),
    "layer":  dict(bg="#eaf2ee", bd="#8bb3a4", title="#2f6152", mono="#3f7a67", dot="#8bb3a4"),
    "db":     dict(bg="#ffffff", bd="#b9c2cc", title="#33414f", mono="#5b6570", dot="#b9c2cc"),
    "tool":   dict(bg="#eef3f8", bd="#9db8d0", title="#1f4e79", mono="#3f6a92", dot="#9db8d0"),
    "bypass": dict(bg="#f7f2ea", bd="#cbb894", title="#7a5c2e", mono="#9a7233", dot="#cbb894"),
    "web":    dict(bg="#ffffff", bd="#bda8cd", title="#5b4a70", mono="#7a6790", dot="#bda8cd"),
    "rule":   dict(bg="#ffffff", bd="#9db8d0", title="#1f4e79", mono="#3f6a92", dot="#9db8d0"),
}


# ================================================================ 排版引擎
# ---------------------------------------------------------------- 量宽 / 折行
def _cw(ch: str) -> float:
    """单字宽度（相对字号）。只求「不挤不空」，不追求和真实字体逐像素一致。"""
    o = ord(ch)
    if o < 128:
        if ch == " ":
            return 0.30
        if ch in "iljI.:,;!'|":
            return 0.32
        if ch in "()[]{}":
            return 0.38
        if ch in "MW@%":
            return 0.82
        if ch.isupper():
            return 0.66
        return 0.56
    if ch == "·":
        return 0.52
    return 1.0


def measure(s: str, size: float) -> float:
    return sum(_cw(c) for c in s) * size


def _tokens(s: str) -> list[str]:
    """ASCII 连续串（MA20 / ≥ / -5% / RPT_XXX）当一个整体，不从中间断。"""
    out, buf = [], ""
    for ch in s:
        if ord(ch) < 128 and ch not in " ":
            buf += ch
        else:
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
    if buf:
        out.append(buf)
    return out


def wrap(s: str, size: float, width: float) -> list[str]:
    lines, cur, w = [], "", 0.0
    for tk in _tokens(s):
        tw = measure(tk, size)
        if cur and w + tw > width:
            lines.append(cur.rstrip())
            cur, w = ("" if tk == " " else tk), (0.0 if tk == " " else tw)
        else:
            cur += tk
            w += tw
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or [""]


# ---------------------------------------------------------------- SVG 画布
class Canvas:
    """只攒字符串；高度最后才知道，所以先画后开 `<svg>`。"""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def rect(self, x, y, w, h, *, fill, stroke="none", rx=8, sw=1.2, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def line(self, x1, y1, x2, y2, *, stroke=LINE, sw=1.2, arrow=False, color_marker=None):
        m = f' marker-end="url(#{color_marker or "arrow"})"' if arrow else ""
        self.parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{sw}"{m}/>')

    def text(self, x, y, s, *, size=10, fill=BODY, weight=400, anchor="start",
             mono=False, ls=None):
        if not s:
            return
        fam = f' font-family="{MONO}"' if mono else ""
        lsp = f' letter-spacing="{ls}"' if ls else ""
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'font-weight="{weight}" text-anchor="{anchor}"{fam}{lsp}>{s}</text>')

    def svg(self, h: int) -> str:
        defs = ('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
                'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                '<path d="M 0 1 L 9 5 L 0 9 z" fill="#8a949e"/></marker>'
                '<marker id="arrow-v" viewBox="0 0 10 10" refX="9" refY="5" '
                'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                '<path d="M 0 1 L 9 5 L 0 9 z" fill="#bda8cd"/></marker></defs>')
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" '
                f'viewBox="0 0 {W} {h}" font-family="{FONT}">{defs}')
        body = f'<rect x="0" y="0" width="{W}" height="{h}" fill="{PAPER}"/>'
        return head + body + "".join(self.parts) + "</svg>"


# ---------------------------------------------------------------- 内容块
class Block:
    """一个盒子：标题 + monospace 副标题 + 若干条。

    条目三型：`("h", …)` 小标题、`("b", …)` 带圆点的要点、`("t", …)` 灰字说明。
    高度按折行结果算，所以同排盒子会被拉齐到最高的那个（`draw_row` 负责）。
    """

    def __init__(self, title, items=(), *, sub=None, tone="db", size=10, lineh=17.6,
                 pad=12):
        self.title = title
        self.sub = sub
        self.items = list(items)
        self.p = PAL[tone]
        self.size = size
        self.lineh = lineh
        self.pad = pad
        self.tone = tone

    def height(self, w: float) -> float:
        inner = w - 2 * self.pad
        h = self.pad + 15                                   # 标题行
        if self.sub:
            h += 3 + 13
        for kind, txt in self.items:
            if kind == "h":
                h += 6 + 15
            elif kind == "b":
                h += max(1, len(wrap(txt, self.size, inner - 11))) * self.lineh
            else:
                h += max(1, len(wrap(txt, self.size + .4, inner))) * self.lineh
        return h + self.pad - 4

    def draw(self, c: Canvas, x, y, w, h):
        p, pad = self.p, self.pad
        c.rect(x, y, w, h, fill=p["bg"], stroke=p["bd"], rx=8)
        cy = y + pad + 11
        c.text(x + pad, cy, self.title, size=11.6 if self.tone != "db" else 11,
               fill=p["title"], weight=700)
        if self.sub:
            cy += 13.5
            c.text(x + pad, cy, self.sub, size=9.6, fill=p["mono"], mono=True)
        cy += 4
        for kind, txt in self.items:
            if kind == "h":
                cy += 15
                c.text(x + pad, cy, txt, size=self.size + .4, fill=p["title"], weight=700)
                cy += 1
            elif kind == "b":
                cy += self.lineh
                c.text(x + pad, cy - 0.6, "·", size=self.size + 2, fill=p["dot"], weight=700)
                for i, ln in enumerate(wrap(txt, self.size, w - 2 * pad - 11)):
                    if i:
                        cy += self.lineh
                    c.text(x + pad + 10, cy, ln, size=self.size, fill=BODY)
            else:
                for ln in wrap(txt, self.size + .4, w - 2 * pad):
                    cy += self.lineh
                    c.text(x + pad, cy, ln, size=self.size + .4, fill=MUTED)


def draw_row(c, blocks, y, *, x=M, total=CW, gap=16, h=None, fixed_w=None):
    """等宽一行，返回行高。

    `fixed_w` 给了就按该宽度排——末行只有两三块时用它，盒子不会突然变宽；
    不给就按 `total` 平分。
    """
    if fixed_w:
        w, hh = fixed_w, (h or max(b.height(fixed_w) for b in blocks))
    else:
        n = len(blocks)
        w = (total - gap * (n - 1)) / n
        hh = h or max(b.height(w) for b in blocks)
    for i, b in enumerate(blocks):
        b.draw(c, x + i * (w + gap), y, w, hh)
    return hh


# ---------------------------------------------------------------- 章节标题 / 竖箭头
def section(c, y, title, sub="", *, arrow=False):
    c.text(M, y, title, size=14, fill="#33414f", weight=700, ls="0.5")
    if sub:
        c.text(M + measure(title, 14) + 18, y, sub, size=11, fill=MUTED)
    return y


def vgap(c, y, label="", *, h=44, x=860):
    """两节之间的竖箭头 + 旁注（写在箭头右边，别压在别的文字上）。"""
    c.line(x, y, x, y + h - 6, stroke=FAINT, sw=1.5, arrow=True)
    if label:
        c.text(x + 12, y + h / 2 + 4, label, size=10, fill=MUTED)
    return y + h


# ================================================================ 内容
# ---------------------------------------------------------------- ① 上游
def s_upstream(c, y):
    section(c, y, "① 上游数据源",
            "只有这一层联网；一次取数只走一条通道，限频 / 断连按源分别退避")
    y += 16
    row1 = [
        Block("东财 push2his", [
            ("b", "一次给全历史，K 线主源之一"),
            ("b", "突发配额用完会整段拒连 TCP"),
        ], sub="日K（不复权 fqt=0）· 沪深300", tone="up"),
        Block("东财 datacenter", [
            ("b", "估值周频 PE/PB/PS"),
            ("b", "按日全市场横截面（价/PE/PB/PS/市值）"),
            ("b", "分页 1000 行/页"),
        ], sub="RPT_VALUEANALYSIS_DET", tone="up"),
        Block("腾讯自选股 westock", [
            ("b", "kline 多码批量（首选）"),
            ("b", "财报 / 分红 / 行情 / 档案"),
            ("b", "全市场横截面 scan"),
        ], sub="MCP 通道 · 按返回行数计费", tone="up"),
        Block("腾讯 web · 通达信", [
            ("b", "fqkline：raw 真值，无成交额"),
            ("b", "tdx：要 OAuth，独立进程 401"),
        ], sub="K 线的第二、第三条腿", tone="up"),
        Block("深交所 · baostock", [
            ("b", "深交所：逐日 0/1，日历权威腿"),
            ("b", "baostock：本机黑名单，不可用"),
        ], sub="日历与交叉校验", tone="up"),
    ]
    y += draw_row(c, row1, y) + 14

    row2 = [
        Block("旁路① 全市场快照的源", [
            ("b", "东财 datacenter 一天一份全市场：6 页 × 1000 行"),
            ("b", "沪深300 日K 一次（给 /extreme 第五张卡）"),
            ("b", "翻到第 30 页说明拿到的不是「一天」，直接报错"),
        ], sub="stock_extreme_point · 自己抓，不经 hub", tone="bypass"),
        Block("旁路② A股大势的源", [
            ("b", "东财 push2his 周K：f61 就是周换手率（中证不给）"),
            ("b", "中证 index-perf：日频收盘 + 滚动 PE，一次 970KB"),
            ("b", "东财国债 RPTA_WEB_TREASURYYIELD：10Y，2015 起"),
            ("b", "乐咕破净统计：2005 至今全A破净家数/总数"),
        ], sub="stock_market_trend · 四个源，全部标准库", tone="bypass"),
    ]
    return y + draw_row(c, row2, y)


# ---------------------------------------------------------------- ② 数据层
def s_layer(c, y):
    section(c, y, "② 共享数据层 stock_data_fetch",
            "唯一的联网出口：六道闸 → 路由 → 节流 → 覆盖判定；命中即返回，缺才打上游")
    y += 16
    inner = [
        Block("preflight 六道闸", [
            ("b", "盘中（联网才拦）· 库可用 · 版本=6"),
            ("b", "PRAGMA wal · 日历 · 磁盘空间"),
            ("b", "库够用就随时能跑"),
        ], tone="layer"),
        Block("router + PREFERRED", [
            ("b", "kline：westock → 东财 → 腾讯 → tdx"),
            ("b", "冷却中的源排到后面，不摘掉"),
            ("b", "换源只补洞，不覆盖已有 bar"),
            ("b", "同域主源连败 3 次 → 进程内降级"),
        ], tone="layer"),
        Block("throttle 令牌桶", [
            ("b", "按返回行数计（不是请求数）"),
            ("b", "westock 20 万行/分 · 东财 1.5 万行/分"),
            ("b", "另有最小间隔：westock 1s · 东财 0.4s"),
            ("b", "单次 take 预算 300s"),
        ], tone="layer"),
        Block("coverage.hit()", [
            ("b", "fp + partial=0 + end ≥ need"),
            ("b", "+ 无阻塞洞 + n ≥ min_points"),
            ("b", "停牌股的 permanent 洞不阻塞"),
        ], tone="layer"),
        Block("失败语义", [
            ("b", "InsufficientDepth 绝不降级返回部分"),
            ("b", "HoleUnreachable 唯一可逐股跳过"),
            ("b", "partial 不推进 end_date"),
        ], tone="layer"),
    ]
    iw = (CW - 20 - 4 * 12) / 5
    ih = max(b.height(iw) for b in inner)
    c.rect(M, y, CW, ih + 24, fill="#eaf2ee", stroke="#8bb3a4", rx=10)
    for i, b in enumerate(inner):
        b.draw(c, M + 10 + i * (iw + 12), y + 12, iw, ih)
    y += ih + 24 + 16
    c.text(M, y + 10, "日历 state/trading_calendar.json（12 年 · 2900+ 个交易日）→ "
                      "hub.target：15:05 之后今天才算「已收盘」；日历落后 >3 天只告警不阻断",
           size=10.4, fill=MUTED)
    return y + 5


# ---------------------------------------------------------------- ③ shared.db
SHARED_TABLES = [
    ("stock", "档案：名称 / 行业 / 上市日 / 是否金融"),
    ("kline", "日K raw，每股留 400 根；量已换成股"),
    ("finance", "三表一期一行，43 期，_Q 与 TTM 并列"),
    ("finance_revision", "上游改数留痕（相对差 >1e-6）"),
    ("dividend", "一股一除权日，每股口径，只增不删"),
    ("valuation", "周频 PE_TTM / PB_MRQ / PS_TTM，≥430 周"),
    ("quote", "当期行情：价 / 流通市值 / 总股本"),
    ("snapshot", "mainop / consensus / rating 三子域"),
    ("scan", "一天一份全市场横截面，留 30 天"),
    ("coverage", "覆盖水位：fp / 洞 / partial / n"),
    ("watermark", "取数水位（按域与范围）"),
    ("meta", "换源 / 降级 / 分红修复留痕"),
    ("annotation", "人工垄断标注：质量报告与 /review 读它"),
    ("price", "全市场当日价与估值（旁路① 写原件）"),
    ("index_price", "沪深300 当日收盘与涨跌幅（旁路① 写）"),
    ("market_index_week", "沪深300 周K + 周换手率（旁路② 写）"),
    ("market_index_perf", "沪深300 日频收盘 + 滚动 PE（旁路② 写）"),
    ("market_macro", "10Y 国债 / 破净占比两条序列（旁路② 写）"),
]


def s_shared_db(c, y):
    section(c, y, "③ shared.db（原件层 · 18 张表）",
            "只存不复权原值；财报 / 分红 append-only；数据与 coverage 同事务")
    y += 16
    per, gap = 6, 12
    bw = (CW - gap * (per - 1)) / per
    for i in range(0, len(SHARED_TABLES), per):
        chunk = SHARED_TABLES[i:i + per]
        blocks = [Block(t, [("t", d)], tone="db", size=9.6, pad=10) for t, d in chunk]
        y += draw_row(c, blocks, y, total=CW, gap=gap, fixed_w=bw) + gap
    y -= gap
    c.text(M, y + 18, "18 张里 13 张由 ② 写；price / index_price / market_* 这 5 张由两个旁路直接写——"
                      "它们不走 hub，只借这一层「同一个文件、同一套路径约定」",
           size=10.2, fill=MUTED)
    return y + 6


# ---------------------------------------------------------------- ④ 四个工具
def s_tools(c, y):
    section(c, y, "④ 四个裁决工具（各自的裁决点）",
            "顺序有依赖：选股是名单源头；批量时下游三个读「基准日 = 那一天」的名单")
    y += 16
    inner = [
        Block("选股 stock_kline_screen", [
            ("h", "定位：谁刚刚启动（量价）"),
            ("b", "粗筛：K=n+1=5 个交易日里任一天涨幅 ∈ [0.5%, 20.5%] → 池 ≈ 4300 只"),
            ("b", "排除：名称含 ST；次新（上市<3 年）只对幸存者惰性查"),
            ("b", "启动日 d*：连续收盘>MA20 回扫 ≤20 根，且前一日未站上 MA20（真上穿）；需 ≥71 根"),
            ("b", "⑦ age ≤4 │ ② 放量 ≥1.5× │ ③ d* 涨幅 ∈[0.5,20.5]%"),
            ("b", "④ MA20 ≥ MA60×0.95 且 MA20 > 3 日前 ×0.99"),
            ("b", "换手分位只排序（--turn-pct 0），不删票"),
            ("b", "入选后标注：毛利率改善、一级行业、市值、分位星级（只展示，不裁决）"),
            ("t", "产出：名单（历史中位 ~18 只）· ./run.py select"),
        ], sub="./run.py select", tone="tool"),
        Block("周期弹性 stock_cycle_elastic", [
            ("h", "只给标签，不评分不排序"),
            ("b", "① 周期股：20 季毛利率 σ ≥5pp；或 CV ≥20% 且中位 ≥5% 且 σ ≥2pp"),
            ("b", "② 弹性：8 季 DOL（环比利润增速 ÷ 营收增速）中位 ≥2.0 → 强"),
            ("b", "　 不稳定：营收同比中位 <3%，或利润跨零 >1/3（取代强 / 弱）"),
            ("b", "③ 位置：产能周转率 40 季分位 <40% 低位、>60% 高位；40~60 不给标签"),
            ("b", "金融股位置标「不适用」；方向标「扩产中 / 产能在退」"),
            ("b", "窗口：取 44 季（40 季位置 + 4 季算同比）；<20 季标「历史不足」仍出报告"),
            ("b", "「弱」与「不稳定」严格分开：前者是知道且不强，后者只是算不出来"),
            ("t", "只加限定语，不改分位 · ./run.py cycle --stock 代码"),
        ], sub="./run.py cycle", tone="tool"),
        Block("质量初筛 stock_quality_screen", [
            ("h", "Q + V + 14 项风险 → 一句结论"),
            ("b", "Q100 = 资本回报 30 + 现金 25 + 资产负债表 20 + 成长 15 + 会计 10"),
            ("b", "V100 = 目标价空间 40% + 预测PE相对 20% + 净利增速 40%（无覆盖不算 V）"),
            ("b", "14 项风险（真伪 / 趋势 / 质量 / 安全）→ 命中 / 提示 / 已知"),
            ("b", "4 条致命直接回避：净资产为负、利息保障<1、商誉>50%、3 年经营现金流为负且净负债/EBIT>4"),
            ("b", "结论 = Q×V 矩阵 9 种；Q<45 一律回避；一致性闸门：命中 ≥2 项，买入类降为回避"),
            ("b", "数据前置不满足（机构覆盖不足 / 关键检查缺失）→ 观察，等待数据完整"),
            ("t", "机构预期 / 十年分位 / 垄断标注都在报告里给依据 · ./run.py quality"),
        ], sub="./run.py quality", tone="tool"),
        Block("顶部判断 stock_judge_top", [
            ("h", "四因子各自出结论，不合计"),
            ("b", "动量：位置 pos250（−5% 内=高位）+ 速度 ret20 → 「位置·速度」"),
            ("b", "换手：amount ÷ 流通市值；缺市值退成交额 250 日分位"),
            ("b", "业绩：季报同比　│　量价：pos250 + 量能"),
            ("b", "旁证（不参与结论）：估值十年分位 / 机构目标价空间 / 应收存货现金流反证"),
            ("b", "周期股追加「周期位置」卡；<30 根报错，<250 根只告警"),
            ("b", "口径：价格一律 raw（与选股同口径）；商誉查不到，改存货增速做第二项反证"),
            ("t", "单只 / --from-select 批量 / --csv 清单 · ./run.py top sh600519"),
        ], sub="./run.py top", tone="tool"),
    ]
    w = (CW - 20 - 3 * 12) / 4
    hh = max(b.height(w) for b in inner)
    c.rect(M, y, CW, hh + 24, fill="#eef3f8", stroke="#9db8d0", rx=10)
    for i, b in enumerate(inner):
        b.draw(c, M + 10 + i * (w + 12), y + 12, w, hh)
    return y + hh + 24


# ---------------------------------------------------------------- ⑤ results.db
RESULT_TABLES = [
    ("run", "台账：tool / target_date / kind(batch|single) / n_ok / n_fail / argv"),
    ("select_result", "名单 + 换手分位 + 市值 + 行业 + 标注"),
    ("cycle_result", "三个标签 + σ / DOL / 分位"),
    ("quality_result", "Q / V / 结论 / 风险计数"),
    ("top_result", "四因子状态 + pos250 + 目标价"),
    ("market_regime", "一周一行：趋势 / ERP / 破净 / 量能 + payload（旁路② 写）"),
    ("price", "全市场当日价与估值（旁路① 复制的看板副本）"),
    ("price_stat", "一天一行：涨跌家数 / 涨跌停 / 中位数"),
    ("index_price", "沪深300 当日收盘与涨跌幅"),
]


def s_results_db(c, y):
    section(c, y, "⑤ results.db（派生结论层 · 9 张表）",
            "四个工具 + 两个旁路自动写，写失败只 warn 一行；每条结果带 payload = detail + report_html")
    y += 16
    per, gap = 6, 12
    bw = (CW - gap * (per - 1)) / per
    for i in range(0, len(RESULT_TABLES), per):
        chunk = RESULT_TABLES[i:i + per]
        blocks = [Block(t, [("t", d)], tone="db", size=9.6, pad=10) for t, d in chunk]
        y += draw_row(c, blocks, y, total=CW, gap=gap, fixed_w=bw) + gap
    y -= gap
    c.text(M, y + 18, "payload.detail ≈ 4–5KB 摘要（旧行没存 report_html 时降级用）；"
                      "payload.report_html = 工具自己渲染的整份 HTML（gzip+base64，约原文 1/2），看板只解压、不重画",
           size=10.2, fill=MUTED)
    c.text(M, y + 36, "↑ 反向依赖：④ 的周期 / 质量 / 顶部批量时读 select_result 的名单（--from-select）；"
                      "market_regime 按「基准日」一周一行，重跑覆盖当周、跨周追加",
           size=10.2, fill=MUTED)
    return y + 14


# ---------------------------------------------------------------- ⑥ 看板
WEB_PAGES = [
    ("/batches", "首页：批次表——四工具跑没跑、缺哪格、怎么补跑；顶上是 A股大势横幅"),
    ("/select", "选股：拖框 / 点行业 / 点市值筛名单（名称不可点）"),
    ("/cycle", "周期：σ × 弹性散点 + 产能分位 + 需求端限定语"),
    ("/quality", "质量：Q × V 散点 + 结论分布"),
    ("/top", "顶部：距 250 日高点最近 20 只 + 四因子矩阵"),
    ("/review", "综合评估：四表并起来算赔率分 + 三条超级成长股标识"),
    ("/extreme", "冷热极点：选交易日看全市场 + 单票历史曲线"),
    ("/trend", "大势历史：按基准日查每周的 趋势 / ERP / 破净 / 量能"),
    ("/book", "原书学习：《成功投资的五条法则》全书页（自包含 HTML）"),
    ("/stock/<代码>", "单票：原版报告三标签（顶部 / 质量 / 周期），?focus= 决定先看哪份"),
]


def s_web(c, y):
    section(c, y, "⑥ 看板 ./run.py web",
            "只读：results.db，加 shared.db 的 annotation 表（人工标注）；`/` 直接进首页")
    y += 16
    per, gap = 5, 14
    for i in range(0, len(WEB_PAGES), per):
        chunk = WEB_PAGES[i:i + per]
        blocks = [Block(p, [("t", d)], tone="web", size=9.6, pad=10) for p, d in chunk]
        y += draw_row(c, blocks, y, total=CW, gap=gap) + gap
    y -= gap
    c.text(M, y + 18, "人看页面 → 决定先研究哪几只（所有工具都不下买卖结论）；"
                      "导航 9 项按取用顺序排，`/stock/<代码>` 从表格里的名称点进去",
           size=10.2, fill=MUTED)
    return y + 6


# ---------------------------------------------------------------- ⑦ 旁路①
def s_extreme(c, y):
    section(c, y, "⑦ 旁路①：全市场快照 stock_extreme_point",
            "独立工具：不 import 数据层、不过 schema 版本闸，只借文件路径约定；./run.py extreme --date YYYY-MM-DD")
    y += 16
    blocks = [
        Block("东财横截面", [
            ("b", "RPT_VALUEANALYSIS_DET"),
            ("b", "按日 6 页 × 1000 行"),
            ("b", "+ 沪深300 日K 1 次"),
        ], sub="一天一份，约 6 个请求", tone="bypass"),
        Block("抓取 / 清洗", [
            ("b", "剔 B 股（900 / 200 开头）"),
            ("b", "市值单位换算"),
            ("b", "混进别的交易日直接报错"),
        ], sub="缺失一律 NULL，不填 0", tone="bypass"),
        Block("shared.db", [
            ("b", "price + index_price"),
            ("b", "原件：整天整份替换"),
            ("b", "（DELETE + INSERT 同事务）"),
        ], sub="丢了用 --force 重抓", tone="bypass"),
        Block("results.db", [
            ("b", "price + price_stat"),
            ("b", "看板用的复制品"),
            ("b", "+ 同事务摘要；--sync 可补"),
        ], sub="写失败原件仍在", tone="bypass"),
        Block("/extreme 冷热极点", [
            ("b", "涨跌家数 / 涨跌停"),
            ("b", "PE·PB·PS 中位数 / 沪深300"),
            ("b", "单票全时点曲线 + 东财外链"),
        ], sub="看板第五张卡读 index_price", tone="web"),
    ]
    y += draw_row(c, blocks, y, gap=14)
    c.text(M, y + 16, "约 5579 只 ×（收盘价 / 涨跌幅 / PE(TTM) / PB(MRQ) / PS(TTM) / 总市值 / 流通市值）；"
                      "涨跌停阈值 主板 9.8 · 双创 19.8 · 北交所 29.8 · ST 4.8；中位数只取正值",
           size=10.2, fill=MUTED)
    return y + 4


# ---------------------------------------------------------------- ⑧ 旁路②
def s_trend(c, y):
    section(c, y, "⑧ 旁路②：A股大势 stock_market_trend",
            "独立工具：抓原件 → 三路判定 → 一周一行写结果库；./run.py trend [--date] [--recompute]")
    y += 16
    blocks = [
        Block("抓四路原件", [
            ("b", "东财周K（f61 = 周换手率）"),
            ("b", "中证 index-perf（日频 PE）"),
            ("b", "东财 10Y 国债 · 乐咕破净"),
        ], sub="四个源，全部标准库", tone="bypass"),
        Block("shared.db 三张 raw", [
            ("b", "market_index_week"),
            ("b", "market_index_perf"),
            ("b", "market_macro"),
        ], sub="只存上游原值，主键 upsert", tone="bypass"),
        Block("三路判定 rules.py", [
            ("b", "趋势 / 赔率 / 情绪"),
            ("b", "纯函数，不碰网络与库"),
            ("b", "三路各算各的，不合成总分"),
        ], sub="阈值全是分位，不是固定数字", tone="bypass"),
        Block("降级与 stale", [
            ("b", "任一路失败 → 上次原件"),
            ("b", "结果行标 stale=1"),
            ("b", "三路全无才退出码 2"),
        ], sub="--recompute 不联网重算", tone="bypass"),
        Block("/trend + 首页横幅", [
            ("b", "market_regime 一周一行"),
            ("b", "首页只摆最近 8 周"),
            ("b", "完整历史进 /trend"),
        ], sub="只作背景板，不参与选股", tone="web"),
    ]
    y += draw_row(c, blocks, y, gap=14)
    c.text(M, y + 16, "data_asof 取三路里最旧的那一天并照实标「数据截至」；中证滞后 1–2 个交易日是常态；"
                      "乐咕是页面上的固定 token，变了就 403（只这一路降级）",
           size=10.2, fill=MUTED)
    return y + 4


# ---------------------------------------------------------------- ⑨ 明细
#: （标题, 副标题, 条目）。前四块讲四个工具的判据，后四块讲综合评估 / 标识 /
#: 冷热口径 / 大势口径——按「看结论时最先被追问的那几条」排序。
DETAILS = [
    ("选股：七条规则与逐股去向", "stock_kline_screen/rules.py · params.py", [
        ("b", "⑦ 新鲜度：age = S − d* ≤ N（每周 N=4）"),
        ("b", "② 放量：vcross = 量(d*) ÷ 前 5 日均量 ≥ 1.5"),
        ("b", "③ 日频：d* 在覆盖窗口内，且当日涨幅 ∈ [0.5, 20.5]%"),
        ("b", "④ 均线：MA20(d*) ≥ MA60×0.95；MA20(d*) > MA20(d*−3)×0.99"),
        ("b", "⑤ 风控：d* 与 S 各查一次——非 ST、非次新（<3 年）"),
        ("b", "⑥ 未破坏：d*+1..S 收盘均在 MA20 上方（定义性，恒不触发）"),
        ("b", "去向只有三种：FS（定位失败）/ JD（裁决失败）/ TN（换手硬过滤）"),
        ("b", "收尾断言：粗筛池 = 入选 + Σ剔除，且两边无交集"),
        ("b", "换手：20 日均额 ÷ S 日流通市值；分位 <20/40/60 给 ★★★ / ★★ / ★"),
        ("b", "门槛：MIN_HISTORY=70（含当日 ≥71 根），不足直接判 FS"),
    ]),
    ("周期：三判据与三档", "stock_cycle_elastic/config.py · decide.py", [
        ("h", "① 是不是周期股（两条通道 OR）"),
        ("b", "绝对：20 季单季毛利率 σ ≥ 5.0pp"),
        ("b", "相对：CV = σ/|中位| ≥ 20%，且中位 ≥5%、σ ≥2.0pp"),
        ("h", "② 弹性（8 季环比 DOL 中位数）"),
        ("b", "≥2.0 强 / <2.0 弱 / 营收同比中位 <3% 或跨零 >1/3 → 不稳定"),
        ("h", "③ 周期位置（产能周转率 40 季分位）"),
        ("b", "<40% 低位 / >60% 高位 / 中间不给标签；金融股「不适用」"),
        ("h", "数据保护（不是判定阈值）"),
        ("b", "单季营收增速 <0.5% 则该季比值跳过；比值 >50 剔除；样本 <4 置空"),
        ("t", "判据来源：东北证券 5pp / 招商 30% 出清口径放宽到 40%"),
        ("h", "页面上的「· 订单回升」是另一回事"),
        ("b", "合同负债同比 ≥+20% / ≤−10%，且占年化营收 ≥2% 才认；只加限定语，不改分位与标签"),
    ]),
    ("质量：Q、V 与结论矩阵", "stock_quality_screen/config.py", [
        ("t", "Q（100）　资本回报30 · 现金25 · 资产负债表20 · 成长15 · 会计10"),
        ("t", "V（100）　目标价空间40% + 预测PE相对20% + 净利增速40%"),
        ("h", "结论矩阵（Q 越低，要求 V 越高）"),
        ("b", "Q≥75：V≥65 买入 / ≥50 等价格 / ≥35 偏贵 / <35 太贵"),
        ("b", "Q65–75：V≥80 买入 / ≥70 可买 / ≥50 观察 / <50 回避"),
        ("b", "Q55–65：V≥75 小仓 / ≥50 观察 / <50 回避"),
        ("b", "Q45–55：V≥80 试错 / ≥65 观察 / <65 回避；Q<45 回避"),
        ("h", "致命项之外只点名、不否决"),
        ("b", "现金短债比 <0.5、净现比 <0.3：写进结论行，不改结论"),
        ("b", "报告第一节的盈利结构三证据（定价权 / 资本回报 / 现金创造）与垄断标注只作说明，不进 14 项风险"),
    ]),
    ("顶部：四因子 + 三条旁证", "stock_judge_top（indicators / judgement / extras）", [
        ("b", "动量：pos250 定位置（−5% 内高位 / −20% 内中位 / 其余低位），ret20 定速度（过热 / 偏热 / 走平 / 偏弱）"),
        ("b", "换手：av20 与 ratio，分母用当期流通市值；缺市值改成交额分位"),
        ("b", "业绩：季报营收与利润同比，看「撑不撑得住」"),
        ("b", "量价：高位用 pos250——下跌中继不再被误判成高位盘整"),
        ("b", "旁证：估值十年分位、机构目标价与空间、应收 / 存货 / 现金流反证"),
        ("b", "纪律：四因子不合并、不打总分；周期股才追第五张「周期位置」卡"),
        ("b", "输出：四因子状态词 + headline，一句话说清「为什么不是顶部」"),
        ("b", "只有「高位 + 热」才叫「高位加速」，反弹不当顶部信号"),
    ]),
    ("综合评估：赔率分（/review）", "stock_review_assess/review.py · 满分 15", [
        ("h", "两条一票否决（不放宽）"),
        ("b", "Q < 45；或命中致命风险（利息保障<1、商誉超 50% 等）"),
        ("h", "加分"),
        ("b", "市值 <50亿 +3 / <100亿 +2 / <200亿 +1"),
        ("b", "估值分位均值 ≤25% +3 / ≤50% +2 / ≤70% +1"),
        ("b", "周期低位 +2 · 弹性强 +2（不稳定 +1）· 垄断标注 +2"),
        ("b", "Q≥80 +2 / ≥65 +1 · 目标价空间 ≥20% +1"),
        ("h", "扣分与分档"),
        ("b", "趋势走弱 −2 · V<55 −1；BUY_CUT=5 → 优先研究，其余观察"),
        ("b", "没有质量结果的那一批 → 待补数据；工具判「回避」的降档但不排除"),
    ]),
    ("超级成长股标识（三条，只做标识）", "stock_review_assess/gates.py · GATE_KEYS", [
        ("b", "① 盈利：扣非 TTM >0；扣非/归母 ≥80%；最新单季扣非不为负"),
        ("b", "② 增速：单季营收同比 ≥25%，且扣非增速 > 营收增速"),
        ("b", "③ 供需：毛利率环比连升两季，或 合同负债同比 ≥+20% 且占年化营收 ≥5%"),
        ("b", "三态：过 ✓ / 不过 ✗ / 未核实 ?——未核实不扣分、不降档、不进「提示」"),
        ("b", "不参与分档、不参与排序、也不把票拿掉；只在标识列 / 顶部三张卡 / tooltip 出现"),
        ("t", "原第 ④ 条「新东西」已下线：库里没有「新业务收入」字段，判断只能靠手写备注"),
    ]),
    ("冷热摘要：口径与硬约定", "stock_extreme_point/stat.py", [
        ("b", "涨跌停：主板 ≥9.8% / 双创 ≥19.8% / 北交所 ≥29.8% / ST ≥4.8%（北交所不套 ST 规则）"),
        ("b", "平盘：|涨跌幅| < 0.005；家数按全市场（约 5579 只）算"),
        ("b", "PE / PB / PS 中位数只取正值，亏损（负值）不参与；n_pe 记参与只数"),
        ("b", "缺失一律 NULL 不填 0；混进别的交易日直接报错"),
        ("b", "排序：点当前列反向；PE/PB/PS 排序时负值一律排最后"),
        ("b", "复制与摘要同事务；结果库写失败原件仍在，--sync 可补"),
        ("b", "翻页上限 30 页：翻到 30 页说明拿到的不是「一天的全市场」，直接报错"),
        ("b", "收盘宽限 15:05：盘中查「今天」上游返回空，CLI 报错并给最近一份的日期"),
    ]),
    ("A股大势：三路口径与阈值", "stock_market_trend/rules.py", [
        ("h", "趋势（沪深300 周线）"),
        ("b", "连续 2 周收盘 < MA20 → 偏弱；MA20 < MA60 且 MA60 低于 4 周前 → 空头条件"),
        ("b", "两条同时成立 → 空头确认；否则 MA20 > MA60 且价 > MA20 → 偏强，其余中性"),
        ("h", "赔率（ERP 分位）"),
        ("b", "ERP = 100/PE(TTM) − 10Y 国债，近 2400 个交易日：≥95 极值 / ≥80 便宜 / ≤20 偏贵"),
        ("h", "情绪（破净 + 地量）"),
        ("b", "全A破净率近 2400 日分位：≥90 恐慌 / ≥70 偏冷；周换手 ≤ 3 年 10% 分位 = 地量"),
        ("t", "三路不合成、不加权、不排序；只作背景板，不参与四个工具的任何判断"),
    ]),
]


def s_details(c, y):
    section(c, y, "⑨ 筛选条件明细",
            "阈值集中在各包 config.py 与 stock_review_assess/，改一处即可；这一节只列会改变结论的数")
    y += 16
    per, gap = 4, 14
    for i in range(0, len(DETAILS), per):
        chunk = DETAILS[i:i + per]
        blocks = [Block(t, items, tone="rule", sub=sub, size=9.8, lineh=16.4, pad=11)
                  for t, sub, items in chunk]
        # 第二行起换暖色底，读起来有分段感（也提示「后半段讲的是各工具自己的口径」）
        if i:
            for b in blocks:
                b.p = PAL["bypass"]
        y += draw_row(c, blocks, y, total=CW, gap=gap) + gap
    return y - gap


# ================================================================ 拼装
def build() -> str:
    """画完整张图，返回 SVG 文本（高度按内容算出来）。"""
    c = Canvas()
    y = 50.0
    c.text(M, y, "stock-value-invest-tool · 数据流转与筛选条件",
           size=24, fill=INK, weight=700)
    c.text(M, y + 24,
           "共享数据层按需取数 → 四个裁决工具各自定性 → 结论只进 results.db → 看板读库；"
           "两个旁路（全市场快照 / A股大势）各抓各的、各写各的表，不参与选股结论",
           size=12, fill=MUTED)
    c.line(M, 90, W - M, 90, stroke=LINE)

    y = s_upstream(c, 120)
    y = vgap(c, y + 20, "取数请求（按域 / 按股；阻塞在闸与节流上）")
    y = s_layer(c, y + 4)
    y = vgap(c, y + 20, "写入 + coverage 同事务")
    y = s_shared_db(c, y + 4)
    y = vgap(c, y + 24, "按需取数：ensure / ensure_many / scan_many")
    y = s_tools(c, y + 4)
    y = vgap(c, y + 20, "写 select / cycle / quality / top_result（失败只 warn）")
    y = s_results_db(c, y + 4)
    y = vgap(c, y + 24, "读库（只读连接）")
    y = s_web(c, y + 4)

    y = vgap(c, y + 26, "旁路一：./run.py extreme")
    y = s_extreme(c, y + 4)
    y = vgap(c, y + 26, "旁路二：./run.py trend")
    y = s_trend(c, y + 4)

    y = vgap(c, y + 26, "")
    y = s_details(c, y + 4)

    y += 26
    c.line(M, y, W - M, y, stroke=LINE)
    c.text(M, y + 24,
           "说明：只画「谁把什么写到哪」，不含全部参数与例外；阈值以各包 config.py 为准。",
           size=10, fill=MUTED)
    c.text(W - M, y + 24, f"{VER} · 按当前代码与 README 核对", size=10, fill=FAINT, anchor="end")
    return c.svg(int(y + 40))


#: 套在 SVG 外面的那层：吸顶条（标题 / 打印提示 / 纯 SVG 入口）+ 纸张边框。
#: 用 `%W%` `%SVG%` 占位而不是 `str.format`，因为 CSS 里满是花括号。
#: 吸顶条上「打开纯 SVG」是相对链接，页面挂在 /dataflow 时由看板的
#: `GET /dataflow.svg` 接住（见 web/app.py）。
HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>stock-value-invest-tool · 数据流转与筛选条件</title>
<style>
  :root { color-scheme: light; }
  html, body { margin: 0; padding: 0; background: #f2f1ee; color: #1f2328;
    font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif; }
  .bar { position: sticky; top: 0; z-index: 3; display: flex; gap: 14px;
    align-items: center; padding: 10px 18px; background: #ffffff;
    border-bottom: 1px solid #e3e6e8; font-size: 12.5px; color: #5b6570; }
  .bar b { color: #22303c; font-size: 13px; }
  .bar span { color: #8a949e; }
  .bar a { color: #2f6152; text-decoration: none; border-bottom: 1px dotted #8bb3a4; }
  .wrap { padding: 18px; }
  .paper { max-width: %W%px; margin: 0 auto; background: #fdfcfa;
    border: 1px solid #e3e6e8; border-radius: 10px; overflow: hidden;
    box-shadow: 0 1px 3px rgba(31,35,40,.06); }
  svg { display: block; width: 100%; height: auto; }
  @media print {
    @page { size: A3 landscape; margin: 8mm; }
    html, body { background: #ffffff; }
    .bar { display: none; }
    .wrap { padding: 0; }
    .paper { border: 0; box-shadow: none; max-width: none; }
  }
</style>
</head>
<body>
<div class="bar">
  <b>stock-value-invest-tool · 数据流转与筛选条件</b>
  <span>滚动查看 · Ctrl/⌘+P 可打印成 A3 横版 PDF</span>
  <a href="dataflow.svg">打开纯 SVG</a>
</div>
<div class="wrap"><div class="paper">
%SVG%
</div></div>
</body>
</html>
"""


def render() -> tuple[str, str]:
    """返回 (svg, html) 两份文本。"""
    svg = build()
    return svg, HTML.replace("%W%", str(W)).replace("%SVG%", svg)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 dataflow.svg / dataflow.html")
    ap.add_argument("--out", default=str(ROOT), help="输出目录（默认仓库根目录）")
    ap.add_argument("--check", action="store_true",
                    help="不写文件，只比对磁盘上的是不是最新的；不一样则退出码 1")
    args = ap.parse_args()

    out = Path(args.out)
    svg, html = render()

    if args.check:
        stale = []
        for name, text in (("dataflow.svg", svg), ("dataflow.html", html)):
            p = out / name
            if not p.is_file() or p.read_text(encoding="utf-8") != text:
                stale.append(name)
        if stale:
            print("过期（跑一遍 scripts/gen_dataflow.py 重画）:", " ".join(stale))
            return 1
        print("最新：图的产物与脚本内容一致")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    (out / "dataflow.svg").write_text(svg, encoding="utf-8")
    (out / "dataflow.html").write_text(html, encoding="utf-8")
    print(f"written: {out / 'dataflow.svg'} ({len(svg):,} 字节), "
          f"{out / 'dataflow.html'} ({len(html):,} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
