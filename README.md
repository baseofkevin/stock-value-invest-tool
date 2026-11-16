# stock-value-invest-tool

个人股票研究工具箱：每周五收盘后跑一遍选股 + 质量 + 周期，买入后按需查顶部。
四个工具的结论都写进 `results.db`，用 `./run.py web` 打开看板读。

四个功能模块，共用一套本地数据层（`stock_data_fetch` + `shared.db`）——库里有的
数据直接用，没有的才联网，拿到的写回库，下次（乃至其它三个模块）复用。

| 模块 | 干什么 | 入口 |
|---|---|---|
| `stock_kline_screen` | 选股：5 日涨幅池 → K 线形态 → 板块/财务过滤 | `run.py select` |
| `stock_cycle_elastic` | 周期弹性：是不是周期股 / 弹性强弱 / 周期位置 | `run.py cycle [--stock 代码]` |
| `stock_quality_screen` | 质量初筛：质量分 Q + 估值分 V + 14 项风险检查 | `run.py quality [--stock 代码]` |
| `stock_judge_top` | 顶部判断：四因子观察股票是否到顶部 | `run.py top <代码>` / `top --from-select` |

## 每周五收盘后（15:10 之后）

```bash
cd /home/kevin/MyData/stock-value-invest-tool
./run.py all
```

`./run.py` 裸跑**只打印帮助，什么也不跑**——全流程要三十多分钟，误敲一个回车就
开跑代价太大。想跑什么必须显式写出来；不记得写什么就 `./run.py -h`。

想按菜单选着跑（比如今天只想重跑质量初筛）：

```bash
./run.py menu
```

它会依次做完六步，最后**直接把看板起起来**（`./run.py all` 一个命令走到底）：

```
⓪ build_calendar        更新交易日历（日历过期会让下游静默产出上一周的报告）
① stock_kline_screen    选股       → results.db / select_result
② stock_cycle_elastic   周期弹性   → results.db / cycle_result
③ stock_quality_screen  质量初筛   → results.db / quality_result
④ stock_judge_top       顶部判断   → results.db / top_result
⑤ ./run.py web          看板       → http://127.0.0.1:5000（Ctrl-C 结束）
```

**屏幕上只有一条进度条。** 每步一行，跑完定格成结果：

```
① 选股（2026-11-13）  [████████████████████████] 6/6   ✅ 38 只 · 21.2s
② 周期弹性（2026-11-13）  [██████░░░░░░░░░░░░░░░░░] 12/38   ⚠️ 35 只 · 失败 3 只 · 1.4s
③ 质量初筛（2026-11-13）  [████████████████████████] 38/38  ✅ 38 只 · 22.6s
④ 顶部判断（38 只 @ 2026-11-13）  [████████████████████] 36/38  ✅ 38 只 · 18.2s

⚠️ ② 周期弹性 有失败，其余步骤完成 · 63.4s
日志 logs/run-20261115-173000.log
```

- 进度条认的是工具打的 `[3/38]`（逐只）和 `[2/6]`（选股的分阶段）
- 长阶段（选股要逐条裁决 4300 只）不打进度行时，进度条会转圈并显示"已经跑了多久"
- **其余输出全部只进日志**：逐只失败、`↻ 上游返回空，重试`、报表、剔除明细、
  聚合汇总都在 `logs/run-<时间戳>.log` 里，一行不少（一次全流程约 250 行）
- 重定向到文件/管道时不画条，每步一行，输出同样只有上面那几行

想看屏幕上的原始输出（排查时用）：

```bash
./run.py all --verbose               # 不画条，子进程输出全部打出来
./run.py select --date 2026-11-13    # 单跑某一步，照样打完整报表
```

日志是事后排查的唯一依据：哪只票失败、为什么失败、重试了几次，都在里面。
名单与结论本身在 `results.db` 里，看板读它（`./run.py web`）。

**某一步个别股票失败不会中断流程。** 判据是"有没有产出"：35 只成功、3 只因
上游缺数据失败，继续往下跑；一只都没成功（名单对不上、库被锁）才停。
要"任何失败都停"：

