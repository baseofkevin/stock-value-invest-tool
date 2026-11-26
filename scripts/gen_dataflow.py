# -*- coding: utf-8 -*-
"""生成仓库根目录的 dataflow.html（数据流转与筛选条件全景图）。

为什么要写成脚本，而不是一张手画图
----------------------------------
页面上写的几乎全是**当前代码里的常量**：18 张表、9 张表、六道闸、K=5、
`KLINE_KEEP_BARS=400`、ERP 2400 日分位 95/80/20、BUY_CUT=5、GATE2 的 25%……
代码一动，页面就开始说谎；而手改一页两百多块文字是件没人愿意干的事，
改完也说不清哪句还对、哪句已经过期。所以这里把页面拆成「渲染层 + 内容」：

* 渲染层只做一件事：把 `Section / Row / Block / Inv` 四型结构吐成 HTML，
  排版交给 CSS（Grid 定列数、折行交给浏览器，不再自己量字宽）。
* 内容是九个 `s_*` 函数，一个章节一个函数，改哪节翻哪节。
* 每条要点就是一行文本，想改措辞直接改字符串，不用管坐标。

为什么从「一张 1720×2754 的 SVG」改成 HTML
------------------------------------------
老版本把整页画成一张固定宽度的 SVG，套上 `svg{width:100%}`：画布 1720 宽、
正文 9.6px，在 1400 宽的屏幕上等比缩到 0.82 倍，实际可读字号不到 8px——
字小不是字号设小了，是**整张图被缩放**了，而且浏览器放大也救不了（越放大
横向越要拖）。改成 HTML 之后正文 14px 起步，字号与画布宽度脱钩，缩放、
选中复制、Ctrl+F、窄窗口换行、打印分页都由浏览器自己处理好。

产出
----
    dataflow.html  自包含一页（样式内联，无外链、无网络请求）；看板的
                   「本项目代码执行流程」入口（`GET /dataflow`）读的就是它，
                   只在 `</body>` 前补一个返回浮标（见 web/app.py）

只产 HTML。早年还有一个 `dataflow.svg`（纯 SVG 副本）和接住它的
`/dataflow.svg` 路由，2026-11-23 随「整页改 HTML」一并删掉了：既然字号已经
和画布宽度脱钩，再留一份 SVG 只会是同一份内容的第二处真相。

用法
----
    python3 scripts/gen_dataflow.py                      # 写回仓库根目录（默认）
    python3 scripts/gen_dataflow.py --out /tmp/flow      # 写到别处比对
    python3 scripts/gen_dataflow.py --check              # 只比对：仓库那份是不是最新的

`--check` 是给「改完代码忘了重画」用的：它把页面重新渲染一遍和磁盘上的比，
不一样就退出码 1，并列出先改的那个文件。CI 或提交前顺手跑一下即可。

依赖：只用标准库。产物是纯字符串拼接，内容不变则两次生成逐字节相同。
"""
from __future__ import annotations

import argparse
from html import escape as _esc
from pathlib import Path

#: 页脚那个日期：内容核对到哪一天。改了内容记得一起改。
VER = "2026-11-23"

ROOT = Path(__file__).resolve().parent.parent

TITLE = "stock_value_invest_tool · 数据流转与筛选条件"
LEAD = ("共享数据层按需取数 → 四个裁决工具各自定性 → 结论只进 results.db → 看板读库；"
        "两个旁路（全市场快照 / A股大势）各抓各的、各写各的表，不参与选股结论")
FOOT_L = "说明：只画「谁把什么写到哪」，不含全部参数与例外；阈值以各包 config.py 为准。"
FOOT_R = f"{VER} · 按当前代码与 README 核对"

