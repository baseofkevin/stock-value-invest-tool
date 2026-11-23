# stock_review_assess —— 综合评估

一批候选票进来，出两样东西：

1. **超级成长股标识**（内部仍叫"硬门槛"、模块里叫 `gates`）——不亏损、增速、
   有供需矛盾。逐条标过 / 不过 / 未核实。
   **只做标识，不排除、不分档、不排序**，而且**只用数据库里现成的数**。
2. **赔率分**（0~15）——谁排在前面。这是**排序**，分低不会让票消失。

两件事刻意分开，而且**不互相打分**：门槛是给人看的判断依据，分数管
"先看谁"。把门槛接进分档只要一行代码，但那样一来工具就替人做了决定——
用户要的是"先看标识、自己去查财报"，所以门槛落到表格的一个列里。

（这一条是 2026-11-20 改的：第一版把门槛做成否决，311 只里 287 只被排除、
「优先研究」直接清零。显示被改掉比不显示更糟，所以退回来只加标识。）

与 `stock_kline_screen` / `stock_quality_screen` / `stock_cycle_elastic` /
`stock_judge_top` 平级：都是顶层包，都**不 import `web`**，都**不自己连库**。
数据由 `web/db.py` 读好，以字典传进来，所以这一层可以单测。

```
stock_review_assess/
    config.py    全部阈值。改一个数刷新页面就生效，没有藏在别处的权重。
    gates.py     硬门槛。纯函数，输入 fin（财务快照），按 GATE_KEYS 出结论。
    review.py    赔率分、分档、build()（原来 web/review.py 的内容搬过来）。
```

`web/review.py` 只剩一层兼容 shim（`tests/test_web.py` 里 19 个用例按老名字
`from web import review` 引）。新代码请直接 `from stock_review_assess import build`。

## 门槛：用户原话 → 实现

| # | 用户原话 | 实现 | 数据 |
|---|---|---|---|
| ① | 亏损公司不买 | 扣非 TTM > 0；**单季扣非 > 0**；**扣非/归母 ≥ 80%** | `finance` 的扣非/归母 |
| ② | 主营增速低于 40% 不买 | 单季营收同比 ≥ **25%**，**且**单季扣非增速 > 营收增速 | `finance` 的单季同比 |
| ③ | 无强供求矛盾的不买 | **毛利率环比连升两季**，或**合同负债同比 ≥ 20% 且 ≥ 年化营收 5%** | `OperatingRevenue_Q`/`OperatingCost_Q`、`ContractLiability` |

原方案里的「无新产品/新市场拓展的不买」**整条去掉了**：数据库里没有"新业务
收入"这个字段，判断只能靠手写备注，跟"只用库里有的数据"直接冲突。要恢复，
得先让 `stock_data_fetch` 把新业务收入抓进库，而不是再加一个手填文件。

### 阈值清单（`config.py` 里就这几个数）

| 常量 | 值 | 含义 | 缺数据时 |
|---|---|---|---|
| `GATE1_DEDUCTED_RATIO_MIN` | `0.80` | 扣非 / 归母 ≥ 80% | **归母 ≤ 0 → 不过**（不是未核实）；**归母缺失 → 跳过这条、仍可能过**；三字段全缺 → 未核实 |
| `GATE2_REV_YOY_MIN` | `25.0` | 单季营收同比下限（%） | 任一同比缺 → 未核实 |
| `GATE3_CL_YOY_MIN` | `0.20` | 合同负债同比 ≥ +20% 记「订单变厚」 | 两期缺一 → 未核实 |
| `GATE3_CL_YOY_FAIL` | `-0.10` | 同比 ≤ −10% 记「订单流失」 | 同上 |
| `GATE3_CL_SHARE_MIN` | `0.05` | 合同负债 / 年化营收下限 | 缺单季营收 → 未核实 |

三条门槛的完整判定（**顺序短路**，先命中的先返回；① 的四步见 `gates.py:63-113`）：

```
① 不亏损   1. np_cum <= 0              → 不过
           2. base = cut_ttm 或 cut_cum；base <= 0 → 不过
           3. cut_q <= 0               → 不过
           4. cut_cum 与 np_cum 都有且 np_cum > 0：
                cut_cum / np_cum < 0.80 → 不过
           5. 其余                      → 过

② 增速     rev_yoy_q >= 25
          且 cut_yoy_q > rev_yoy_q     （相等也算不过）

③ 供需     (a) gm_q[0] > gm_q[1] > gm_q[2]
        或 (b) cl / (rev_q*4) >= 0.05 且 cl / cl_prev - 1 >= 0.20
        两条一过一不过 → 未核实（不下结论）
        两条都不过     → 不过
        一条不过 + 另一条未核实 → 不过
        两条都未核实   → 未核实
```

`cut_ttm` 拼不齐四季时退回 `cut_cum`（累计扣非），口径不同——这是已知的将就，
见文末「待办」。

