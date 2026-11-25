# web — results.db 的本地看板（Flask + Chart.js）

把 `results.db` 渲染成一组页面：批次总览、四个工具的结论页、综合评估、冷热极点、
A股大势历史、个股合并页。**只读**——网站不写库，工具照旧把结果写进去，刷新页面即最新。

```bash
./run.py web                  # 常用入口；默认 127.0.0.1:5000 + debug
./run.py web --port 8000
./run.py web --no-debug       # 关掉调试模式（不自动重载）
python3 -m web                # 等价（不经过 run.py）
python3 -m web --db /tmp/copy.db   # 指到另一份结果库（run.py 没这个参数）
```

启动后打印一行 `看板：http://127.0.0.1:5000   结果库 <path>`，`Ctrl-C` 退出。
需要先装 Flask（不是标准库）：`python3 -m pip install flask`；没装时 `run.py web`
会打印这一行并以 **2** 退出，不是抛 traceback。

## 为什么是服务端渲染

数据量是"几十只票"，一次查询 2ms。为了这点数据引一个前端框架、再写一套
fetch / loading / empty 状态，除了把 `db.py` 的 SQL 搬到浏览器端，什么也没换来。
Jinja 直接吐 HTML，Chart.js 只负责画那几张图（`static/chart.umd.min.js`，本地文件，
不依赖 CDN）。

## 只读约定

`web/db.py` 里所有连接都是 `file:...?mode=ro`：

- 网站不该成为第二个写库的进程：`results.db` 是 WAL 模式，工具在写、页面在读，
  很容易踩到 `database is locked`。只读连接不加写锁，也不会顺手把 `-wal`
  checkpoint 掉。
- **schema 升级也不在这里做**：老库缺 `run.kind` 列时 `_kind_expr()` 退化成
  常量 `'batch'`，页面照常开，升级交给下一次跑工具（`stock_data_fetch/results.py`
  的 `_migrate`）。
- `tests/test_web.py` 里有 `TestReadOnly` 专门守着这条（2 项）。

## 页面地图（13 条路由）

| 路由 | 页面 | 数据来源 | 备注 |
|---|---|---|---|
| `/` | — | — | `redirect` 到 `/batches`（保留只为旧书签和 `run.py web` 打印的地址） |
| `/batches` | **首页** | `run` + 四张结果表 + `market_regime` | 全流程总览 + 每周五那批票四步齐不齐 + 顶部 A股大势横幅 |
| `/select` | 选股 | `select_result` | 池内换手分位（排序依据）+ 行业分布（名称不可点） |
| `/cycle` | 周期 | `cycle_result` + `shared.db.finance` | 三个判断的分布 + 毛利率波动 × 弹性散点；额外挂合同负债限定语 |
| `/quality` | 质量 | `quality_result` | Q × V 散点（结论用颜色区分）+ 结论分布 |
| `/top` | 顶部 | `top_result` | 距 250 日高点的横条 + 四因子状态矩阵 |
| `/review` | 综合评估 | `db.merged()` + `shared.db` 的 `annotation` / `finance` | **不查新表**，全在 `stock_review_assess.build()` 里现算 |
| `/extreme` | 冷热极点 | `results.db` 的 `price` / `price_stat` / `index_price` | 按日横截面 + 每日摘要，独立的下拉（不共用批次选择器） |
| `/trend` | A股大势 | `results.db.market_regime` + `shared.db.market_index_week` / `market_index_day` | 全量历史表 + 量能曲线（周/日两个视图，日线可翻历史区间） |
| `/trend/day.json` | 同上 | `shared.db.market_index_day` | 日视图翻页/换跨度/跳日期时按需取的一段（JSON） |
| `/stock/<code>` | 个股 | 四张结果表（按 `db.ORDER`） | 四边结论并排、标签切换；`?focus=` 决定打开时停在哪一页 |
| `/stock/<code>/report/<tool>` | 原版单股报告 | `payload.report_html` | 解压后原样返回；`?run=` 指定哪一次 |
| `/book` | 原书学习 | `static/five-rules-book-full-learning.html` | 5.5MB 自包含 HTML，只在末尾补一个返回浮标 |
| `/dataflow` | 代码执行流程 | 仓库根的 `dataflow.html` | 同上，补另一个配色的浮标 |

导航（`nav`，9 项，写在 `context_processor` 里）：
首页 / 选股 / 周期 / 质量 / 顶部 / 综合评估 / 冷热极点 / A股大势 / 原书学习。