#: 盒子配色：底色 / 描边 / 标题字 / monospace 副标题 / bullet 圆点。
#: 一套颜色对应一类东西（上游 / 数据层 / 表 / 工具 / 旁路 / 看板），
#: 换色就是换「这类东西长什么样」，九个章节共用，别在一节里单独调。
#: 渲染层会按这张表生成 `.tone-*` 的 CSS 变量与章节徽章配色，改这里就够了。
PAL = {
    "up":     dict(bg="#faf5ea", bd="#dfcfb2", title="#6b5636", mono="#9a6700", dot="#c9b28a"),
    "layer":  dict(bg="#eef5f1", bd="#a9c7ba", title="#2f6152", mono="#3f7a67", dot="#9dc0b2"),
    "db":     dict(bg="#ffffff", bd="#d5dbe2", title="#33414f", mono="#5b6570", dot="#c3cad2"),
    "tool":   dict(bg="#f1f6fa", bd="#b6cade", title="#1f4e79", mono="#3f6a92", dot="#adc3d8"),
    "bypass": dict(bg="#fbf6ee", bd="#ddcdae", title="#7a5c2e", mono="#9a7233", dot="#d3bd93"),
    "web":    dict(bg="#ffffff", bd="#d9cde3", title="#5b4a70", mono="#7a6790", dot="#c9b8d6"),
    "rule":   dict(bg="#ffffff", bd="#b6cade", title="#1f4e79", mono="#3f6a92", dot="#adc3d8"),
}


# ================================================================ 内容结构
class Block:
    """一张卡片：标题 + monospace 副标题 + 若干条。

    条目三型，按顺序渲染（顺序有意义，别合并）：
        ("h", …)  段内小标题（加粗、无圆点）
        ("b", …)  带圆点的要点
        ("t", …)  灰字说明 / 结论行
    短条目不必手写折行——浏览器会折，这里一个字都不量。
    """

    def __init__(self, title, items=(), *, sub=None, tone="db"):
        self.title = title
        self.sub = sub
        self.items = list(items)
        self.tone = tone


class Inv:
    """清单卡：一批「名字 + 一句话」成对列出（表名、页面路径）。

    为什么不用卡片：18 张表做成 18 个盒子，盒子会被挤到 200px 宽、每行只搁
    十几个字，一屏里全是小的等高方块，反而看不清。清单是两列「名字 | 说明」
    的行，一列 9 行，边扫边对，这是表格该有的样子。
    """

    def __init__(self, items, *, tone="db", cols=2):
        self.items = list(items)
        self.tone = tone
        self.cols = cols


class Row:
    """一行，里面是卡片（Block）或清单（Inv）。

    `cols` 不给就按这一行的块数等分——**别再交给 `auto-fit` 自己算**：
    六块的整数行在 1400 宽的屏幕上会被算成 5 列，第 6 块掉到下一行单独一个，
    看着像漏了。窄屏时由 CSS 的媒体查询接管，退回自动换行。
    """

    def __init__(self, blocks, *, tone=None, cols=None):
        self.blocks = list(blocks)
        self.tone = tone          # 给了就覆盖整行卡片的配色
        self.cols = cols


class Section:
    """一章：`① 上游数据源` + 若干行卡片 + 若干条脚注 + 通往下一章的箭头旁注。

    `nav` 是目录里那个短标签（章节标题太长，粘在顶上会占满两行）。
    `after` 是本章画完、箭头指着的那句话——它说明「下一步发生了什么」，
    和老 SVG 版里两个章节之间的竖箭头旁注是同一句话。
    """

    def __init__(self, sid, num, title, sub="", *, nav, rows=(), notes=(), tone="layer",
                 after=""):
        self.sid = sid
        self.num = num
        self.title = title
        self.sub = sub
        self.nav = nav
        self.rows = list(rows)
        self.notes = list(notes)
        self.tone = tone
        self.after = after


# ================================================================ 内容
# ---------------------------------------------------------------- ① 上游
def s_upstream():
    return Section(
        "s1", "①", "上游数据源",
        "只有这一层联网；一次取数只走一条通道，限频 / 断连按源分别退避",
        nav="① 上游", tone="up",
        after="取数请求（按域 / 按股；阻塞在闸与节流上）",
        rows=[
            Row([
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
            ]),
            Row([
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
            ]),
        ])


# ---------------------------------------------------------------- ② 数据层
def s_layer():
    return Section(
        "s2", "②", "共享数据层 stock_data_fetch",
        "唯一的联网出口：六道闸 → 路由 → 节流 → 覆盖判定；命中即返回，缺才打上游",
        nav="② 数据层", tone="layer",
        after="写入 + coverage 同事务",
        rows=[
            Row([
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
            ]),
        ],
        notes=["日历 state/trading_calendar.json（12 年 · 2900+ 个交易日）→ "
               "hub.target：15:05 之后今天才算「已收盘」；日历落后 >3 天只告警不阻断"])


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
    ("scan", "一天一份全市场横截面，留 120 天"),
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


