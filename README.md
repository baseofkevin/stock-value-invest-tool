# stock-value-invest-tool

个人股票研究工具箱：每周五收盘后跑一遍选股 + 质量 + 周期，买入后按需查顶部。

四个功能模块，共用一套本地数据层（`stock_data_fetch` + `shared.db`）——库里有的
数据直接用，没有的才联网，拿到的写回库，下次（乃至其它三个模块）复用。

| 模块 | 干什么 | 入口 |
|---|---|---|
| `stock_kline_screen` | 选股：5 日涨幅池 → K 线形态 → 板块/财务过滤 | `run.py select` |
| `stock_cycle_elastic` | 周期弹性：是不是周期股 / 弹性强弱 / 周期位置 | `run.py cycle [--stock 代码]` |
| `stock_quality_screen` | 质量初筛：质量分 Q + 估值分 V + 14 项风险检查 | `run.py quality [--stock 代码]` |
| `stock_judge_top` | 顶部判断：四因子观察股票是否到顶部 | `run.py top <代码>` / `top --csv` |

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

它会依次做四件事，并在最后打印汇总（每步耗时、退出码、四个产物目录）：

```
⓪ build_calendar        更新交易日历（日历不更新会让下游静默产出上一周的报告）
① stock_kline_screen    选股 → output/kline-screen/选股_<日期>.csv
② stock_cycle_elastic   周期弹性 → output/cycle-elastic/<日期>/
③ stock_quality_screen  质量初筛 → output/quality-screen/<日期>/
```

`stock_judge_top`（顶部判断）不在全流程里：它是**买入之后**用来判断股价有没有到顶部区域的，
单只、随时跑。

```bash
./run.py top sh600519                      # 基准日自动取「今天（过了 15:05）」
./run.py top sh600519 --date 2026-11-13
./run.py top sh600519 sz000001             # 一次多只

./run.py top --from-select                 # 批量：把当天的选股结果整体扫一遍
./run.py top --csv 我的持仓.csv            # 批量：自己给清单（读「代码」列）
```

批量会多出一个索引页 `output/judge-top/四因子观察-批量_<日期>_<时间>.html`，
按「距 250 日高点」从近到远排，一眼看出谁贴着一年高点。

## 单独跑某一步

```bash
./run.py select  --date 2026-11-20         # 只选股（只能批量）
./run.py cycle                             # 周期弹性批量（用当天的选股 CSV）
./run.py quality                           # 质量初筛批量（用当天的选股 CSV）
./run.py calendar                          # 只更新日历
```

### 单只也能跑

周期弹性和质量初筛都支持单只，不需要当天的选股 CSV：

```bash
./run.py cycle   --stock 600519            # 周期弹性：这一只是不是周期股、位置在哪
./run.py quality --stock 600519            # 质量初筛：Q/V 分数与风险检查
./run.py top     600519                    # 顶部判断
```

菜单里也是同样的：选 3 或 4，股票代码处**直接回车 = 批量跑当天 CSV**，填代码 = 只跑那一只。

注意单只模式的基准日由程序自己决定（今天过了 15:05 就用今天，否则上一个交易日），
**不接受 `--date`**——那两个程序没有日期参数。要按历史某天出报告只能走批量模式。

子进程参数可以透传：`./run.py quality --stock 600519 --force`。

下游（cycle/quality）认的是 `output/kline-screen/选股_<基准日>.csv`，所以它们的
基准日必须和那天的选股一致；不给 `--date` 就按「今天（过了 15:05）」算，
和 `stock_data_fetch` 内部的 `target_date()` 同一套规则。

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
4. **选股 0 只时下游没有输入**，脚本在交接处停下，不会带着空表跑到一半才炸。

日志：每次运行写 `output/logs/run-<时间戳>.log`（完整子进程输出，事后可查）。

## 环境

- Python 3.13，只用标准库。
- 数据库 `shared.db`（SQLite，WAL）。备份在 `backup/`，不进版本库。
- 交易日历 `state/trading_calendar.json`（`build_calendar` 生成）。

## 测试

```bash
python3 -m stock_data_fetch.tests                  # 数据层 12 个文件
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