`/` 与 `/dataflow` **不在**导航里：前者只是跳转，后者是"给自己看的流程图"。
高亮判据是 `request.path == href` 或 `request.path.startswith(href)`
（`/stock/...` 不会点亮任何一项，符合预期）。

## 两种"时间"要分开看

| | 字段 | 含义 |
|---|---|---|
| **批次** | `run.target_date` | 那个周五。四个工具同一天的结果是一批，页面上永远整批看，**不把周三单跑的一只票掺进周五的统计** |
| **运行** | `run.started_at` + `run.id` | 实际跑的时刻。同一只票可能被跑过很多次 |

个股页**只取 `run.id` 最大的那一次**（`db.symbol_row()`）。周三单只重算过，就不该
再显示周五那批的旧数。早期有一版把历次记录排成时间线，2026-11-15 去掉了：一只票
几十条，看完仍然不知道该信哪条。

`run.kind` 只有两个值：`batch`（`KIND_LABEL` → "批量"）/ `single`（"单只"）。
批次列表只统计 `kind='batch'`，单只随手的结论不混进周五那一批。
老库（schema v1）没有这一列时按 `batch` 算。

## 通用查询参数

四个工具页（`/select` `/cycle` `/quality` `/top`）和 `/review` 共用 `_pick()`：

| 参数 | 含义 |
|---|---|
| `?date=YYYY-MM-DD` | 看**指定批次**。那天没跑过工具时结果为空、页面照常开（`_bar` 上写"基准日 <该日期>"），不会 500 |
| `?run=<id>` | 看**那一次运行**；`date` 没给时用它的 `target_date` 补上 |
| 两者都不给 | 停在最新批次 |

`/stock/<code>` 另有 `?focus=top|quality|cycle`（不在 `TAB_TOOLS` 里就当没给），
`/stock/<code>/report/<tool>` 有 `?run=`，`/extreme` 和 `/trend` 有自己的一套
（见下两节）。

参数脏了（`?date=abc`、`?run=xyz`）不会 500：`_iso_or_none()` / `int()` 的
`try/except` 一律当"没给"处理。

## 各页要点

### `/batches`（首页）

三块：

1. **A股大势横幅** — `db.regime(conn)` 默认 `limit=8`，摆最近 8 周的三路状态；
   右上角主词是**基准日**（= 你跑的那天），`数据截至` 降为副行——它取三路里最旧
   的一天，常年比基准日早一到几天（乐咕晚一天 + 周换手要等本周收盘）。把 `数据截至`
   当主词，会让人看着「跑在 11-23、数据截至 11-20」以为面板停在上一行；
2. **批次卡片列表** — `db.batches(conn, limit=52)`，每个基准日一条，显示
   `n_tools/4 步`、跑过几次（`runs`）、缺哪几步（`missing` + `miss` 里的补跑命令）；
3. **汇总 / 计数** — `db.summary()` / `db.counts()`。

缺步的提示会区分"能补跑"和"周日/节假日不用补"：`_is_trading_day()` 先读交易日历
（`state/trading_calendar.json`，可用 `STOCKHUB_CALENDAR` 指到别处），读不到就退回
按周一至周五判断。补跑命令由 `_tool_command()` 生成，`top` 那条是
`./run.py top --from-select --date <day>`（其余是 `./run.py <tool> --date <day>`）。

### `/trend`（A股大势历史）

- `db.regime(conn, limit=None, start=, end=)`：`limit=None` = 不截断；
  `start` / `end` 是**闭区间**、按**基准日**过滤（不是 `data_asof`）——
  所以筛选结果不会因为中证晚一天而改变，那一天的判断仍然属于跑它的那个周五。
- 两个参数反着给（`start > end`）会自动交换，不报错。
- 页面顶部的卡片**永远展示最新一周**，不受筛选影响；筛选只作用于下面那张历史表。
- 量能曲线来自 `db.turnover_series()`：**这是唯一读 `shared.db` 画图的地方**
  （`market_regime` 一次运行一行，凑不出 260 根周线）。
  同一块面板上还有 `db.day_series()` 画的**日线视图**，读 `shared.db.market_index_day`。
  两个函数返回**同一种形状**（点列 + 三条 `*_series` + `dry/low/high/heavy_line`），
  所以 `trend.html` 里只有一套绘图代码，切换只是换个视图对象重画一遍。