def s_shared_db():
    return Section(
        "s3", "③", "shared.db（原件层 · 18 张表）",
        "只存不复权原值；财报 / 分红 append-only；数据与 coverage 同事务",
        nav="③ shared.db", tone="db",
        after="按需取数：ensure / ensure_many / scan_many",
        rows=[Row([Inv(SHARED_TABLES)])],
        notes=["18 张里 13 张由 ② 写；price / index_price / market_* 这 5 张由两个旁路直接写——"
               "它们不走 hub，只借这一层「同一个文件、同一套路径约定」"])


# ---------------------------------------------------------------- ④ 四个工具
def s_tools():
    return Section(
        "s4", "④", "四个裁决工具（各自的裁决点）",
        "顺序有依赖：选股是名单源头；批量时下游三个读「基准日 = 那一天」的名单",
        nav="④ 工具", tone="tool",
        after="写 select / cycle / quality / top_result（失败只 warn）",
        rows=[
            Row([
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
                    ("b", "旁证（不参与结论）：估值十年分位 / 机构目标价空间 / 应收存货现金流反证 / 周期状态"),
                    ("b", "不另出「周期位置」卡（2026-11-24 下线）；周期状态只进旁证表；<30 根报错，<250 根只告警"),
                    ("b", "口径：价格一律 raw（与选股同口径）；商誉查不到，改存货增速做第二项反证"),
                    ("t", "单只 / --from-select 批量 / --csv 清单 · ./run.py top sh600519"),
                ], sub="./run.py top", tone="tool"),
            ]),
        ])


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


def s_results_db():
    return Section(
        "s5", "⑤", "results.db（派生结论层 · 9 张表）",
        "四个工具 + 两个旁路自动写，写失败只 warn 一行；每条结果带 payload = detail + report_html",
        nav="⑤ results.db", tone="db",
        after="读库（只读连接）",
        rows=[Row([Inv(RESULT_TABLES)])],
        notes=[
            "payload.detail ≈ 4–5KB 摘要（旧行没存 report_html 时降级用）；"
            "payload.report_html = 工具自己渲染的整份 HTML（gzip+base64，约原文 1/2），看板只解压、不重画",
            "↑ 反向依赖：④ 的周期 / 质量 / 顶部批量时读 select_result 的名单（--from-select）；"
            "market_regime 按「基准日」一周一行，重跑覆盖当周、跨周追加",
        ])


# ---------------------------------------------------------------- ⑥ 看板
WEB_PAGES = [
    ("/batches", "首页：批次表——四工具跑没跑、缺哪格、怎么补跑；顶上是 A股大势横幅"),
    ("/select", "选股：拖框 / 点行业 / 点市值筛名单（名称不可点）"),
    ("/cycle", "周期：σ × 弹性散点 + 产能分位 + 需求端限定语"),
    ("/quality", "质量：Q × V 散点 + 结论分布"),
    ("/top", "顶部：距 250 日高点最近 20 只 + 四因子矩阵"),
    ("/review", "综合评估：四表并起来算赔率分 + 三条超级成长股标识"),
    ("/extreme", "冷热极点：选交易日看全市场 + 单票历史曲线"),
    ("/trend", "大势历史：顶部换手率 × 周收盘双轴图；按基准日查每周的 趋势 / ERP / 破净 / 量能"),
    ("/book", "原书学习：《成功投资的五条法则》全书页（自包含 HTML）"),
    ("/stock/<代码>", "单票：原版报告三标签（顶部 / 质量 / 周期），?focus= 决定先看哪份"),
]


def s_web():
    return Section(
        "s6", "⑥", "看板 ./run.py web",
        "只读：results.db，加 shared.db 的 annotation 表（人工标注）；`/` 直接进首页",
        nav="⑥ 看板", tone="web",
        after="旁路一：./run.py extreme",
        rows=[Row([Inv(WEB_PAGES, tone="web")])],
        notes=["人看页面 → 决定先研究哪几只（所有工具都不下买卖结论）；"
               "导航 9 项按取用顺序排，`/stock/<代码>` 从表格里的名称点进去",
               "另有 `/dataflow`（首页 hero 右上角那个入口）：就是本页，仓库根的 dataflow.html，"
               "由 scripts/gen_dataflow.py 生成，自包含 HTML、不拉任何外链"])


