# stock_judge_top —— 单股四因子独立观察（共享数据层版）

> 2026-11-15 从 `~/MyData/stock-judge-top-tool` 迁移重建。原版 1911 行、
> 纯标准库零依赖；本次迁移**逻辑层一字未动**（indicators / finance /
> judgement / views / render / log 全部原样拷贝），只重写了数据层与入口。

    python3 -m stock_judge_top sh600519 [--date 2026-11-13] [--json] [--refresh]
    python3 -m stock_judge_top --csv 选股.csv [--workers 4] [--quiet]

四因子（动量 / 换手 / 业绩 / 量价）各自独立出结果，不打分、不合并总分、
不判顶——这是原版的设计立场，迁移不改。

`--date` 不给时取**共享库的目标交易日**（今天过了 15:05 才是今天，否则上一个
交易日）。原来默认 `date.today()`，周末跑会先报「末端不足（库 11-13 / 需 11-15）」
再报「当前无 K 线（可能停牌）」——把"今天不是交易日"说成"停牌"。

## 0. 批量模式（2026-11-15 新增）

持仓一多、或者想在下周五的选股结果上整体扫一遍"谁已经贴着 250 日高点"，
一只只敲太慢：

    python3 -m stock_judge_top --csv output/kline-screen/选股_2026-11-20.csv --workers 4

- 读 CSV 的**代码列**（`代码` / `股票代码` / `证券代码` / `code` / `symbol`）；
  没有表头时取第一列。同一只出现多次只跑一次（保序）。
- 单只失败不影响其余，最后汇总「成功 N / 失败 M」并列出失败代码。
- 每只照旧出一份 HTML，另外多一个索引页
  `output/judge-top/四因子观察-批量_<日期>_<时间>.html`：按「距 250 日高点」
  从近到远排序，一眼看出谁在高位。
- 并发是**每线程一个库连接**（SQLite 连接不能跨线程），`--workers` 默认 4。

## 1. 与原版的差异（只有数据层）

| 原版（westock MCP） | 本版（共享库 hub） |
|---|---|
| `data_kline` fq="qfq"，3 段×1 年 limit=300，缺段抛错 | `ensure_many("kline", ...)` 取 **raw**，`factor.adjusted()` 本地算 qfq |
| 换手率 `exchange` 上游直接给 | **折算**：`amount ÷ 流通市值 × 100`（见 §2.2） |
| `data_finance` fields=all num=60 | `ensure_many("finance", ...)` + `finance_full()["income"]` |
| `data_quote` 取名称/存在性 | profile 域（`stock` 表） |
| 单股 JSON 缓存 `cache/top_*.json` | 删除，缓存归 hub（coverage 闸） |
| westock 令牌 / MCP URL / 外部 CLI 回退 | 删除，`preflight()` 六道闸 |

CLI 接口不变；`--refresh` 现在透传为 hub 的 `force`（清重试标记后重取），
新增 `--db`（透传共享库路径）。

## 2. 三个口径决定

### 2.1 qfq 本地算（不改共享库地基）

共享库 kline 只存 raw（`fp=fq=raw`）。前复权是"同一份 raw × 不同因子"，
不该为某个工具再存一份复权序列。`factor.adjusted()` 按 dividend 表算
`qfq(t) = raw(t) × factor(t) / factor(anchor)`，锚点 = 基准日（与原版
westock qfq 的锚一致）。**取 kline 前必须先 ensure dividend**——没取过
dividend 时因子恒为 1.0，qfq 会静默等于 raw（这是最危险的一类错）。

价格类字段（`last`/`high`/`low`/`open`）取 qfq；`volume`/`amount` 保持 raw
（量在除权日不缩放，`factor.PRICE_COLS` 的注释就是这条）。

### 2.2 换手率折算

共享库 kline 没有 `exchange` 列，而 indicators 的换手因子（av20/ratio/AVG20）
需要它。折算式：

    exchange = amount ÷ circ_mv × 100    （amount 缺失时退回 close(raw) × volume）

与 `stock_kline_screen/turnover.py` 的 `turnover20` 同款：分母用 quote 域
"当期一份"的流通市值，历史某日的真实流通市值不在库里。两边接受同一个近似。
流通市值取不到时换手类指标显示 —，并在 warnings 里说明，不静默。

### 2.3 根数门槛 30、深度期望 750

原版分 3 段各拉 1 年（防段间空洞，缺段抛错）。hub 的 kline 是一次连续区间，
空洞由 `InsufficientDepth` / `HoleUnreachable` 表达，不需要人工分段对账。
指标实际用的最长窗口是 250 日，库里 300 根即可全量计算；不足 250 根时沿原版
语义只警告不拒算，不足 30 根才报错。

## 3. 测试

    cd /home/kevin/MyData/stock-value-invest-tool && \
    python3 -m stock_judge_top.tests

8 个文件（unittest）：indicators / finance / judgement / views / render /
evaluate / judge（CLI 与退出码）+ 新增 hubdata（§2.1/§2.2 的映射断言）。
全部离线。原版的 test_mcp / test_westock / test_data 随数据层一起删除。

## 4. 冒烟记录（2026-11-15）

- `sh600519 --date 2026-11-13 --verbose`：四因子完整报告，HTML 写入
  `output/judge-top/贵州茅台_*.html`；业绩卡片 2026Q3（402.67 亿营收，
  同比 +3.1%）——财务链路（finance_full → westock 字段名）数值正确。
- `sh600000 --json`：JSON 契约（quote 剔除 / views 四键 / warnings 空 /
  av20=0.196%）。
- `sz300750`（库里无 K 线）+ 恰逢腾讯 WAF 封禁窗口：exit 1、错误信息清晰，
  失败路径按预期工作。

## 5. 已知限制

- **换手率分母是当期市值**：补跑历史基准日时，一年前的换手率会用今天的
  流通市值折算（限售解禁会漂移）。当期运行无此问题。
- **`--json-full` 的 quote 是 profile 行**（name/industry/list_date），
  不是原版的实时行情快照——四因子与展示只消费 name。