**周/日是两个口径，不是粗细两个版本。** 日换手率按日累计，一周的量约等于五天之和，
所以同一个"近一年中位数"在两边差约 5 倍：周是 52 周 / 地量 1.0% / 取整 0.1%，
日是 252 交易日 / 地量 0.2% / 取整 0.01%。这两个数**不能互换**，也不能共用一个 Y 轴。

日线视图**不参与档位判定**：表格里的"量能"仍由 `stock_market_trend.rules.sentiment()`
按周线判。页面上写明这一点（`VIEWS.day.note`），否则会出现"图上放量、表里平稳"的错觉。
`market_index_day` 不存在时 `day_series()` 返回 `available=False`，"日"按钮置灰并
提示补跑命令，而不是画一张空图。

**日视图自带区间浏览器**（周图没有——它是 260 周固定背景）。日K 从 2014-07 起
3000 根，默认只画最近 250 根；想看更早的（"2025-11-12 以前那段"）有三条路：

- `‹ 前一段 / 后一段 ›`：按当前跨度整段整段地翻（`first - 1 天` / `latest + 1 天`
  做锚点，交易日个数 SQL 自己数）；
- 跨度 `1 / 3 / 5 年`：250 / 750 / 1250 个交易日（`DAY_MAX_N = 1300` 封顶——不是怕慢，
  是 1300 根摊在 1000px 上已经 1.3 点/像素，再密只剩一团墨）；
- 「看这天以前」日期框：`end=那天` 往前取一整段。

翻页走 `/trend/day.json?n=&start=&end=` 按需取那一段，不整页刷新——口径与首屏
`db.day_series()` 是**同一个函数**，参数脏了退回默认窗口不 500。每点多带一个
`rank`：相对**它自己往前 252 个交易日**的分位，不是相对所选窗口——否则跨度从
1 年切到 5 年，悬停里那句"近 1 年 X% 分位"就跟着窗口漂成假话。控件只在"日线"
下出现，切回周线整行 `hidden`（`syncRange()`）。

### `/extreme`（冷热极点）

刻意**不共用** `_bar.html` 那个批次选择器：它的选项是"哪天跑过工具"，这里是
"哪天有全市场价格"，两者天然不同步——`price` 每个交易日都能有，而四个工具一周
才跑一批。混用一个下拉会让人误以为"没跑工具的日子没行情"。

| 参数 | 默认 | 含义 |
|---|---|---|
| `?date=` | 最新有数据的那天 | 不在 `price_dates()` 里就退到最新 |
| `?q=` | 空 | 代码/名称片段搜索 |
| `?sort=` | `pct` | 排序字段，走白名单 `PRICE_SORTS`，未知值退回 `pct` |
| `?order=` | `desc` | 只有 `asc` 认，其余一律 `desc` |
| `?start=` / `?end=` | 空 | **只在 `?q=` 唯一命中时**生效：那只票历史曲线的区间 |

`?q=` 把页面切成五种形态（`db.extreme_page()` 返回的 `shape`）：

| shape | 什么时候 | 页面上是 |
|---|---|---|
| `empty` | 没有 `price` 表 / 一行都没有 | 一句"先跑 price" |
| `no_match` | 搜索没命中 | 搜索框 + "没找到" |
| `pick` | 命中多只 | 候选清单（点一只看历史） |
| `history` | 唯一命中 | 这只票的区间曲线 + 全部时点 |
| `market` | 没搜索 | 当天的全市场表（默认视图） |

排序时 `NULL` 一律排最后；PE/PB/PS 的"坏值"判定是 `IS NULL OR <= 0`（负 PE 是
亏损，绝对值没意义，`PE 升序` 时它会排最前面，看着像"最便宜的一批"其实是亏得
最狠的）。这与 `price_stat` 算中位数只取正值、`_fmt_pos()` 把负值显示成 `—`
是同一套口径。

`price_dates()` 取的是 `price` 与 `price_stat` 两张表的**并集**，不是"有摘要就只看
摘要"——`price_stat` 可以缺天（老库、或某天只跑了取数没跑 `--restat`），而那天在
`price` 里明明有数据；只认摘要的话那些时点会从下拉里凭空消失。

### `/review`（综合评估）

不查任何新表：`db.merged(conn, date)` 把四个工具的结论并成一只票一行，然后交给
`stock_review_assess.build(rows, moats, financials)` 现算。三类外部数据里只有两项
来自 `shared.db`：

```text
垄断性  shared.db.annotation  → db.moats()
财务    shared.db.finance     → db.financials(symbols)（只取门槛要用的字段）
```