# ---------------------------------------------------------------- ⑦ 旁路①
def s_extreme():
    return Section(
        "s7", "⑦", "旁路①：全市场快照 stock_extreme_point",
        "独立工具：不 import 数据层、不过 schema 版本闸，只借文件路径约定；"
        "./run.py extreme --date YYYY-MM-DD",
        nav="⑦ 旁路①", tone="bypass",
        after="旁路二：./run.py trend",
        rows=[
            Row([
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
            ]),
        ],
        notes=["约 5579 只 ×（收盘价 / 涨跌幅 / PE(TTM) / PB(MRQ) / PS(TTM) / 总市值 / 流通市值）；"
               "涨跌停阈值 主板 9.8 · 双创 19.8 · 北交所 29.8 · ST 4.8；中位数只取正值"])


# ---------------------------------------------------------------- ⑧ 旁路②
def s_trend():
    return Section(
        "s8", "⑧", "旁路②：A股大势 stock_market_trend",
        "独立工具：抓原件 → 三路判定 → 一周一行写结果库；./run.py trend [--date] [--recompute]",
        nav="⑧ 旁路②", tone="bypass",
        rows=[
            Row([
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
                    ("b", "量能五档：M = 近 52 周中位，但地量固定 1%"),
                    ("b", "缩量 <0.9M / 平稳 0.9~1.1M / 放量 ≥1.1M / 天量 ≥1.5M"),
                ], sub="趋势与赔率看分位；量能看相对水位", tone="bypass"),
                Block("降级与 stale", [
                    ("b", "任一路失败 → 上次原件"),
                    ("b", "结果行标 stale=1"),
                    ("b", "三路全无才退出码 2"),
                ], sub="--recompute 不联网重算", tone="bypass"),
                Block("/trend + 首页横幅", [
                    ("b", "market_regime 一周一行"),
                    ("b", "顶部换手率 × 周收盘双轴图"),
                    ("b", "首页只摆最近 8 周"),
                    ("b", "完整历史进 /trend"),
                ], sub="只作背景板，不参与选股", tone="web"),
            ]),
        ],
        notes=["data_asof 取三路里最旧的那一天并照实标「数据截至」；中证滞后 1–2 个交易日是常态；"
               "乐咕是页面上的固定 token，变了就 403（只这一路降级）",
               "周中跑的那一次，东财会给一根「当周至今」的 bar（换手只累计到当天）："
               "rules.split_partial() 把它摘掉，改用上一完整周判，页面上画空心点标「未走完」"])


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
    ("顶部：四因子 + 四条旁证", "stock_judge_top（indicators / judgement / extras）", [
        ("b", "动量：pos250 定位置（−5% 内高位 / −20% 内中位 / 其余低位），ret20 定速度（过热 / 偏热 / 走平 / 偏弱）"),
        ("b", "换手：av20 与 ratio，分母用当期流通市值；缺市值改成交额分位"),
        ("b", "业绩：季报营收与利润同比，看「撑不撑得住」"),
        ("b", "量价：高位用 pos250——下跌中继不再被误判成高位盘整"),
        ("b", "旁证：估值十年分位、机构目标价与空间、应收 / 存货 / 现金流反证、周期状态"),
        ("b", "纪律：四因子不合并、不打总分；周期状态只进旁证表，不另出卡片"),
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
        ("h", "情绪（破净 + 周换手五档）"),
        ("b", "全A破净率近 2400 日分位：≥90 恐慌 / ≥70 偏冷"),
        ("b", "地量 ≤1% / 缩量 <0.9M / 平稳 0.9~1.1M / 放量 ≥1.1M / 天量 ≥1.5M"),
        ("b", "M = 近 52 周换手中位；除 1% 这个地板，另三条线逐周现算、跟着水位漂"),
        ("b", "周中「本周至今」那根摘掉不判，按上一完整周判（档位晚一周，不会高估）"),
        ("t", "三路不合成、不加权、不排序；只作背景板，不参与四个工具的任何判断"),
    ]),
]


