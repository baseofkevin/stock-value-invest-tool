# stock_judge_top —— 单股四因子独立观察（共享数据层版）

> 2026-11-15 从 `~/MyData/stock-judge-top-tool` 迁移重建。原版 1911 行、
> 纯标准库零依赖；本次迁移**逻辑层一字未动**（indicators / finance /
> judgement / views / log 全部原样拷贝），只重写了数据层、入口与产出方式。
>
> 同日稍晚，HTML 报告改成**不落文件**：`render/html.py` 的 `html_report(r)`
> 仍然渲染那份版式，但结果压进 `payload.report_html` 存 `results.db`，
> 看板 `/stock/<代码>/report/top` 原样吐出来。人看结果仍然只走 `./run.py web`。

    python3 -m stock_judge_top sh600519 [--date 2026-11-13] [--json] [--refresh]
    python3 -m stock_judge_top --from-select 2026-11-13 [--workers 4] [--quiet]
    python3 -m stock_judge_top --csv 我的持仓.csv [--workers 4] [--quiet]

四因子（动量 / 换手 / 业绩 / 量价）各自独立出结果，不打分、不合并总分、
不判顶——这是原版的设计立场，迁移不改。

2026-11-15 的第二轮改动（见 §6）加了**四条旁证**（估值分位 / 机构目标价 /
基本面反证 / 周期状态）。前三条总是尝试，周期状态只在判成周期股时给出。
2026-11-24 起**不再出第五张「周期位置」卡片**：周期结论只留在报告
旁证表的「周期状态」一行和批量列表的「周期」列（见 §6.4）。
它们都是独立的一句话，更不参与任何合计——加旁证的目的是让人自己看
"贵不贵、撑不撑得住"，不是给四个因子再凑一个第五分。

`--date` 不给时取**共享库的目标交易日**（今天过了 15:05 才是今天，否则上一个
交易日）。原来默认 `date.today()`，周末跑会先报「末端不足（库 11-13 / 需 11-15）」
再报「当前无 K 线（可能停牌）」——把"今天不是交易日"说成"停牌"。

## 0. 批量模式

持仓一多、或者想在下周五的选股结果上整体扫一遍"谁已经贴着 250 日高点"，
一只只敲太慢：

    python3 -m stock_judge_top --from-select 2026-11-13 --workers 4
    python3 -m stock_judge_top --csv 我的持仓.csv --workers 4

- 名单默认取 `results.db` 里那一天的选股结果（`--from-select`，省略日期=最近一次）；
  `--csv` 读**代码列**，认这 7 个列名（大小写不敏感，`batch.CODE_COLUMNS`）：
  `代码` / `股票代码` / `证券代码` / `code` / `symbol` / `股票编码` / `证券编码`；
  一个都没对上时**取第一列**。同一只出现多次只跑一次（保序）。
- 单只失败不影响其余，最后汇总「成功 N / 失败 M」并列出失败代码。
- 每只的结论写进 `results.db` 的 `top_result` 表；看板的「顶部」页按
  「距 250 日高点」从近到远排，一眼看出谁在高位。
- 并发是**每线程一个库连接**（SQLite 连接不能跨线程），`--workers` 默认 4。

> `--from-select` 与 `--csv` 的差别只在名单来源。两者都和位置参数（股票代码）
> **互斥**，同时给会直接报参数错误退 2（`cli.py:96-101`）。

### CLI 全参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `code`（位置参数） | — | `sh600519` / `600519` / `600519.SH`；用 `--csv` 时省略 |
| `--from-select [日期]` | — | 名单取结果库这一天（`nargs="?"`，省略=最近一次） |
| `--csv CSV` | — | 从 CSV 代码列逐只出报告 |
| `--workers N` | `4` | 批量并发线程数（每线程一个库连接） |
| `--date` | 库的目标交易日 | 基准日；不给就取"今天（过了 15:05）或上一个交易日" |
| `--refresh` | 关 | 忽略缓存重新拉（透传为 hub 的 `force`：清重试标记后重取） |
| `--json` | 关 | stdout 只输出 JSON（**不受 `--quiet` 影响**） |
| `--json-full` | 关 | JSON 里同时带 `quote`（默认剔除） |
| `--verbose` | 关 | 打印执行过程与完整的命令行报告（默认**只写 results.db**） |
| `--quiet` | 关 | 不输出日志与命令行报告，只写 results.db |
| `--progress` | 关 | 只打逐只进度行 `[ 3/38] …`（给 `run.py` 画进度条），不打报告表格 |
| `--db PATH` | `$STOCKHUB_DB` 或项目内 `shared.db` | 共享库路径 |

