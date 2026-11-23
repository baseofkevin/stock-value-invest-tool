# stock-value-invest-tool

个人价值投资量化工具箱：**每周五收盘后跑一遍选股 → 周期 → 质量 → 顶部 → 大势**，
买入后按需查顶部。所有结论只进 `results.db` 一个文件，用 `./run.py web` 打开看板读。

一句话原则：**选股是名单的源头**。周期弹性 / 质量初筛 / 顶部判断在批量模式下，
都从 `results.db` 里读「基准日 = 那一天」的选股名单；那天没跑选股，三个下游就没有输入。

---

## 1. 七个工具，一套数据层

| 模块 | 干什么 | 入口 | 结论落哪 |
|---|---|---|---|
| `stock_kline_screen` | 选股：5 日涨幅池 → 真上穿 MA20 + 放量 ≥1.5 倍 + 均线结构 → 非 ST / 非次新（财务与行业只标注，不裁决） | `./run.py select` | `select_result` |
| `stock_cycle_elastic` | 周期弹性：是不是周期股（毛利率 5 年 σ / CV）、弹性强弱（利润增速÷收入增速）、周期位置（产能周转率 10 年分位） | `./run.py cycle` | `cycle_result` |
| `stock_quality_screen` | 质量初筛：质量分 Q + 估值分 V + 14 项风险检查 + 机构预期 → 一句结论 | `./run.py quality` | `quality_result` |
| `stock_judge_top` | 顶部判断：动量 / 换手 / 业绩 / 量价 四因子，各自出结果、不打总分 | `./run.py top` | `top_result` |
| `stock_review_assess` | 综合评估：赔率分（0~15）排序 + 「超级成长股标识」（只标识，不改档位） | 看板 `/review` | 不落库，页面现算 |
| `stock_extreme_point` | 冷热极点：某交易日全市场 5500+ 只的收盘价 / PE / PB / PS / 市值 + 当日摘要 | `./run.py extreme` | `shared.db.price`（原件）+ `results.db.price`/`price_stat`（副本） |
| `stock_market_trend` | A股大势：沪深300 周线趋势 / ERP 赔率 / 破净+量能情绪（背景板，不参与选股结论） | `./run.py trend` | `market_regime` |

共用一层本地数据层 **`stock_data_fetch`**（+ `shared.db`）：库里有的直接用，
没有的才联网，拿到的写回库，下次（乃至其它工具）复用。

两个旁路包**不 import 数据层**，只用文件路径约定（同一个仓库根、同样的
`STOCK_SHARED_DB` / `STOCK_RESULTS_DB` / `STOCKHUB_CALENDAR`）：

- `stock_extreme_point`：全市场一天只有 6 个上游请求，不值得为它拖进 hub / router / coverage / throttle。
- `stock_market_trend`：三路信号十来个请求，且它的结论只作背景板。

`stock_review_assess` 与四个工具平级，但**不连库、不 import `web`**：数据由
`web/db.py` 读好、以字典传进去，所以它可以单测。

---

## 2. 一条依赖链

```
        ┌──────────┐
        │   选股    │  产出「名单」→ results.db / select_result
        └────┬─────┘
     ┌───────┼────────┐
     ▼       ▼        ▼
  周期弹性  质量初筛  顶部判断     ← 批量时都读上面那份名单
```

下游三个**不自己找票**。`./run.py quality --date 2026-11-06` 内部执行的是
`stock_report.py --from-select 2026-11-06`，即「去库里取 11-06 那天的选股结果」。
那天没有选股记录 → 直接退 **2**，提示先跑 `./run.py select --date X`，
不会拿别的日期凑数。

`stock_extreme_point` / `stock_market_trend` / `stock_review_assess` **不在链上**：
前者看的是「某个交易日的全市场」，后两者的结论只做背景和排序参考。

---

## 3. 两个数据库

| 文件 | 装什么 | 丢了会怎样 |
|---|---|---|
| `shared.db` | **上游原件**：K 线、财报、分红、估值、快照、横截面 `scan`、人工标注 `annotation`、覆盖表 `coverage`，外加两个旁路包写的 `price` / `index_price` / `market_*` | 只能重新联网抓，几十分钟到几小时 |
| `results.db` | **派生结论**：四个工具的名单与档位、大势周记 `market_regime`，以及看板要的全市场副本 `price` + `price_stat` + `index_price` | 重跑一遍就有（几分钟） |

分开的理由：`shared.db` 有严格的不变量（只追加 raw、数据与覆盖同事务），
`maintain.py --apply` 会 VACUUM 并清理；结论如果也躺在里面，一次清理就可能
把上周的名单一起带走。看板**只读** `results.db`，所以上游清理、抓取失败都不会
让页面上已经存下来的历史时点跟着消失。

