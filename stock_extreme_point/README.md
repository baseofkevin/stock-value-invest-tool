# stock_extreme_point — 全市场价格 / 估值快照与冷热摘要

一个独立的小工具：把**某个交易日的全市场**收盘价 + PE(TTM) + PB(MRQ) + PS(TTM) +
市值取回来，落进两个库，再算出那一天「涨跌家数 / 涨跌停 / 估值中位数」的摘要。

```bash
python3 -m stock_extreme_point                     # 今天（过了 15:05）或上一交易日
python3 -m stock_extreme_point --date 2026-11-16
python3 -m stock_extreme_point --date 2026-11-16 --force
python3 -m stock_extreme_point --restat            # 只重算摘要，不联网
python3 -m stock_extreme_point --sync              # 原始库 → 结果库，不联网
python3 -m stock_extreme_point --prune-keep 30     # 顺手删 30 天前的
python3 -m stock_extreme_point --code 002096 --date 2026-11-03  # 只补一只（不删当天其它行）
```

日常用 `./run.py price`：多一层进度条、日志和交易日历校验，参数一一对应。

## 为什么单独一个包

它和 `stock_data_fetch` **没有代码依赖**，只有文件路径约定（同一个仓库根，
同样的 `STOCK_SHARED_DB` / `STOCK_RESULTS_DB` / `STOCKHUB_CALENDAR` 环境变量名）。

全市场一天只有 6 个上游请求，不值得为它拖进 hub / router / coverage / throttle
那一整套，也不该让「取一天价格」被数据层的 schema 版本闸卡住。所以：

- 抓取、落库、摘要口径全在这个包里；
- `shared.db` **不需要**为它升 schema 版本（建表全是 `IF NOT EXISTS`）；
- 数据层回到 `SCHEMA_VERSION = 6`，`need` / `router` / `sources` 里没有 `price` 域。

## 数据流向

```text
上游（东财 RPT_VALUEANALYSIS_DET 按日横截面 6 个请求 + 沪深300 日 K 1 个请求）
  └─ shared.db  price + index_price          原件：整天整份替换
       └─ results.db price + index_price     复制品（看板只读它）
            + price_stat                     摘要：与 price 同事务写入
```

**先写原件、再从原件复制**，而不是「抓一次写两个库」。复制这一步就是 `--sync`
的那段代码，结果库缺一天永远能用同一个动作补上，而且不联网。抓取失败也不会
影响两个库里已经存下来的历史。

`--code` 是例外下的**单只 upsert**：只改 `(date, symbol)` 这一行，不执行整天替换，
因此不会把同一天的其它股票删掉；结果库的 `price_stat` 会按当天已有全部行重算。

| 库 | 表 | 粒度 |
|---|---|---|
| `shared.db` | `price` | 一天 × 一股（约 5500 行/天） |
| `shared.db` | `index_price` | 一天 × 一个指数（目前只有沪深300） |
| `results.db` | `price` | 同上（看板读这份） |
| `results.db` | `index_price` | 同上（看板读这份） |
| `results.db` | `price_stat` | 一天一行（摘要） |

`price` 两个库都有是**刻意的冗余**：一份是原件，一份是看板要的复制品。看板只读
`results.db`，所以上游清理、抓取失败都不会让页面上的历史时点跟着消失。

## 表结构

### `price`

| 列 | 含义 |
|---|---|
| `date` | 交易日 `YYYY-MM-DD` |
| `symbol` | `sh600519` / `sz000858` / `bj920992` |
| `name` | 上游当日给的简称 |
| `close` | 收盘价（元） |
| `pct_chg` | 涨跌幅，**百分数**（`-1.55` = 跌 1.55%） |
| `pe_ttm` | 市盈率 TTM，亏损为负或 NULL |
| `pb_mrq` | 市净率 MRQ |
| `ps_ttm` | 市销率 TTM（本表存在的理由：别的免费源都不给） |
| `total_mv` / `circ_mv` | 总市值 / 流通市值（元） |
| `src` / `updated_at` | 来源与写入时间 |

主键 `(date, symbol)`，索引 `symbol`。两个库的列**完全一致**，DDL 由
`schema.PRICE_DDL` 一处生成。

### `index_price`

`date` / `symbol` / `name` / `close` / `pct_chg` / `src` / `updated_at`，
主键 `(date, symbol)`。目前只存沪深300（`sh000300`，显示为 `000300.SH`），
来源是东财 `push2his` 日 K 接口。

指数**故意不进 `price`**：`price` 每一行都是一只可交易股票，汇总卡按它算
「5579 只 / 涨跌家数」；指数没有 PE/PB/PS 和市值，塞进去会让家数和统计口径失真。

### `price_stat`（只在 `results.db`）

`n` / `n_up` / `n_down` / `n_flat` / `n_limit_up` / `n_limit_down` /
`n_pe` / `n_pb` / `n_ps` / `pe_median` / `pb_median` / `ps_median` /
`mv_sum` / `src` / `updated_at`，主键 `date`。

口径只在 `stat.py` 一处定义：

- **涨跌停**：主板 9.8 / 创业板与科创板 19.8 / 北交所 29.8 / ST 4.8；
  ST 判断先于板块（北交所除外，那里没有 5% 限制）。