**`--verbose` / `--quiet` / `--progress` 是三档输出强度**，默认最弱（只落库）：
`configure(quiet = a.quiet or not a.verbose)`——不加 `--verbose` 就等于静默，
这是 2026-11-15 为批量模式定的（38 只每只打一份报告，终端会翻到看不清）。

### 退出码

| 码 | 含义 |
|:--:|---|
| 0 | 成功 |
| 1 | 单只失败（取数/计算异常；冒烟里 `sz300750` 因库里无 K 线 + WAF 封禁就是这个） |
| 2 | 参数错误（`--csv` 与代码同时给、`--from-select` 与另两种同时给、代码无法识别） |
| 3 | **preflight 未过**（六道闸任一失败）—— 注意这个码与 `run.py` 的"选股 0 只"重号，见 §5 |

## 1. 与原版的差异（只有数据层）

| 原版（westock MCP） | 本版（共享库 hub） |
|---|---|
| `data_kline` fq="qfq"，3 段×1 年 limit=300，缺段抛错 | `ensure_many("kline", ...)` 取 **raw**（不转 qfq，见 §2.1） |
| 换手率 `exchange` 上游直接给 | **折算**：`amount ÷ 流通市值 × 100`；无市值时用成交额自身分位（见 §2.2） |
| `data_finance` fields=all num=60 | `ensure_many("finance", ...)` + `finance_full()["income"]` |
| `data_quote` 取名称/存在性 | profile 域（`stock` 表） |
| 单股 JSON 缓存 `cache/top_*.json` | 删除，缓存归 hub（coverage 闸） |
| westock 令牌 / MCP URL / 外部 CLI 回退 | 删除，`preflight()` 六道闸 |

CLI 接口不变；`--refresh` 现在透传为 hub 的 `force`（清重试标记后重取），
新增 `--db`（透传共享库路径）。

## 2. 三个口径决定

### 2.1 价格用 raw（2026-11-15 改，原为 qfq）

**先纠正一次自我推翻**：迁移时这里写的是"qfq 本地算"，理由是"距高点要跨除权"。
实测下来两个理由都不成立，而且 qfq 引入了一个更难发现的问题——**和选股工具
的口径不一致**。

    农业银行 sh601288，基准日 2026-11-13：
        原始收盘 5.13        前复权 5.04
        MA20 raw 7.371        MA20 qfq 7.358     （差 0.16pp）

差异小，但既然两个工具回答的是同一只票的同一个问题，口径就该一样：
`stock_kline_screen` 全链路用 raw，`stock_judge_top` 没有理由不同。
于是 `data._nodes()` 不再调 `factor.adjusted()`，也不再 ensure dividend。

代价（写进报告口径区，不藏着）：除权日附近的历史高点带着缺口，
「距 250 日高点」在有分红时**略偏保守**（例：10 派 3 元会让高点虚高约 3%）。
位置类指标是同一序列内的比值，方向不受复权影响；真正会被复权改变的是
"跨除权日的累计涨跌幅"，而这里用的是 5/20/60 日短窗口，落在除权日上的概率很低。

`volume`/`amount` 本来就是 raw，不受影响。

### 2.2 换手率折算

共享库 kline 没有 `exchange` 列，而 indicators 的换手因子（av20/ratio/AVG20）
需要它。折算式：

    exchange = amount ÷ circ_mv × 100    （amount 缺失时退回 close(raw) × volume）

与 `stock_kline_screen/turnover.py` 的 `turnover20` 同款：分母用 quote 域
"当期一份"的流通市值，历史某日的真实流通市值不在库里。两边接受同一个近似。
另外，卡片底部给一行**参考·成交额分位**（同一窗口，250 个交易日）。它只作对照，
不当主判据：成交额随股价水平一起涨——一只票翻倍后，同样的成交量会多出一倍
成交额，分位就被"涨过了"本身推高，恰好在我们最关心的高位票上系统性偏高。
换手率 = 成交额 ÷ 流通市值，已经把这层价格效应除掉，所以拥挤度以换手率为准。

流通市值取不到时，**不再显示「数据不足」**，改用**成交额自身的 250 日分位**
（`turnover_basis = "amount"`，卡片标题换成「交易拥挤度」、副标题写明原因）。
绝对换手率的高低依赖流通盘大小，同一只票和它自己的历史比才有意义；
成交额分位与换手分位在判"挤不挤"这件事上等价，且不依赖市值。
实测 41 只里有 2 只原本走这条路（胜利股份、好利科技）。

### 2.3 根数门槛 30、深度期望 750

原版分 3 段各拉 1 年（防段间空洞，缺段抛错）。hub 的 kline 是一次连续区间，
空洞由 `InsufficientDepth` / `HoleUnreachable` 表达，不需要人工分段对账。
指标实际用的最长窗口是 250 日，库里 300 根即可全量计算；不足 250 根时
`pos250` / `high250` 一律给 None，动量/量价/市场阶段自动退回 60 日口径
（副标题会标明「60日」），不再拿「现有几十根」冒充 250 日高点；不足 30 根才报错。