**① 的三个边界最容易读错**（本文早先都写错过，已按代码改）：

- **`cut_q` 必须 > 0**，代码是 `cut_q <= 0 → fail`，所以单季扣非**恰好为 0 也判不过**；
- **归母 ≤ 0 是"不过"**，不是"未核实"——亏损是明确的坏消息，缺数才是没有消息；
- 归母缺失时 80% 那条被跳过（第 4 步要求 `np_cum is not None`），所以**仍可能判过**。

### ② 为什么是 25% 不是 40%

40% 照字面执行，会筛掉用户自己举的那个例子：营收 +33%、扣非 +75%、
Q3 扣非 +161%。模组行业是"老业务价格战拖累总量、新业务爆发"，总量增速
天然低于结构增速。

所以总量这一条只留 25% 的底线（`GATE2_REV_YOY_MIN`）。想回到原口径，
把它改成 `40.0`，代价是拐点型公司全部消失。

### ③ 只用数据库里有的数

财报里没有字段叫"这个行业是不是供不应求"，所以只认两条能在报表上算出来的证据，
**占一条即可**：

**a. 毛利率环比连升两季**（当季 > 上季 > 上上季）
分子分母在 `finance` 里（`OperatingRevenue_Q` / `OperatingCost_Q`）。
涨价或产品结构上移会推高毛利率，降价换量会压低它。

**b. 合同负债同比 ≥ 20%，且合同负债 ≥ 年化营收的 5%**
`ContractLiability` 是客户预付的钱，同比（不是环比）比去年同季——合同负债
在 Q4 集中确认，环比比出来的多半是季节性。
占比门槛必须加：中位公司的合同负债只有年化营收的 **2.4%**，小基数上翻一倍
只是噪声。

两条打架（毛利率在涨、订单在缩）时**不下结论**，落 `unknown`——毛利率涨可能
只是原材料降价，这事儿得人看。

刻意**不加**"提价公告 / 交期拉长 / 产能受限"：共享库里没有这些字段，
硬加就只能靠手写备注，等于把判断建立在数据库之外。金融股没有营业成本、
合同负债也常常不适用，两条都判不了，落 `unknown`。

## 未核实 ≠ 通过，也 ≠ 不通过

`unknown` 是第三种状态，页面上单独标出来（灰色小方块）：

* **不是"通过"**。`gate_all_pass` 只数真正 `pass` 的，未核实的不算。
* **也不是"不通过"**。它不扣分、不降档、不进「提示」列。
* 顶部三张卡把「过 / 不过 / 未核实」分开数，一眼能看出筛得有多严。

## 数据从哪来

`web/db.py`：

| 函数 | 读什么 | 粒度 |
|---|---|---|
| `merged(conn, date)` | `results.db` 四个工具的结果 | 一只票一行 |
| `moats()` | `shared.db` 的 `annotation`（垄断性） | 全市场 |
| `financials(symbols)` | `shared.db` 的 `finance`（最近 5 个报告期） | 按批取，0.3s |

`financials()` 里的字段（`finance.raw_json` 挖出来的）：

```
rev_yoy_q   OperatingRevenueGrowRate_Q    单季营收同比
cut_yoy_q   NPParentCompanyCutYOY_Q       单季扣非同比
cut_cum     NPDeductNonRecurringPL        累计扣非
np_cum      NPParentCompanyOwners         累计归母
cut_q       cashflow.NPDeductNonRecurringPL_Q   最新单季扣非
cut_ttm     最近四季 cut_q 之和（凑不齐就 None，退回 cut_cum）
gm_q        (OperatingRevenue_Q - OperatingCost_Q) / OperatingRevenue_Q
rev_q       最新单季营收，用来算合同负债占营收的比重
cl          最新一季 ContractLiability（合同负债，预收款）
cl_prev     去年同季的合同负债（同比，不是环比）
```

2026-11-16 批（311 只）覆盖：财务 311/311、单季营收同比 303、单季扣非同比
304、季度成本 292、合同负债两期齐全 299。缺的落到 `unknown`，页面标「未核实」。

三条门槛在这一批的结果：不亏损 182 过 / 125 不过 / 4 未核实；增速 27 / 276 / 8；
供需 63 / 81 / 167（63 只过里：9 只两条都满足、37 只靠毛利率、**17 只只靠合同负债**；另有 8 只因两条打架落「未核实」）。

## 分档

分档**只看质量**，和加门槛之前完全一样；标识只出现在「超级成长股标识」列
（2026-11-20 之前叫「门槛」列）和顶部三张卡里。

| 档 | key | 条件 |
|---|---|---|
| 排除 | `out` | `Q < Q_HARD(45)`，或命中致命风险 |
| 待补数据 | `nodata` | 这一批没跑质量工具（或这只票失败） |
| 优先研究 | `buy` | 赔率分 ≥ `BUY_CUT`（5），且工具没有判回避/数据不足 |
| 观察 | `watch` | 其余（赔率分不够，或工具自己判了回避/数据不足） |