```bash
./run.py all --strict
```

不想起看板（放进定时任务、或在没有浏览器的机器上跑）：

```bash
./run.py all --no-web
./run.py all --port 8080          # 换端口
```

**四个工具不再产出 CSV 和 HTML**（2026-11-15 起）。结论只有一处：`results.db`。

顶部判断单独跑（买入之后用来判断股价有没有到顶部区域的，单只、随时跑）：

```bash
./run.py top sh600519                      # 基准日自动取「今天（过了 15:05）」
./run.py top sh600519 --date 2026-11-13
./run.py top sh600519 sz000001             # 一次多只

./run.py top --from-select                 # 批量：把当天的选股结果整体扫一遍
./run.py top --csv 我的持仓.csv            # 批量：自己给清单（读「代码」列）
./run.py quality --csv 我的持仓.csv        # 质量/周期也能直接吃 CSV（与 --stock 二选一）
./run.py quality --csv 我的持仓.csv --workers 8   # 大名单提并发（默认 4）
./run.py cycle --csv 我的持仓.csv
./run.py backup                            # 备份两个数据库
```

批量结果同样进 `results.db`，看板的「顶部」页按「距 250 日高点」从近到远排，
一眼看出谁贴着一年高点。

批量中途 Ctrl-C，已完成的票会先落库；重跑同一份名单会跳过它们。
要全部重算：quality/cycle 加 `--force`，top 加 `--refresh`。

## 单独跑某一步

```bash
./run.py select  --date 2026-11-20         # 只选股（只能批量）
./run.py cycle                             # 周期弹性批量（用库里的选股名单）
./run.py quality                           # 质量初筛批量（用库里的选股名单）
./run.py web                               # 只打开看板
```

日历没有单独的子命令——它只是全流程的第 0 步，`all` 会在过期时自己更新。
真要单独更新（比如换了数据源想重生成），直接：

```bash
python3 -m stock_data_fetch.build_calendar
```

### 单只也能跑

周期弹性和质量初筛都支持单只，不需要当天的名单：

```bash
./run.py cycle   --stock 600519            # 周期弹性：这一只是不是周期股、位置在哪
./run.py quality --stock 600519            # 质量初筛：Q/V 分数与风险检查
./run.py top     600519                    # 顶部判断
```

菜单里也是同样的：选 3 或 4，股票代码处**直接回车 = 批量跑当天名单**，填代码 = 只跑那一只。

注意单只模式的基准日由程序自己决定（今天过了 15:05 就用今天，否则上一个交易日），
**不接受 `--date`**——那两个程序没有日期参数。要按历史某天出报告只能走批量模式。

子进程参数可以透传：`./run.py quality --stock 600519 --force`。

下游（cycle/quality/top）的名单来自 `results.db` 里那一天的选股结果
（`--from-select <日期>`），所以基准日必须和那天的选股一致；不给 `--date`
就按「今天（过了 15:05）」算，和 `stock_data_fetch` 内部的 `target_date()`
同一套规则。名单在库里对不上就直接退 2，不会拿别的日期凑数。

## 几个容易踩的坑（脚本已经替你处理）

1. **日历不更新 → 静默产出旧报告。** 目标日按「今天是交易日且过了 15:05」决定，
   而它先在日历里查今天；日历只到上周五时，会自动回落到上周五，文件名和目录名
   都看不出来。所以 `run.py` 在工作日发现日历过期会先跑一次 `build_calendar`，
   显式 `--date` 不在日历里则直接报错（不会猜"最近的交易日"）。
2. **盘中不该出结论。** 15:05 之前跑会退到上一个交易日——拿半天的数据算
   「收盘站上 MA20」本身就是错的。
3. **`cycle_report.py` / `stock_report.py` 必须在各自目录里跑**（顶层 `import config`），
   直接 `python -m stock_cycle_elastic.cycle_report` 会 `ModuleNotFoundError`。
   `run.py` 已经把 cwd 切好。
4. **选股 0 只时下游没有输入**，脚本在交接处停下，不会带着空名单跑到一半才炸。