## 3. 测试

    cd /home/kevin/MyData/stock_value_invest_tool && \
    STOCKHUB_TMP=$PWD/.testtmp python3 -m stock_judge_top.tests

**9 个文件、合计 134 项**（2026-11-23 实跑）。全部离线。

| 文件 | 用例 | 覆盖 |
|---|---:|------|
| `test_indicators.py` | 26 | 四因子的滚动窗口 / `pos250` / `pos60` / 分位 |
| `test_views.py` | 26 | 四键视图组装与文案 |
| `test_judgement.py` | 16 | 业绩与环比的二维修正判定 |
| `test_finance.py` | 15 | `finance_full() → 指标` 的字段映射 |
| `test_batch.py` | 14 | 批量编排、坏代码跳过（**不致命**）、去重保序 |
| `test_evaluate.py` | 13 | 端到端评测结构（含 `extras` / `gaps`） |
| `test_judge.py` | 13 | CLI 参数、参数冲突、退出码映射 |
| `test_hubdata.py` | 9 | §2.1/§2.2 的映射断言（raw 而非 qfq、换手折算） |
| `test_render.py` | 2 | HTML 渲染骨架（不再断言落盘路径） |

原版的 `test_mcp` / `test_westock` / `test_data` 随数据层一起删除；
`test_render` 里绝大部分 HTML 断言随"不落文件"一起删，只留 2 项骨架检查。

> 早先本文写"8 个文件"、名单里漏了 `test_batch`——它是后来补的，
> 而 `tests/__main__.py` 早就把它接进去了，所以"跑全部"一直是 9 个。

## 4. 冒烟记录（2026-11-15）

- `sh600519 --date 2026-11-13 --verbose`：四因子完整报告打到终端，结论写入
  `results.db`；业绩卡片 2026Q3（402.67 亿营收，同比 +3.1%）——
  财务链路（finance_full → westock 字段名）数值正确。
- `sh600000 --json`：JSON 契约（quote 剔除 / views 四键 / warnings 空 /
  av20=0.196%）。
- `sz300750`（库里无 K 线）+ 恰逢腾讯 WAF 封禁窗口：exit 1、错误信息清晰，
  失败路径按预期工作。

## 5. 已知限制

- **换手率分母是当期市值**：补跑历史基准日时，一年前的换手率会用今天的
  流通市值折算（限售解禁会漂移）。当期运行无此问题。
- **商誉这项反证缺席**：westock `balance` 没有商誉列（见 §6.3），
  基本面反证只能用应收/存货/现金流三项。
- **估值分位受覆盖限制**：`valuation` 域按需拉取，新票第一次要打上游；
  上游没有该股的估值序列时这一行显示 —，并在缺口清单里说明。
- **周期位置是"季报级"判断**：最新一期没披露时，读到的是上一季，
  对正在反转的行业会滞后一个季度。
- **`--json-full` 的 quote 是 profile 行**（name/industry/list_date），
  不是原版的实时行情快照——四因子与展示只消费 name。

## 6. 2026-11-15 第二轮改动（旁证 + 条件卡片 + 两处修正）

### 6.1 动量：状态改成二维

旧版把"涨得多"和"位置高"揉成一个标签，实测 40 只里 4 只被误标：阿尔特
20 日 +40.74%（极热），但距 250 日高点还有 −14%（反弹，不是高位）。
两件事分开看：

    位置  where = pos250（−5% 内高位 / −20% 内中位 / 其余低位；缺 250 日时退 60 日）
    速度  speed = ret20（过热 / 偏热 / 走平 / 偏弱）
    状态 = "位置·速度"，例：中位·过热 / 低位·走平

headline 的规则跟着改：只有**高位 + 热**才叫「高位加速」；非高位不论多热，
都说成「距高点仍有 X%，20 日 Y% —— 低位反抽 / 尚未到高位，不是高位过热」。
这条正是为了防止"反弹被当成顶部信号"。

### 6.2 量价：高位用 250 日

旧版用 `pos60` 判"高位"——60 日高点在长期下跌里会不断刷新，
把"下跌中继"误判成"高位盘整"。改用 `pos250`（缺则退 60 日，副标题标明）。

### 6.3 四条旁证（各一句话，不参与结论）