---

## 4. 快速开始

```bash
cd /home/kevin/MyData/stock-value-invest-tool

./run.py                 # 裸跑只打印帮助，什么都不跑（全流程三十多分钟，防误触）
./run.py all             # 周五盘后全流程：日历 → 选股 → 周期 → 质量 → 顶部 → 大势
./run.py menu            # 不记命令就选菜单
./run.py web             # 看板 http://127.0.0.1:5000（只读、不联网）
```

- **必须在 15:05 之后跑**（收盘集合竞价 + 上游落库留 5 分钟）。之前跑会退到上一个交易日。
- 工作日发现日历过期会**自动**跑一次 `build_calendar`（周末不自动）。
- 屏幕上只有几步进度条，其余全进 `logs/run-<时间戳>.log`。

单只随时查（不需要名单）：

```bash
./run.py top     600519        # 顶部四因子
./run.py quality --stock 600519  # Q/V 与 14 项风险
./run.py cycle   --stock 600519  # 是不是周期股、位置在哪
```

自备名单（持仓、自选）：

```bash
./run.py quality --csv ~/MyData/持仓.csv --workers 8
./run.py top --csv ~/MyData/持仓.csv --date 2026-11-13
```

---

## 5. 文档地图

| 文件 | 讲什么 |
|---|---|
| `USAGE.md` | **按情景查命令**：我现在该敲哪条、`--date` 到底管什么、补跑怎么补、踩坑对照表 |
| `info.md` | **这套工具是什么**：七个模块、一条链、两个库、看板每一页看什么、综合评估怎么排 |
| `stock_data_fetch/README.md` | 共享数据层的完整设计：四层模型、13 张表、命中判定、节流与源路由、六道闸、运维四件套 |
| `stock_kline_screen/README.md` | 选股：七条规则、十个子步骤、口径差异、失败语义 |
| `stock_cycle_elastic/README.md` | 周期弹性：三个判断三个指标、11 个阈值、为什么合同负债只做限定语 |
| `stock_quality_screen/README.md` | 质量初筛：Q/V 口径、14 项风险、决策矩阵与一致性闸门 |
| `stock_judge_top/README.md` | 顶部判断：四因子 + 四条旁证、raw 价格与换手折算 |
| `stock_extreme_point/README.md` | 冷热极点：表结构、涨跌停与中位数口径、两库同写的约定 |
| `stock_market_trend/README.md` | A股大势：三路阈值、量能五档、周中半根 bar 的处理 |
| `stock_review_assess/README.md`<br>`stock_review_assess/综合评估-硬门槛说明-20261120.md` | 综合评估：三条门槛的三态判定、赔率分、分档与排序 |
| `web/README.md` | 看板：13 条路由、9 项导航、查询参数、只读约定 |
| `notes/` | 投资笔记与读书笔记（《股市真规则》），不是代码文档 |

`dataflow.html` 是**生成物**（`scripts/gen_dataflow.py`），别手改；
它把「数据从哪来、落哪张表、被哪条阈值筛」画成一页，看板 `/dataflow` 读它。

---

## 6. 环境与测试

- Python **3.13**。七个工具 + 数据层只用标准库；只有看板需要 Flask（`pip install flask`）。
- 交易日历 `state/trading_calendar.json`（`python3 -m stock_data_fetch.build_calendar` 生成，12 年）。

```bash
python3 -m stock_data_fetch.tests                                # 数据层（13 个文件 / 911 项）
python3 -m stock_kline_screen.tests                              # 选股（5 个文件 / 103 项）
python3 -m unittest discover -s stock_cycle_elastic/tests -t stock_cycle_elastic   # 419 项
python3 -m unittest discover -s stock_quality_screen/tests -t stock_quality_screen # 255 项
python3 -m stock_judge_top.tests                                 # 顶部（9 个文件 / 134 项）
python3 -m stock_extreme_point.tests.test_extreme_point           # 快照（100 项）
python3 -m unittest discover -s tests -t .                        # 入口 + 看板 + 综合评估 + 大势（4 文件 / 213 项）
```

全部离线（数据层另有 10 项联网用例，要 `STOCKHUB_LIVE=1` 才跑）。
临时库落在仓库内 `.testtmp/`——这台机器 `/tmp` 只有 10MB tmpfs。

---

## 7. 维护

```bash
./run.py backup                                # 两个库 VACUUM INTO 到 backup/，默认留 7 份
python3 -m stock_data_fetch.doctor             # 只读体检（退出码可当闸门）
python3 -m stock_data_fetch.maintain --apply   # 清理 + VACUUM（默认只审计）
python3 -m stock_data_fetch.results            # 各工具最近一次运行的台账
```