日志：每次运行写 `logs/run-<时间戳>.log`（完整子进程输出，事后可查）。
仓库里已经没有 `output/` 了——报告、CSV、HTML 都不再落盘，结论只进 `results.db`。

## 两个数据库

| 文件 | 装什么 | 丢掉了会怎样 |
|---|---|---|
| `shared.db` | **上游数据**：K 线、财务、估值、覆盖表（`coverage`） | 只能重新联网抓，几十分钟 |
| `results.db` | **派生结论**：四个工具每次跑出来的名单与档位 | 重跑一遍就有（几分钟） |

分开的理由：`shared.db` 有严格的不变量（只追加 raw、覆盖与数据同事务），
`maintain.py --apply` 会 VACUUM 并清 coverage。结论如果也躺在里面，
一次清理就可能把上周的名单一起带走，备份也会白白多拷一份。

`results.db` 每工具一张表（`select_result` / `cycle_result` / `quality_result` /
`top_result`）+ 一张 `run` 台账，每条结果都带 `payload`（完整 JSON）。
四个工具**自动写**，写失败只 warn 一行、绝不影响出报告。看板只读它，
所以"页面上的"和"库里的"永远是同一份东西，不存在导出对不上的问题。

```bash
python3 -m stock_data_fetch.results                    # 各工具最近一次运行
python3 -m stock_data_fetch.results --tool top         # 只看顶部判断
python3 -m stock_data_fetch.results --tool quality --symbol sh600019   # 一只票的历史
python3 -m stock_data_fetch.results --prune            # 每工具只留最近一次运行
```

跨工具的联合查询就是一个普通 JOIN（这是一张库而不是四个库的原因）：

```sql
select t.symbol, t.name, s.pool_turnover_rank, q.conclusion, t.momentum_state
from (select * from top_result   where run_id=(select max(id) from run where tool='top'))   t
left join (select * from select_result  where run_id=(select max(id) from run where tool='select'))  s on s.symbol=t.symbol
left join (select * from quality_result where run_id=(select max(id) from run where tool='quality')) q on q.symbol=t.symbol;
```

## 看板（`./run.py web`）

Flask + Chart.js，单进程、只读、无登录。默认只监听 `127.0.0.1:5000`，
要换端口 `./run.py web --port 8080`。启动后会打印实际地址。

导航就六页（顺序 = 取用顺序）：

| 页面 | 看什么 |
|---|---|
| `/batches` 批次 | 每个基准日一行，四个工具各跑没跑、跑了几只、几点跑的；点一格进那一批 |
| `/select` 选股 | 换手分位 × 20 日换手散点，行业分布（名称**不可点**：这页看的是名单和排序依据） |
| `/cycle` 周期 | 毛利率 σ × 弹性散点，产能分位 |
| `/quality` 质量 | Q × V 散点，结论分布 |
| `/top` 顶部 | 距 250 日高点（最接近的 20 只）、动量状态、四因子矩阵 |
| `/review` 综合评估 | **四个工具并起来算赔率分**：只有 Q<45 / 致命风险 两条否决，其余按小市值、估值低位、周期底、弹性、垄断性加减分排序——给出「先研究哪几只」 |
| `/stock/<代码>` | 单只票：一份**原版报告**（顶部 / 质量 / 周期三个标签切换） |

`/stock/<代码>` 不是导航项，是从表格里点名称进去的。三份报告**不摞在一页**：
上面一条标签栏（顶部 · 四因子 / 企业质量初筛 / 周期弹性初筛），下面只摆当前那一份，
点标签换 `?focus=top|quality|cycle` 整页重渲染。周期/质量/顶部三页的名称链接带着
对应的 `focus`，从哪页点进来就停在哪一页（不给 focus 就先看顶部）。
（早先试过三份报告上下堆着，等于三份文档各带一个大表头贴在一起，2026-11-15 反馈"太乱"。）
有原版报告时，整页换成报告自己的版式：底色 `#f5f6f4`、墨绿主色、1040 内容宽，
深蓝 hero 和四张摘要卡都收起来——报告自己已经有表头和卡片，再包一层就是
"卡片里套卡片"。报告下方的「取数缺口」也用同一套白卡片；
旧行没有报告时才回到看板的 hero + 四张卡 + 明细视图。
`/` 直接转到 `/review`
（原来的「概览」页删了：它那张合并表就是综合评估下面的全表；「运行记录」页也删了，
运行台账看批次页和各工具页）。