- **中位数只取正值**：亏损股的负 PE 混进来会把全市场中位数拉到个位数，
  看起来「遍地便宜」，其实是一半公司在亏。`n_pe` / `n_pb` / `n_ps` 记录
  参与计算的只数。
- **平盘**：`|涨跌幅| < 0.005`。

## 几条硬约定（每条都对应过一个坑）

1. **整天整份替换**：先 `DELETE WHERE date=?` 再批量插入，一个事务。上游这次
   5579 只、上次 5500 只，逐行 upsert 会把上次多出来的留在库里，悄悄拉偏
   「这天全市场 PE 中位数」，而任何地方都不会报错。
2. **价格与摘要同事务**：分开写会出现「价格换了一批、摘要还是上一批」，
   页面上卡片和表格对不上，却没有报错。
3. **写结果库失败不影响原始库**：`shared.db` 是原件，写成功了结果库随时能
   `--sync` 补；复制失败只 warn 一行。
4. **缺失一律 NULL，不填 0**：亏损股的 PE 是负的或空的，填 0 会被当成「极度便宜」。
5. **上游混进别的交易日直接报错**：校验的是上游原值 `TRADE_DATE`，不是映射后的行
   ——行里的 `date` 是自己填的，拿它比等于自己跟自己比。

## 上游与边界

全市场：东财 `datacenter` 的 `RPT_VALUEANALYSIS_DET`，`filter=(TRADE_DATE='YYYY-MM-DD')`，
一页 1000 行，全市场 5579 行 / 6 页。沪深300：东财 `push2his` 日 K，一次请求取当天一根。字段 `SECUCODE`（`600519.SH`）是唯一能拼出
`sh600519` 的字段——`SECURITY_CODE` 丢了市场，沪深 `000xxx` 会撞在一起。

- **收盘快照**：上游当天收盘后才发布。盘中或非交易日查「今天」返回
  `9201 返回数据为空`，CLI 报错退出并提示最近一份是哪天；**不静默回退**。
- **B 股剔掉**：接口会返回 `900xxx.SH` / `200xxx.SZ`，而项目其余数据只有 A 股 +
  北交所。按前缀剔除，不用白名单（交易所新增代码段时白名单会静默漏新股）。
- **PS 只有这条路**：腾讯 `data_quote` 只给 PE/PB；东财 `push2` 行情接口只有
  「营业收入」，自己除等于再打 5000 次。
- **连接中断**：连续请求后东财会直接掐连接（`RemoteDisconnected`），`source._get`
  接住并转成 `SourceError`，不会让一个看不懂的异常穿过 CLI。

## CLI

| 参数 | 作用 |
|---|---|
| `--date YYYY-MM-DD` | 指定交易日；不给就用交易日历定（今天过 15:05 且是交易日才算今天） |
| `--force` | 忽略库里已有的那天，重取（整天整份替换） |
| `--restat` | 只按结果库已有的 `price` 重算 `price_stat`，**不联网**；配 `--date` 只算一天 |
| `--sync` | 把 `shared.db` 已有的日子（`price` + `index_price`）复制/覆盖到 `results.db`，**不联网** |
| `--prune-keep N` | 取完后删掉 N 天之前的 `price`；0 = 不清理（默认） |
| `--shared-db` / `--results-db` | 换库路径调试 |
| `--calendar` | 换交易日历 JSON |

退出码：0 成功 / 1 取数或写库失败 / 2 用法错误（含「不是交易日」）。

## 看板

`./run.py web` → 导航「冷热极点」（`/extreme`）读 `results.db` 的
`price` + `price_stat` + `index_price`：

- 下拉选时点 → 全市场表（框内滚动、表头固定），按涨跌幅/PE/PB/PS/市值排序；
- 每行「打开 ↗」跳东方财富行情页（沪深 `sh600519` / 北交所 `bj/920992` 路径不同）；
- 五张卡：涨跌家数、涨跌停、PE 中位数、PB/PS 中位数，以及沪深300 当日收盘/涨跌幅；
- 搜索代码或名称 → 一只票在 `price` 里的全部时点；可选起止日期，
  画收盘价曲线（点上标注累计涨跌幅，Tooltip 含当日/累计），缺口断开，
  表格按时间正序并同时给当日/累计涨跌。

PE/PB/PS 排序时负值（亏损）一律排最后，与中位数只取正值同一个口径。

## 测试

```bash
cd /home/kevin/MyData/stock-value-invest-tool
python3 -m stock_extreme_point.tests.test_extreme_point
```

离线、不联网：假 reader 注入 `source.fetch_day(get=...)`，覆盖解析 / 分页 /
B 股剔除 / 混日期拦截 / 摘要口径 / 两库落库 / CLI / 建表脚本。临时库放在仓库内
`.testtmp/`（这台机器 `/tmp` 只有 10MB tmpfs）。

真上游冒烟（会联网、写临时库）：

```bash
python3 -m stock_extreme_point --date <最近交易日> \
  --shared-db .testtmp/smoke/shared.db --results-db .testtmp/smoke/results.db
```

这个包不经过数据层的盘中闸（它不 import `stock_data_fetch`）：日期由 `--date` 或
交易日历定，数据有没有由上游回答。