`stock_review_assess` 自己不连库（只吃字典），所以能单测、也能换数据源；
`web/review.py` 是 14 行兼容层（`from stock_review_assess.review import *` +
几个常量），留着只因为 `tests/test_web.py` 有 19 个用例写的是
`from web import review`。**新代码请直接 `from stock_review_assess import build`。**

### `/cycle`（周期）的合同负债

比 `_page()` 多一步：给每行挂 `demand`（`_demand_suffix()`），数据来自
`shared.db` 的合同负债（`db.contract_liability()`）。

这一列**只做展示**，不改任何标签：判断③ 的分位是**产能周转率（供给端）**，
算法是干净的，塞一个资产负债表项进去这个数就没法复算了。所以做法与工具里已有的
`capacity_direction` 一样——**只加限定语，不改标签**。

门限：合同负债占年化营收 ≥ **2%** 才认（这批 113 只的中位数是 1.5%），同比
≥ `GATE3_CL_YOY_MIN` 记"回升"、≤ `GATE3_CL_YOY_FAIL` 记"走弱"，中间不给话。
比 `/review` 那道 5% 松——那边 CL 要让"供需"判过，是门槛；这边只是一句话，
严了就等于没有。措辞还跟着位置走：低位讲"能不能起来了"，高位讲"是不是还在撑着"。

### `/stock/<code>` 与 `/stock/<code>/report/<tool>`

四边结论并排，用标签切换（`TAB_TOOLS = ["top", "quality", "cycle"]`，
顺序固定，`?focus=` 只决定打开时停在哪一页）。四张结果表全部 404 才算查不到。

`views` 优先用 `payload.views_full`（比 `views` 多 `headline` / `metrics`）：
单只页那张卡要说清"为什么是这个结论"，只有状态词是不够的。

`/report/<tool>` 返回工具渲染好的原版报告（`payload.report_html`，gzip + base64
存库，`db.report_html()` 解压）。没有就是 404 加一句人话（"2026-11-15 之前跑的行，
或那次渲染失败"），而**不是**空页面。

**为什么报告要在工具那边渲染好再存库**：四个工具各有一个同名 `config.py`，
同一个 Flask 进程里没法同时 import 它们，所以看板只解压、不渲染——页面呈现的
就是当时那份报告，不随首页代码改动而变化。

### `/book` 与 `/dataflow`

两个都是"整页原样端出 + 补一个返回浮标"，且都按**文件 mtime 缓存**
（`app.config["BOOK_CACHE"]` / `FLOW_CACHE`）：重画一次图、重新生成一次书页，
mtime 一变缓存自然失效，不用手工清。

- `/book` 读 `web/static/five-rules-book-full-learning.html`，浮标用书页自己的
  CSS 变量上色（换主题会跟着变），位置在左下角（右下角被"术语提问"悬浮球占了）。
- `/dataflow` 读**仓库根**的 `dataflow.html`（由 `scripts/gen_dataflow.py` 生成）。
  不在 `static/` 复制副本：图是会重画的，副本最容易忘记更新。它没有 CSS 变量可借，
  浮标颜色写死（墨绿 `#2f6152`）。
- 两者都是 `_with_back_home()` 在 `</body>` 前插入；找不到 `</body>` 就原样返回
  （宁可不加，也不弄坏页面）。
- 文件缺失 → `abort(404)` 带 description，不是 500。

## 出错怎么显示

| 情况 | 结果 |
|---|---|
| `results.db` 不存在 | `db.connect()` 抛 `FileNotFoundError` → 500 页，提示"先把四个工具跑一遍" |
| 路由不存在 / 个股查不到 / 报告没存 | 404 页（`error.html`），带"上面的导航能回去" |
| 老库缺表缺列 | 不报错，页面对应块显示 `—` 或整块不渲染 |

`error.html` 只有一个 `code` + 一句 `msg`，两个 handler 共用。

## 目录与文件

```text
web/
  __init__.py        11 行，导出 create_app / main
  __main__.py        4 行，python3 -m web 的入口
  app.py            686 行：应用工厂 + 13 条路由 + 格式化工具
  db.py            1270 行：所有 SQL 与页面数据组装（**路由里不写 SQL**）
  review.py          14 行：兼容层，规则已搬到 stock_review_assess/
  templates/        17 个 Jinja 模板（1 个壳 + 16 个整页/片段）
  static/           style.css / chart.umd.min.js / chart-filter.js /
                    stock-jump.js / price-history.js / 书页 HTML
```