def s_details():
    rows = []
    for i in range(0, len(DETAILS), 4):
        blocks = [Block(t, items, sub=sub, tone="rule")
                  for t, sub, items in DETAILS[i:i + 4]]
        # 第二行起换暖色底，读起来有分段感（也提示「后半段讲的是各工具自己的口径」）
        rows.append(Row(blocks, tone=None if i == 0 else "bypass"))
    return Section(
        "s9", "⑨", "筛选条件明细",
        "阈值集中在各包 config.py 与 stock_review_assess/，改一处即可；这一节只列会改变结论的数",
        nav="⑨ 明细", tone="rule", rows=rows)


def sections() -> list[Section]:
    return [s_upstream(), s_layer(), s_shared_db(), s_tools(), s_results_db(),
            s_web(), s_extreme(), s_trend(), s_details()]


# ================================================================ 渲染
def esc(s: str) -> str:
    """正文里的 `<35 太贵` `商誉>50%` 必须转义，否则浏览器当标签吃掉。"""
    return _esc(s, quote=False)


def tone_css() -> str:
    """把 PAL 摊成 CSS 变量：卡片、章节徽章共用一套色。"""
    out = []
    for k, v in PAL.items():
        out.append(f".tone-{k}{{--c-bg:{v['bg']};--c-bd:{v['bd']};"
                   f"--c-title:{v['title']};--c-mono:{v['mono']};--c-dot:{v['dot']}}}")
        out.append(f".sec.tone-{k} .num{{background:{v['bg']};border-color:{v['bd']};"
                   f"color:{v['title']}}}")
    return "\n".join(out)


def r_block(b: Block, tone: str | None = None) -> str:
    """一张卡片。连续的 `b` 条目合成一个 `<ul>`，遇到 h / t 就断开。"""
    parts: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            parts.append('<ul>' + "".join(f"<li>{esc(x)}</li>" for x in buf) + "</ul>")
            buf.clear()

    for kind, txt in b.items:
        if kind == "b":
            buf.append(txt)
        else:
            flush()
            parts.append(f'<p class="{"ih" if kind == "h" else "it"}">{esc(txt)}</p>')
    flush()

    mono = b.title.startswith("/")
    h3 = (f'<h3 class="mono">{esc(b.title)}</h3>' if mono
          else f"<h3>{esc(b.title)}</h3>")
    sub = f'<p class="sub">{esc(b.sub)}</p>' if b.sub else ""
    return (f'<article class="card tone-{tone or b.tone}">{h3}{sub}'
            f'{"".join(parts)}</article>')


def r_inv(v: Inv) -> str:
    """清单卡：两列「名字 | 说明」，一列 N 行；窄屏由 CSS 退回一列。

    最后一行的条目不带分隔线（`class="last"`）——只有行数不是列数整数倍时，
    才能看出哪几个是「最后一行」，所以这个判断放在这里做，不交给 CSS。
    """
    n = len(v.items)
    tail = n % v.cols or v.cols
    cells = []
    for i, (k, d) in enumerate(v.items):
        cls = "ii last" if i >= n - tail else "ii"
        cells.append(f'<div class="{cls}"><dt>{esc(k)}</dt><dd>{esc(d)}</dd></div>')
    return (f'<article class="card inv tone-{v.tone}">'
            f'<dl class="ig" style="--inv:{v.cols}">{"".join(cells)}</dl></article>')


def r_row(row: Row) -> str:
    cards = [r_inv(b) if isinstance(b, Inv) else r_block(b, row.tone) for b in row.blocks]
    n = row.cols or len(cards)
    return f'<div class="row" style="--n:{n}">{"".join(cards)}</div>'


def r_section(s: Section) -> str:
    sub = f'<span class="sub">{esc(s.sub)}</span>' if s.sub else ""
    out = [f'<section class="sec tone-{s.tone}" id="{s.sid}">',
           "<details open>",
           '<summary><span class="num" aria-hidden="true">' + esc(s.num) + "</span>"
           f'<span class="hd"><span class="ttl">{esc(s.title)}</span>{sub}</span>'
           '<span class="chev" aria-hidden="true"></span></summary>',
           '<div class="body">']
    out += [r_row(r) for r in s.rows]
    out += [f'<p class="note">{esc(n)}</p>' for n in s.notes]
    out += ["</div>", "</details>", "</section>"]
    return "".join(out)