档内排序也还是老规则：赔率分降序 → 弹性（`strong` 0 / `weak` 1 / `unstable` 2 / 空 3）
→ Q → 代码。门槛不参与排序，名次和加它之前一模一样。

> ⚠️ **"工具判回避"是降到「观察」，不是「排除」**（`review.py:434-440`）。
> 但 `config.TIERS["out"]["hint"]` 的文案写的是「Q < 45 / 命中致命风险 / **结论就是回避**」，
> 与实际分流不符——看代码，别信那句 hint。这一处**没有修代码**（本次只动文档），
> 记在这里待改。

## 赔率分（0~15）

`SCORE_MAX = 15`，七项加分 + 两项扣分。**分低只影响排序，不会让票消失**。

| 项 | 分值 | 判据 |
|---|---:|---|
| 市值小 | 3 / 2 / 1 | `<50 亿` / `<100 亿` / `<200 亿`（`<`，不是 `≤`） |
| 估值低位 | 3 / 2 / 1 | PE/PB/PS 分位均值 `<25%` / `<50%` / `<70%` |
| 周期低位 | 2 | 是周期股**且**产能位置 = `low` |
| 盈利弹性 | 2 / 1 | `strong` 2 分、`unstable` 1 分（`weak` 0 分） |
| 垄断性 | 2 | 有标注且不是「无标注」/「待核实」 |
| 质量 | 2 / 1 | `Q ≥ 80` / `Q ≥ 65`（这一项是 `≥`，方向与前三项相反） |
| 目标价空间 | 1 | `upside ≥ 20%` |
| 趋势走弱 | **−2** | `TREND_PENALTY`，最重的单项扣分 |
| 估值分偏低 | **−1** | `V < V_WEAK(55)` |

3+3+2+2+2+2+1 = 15，正好等于 `SCORE_MAX`。

> **分档只认两项加分以外的门槛**：`Q_HARD` 与致命项。其余加分/扣分都只在排序里体现。
> 这也是"为什么小市值不一定排前、但要扣趋势分"的由来。

## 待办 / 已知将就

* **扣非 TTM 口径**：`finance` 表没有 `NPDeductNonRecurringPL_TTM`，只能拿最近
  四季单季扣非相加；缺一期就退回累计扣非（口径不同）。要精确，得让
  `stock_data_fetch` 落一个 TTM 字段。
* **金融股判不了供需**：银行/券商没有 `OperatingCost_Q`，毛利率算不出来；
  合同负债也常常不适用，所以基本都停在「未核实」。
* **合同负债的季节性只解决了一半**：用同比避开了 Q4 集中确认，但并购并表、
  大单预收仍会让同比失真。5% 占比门槛挡掉的是小基数噪声，挡不了这个。
* **第 ④ 条没实现**：库里没有"新业务收入"字段，要恢复得先抓数。

## 测试

```bash
cd /home/kevin/MyData/stock-value-invest-tool
python3 -m unittest tests.test_review_gates -v     # 硬门槛
python3 -m unittest tests.test_web -v              # 页面 + 老的赔率分规则

# 全套（含 web 的页面测试）
python3 -m unittest discover -s tests -t .
# Ran 213 tests ... OK（2026-11-23 实跑）
```

本模块自己的用例在 `tests/test_review_gates.py`；`tests/test_web.py` 里 19 个用例
按老名字 `from web import review` 引，走的是 `web/review.py` 那层 14 行 shim。

**这几条是踩过坑才写下的断言**（`tests/test_review_gates.py` 21 个用例）：

1. `unknown` 不挡买入——否则第 ③ 条永远 `unknown`，整页一只都进不来
   （`test_unknown_gate_does_not_block_buy`）；
2. 两条证据打架时必须落 `unknown`，不能"取更严的"（`test_conflicting_signals_are_unknown`）；
3. 合同负债占比 < 5% 时**不算流失**，只能 `unknown`（`test_small_contract_liability_is_ignored`）；
4. 归母 ≤ 0 必须判 `fail`，不能被当成"缺数据"（`test_losing_company_fails`）；
5. TTM 为正但**单季扣非为负**必须判 `fail`（`test_negative_single_quarter_fails_even_if_ttm_positive`）；
6. 门槛判 `fail` **不改变分档、也不改变名次**（`test_gate_fail_does_not_change_tier` /
   `test_gate_fail_keeps_buy_rank`）——这条是"只标识"承诺的机器看守。

> 七项满分之和 == `SCORE_MAX` 这件事**没有测试守着**。改 `MCAP_STEPS` / `PCT_STEPS` /
> `Q_STEPS` / `ELASTIC_POINTS` 里任一档时，记得手动把 `config.SCORE_MAX` 一起改，
> 否则页面上「11/15」那个比例会算错。