模板分两类：`base.html` 是所有页面的壳（顶栏 + 导航 + 《股市真规则》五条规则 +
脚注），`_` 开头的是片段（`_bar.html` 批次选择器、`_gates.html` 硬门槛、
`_stock_*.html` 个股页的三个标签块），其余是整页。

### `payload` 套了两层，要剥

工具写库时 `results.save()` 存的是"整行 dict"，而调用方又在行里放了一个
`payload` 字段，于是库里的 JSON 是**套了一层**的。所以：

```text
db._load(row)   → row["data"] = json.loads(payload 列)      读不出来给 {}
db.inner(row)   → 沿 data["payload"] 向下剥，最多 3 层        页面只认这个
db.report_html(row) → inner(row)["report_html"] → gzip → base64 → HTML
```

`_js()` 遇到坏 JSON 返回空 dict，`inner()` 剥不到就返回原 dict——老库、手改过的库
都不该让页面 500。

模板里能用的格式化工具由 `create_app()` 一次性注入（`app.jinja_env.filters`），
不用在模板里 import：

| 过滤器 | 作用 |
|---|---|
| `pct` | `0.0123` → `+1.2%`（带符号） |
| `spct` | 已经是百分数的值 → `+2.31%`；`|v| < 0.005` 时不带符号 |
| `num` | 千分位数字，`None` → `—` |
| `ratio` | 两位小数（PB / PS 这类比值） |
| `yi` | 元 → 亿元（市值 1.1e14 写成"元"没人读得出来） |
| `pe` | PE(TTM)：`<= 0` 显示**亏损**而不是负号数字；`None` 是 `—`（亏损和没数据是两回事） |
| `pos` | 只对正值有意义的比率（PB / PS）：负值给 `—` |
| `date` / `clock` / `weekday` | 取前 10 位 / `11-15 15:23` / `周五` |
| `tone` | 状态词 → 色调 class（见下） |
| `poslabel` / `cycle_state` | `low/high/neutral/na` → 低位/高位/中位/不适用；`周期·高位` → `高位` |
| `quoteurl` | `sh600519` → 东财行情页（认不出返回空串，模板据此不渲染链接） |
| `detail` / `hasreport` | 从 payload 里取"明细" / 判断有没有原版报告 |

`quoteurl` 的路径**不统一**：沪深是 `quote.eastmoney.com/sh600519.html`，
北交所是 `quote.eastmoney.com/bj/920992.html`（少了那道斜杠会 404）；末尾
`#fullScreenChart` 是"打开就是全屏 K 线"。

### 状态词 → 色调

`TONE_MAP` 是一个**有序**元组，`tone()` 按顺序做**子串**匹配，命中第一个就返回，
认不出来的一律 `info`（刻意不用 `warn`，免得虚报风险）。

实测（`tone()` 的真实返回）：

| 状态词 | 色调 | | 状态词 | 色调 |
|---|---|---|---|---|
| 天量 | `hot` | | 地量 | `good` |
| 放量 | `warn` | | 缩量 | `ok` |
| 平稳 | `muted` | | 偏强 / 通过 / 低估 | `good` |
| 偏贵 / 高位 / 命中 / 回避 | `warn` | | 偏弱 / 低位 / 走平 | `muted` |
| 中性 / 中位 / 观察 | `info` | | **空头确认 / 极值 / 便宜 / 恐慌 / 偏冷 / 正常** | `info`（未收录） |

最后一行是现状：这几个词不在 `TONE_MAP` 里，落到兜底的 `info`。**只影响配色，
不影响任何判定**——状态词本身是 `stock_market_trend` 定格的。

## CLI 参数

| 入口 | 参数 |
|---|---|
| `python3 -m web` | `--host`（默认 `127.0.0.1`）/ `--port`（默认 `5000`）/ `--db`（默认 `results.db`）/ `--debug` \| `--no-debug`（**默认 debug 开**） |
| `./run.py web` | `--host` / `--port` / `--debug` \| `--no-debug`——**没有 `--db`** |

差别的原因：`run.py` 把 `DB_PATH = ROOT/results.db` 直接传给 `create_app()`，
而 `create_app()` 的入参优先级最高（高于 `STOCK_RESULTS_DB`）。所以：

```text
想给看板换一份库  →  python3 -m web --db /tmp/copy.db
                     （或者改 web/db.py 认的 STOCK_RESULTS_DB 再走 -m web）
```