### 个股页那份「原版报告」是怎么来的

个股页不重画工具的报告，而是把**工具自己那一份**嵌进来（`<iframe>`，CSS 互不干扰，
高度由页面脚本按内容撑开），一次只嵌当前标签那一份：

* 工具在**算完写库时**就渲染好整份 HTML，压一压（gzip+base64，约为原文 1/2）
  存进 `payload.report_html`；看板只做解压、原样返回：
  `/stock/<代码>/report/{quality|cycle|top}[?run=<id>]`。
* 为什么不落文件：`output/` 早就删了，人看报告走看板；为什么不重新生成：
  四个工具各有同名 `config.py`，同一个 Flask 进程里没法同时 import。
* 为什么不重画：质量那份五节（生意本身 / 经营质量与风险 / 估值与机构预期 /
  综合评分 / 后续跟踪）、周期那份三个判断表 + 三段「判断含义」说明 + 三张 SVG 图、
  顶部那份四因子卡片 + 计算过程，都是工具里几百行的版式，看板复刻必然走形。
* 报告的生成时间、基准日、是批量还是单只，写在标签栏右侧；
  「单独打开这页」直接打开当前这份报告。

工具也会把页面要用的**摘要**存进 `payload.detail`（质量约 5KB：Q/V 每个子项、
14 条风险检查逐条、PE/PB/PS 分位、机构三个预测年度、跟踪清单；周期约 4KB：
三个判断 + 三条指标序列）。`payload.detail` 是**降级用的**：旧行没存
`report_html` 时，个股页退回看板自己的明细视图；再没有就退回一张薄表加一句
「重跑一次就有」（`./run.py quality --date <批次> --force`）。

页面上的每个数字都来自 `results.db`，没有第二份缓存；把库删掉重跑一遍，
页面自然就更新了。`--db` 可以指向别的库（比如一份备份）用来对比。

### 综合评估怎么排的（`/review`）

四个工具各说各的，这一页把它们拼成一个**赔率分**再排序。
它是**候选池排序器**，不是买入清单：排在前面的票仍然要自己读完个股页再决定。

规则写死在 `web/review.py` 里，**只有两条一票否决**：

* **排除**：Q < 45，或命中致命风险（利息保障 < 1、商誉超 50% 等）。
  **这一条不放宽**，赔率分再高也不抵。
* **待补数据**：这一批没有质量结果（工具没跑或这只票失败）。
* **优先研究**：过了硬门槛，且赔率分 ≥ `BUY_CUT`（默认 5，满分 15）。
* **观察**：过了硬门槛但分数不够；工具自己判「回避」/「数据不足」的也在这里
  ——**降档但不排除**，理由写在「提示」列。
* **档内排序**：赔率分从高到低，同分看盈利弹性，然后 Q，最后按代码。

赔率分怎么来的（加分项，占得越多越靠前）：

| 项 | 数据来源 | 分 |
|---|---|---|
| 市值小 | `select_result.market_cap_yi` | <50亿 +3 / <100亿 +2 / <200亿 +1 |
| 估值低位 | `quality.detail.v.historical.metrics` 的 PE/PB/PS 分位均值 | ≤25% +3 / ≤50% +2 / ≤70% +1 |
| 周期低位 | `cycle_result.position_label`（产能周转率 <40 分位） | +2（仅周期股） |
| 盈利弹性 | `cycle_result.elastic_label` | 强 +2 /「不稳定」+1 |
| 垄断性 | `shared.db` 的 `annotation` 表（人工维护） | +2 |
| 质量 | `quality_result.q` | ≥80 +2 / ≥65 +1 |
| 目标价空间 | `top_result.target_upside` | ≥+20% +1 |
| **扣分** 趋势走弱 | `quality.detail.risks` 里毛利率/合同负债判坏 | −2 |
| **扣分** 估值分偏低 | `quality_result.v` | <55 −1 |