def r_flow(label: str) -> str:
    """两个章节之间那条竖箭头 + 旁注：说明「下一步发生了什么」。"""
    return ('<div class="flow"><span class="rail" aria-hidden="true"></span>'
            f'<span class="tag">{esc(label)}</span></div>')


def build_body() -> str:
    out = []
    for s in sections():
        out.append(r_section(s))
        if s.after:
            out.append(r_flow(s.after))
    return "\n".join(out)


def build_nav() -> str:
    return "".join(f'<a href="#{s.sid}">{esc(s.nav)}</a>' for s in sections())


def build() -> str:
    """整页 HTML。样式内联，无外链、无网络请求。"""
    return (HTML.replace("%TONE%", tone_css())
                .replace("%NAV%", build_nav())
                .replace("%BODY%", build_body())
                .replace("%TITLE%", esc(TITLE))
                .replace("%LEAD%", esc(LEAD))
                .replace("%FOOT_L%", esc(FOOT_L))
                .replace("%FOOT_R%", esc(FOOT_R)))


#: 页面骨架。用 `%XXX%` 占位而不是 `str.format`，因为 CSS 里满是花括号。
#: 排版约定（改字号只改这里，不要动内容）：
#:   正文 14px / 行高 1.62，卡片标题 15.5px，章节标题 19px，页面标题 26px。
#:   三档字号即可，再多就没层次了。
HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%TITLE%</title>
<style>
  :root { color-scheme: light;
    --ink:#22303c; --body:#1f2328; --muted:#5b6570; --faint:#8a949e;
    --line:#e3e6e8; --line2:#eef0f2; --paper:#fdfcfa; --bg:#f2f1ee;
    --accent:#2f6152; --mono: ui-monospace, SFMono-Regular, Menlo, "DejaVu Sans Mono", monospace; }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body { margin:0; background:var(--bg); color:var(--body);
    font-family:"Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei","PingFang SC",sans-serif;
    font-size:15px; line-height:1.65; -webkit-font-smoothing:antialiased; }
  .wrap { max-width:1560px; margin:0 auto; padding:26px 26px 72px; }

  /* ---------- 页头 ---------- */
  .hero { padding:4px 0 16px; border-bottom:1px solid var(--line); }
  .hero h1 { margin:0 0 10px; font-size:26px; line-height:1.35; color:var(--ink); letter-spacing:.2px; }
  .hero p { margin:0; max-width:118ch; color:var(--muted); font-size:14.5px; }
  .hero .meta { margin-top:10px; display:flex; gap:14px; flex-wrap:wrap; color:var(--faint); font-size:12.5px; }
  .hero .meta b { color:var(--muted); font-weight:600; }

  /* ---------- 目录（吸顶） ---------- */
  .toc { position:sticky; top:0; z-index:20; display:flex; flex-wrap:wrap; gap:7px;
    align-items:center; padding:9px 0 10px; margin-bottom:4px;
    background:rgba(242,241,238,.95); backdrop-filter:blur(8px);
    border-bottom:1px solid var(--line); }
  .toc a { display:inline-flex; align-items:center; padding:4px 11px; border-radius:999px;
    background:#fff; border:1px solid var(--line); color:#3b4653; font-size:13px;
    text-decoration:none; white-space:nowrap; transition:.12s; }
  .toc a:hover { border-color:#a9c7ba; color:var(--accent); background:#f4f9f6; }
  .toc .sep { margin-left:auto; display:flex; gap:6px; }
  .toc button { font:inherit; font-size:12.5px; padding:4px 10px; border-radius:8px;
    background:#fff; border:1px solid var(--line); color:var(--muted); cursor:pointer; }
  .toc button:hover { border-color:#a9c7ba; color:var(--accent); }

  /* ---------- 章节 ---------- */
  .sec { margin:22px 0 0; scroll-margin-top:58px; }
  .sec > details { border:1px solid var(--line); border-radius:12px; background:#fff;
    overflow:hidden; }
  .sec > details > summary { display:flex; align-items:baseline; gap:11px; cursor:pointer;
    padding:10px 15px; list-style:none; user-select:none; background:#fff; }
  .sec > details > summary::-webkit-details-marker { display:none; }
  .sec > details[open] > summary { border-bottom:1px solid var(--line2); }
  .sec > details > summary:hover { background:#fcfdfc; }
  .num { flex:none; display:inline-flex; align-items:center; justify-content:center;
    width:25px; height:25px; border-radius:8px; border:1px solid var(--line);
    font-size:14px; font-weight:700; transform:translateY(3px); }
  /* 标题与副标题同属一块：窗口窄时副标题换到标题下面，不去挤标题 */
  .hd { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; min-width:0; }
  .ttl { font-size:19px; font-weight:700; color:var(--ink); letter-spacing:.2px;
    white-space:nowrap; }
  .sec > details > summary .sub { font-size:13px; color:var(--muted); line-height:1.5; }
  .chev { margin-left:auto; flex:none; width:9px; height:9px; border-right:2px solid var(--faint);
    border-bottom:2px solid var(--faint); transform:rotate(45deg) translateY(-2px);
    transition:.18s; }
  .sec > details:not([open]) .chev { transform:rotate(-45deg) translateX(-2px); }
  .body { padding:14px 15px 15px; background:#f6f7f7; }
  .sec > details:not([open]) > .body { display:none; }

  /* ---------- 卡片 ---------- */
  .row { display:grid; gap:12px; grid-template-columns:repeat(var(--n,4), minmax(0,1fr)); }
  .row + .row { margin-top:12px; }
  .card { background:var(--c-bg,#fff); border:1px solid var(--c-bd,var(--line));
    border-radius:12px; padding:13px 15px 14px; box-shadow:0 1px 2px rgba(31,35,40,.045); }
  .card h3 { margin:0; font-size:15.5px; line-height:1.4; color:var(--c-title,#33414f);
    font-weight:700; letter-spacing:.2px; }
  .card h3.mono { font-family:var(--mono); font-size:14.5px; letter-spacing:0; }
  .card .sub { margin:3px 0 0; font-family:var(--mono); font-size:12px; line-height:1.45;
    color:var(--c-mono,#5b6570); word-break:break-word; }
  .card ul { margin:9px 0 0; padding:0; list-style:none; }
  .card li { position:relative; padding-left:14px; font-size:14px; line-height:1.62; }
  .card li + li { margin-top:4px; }
  .card li::before { content:"·"; position:absolute; left:3px; top:-1px;
    font-weight:700; font-size:16px; color:var(--c-dot,#c3cad2); }
  .card .ih { margin:12px 0 0; font-size:13.5px; font-weight:700; line-height:1.5;
    color:var(--c-title,#33414f); }
  .card .it { margin:9px 0 0; font-size:13.5px; line-height:1.6; color:var(--muted); }
  .card h3 + .it, .card h3 + ul, .card .sub + .it, .card .sub + ul { margin-top:8px; }

  /* ---------- 清单卡（表名 / 页面路径） ---------- */
  .card.inv { padding:11px 16px 12px; }
  .ig { margin:0; display:grid; gap:0 34px;
    grid-template-columns:repeat(var(--inv,2), minmax(0,1fr)); }
  .ii { display:flex; gap:12px; align-items:baseline; padding:6px 0;
    border-bottom:1px solid var(--line2); }
  .ii.last { border-bottom:0; }
  .ii dt { flex:0 0 148px; margin:0; font-family:var(--mono); font-size:13px;
    font-weight:700; color:var(--c-title,#33414f); line-height:1.5; word-break:break-word; }
  .ii dd { margin:0; font-size:13.5px; line-height:1.55; color:#495460; }

  /* ---------- 脚注 / 章节间箭头 ---------- */
  .note { margin:11px 0 0; padding:8px 14px; background:#fff; font-size:13.5px;
    line-height:1.7; color:#4a5560; border:1px solid var(--line2);
    border-left:3px solid var(--c-bd,var(--line)); border-radius:0 8px 8px 0; }
  .flow { display:flex; align-items:center; gap:12px; margin:13px 0 0 34px; }
  .flow .rail { position:relative; flex:none; width:2px; height:24px;
    background:linear-gradient(#dfe4e8,#c9d2d9); border-radius:2px; }
  .flow .rail::after { content:""; position:absolute; left:-4px; bottom:-6px;
    border:5px solid transparent; border-top-color:#c9d2d9; }
  .flow .tag { font-size:13px; color:var(--muted); background:#fff; border:1px solid var(--line);
    padding:2px 11px; border-radius:999px; }

  /* ---------- 页脚 ---------- */
  .foot { margin-top:34px; padding-top:14px; border-top:1px solid var(--line);
    display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap;
    font-size:13px; color:var(--muted); }
  .foot .r { color:var(--faint); }

%TONE%

  @media print {
    @page { size:A4 portrait; margin:12mm; }
    html, body { background:#fff; font-size:11pt; }
    .wrap { max-width:none; padding:0; }
    .toc, .hero .meta { display:none; }
    .sec { break-inside:auto; }
    .sec > details { border-color:#d8d8d8; }
    .sec > details > summary { break-after:avoid; }
    .body { background:#fff !important; }
    /* 卡片默认不许跨页；但 18 张表的清单卡比一页还高，必须允许它断，
       否则整块被推到下一页，上一页留半个白页。断的时候以「行」为单位。 */
    .card { box-shadow:none; break-inside:avoid; }
    .card.inv { break-inside:auto; }
    .ii { break-inside:avoid; }
    .note { break-inside:avoid; }
    .hero { break-after:avoid; }
  }
  /* 窄屏：放弃「每行固定几块」，退回自动换行；列宽下限 240px，两列清单退成一列 */
  @media (max-width:1240px) {
    .row { grid-template-columns:repeat(auto-fit, minmax(min(100%, 240px), 1fr)); }
    .ig { grid-template-columns:repeat(auto-fit, minmax(min(100%, 380px), 1fr)); }
    .ii dt { flex-basis:132px; }
  }
  @media (max-width:640px) {
    .wrap { padding:16px 14px 48px; }
    .hero h1 { font-size:21px; }
    .ttl { font-size:17px; white-space:normal; }
    .hd { display:block; }
    .hd .sub { display:block; margin-top:2px; }
    .ii { display:block; }
    .ii dt { flex-basis:auto; margin-bottom:1px; }
  }
</style>
</head>
<body>
<div class="wrap">

<header class="hero">
  <h1>%TITLE%</h1>
  <p>%LEAD%</p>
  <div class="meta"><b>阅读</b><span>正文 14px · 章可折叠</span>
    <b>打印</b><span>Ctrl/⌘+P 出 A4 竖版 PDF（自动展开全部章节）</span></div>
</header>

<nav class="toc" aria-label="章节">
  %NAV%
  <span class="sep">
    <button type="button" data-act="open">全部展开</button>
    <button type="button" data-act="close">全部收起</button>
  </span>
</nav>

<main>
%BODY%
</main>

<div class="foot">
  <span>%FOOT_L%</span>
  <span class="r">%FOOT_R%</span>
</div>

</div>
<script>
(function () {
  var all = function () { return document.querySelectorAll('main details'); };
  // 目录跳转：目标章节要是收起的，先展开再跳，不然点了像没反应
  document.querySelectorAll('.toc a').forEach(function (a) {
    a.addEventListener('click', function () {
      var el = document.querySelector(a.getAttribute('href'));
      if (el && el.tagName === 'DETAILS') { el.open = true; }
    });
  });
  document.querySelectorAll('.toc button').forEach(function (b) {
    b.addEventListener('click', function () {
      var open = b.dataset.act === 'open';
      all().forEach(function (d) { d.open = open; });
    });
  });
  // 打印前全部展开：折叠状态的内容打不出来
  window.addEventListener('beforeprint', function () {
    all().forEach(function (d) { d.open = true; });
  });
})();
</script>
</body>
</html>
"""


def render() -> str:
    return build()


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 dataflow.html")
    ap.add_argument("--out", default=str(ROOT), help="输出目录（默认仓库根目录）")
    ap.add_argument("--check", action="store_true",
                    help="不写文件，只比对磁盘上的是不是最新的；不一样则退出码 1")
    args = ap.parse_args()

    out = Path(args.out)
    html = render()
    name = "dataflow.html"

    if args.check:
        p = out / name
        if not p.is_file() or p.read_text(encoding="utf-8") != html:
            print(f"过期（跑一遍 scripts/gen_dataflow.py 重画）: {name}")
            return 1
        print("最新：页面与脚本内容一致")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(html, encoding="utf-8")
    print(f"written: {out / name} ({len(html):,} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