`./run.py web` 永远读仓库根那份 `results.db`。共用 `STOCK_RESULTS_DB` 的是
`run.py` 里另一处（`_price_count()` 那行提示），别混淆。

`--host 0.0.0.0` 能让局域网访问，但那意味着把持仓和结论摊给同网段所有人——
默认 `127.0.0.1` 是有意的。

## 已知边界（现状，不改代码）

1. **`TONE_MAP` 有重复项**：`("加速", "warn")` 在前、`("加速", "info")` 在后，
   子串匹配命中第一个，所以第二条**永远不会生效**（`加速` 恒为 `warn`）。
   无害，但读到会以为有两套配色。
2. **`/trend` 的筛选不影响顶部卡片**：卡片固定展示最新一周，这是有意的
   （"现在怎么样"不该被历史筛选带走），但页面上没有额外提示，容易以为筛选也没生效。
3. **`/extreme` 的 `?start` / `?end` 只在唯一命中时生效**，`market` 形态下传了
   也没用；参数不会报错，只是被忽略。
4. **`web/db.py` 里有一份 `rules.py` 常量的副本**（`TURNOVER_DRY_LINE` /
   `TURNOVER_LOW_MULT` / `HIGH` / `HOT` / `HEAVY_WIN` / `_EPS`）。两份必须同值，
   靠 `tests/test_web.py` 的一条断言钉住；改了 `rules.py` 忘了改这边，测试会挂，
   但**页面本身不会报错**（只是图上的线算了另一套）。
5. **`web/db.py` 的模块级 `RESULTS_DB` / `SHARED_DB` 在 import 时求值**。
   用 `create_app(path)` 换库时，走 `app.config["DB_PATH"]` 的那些页面正常，
   但任何**不传 db 参数**的调用仍读 import 时的路径。目前只有 `turnover_series()` /
   `day_series()` 属于这类（都读 `shared.db`，与结果库无关，所以实际没影响，
   但测试里换库要记得同时改 `webdb.SHARED_DB`）——新增函数时记得显式传 `db`。
6. **`/book` 依赖一个 5.5MB 的生成文件**。仓库里没有这个文件时该页 404，
   导航里那一项照样在。
7. **没有登录、没有写操作、没有分页**。`/batches` 最多 52 批、`/extreme` 一次
   拉一天的全市场（约 5500 行），图表靠前端过滤（`chart-filter.js`）。

## 测试

```bash
cd /home/kevin/MyData/stock_value_invest_tool
STOCKHUB_TMP=$PWD/.testtmp python3 -m unittest tests.test_web -v
```

**105 项，2026-11-23 实跑全绿**（`tests/test_web.py`，不是一个包内的 tests 目录）。
按类拆开：

| 类 | 项数 | 盯什么 |
|---|---|---|
| `TestExtremePage` | 15 | 五种 `shape`、排序白名单、区间筛选、坏值排最后 |
| `TestNavAndLinks` | 9 | 9 项导航、高亮、链接不 404 |
| `TurnoverSeriesTest` | 9 | 三条线逐周重算、窗口不足、`partial_index`、**与 `rules` 常量对齐** |
| `TestBatches` | 8 | 批次分组、缺步提示、`n_tools/4` |
| `TestStockReports` | 8 | gzip payload 解压、`?run=` 定位、缺失时 404 |
| `TestPages` | 7 | 四个工具页能开、空库不炸 |
| `DemandSuffixTest` | 5 | 合同负债限定语的 6 种措辞与门限 |
| `TestRegimeBanner` | 4 | 横幅三路/缺一路/表不存在 |
| `TestStockDetail` | 4 | 个股页四边并排、`?focus=` |
| `TestLabels` | 4 | 状态词 → 中文 / 色调 |
| `TestReviewPage` | 3 | 综合评估页能开 |
| `ContractLiabilityTest` | 3 | 合同负债取数与同比 |
| `TestRegimeHistory` | 2 | 闭区间筛选、卡片不受筛选影响 |
| `TestReadOnly` | 2 | **不写库** |
| `AnnotationStateTest` | 2 | 垄断性标注降级 |
| `TestStockHistorySections` | 1 | 去掉的"历次结果"没有回来 |
| `TestReviewRules` | 19 | 综合评估规则本身（借 `from web import review` 引，规则在 `stock_review_assess/`） |

合计 105。每个用例在 `setUp()` 里 `tempfile.mkdtemp()` 造一个临时 `results.db`
并把 `STOCK_RESULTS_DB` 指过去，不碰仓库里那两份真库；`tearDown()` 复原环境变量。