几个刻意的取舍：

* **软条件一律扣分，不否决**。毛利率一个季度走弱、机构没覆盖，都不等于公司不好
  ——扣几分让它排后面，而不是让它消失。
* **V 分缺失不扣分**。缺 V 的 63 只里 60 只市值 <200 亿（机构覆盖不到 3 家），
  把 V 当门槛等于系统性排除小市值，与「找小盘」的目标直接冲突。
* **垄断性没标注不扣分**。`annotation` 表是人工核实的（覆盖率天然稀疏），
  「没查过」不等于「没有」；「无标注」是查过之后的结论，同样是 0 分。
* **「不稳定」给 1 分**。营收长期走平、利润不跨零的公司往往是稳的，
  只是 DOL 这个比值没意义，不该当 0 分罚它。
* 非周期股的强弹性照样加分，但「为什么值得看」里标「·存疑」：DOL 在它身上是
  经营杠杆，不是周期放大器（与工具自己那句「①否②强·存疑」对齐）。

阈值集中在 `web/review.py` 顶部（`Q_HARD / BUY_CUT / MCAP_STEPS / PCT_STEPS /
Q_STEPS / TREND_PENALTY / V_WEAK`）。**唯一的松紧旋钮是 `BUY_CUT`**：
2026-11-06 那批实测 5 分 → 14 只、4 分 → 19 只、6 分 → 8 只。
**利润弹性的 2× 不在这里**：它是 `stock_cycle_elastic/config.py` 的
`ELASTIC_RATIO_MIN`，这一页只读工具给出的标签，不另定一条线。

这一页只做**排序和提示**，不下买卖结论。

### 批次和单只是两回事

库里的运行记录带 `kind`：`batch` = 一次覆盖整份名单（每周五那批），
`single` = 单只随手跑一次（`--stock`）。看板据此分开显示：

* **批次页 / 工具页**只统计 `batch`。周五那一批就是四个工具 `target_date`
  相同的那些运行；`?date=2026-11-06` 看上周那批，`?run=12` 看某一次具体运行。
* **个股页**只摆当前那一份报告，不再列历次结果；想看某只票被批量算过、
  又被单只重算过几次，走批次页或对应工具页。

Flask 只在这个看板上用得到，四个工具本身仍然只用标准库：

```bash
pip install flask          # 或 python3 -m pip install flask
```

## 备份

```bash
./run.py backup              # shared.db + results.db → backup/（保留最近 7 份）
./run.py backup --list       # 看已有备份
./run.py backup --dir ~/MyData/backups --keep 14
```

用 `VACUUM INTO` 而不是 `cp`：`shared.db` 是 WAL 模式，直接复制可能拿到一个
缺最新提交的库；`VACUUM INTO` 走一致性读，顺带整理碎片（107 MB → 实测同量级）。
每份备份都跑 `pragma integrity_check` 与表数统计，**校验不过就删掉并报错**——
备份的意义是"能恢复"，拷完不验等于把发现问题的时间推迟到真出事那天。

保留策略只删本命令自己生成的（`shared.db.YYYYMMDD-HHMMSS`）；手工改名的
（如 `shared.db.20261115-before-v6`）一律不动。

## 环境

- Python 3.13。四个工具只用标准库；看板需要 Flask（`pip install flask`）。
- 交易日历 `state/trading_calendar.json`（`build_calendar` 生成）。

## 测试

```bash
python3 -m stock_data_fetch.tests                  # 数据层 13 个文件
python3 -m stock_kline_screen.tests                # 选股 4 个文件
python3 -m unittest discover -s stock_quality_screen/tests -t stock_quality_screen
python3 -m unittest discover -s stock_cycle_elastic/tests -t stock_cycle_elastic
python3 -m stock_judge_top.tests
python3 -m unittest discover -s tests -t .         # 入口脚本
```

## 维护

```bash
python3 -m stock_data_fetch.doctor                 # 体检（退出码可当闸门）
python3 -m stock_data_fetch.maintain --apply       # 清理孤儿覆盖、裁剪历史、VACUUM
```