代码里就四张（`evaluate.py:87-89` 的注释原话是"这四张都只是旁证"）：

    extras.valuation   PE/PB/PS 当前值 + 十年周线分位          ← 抄 quality 的算法与阈值
    extras.consensus   机构目标价 + 距现价空间 + 覆盖年度数      ← snapshot:consensus
    extras.risk        应收/存货增速 vs 营收、经营现金流/净利润  ← 抄 quality 的反证项
    extras.cycle       判断①是否周期股 + 弹性 + 产能周转率分位   ← 抄 cycle 的阈值（§6.4 的卡片）

前三张**总是尝试**，第四张只有判成周期股才出卡片。任何一张取不到都只往 `gaps`
里记一条缺口，**绝不拦住四因子**。

其中 `extras.risk` 的判据是三行（`extras.py:140-197`）：

| 项 | 命中 | 提示 | 其余 |
|---|---|---|---|
| 应收增速 − 营收增速 | ≥ 30pct | ≥ 15pct | clear |
| 存货增速 − 营收增速 | ≥ 30pct | ≥ 15pct | clear |
| 经营现金流 / 净利润 | < 0 | < 0.7 | clear |

阈值与判定口径**从那边抄过来，不 import**：`stock_quality_screen` 和
`stock_cycle_elastic` 都是能单独拷走的独立包（顶层 `import config`），
直接 import 会把三个包焊死。抄的是"规则"（阈值常量、分位算法），
不抄取数编排和渲染。规则若在那边改了，这里要跟着改——每个函数的
docstring 都写了对应源文件。

`extras.cycle` 的阈值表整份抄在 `extras.CYCLE` 字典里（`gm_window=20`、
`gm_std_cycle=5.0`、`gm_cv_min=20.0`、`elastic_ratio_min=2.0`、
`position_window=40`、`position_low=40.0` / `position_high=60.0` …），
注释就一句"与 `stock_cycle_elastic/config.py` 同值（改那边也要改这里）"。
`extras.VALUATION_YEARS = 10`、`VALUATION_MIN_WEEKS = **100**`——
注意 quality 那边是 52 周起算，这里为了"十年周线分位"抬到 100 周。

**商誉查不了**：westock 的 `balance` 没有商誉列（quality 是从东财 `gbalance`
子域取的）。不为一项反证多打一个上游，改用存货增速做第二项反证——
高位放量 + 存货增速远超营收，同样是"这波涨没有基本面"的证据。

### 6.4 周期位置卡片 → 已下线（2026-11-24）

曾经的设计：只在**判断① 判定为周期股**时追加第五张卡片，σ/CV 判是不是
周期股 → 弹性比值判敏感度 → 产能周转率 40 季分位判景气位置，三判断各自
出档位、不合并（与 cycle 工具一致）。单季数据直接用上游的 `_Q` 字段。

**2026-11-24 按使用决定下线这张卡片**：报告固定四张因子卡，周期结论
不再单独出卡。三判断的**计算逻辑原样保留**（`extras.py` 的 `cycle` 域），
只是换了个更轻的出口——

- 报告底部旁证表的**「周期状态」一行**（高位 / 中位 / 低位 + 一句话解读）；
- 批量列表 `/top` 的**「周期」列**（`cycle_state`）；
- 个股页 `/stock/<code>?focus=cycle` 仍可看 cycle 工具的完整三判据报告。

`views/cycle.py`（卡片视图）随之删除；历史报告里已存的第五张卡也已在
库里批量清除。

### 6.5 两处修正

- 跨包修正（**本包没有这段代码**）：`stock_quality_screen/score.py` 的 `_fatal()` 里
  「连续 3 年经营现金流为负且净负债/EBIT > 4」在 EBIT ≤ 0 时恒不成立
  （比值是负数，`> 4` 永远假）——最该命中的一类公司反而绕过了。
  `grep _fatal stock_judge_top/` 是零命中：本工具的 `extras.risk` 只有三行阈值，
  没有致命项概念，所以这条修正在 quality 那边落地，这里只是同源问题。
- 报告「数据与口径」区新增**取数缺口清单表**（域 / 原因 / 影响面），
  替代原来只有一行行 warning 文字的做法：读者要能分清"缺数据"和"算不出来"，
  也要知道哪一条真会影响结论。

### 6.6 结果表 `top_result`

`results.db` 的 `top_result` 一共 **14 列**（`primary key(run_id, symbol)`）：

```
run_id symbol name close pos250 days_since_high
momentum_state turnover_state earnings_state volume_state cycle_state
pe_pct target_upside payload
```

`payload` 里带 `kline` / `finance` / `quote` / `warnings` / `extras` / `gaps` /
`earnings` / `report_html`。`extras` 那四张旁证**原样入库**——看板个股页直接读它，
不重算。取数缺口走 `gaps`（`{domain, symbol, why}` 三元组），渲染成缺口清单表。

看板「顶部」页按 `pos250`（距 250 日高点）从近到远排；`cycle_state` 只有周期股才有值。
