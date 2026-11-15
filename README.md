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

中间会打印汇总（每步耗时、退出码、写进了哪张表），日志在 `logs/run-<时间戳>.log`。

屏幕上只有关键信息：**逐只失败的播报不打印**（选股一批 4300 只里 145 只
K 线不足、周期 3 只利润表为空，这种行原本要占掉半屏），只留聚合后的
「145 只历史不足」「完成 35 只，失败 3 只」。日志文件里一行不少，要看就：

```bash
./run.py all --verbose      # 屏幕上也不省略
```
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
./run.py backup                            # 备份两个数据库
```

批量结果同样进 `results.db`，看板的「顶部」页按「距 250 日高点」从近到远排，
一眼看出谁贴着一年高点。

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

| 页面 | 看什么 |
|---|---|
| `/` 概览 | 当前批次（可切换）+ 一张合并表：一只票一行，选股 / 周期 / 质量 / 顶部并排 |
| `/batches` 批次 | 每个基准日一行，四个工具各跑没跑、跑了几只、几点跑的；点一格进那一批 |
| `/select` 选股 | 换手分位 × 20 日换手散点，行业分布 |
| `/cycle` 周期 | 毛利率 σ × 弹性散点，产能分位 |
| `/quality` 质量 | Q × V 散点，结论分布 |
| `/top` 顶部 | 距 250 日高点（最接近的 20 只）、动量状态、四因子矩阵 |
| `/stock/<代码>` | 单只票：四张卡 + 四因子 + 旁证 + 周期三判断 + **历次结果（按跑的时刻）** |
| `/runs` 运行记录 | 每次运行的基准日、批量/单只、行数、耗时、命令行 |

页面上的每个数字都来自 `results.db`，没有第二份缓存；把库删掉重跑一遍，
页面自然就更新了。`--db` 可以指向别的库（比如一份备份）用来对比。

### 批次和单只是两回事

库里的运行记录带 `kind`：`batch` = 一次覆盖整份名单（每周五那批），
`single` = 单只随手跑一次（`--stock`）。看板据此分开显示：

* **批次页 / 工具页**只统计 `batch`。周五那一批就是四个工具 `target_date`
  相同的那些运行；`?date=2026-11-06` 看上周那批，`?run=12` 看某一次具体运行。
* **个股页的「历次结果」**两种都列，按跑的时刻倒序，每条都标着它属于哪一批、
  是批量还是单只。同一只票被批量算过又被单只重算过时，一眼能看到两次的差别。

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
