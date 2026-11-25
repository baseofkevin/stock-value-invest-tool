"""配置：路径、闸门参数、节流参数。

原则：**所有可调数值只在这一个文件里**。别的模块不许出现魔法数字。
这样"把限频间隔从 1600ms 改成 900ms"是一处改动，而不是满仓库 grep。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---- 不可配置的常量 ----------------------------------------------------------

SCHEMA_VERSION = 6
"""与 `schema.sql` 里的 `PRAGMA user_version` 必须一致。不符就拒绝启动。

**v1 → v2（2026-11-14，第 7 步）只做了一件事：加 `scan` 表（按日全市场横截面）。**
纯新增表，不改任何既有列，所以 `db.upgrade()` 允许它自动迁移——
`schema.sql` 全部是 `IF NOT EXISTS`，对老库执行一遍等于只把新表补上。
再往后的版本如果动了列或主键，仍然只能删库重建（数据可再生）。

**v2 → v3（2026-11-14 晚）给 `snapshot.domain` 的 CHECK 加了一个值 `gbalance`。**
这是第一次改既有约束——`CREATE TABLE IF NOT EXISTS` 不会更新已存在表的 CHECK，
所以不能靠"重放 schema.sql"完成，必须在 `db.UPGRADE_STEPS` 里做一次表重建
（建新表 → 拷数据 → 删旧表 → 改名）。那条迁移可重入，理由写在那里。

**v3 → v4（2026-11-14 深夜）给 `stock` 表加 `sector` / `business` 两列。**
这是重构 quality-screen 时暴露的缺口：上游 `data_profile` 明明返回主营业务描述
（`business`："药品和医用医疗器械的研发、生产和销售。"），共享层却只存了
name / industry / list_date，于是报告第一节「生意本身」空着、行业也退化成
「暂无披露」。加列走 `ALTER TABLE ADD COLUMN`——不重建表、不拷数据，
SQLite 的原生轻量操作，比 v3 那次表重建安全得多。已存的 35 只票 `business`
为空，重跑一次 profile 就会补上（`COALESCE` 保证不清掉已有值）。

**v4 → v5（2026-11-14）新增 `annotation` 表：人工标注入库。**
垄断性标注原本是 quality-screen 目录下的 `annotations/moat_annotations.yaml`，
问题有三个：跟着代码目录走（换机器/换工作区就丢）、改一条要动文本文件、
四个工具只有 quality 能读到。现在入库后可被任何工具取用，还能用 CLI 维护。
这一步只**建表**，`CREATE TABLE IF NOT EXISTS` 本身幂等，不需要额外动作；
但历史 yaml 的 113 条要显式导入一次（`python -m stock_data_fetch.annotation import`）。

**v5 → v6（2026-11-15）删掉 `ix_kline_date`。**
那条索引是 v1 时"给按日扫全市场预留"的，但横截面查询全部走 `scan` 表，
`kline` 侧只有"按 symbol 取一段"，主键 `(symbol, date)` 聚簇已经够用。
实测 122 万行时它占 **33.2 MB = 153 MB 库体积的 22%**，而全仓没有任何查询用到它
（`grep "FROM kline WHERE date"` 零命中）。删除走 `UPGRADE_STEPS[(5,6)]` 的
`DROP INDEX IF EXISTS`，幂等可重入，不动任何数据。
"""

SCAN_RETENTION_DAYS = 30
"""`scan` 表只留最近这么多天的横截面（GC 用）。

横截面是"当期快照"：四个工具的粗筛只关心最近 K+1 天（K ≤ 5），十年历史对它没有意义，
而 5026 行/天 × 250 天/年 ≈ 1.2GB/年——这是本层唯一一张"不裁剪就会失控"的表。

**为什么从 120 改成 30（2026-11-15）：** 实测 7 天 35,217 行、3.36 MB（约 95 B/行），
按 120 天算会涨到 **57 MB**，而没有任何工具用到 7 天以上。留 30 天是给"跨月对比"
这类临时手工查询留余量，也够覆盖一次长假（最长 9 天）。确实需要更长时，
调 `hub.prune_scan(keep_days=...)` 显式覆盖，别改这个默认值。
"""

KLINE_KEEP_BARS = 400
"""每只股票在 `kline` 表里保留多少根日 K（GC 用，`hub.prune_kline()`）。

**为什么必须有这个数**：`kline` 是唯一一张"股票 × 交易日"双维度的表，只写不删。
实测 122 万行 = 104 MB，占 `shared.db`（153 MB）的 68%；按 5000 只 × 250 个交易日
× 112 B 估算，**一年就是 +140 MB**，而共享层的初衷是"按需取、不要囤"。

**为什么是 400 而不是 300**：`want.kline(bars=300)` 是最大消费者（judge_top 用 250、
kline_screen 用 71）。留 400 意味着即使某只票中间停牌 100 天，最近 300 根也仍完整，
不会出现"工具要 300 根、库里被裁到只剩 299 根 → 每次都去补历史 → 永远补不齐"的抖动。
停牌超过 100 个交易日的票本来也不该出现在候选池里。

真正需要更长历史时（比如回测），用 `hub.prune_kline(keep_bars=...)` 显式放宽，
或者临时 `want.kline(bars=N)` 手动补——但**不要把默认值调大**：这张表每多留 100 根，
就是 +35 MB。
"""


# ---- 人工标注（域 annotation）------------------------------------------------

MOAT_UNVERIFIED = "待核实"
"""占位状态：**已建记录、尚未核实**。2026-11-16 加。

存在的理由是把"覆盖"和"判定"分开：全市场 5000 多只要先各有一条记录，
但绝大多数没读过公告，直接写「无标注」等于谎称"查过了、确实没有"。
所以「待核实」既不是结论也不是缺口，是一个**待办标记**——
它不加分（`review._has_moat` 排除它），报告里也不渲染垄断块
（`annotation.row_to_moat` 的 `annotated` 排除它）。
"""

MOAT_TYPES = ("独家资源", "配额牌照", "特许经营", "独家品种", "技术壁垒", "网络",
              "无标注", MOAT_UNVERIFIED)
"""垄断性标注的合法取值。**枚举而不是自由文本**，理由是它要参与判定：
`moat_type != "无标注"` 就是"这家有垄断性"。写成自由文本的话，
「独家资源」「独家 资源」「资源垄断」会被当成三个不同的东西，
而代码只认得其中一种，另外两种静默变成"无标注"。

「无标注」既是枚举值也是**默认值**：它的含义是"我们查过，没发现垄断性"，
和"还没查过"是不同的两件事——前者是结论，后者是缺口。「待核实」专门表示
后者（见 `MOAT_UNVERIFIED`）。`annotated` 字段据此区分：
只有落在那 6 类垄断里才算 `annotated=True`。
"""

ANNOTATION_KIND_MOAT = "moat"
"""`annotation.kind` 的取值。目前只有垄断性这一种；将来加质量控制、
风险提示等标注时在这里加常量，表结构不用动。"""

ANNOTATION_STALE_DAYS = 365
"""标注核实日超过这么多天，报告显示「待复核」。

**不删除旧标注**：一条 2024 年核实的"独家品种"，2026 年可能已经不独家了，
但它当年确实独家——抹掉就丢掉了判断依据。所以只标记 stale，让人自己去看。
"""

SCAN_EXPR = "intersect([ClosePrice > 0])"
"""横截面的筛选表达式。**只有这一个条件，是实测逼出来的。**

westock 的 `tool_filter` 有个**约 2 秒的服务端硬超时**，超了连 HTTP 200 都拿不到，
只拿到一段 `RoundTrip timeout ... cost: 2.0003s` 的异常文本。实测（2026-11-05，
全市场 5031 只）：

| 表达式 | limit | 结果 |
|---|---|---|
| `ClosePrice > 0` | 5600 | ✅ 5031 行，2.4s |
| `ClosePrice > 0` + 两个市值 + 两个净流入 | 5600 | ❌ 超时 |
| `ClosePrice > 0` + 两个市值 + 两个净流入 | 1000 | ✅ |
| 同上，加 `ChangePCT >= -1` | 3500 | ✅ 1389 行，2.3s |
| `ClosePrice > 0` | 8000 | ❌ 超时 |

结论：**超时是"表达式条件数 × 返回行数"的乘积**，不是单纯的 limit 或行数。
想一次拿全市场，表达式就必须只有一条。

## 为什么不再顺手把市值/净流入也带回来

带回来的唯一理由是"少一次 `quote` 请求"。但代价是表达式从 1 条变成 5 条，
全市场口径直接挂掉——要么退回"只能拿涨幅 ≥ 0 的 4452 只"，要么只能拿 1000 条。

而这三个字段的消费方都很挑剔：`circ_mv` 只有 kline-screen 算换手率要用，
且**只对它选出来的那一小池**要（几百只，走 `quote` 域按 40 只一批，十几批就完），
`main_net_flow` 更只是展示。拿"全市场少一次请求"去换"能不能拿全"，
这个交易不划算。

## `ClosePrice > 0` 是真过滤

停牌股不返回（当日无价）。这对任何"涨幅达标"的粗筛都是**等价**的——
停牌当日涨幅为 0，选不进来。
"""

SCAN_ORDERBY = ("ChangePCT", "desc")
"""排序。**按涨幅降序**：真到了截断那天，缺的是涨幅最低的那批，
而所有工具的粗筛都是"涨幅要够高"，缺低的那头损失最小；
报错信息里的"最后一名涨幅"也才有意义。"""

SCAN_POINTS = 5100
"""一次横截面预计返回的行数，**只用于估算上游配额**（`router._expect`）。

估多了只是慢一点，估少了会打穿上游（`CONTRACT` §7.1）。实测全市场 5031 只，
留 2% 余量（新股 + 北交所）。
"""

SCAN_LIMIT = 5600
"""`tool_filter` 的 `limit`。**不是"取多少"的偏好，是"不能截断"的安全线。**

上游 `limit` 超了不报错，只是少给——返回体里 `totalStocks` 仍报**满足表达式的真实总数**
（实测 `ClosePrice > 0` 时 5031）。适配器里有一条硬校验：
`totalStocks > len(stocks)` 直接抛，绝不静默截断。

5600 > 当前全市场 5031，留 11% 余量。**别往上调**：`limit=8000` 实测超时，
而这个参数本身就在超时的乘积里。
"""

PKG = Path(__file__).resolve().parent
"""`stock_data_fetch/` 包目录。`schema.sql` **放在包里**，不放在项目根——
放根上的话 `pip install` 之后 `create_db()` 会找不到 DDL（库能开、表建不出来，
报错还发生在很后面）。"""

HUB_ROOT = PKG.parent
"""项目根（`stock_value_invest_tool/`）。

**从包的物理位置推导，不写死 `~/MyData/xxx`**：2026-11-15 项目从
`stock-kline-screen-tool` 改名成 `stock_value_invest_tool`，旧写法让两处路径当场失效，
改了名就跑不起来。推导出来的路径会跟着目录走，改名/换机器/软链都不受影响。
"""

DEFAULT_DB = HUB_ROOT / "shared.db"
"""数据库在**代码之外**（不在 `stock_data_fetch/` 包里），但仍在项目根下。

放包内的话 `pip install` 会把它带进 site-packages，一份代码配一份库，
换台机器就有两套数据。放项目根则「一个项目 = 一份库 + 一份产物」。
"""

DEFAULT_STATE = HUB_ROOT / "state"

CALENDAR_NAME = "trading_calendar.json"

DEFAULT_CALENDAR = DEFAULT_STATE / CALENDAR_NAME
"""默认日历：项目内 `state/trading_calendar.json`（`build_calendar.py` 生成，12 年）。

**2026-11-17 改**：原来指向 `~/MyData/stock-kline-screen-tool/database/trading_calendar.json`
（kline-screen 时代那份 4 年日历）做"新环境兜底"。那个目录已经不存在了，
于是所有**直接构造 `Config()`** 的路径——首当其冲是六个测试文件——都在
`TradingCalendar.load()` 抛 `HubNotReady`，`python3 -m stock_data_fetch.tests` 整体报错，
而日常运行不受影响（`Config.from_env()` 走 `resolve_calendar()`，本来就用这份）。
保持"路径可从代码推导"的同一原则：跟着 `HUB_ROOT` 走，不依赖别的仓库是否还在。
文件不存在时给的是明确的一句「交易日历不存在… 先跑 build_calendar」，
不是悄悄退回 4 年深度——4 年会让估值必然 `InsufficientDepth`（§6.6）。
"""

DEFAULT_SCHEMA = PKG / "schema.sql"


def resolve_calendar(state_dir: Path | None = None) -> Path:
    """`state_dir` 下有日历就用它，否则用项目内默认那份（`DEFAULT_CALENDAR`）。

    只在 `Config.from_env()` 里调用，不放进 `preflight()`——测试直接构造
    `Config()` 时应当拿到确定的路径，不然结果取决于机器上有没有生成过日历文件，
    测试会变得不可复现。`STOCKHUB_STATE` 指向别处（调试/临时库）时，
    那个目录里没日历就退回项目内这份，不至于因为换目录就没有日历可用。
    """
    own = Path(state_dir or DEFAULT_STATE) / CALENDAR_NAME
    return own if own.exists() else DEFAULT_CALENDAR


# ---- 节流参数（见 CONTRACT §7）------------------------------------------------

RATE_ROWS_PER_MIN = {
    # 令牌桶速率，按**返回行数**计。0 表示该源不走令牌桶（只用最小间隔）。
    # westock 2026-11-15 从 15000 提到 200000：MCP 官方通道（原版
    # kline-screen-tool 的取数通道）。kline 走多码批量后单请求 40 只×300 根
    # = 1 万行，15000 行/分钟会把批量卡回 1.5 请求/分钟——等于没批量。
    # 真上游限频的表现是"整批越回越少"（滑动窗口），router 的 strike 机制会兜住。
    "westock": 200000.0,
    "eastmoney": 15000.0,
    "tencent": 15000.0,
    "tdx": 0.0,
    "baostock": 0.0,
}

MIN_INTERVAL_MS = {
    # 最小请求间隔。tdx 无实测限频，取保守值；东财沿用它现有的 400ms。
    #
    # **westock 从 0 改成 1000ms（2026-11-15）**：它不是按行数限频的，是**按每分钟
    # 请求数**。令牌桶只按行计费，quote 一只一行、kline 增量一只一行，于是桶永远是
    # 满的、请求却一秒打十几个，服务端直接回"限频"：
    #
    #   实测一：kline 空洞逐个补（117 次单只请求）→ 报限频，冷却 30s→60s→120s 递增
    #   实测二：quote 整池 3271 只 = 82 批，前 66 批成功、后 16 批全 None
    #
    # 两次都指向同一个量级：**约 60~70 次/分钟**。1000ms 正好压在 60 次/分钟。
    # 代价是每天多等几十秒（4300 只日增量 ≈ 73 批 ≈ 73 秒），换来的是不触发冷却。
    # 想让单次跑更快可以临时调：
    #   STOCKHUB_MIN_INTERVAL_WESTOCK_MS=0 python -m stock_kline_screen
    "westock": 1000.0,
    "eastmoney": 400.0,
    "tencent": 200.0,
    "tdx": 300.0,
    "baostock": 200.0,
}

COOLDOWN_BASE = 30.0
"""首次限频冷却（秒）。实测 6s 就能恢复，但持续请求会立刻再触发，故取 30。"""

COOLDOWN_MAX = 300.0
"""连续触发后的冷却上限（秒）。"""

CLEAN_STREAK_RESET = 3
"""连续这么多次干净返回，冷却长度清零（`CONTRACT` §7.2）。"""

BURST_ROWS = 12000.0
"""桶容量（行）。一批 60 只 × 200 根 = 12000，得让一批一次拿满，否则"取一批"会被拆成
好几次等待，反而拖长整体时间。"""

THROTTLE_BUDGET = 300.0
"""单次 `take()` 累计等待上限（秒），超了抛 ThrottleTimeout。"""

DEGRADE_AFTER = 3
"""同一域的主源连续失败这么多次 → 本次进程内降级到备源（`SOURCES` §7）。"""


# ---- 闸门参数（见 CONTRACT §4）------------------------------------------------

MARKET_OPEN = (9, 15)
"""盘中闸下界：含集合竞价。09:30 才开始拦的话，09:16 会拿到半成品 bar。"""

MARKET_CLOSE = (15, 5)
"""盘中闸上界：给收盘集合竞价与上游落库留 5 分钟。"""

CLOSE_GRACE = MARKET_CLOSE
"""`target_date()` 的"当日算不算已收盘"分界。**必须与闸上界同一个值**，
否则会出现"闸放行了但目标日还是昨天"这种说不清的错位。"""

MIN_FREE_MB = 500
"""低于这个剩余空间只告警不拦——清理是 GC 的职责，不是启动闸的职责。"""

CALENDAR_MAX_LAG_DAYS = 30
"""日历最后一天距今天超过这么多天 → 认为日历过期（闸 5）。"""


@dataclass
class Config:
    db_path: Path = DEFAULT_DB
    state_dir: Path = DEFAULT_STATE
    calendar_path: Path = DEFAULT_CALENDAR
    schema_path: Path = DEFAULT_SCHEMA

    allow_intraday: bool = False
    log_level: str = "INFO"

    rate_rows_per_min: dict[str, float] = field(default_factory=lambda: dict(RATE_ROWS_PER_MIN))
    min_interval_ms: dict[str, float] = field(default_factory=lambda: dict(MIN_INTERVAL_MS))
    cooldown_base: float = COOLDOWN_BASE
    cooldown_max: float = COOLDOWN_MAX
    clean_streak_reset: int = CLEAN_STREAK_RESET
    burst_rows: float = BURST_ROWS
    throttle_budget: float = THROTTLE_BUDGET
    degrade_after: int = DEGRADE_AFTER

    market_open: tuple[int, int] = MARKET_OPEN
    market_close: tuple[int, int] = MARKET_CLOSE
    close_grace: tuple[int, int] = CLOSE_GRACE
    min_free_mb: int = MIN_FREE_MB
    calendar_max_lag_days: int = CALENDAR_MAX_LAG_DAYS

    # ---- 读取环境变量 ----
    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        if v := os.environ.get("STOCKHUB_DB"):
            c.db_path = Path(v).expanduser()
        if v := os.environ.get("STOCKHUB_STATE"):
            c.state_dir = Path(v).expanduser()
        if v := os.environ.get("STOCKHUB_CALENDAR"):
            c.calendar_path = Path(v).expanduser()
        else:
            # 没显式指定就用"hub 自己的长日历，没有才退回 4 年那份"。
            # 这一句放在这里而不是 preflight 里：`Config()` 必须保持确定性（见函数说明）。
            c.calendar_path = resolve_calendar(c.state_dir)
        if v := os.environ.get("STOCKHUB_SCHEMA"):
            c.schema_path = Path(v).expanduser()
        c.allow_intraday = os.environ.get("STOCKHUB_ALLOW_INTRADAY", "") not in ("", "0")
        if v := os.environ.get("STOCKHUB_LOG"):
            c.log_level = v.upper()
        if v := os.environ.get("STOCKHUB_COOLDOWN_BASE"):
            c.cooldown_base = _f(v, c.cooldown_base)
        if v := os.environ.get("STOCKHUB_BURST_ROWS"):
            c.burst_rows = _f(v, c.burst_rows)
        for name in list(c.rate_rows_per_min):
            key = f"STOCKHUB_RATE_{name.upper()}"
            if v := os.environ.get(key):
                c.rate_rows_per_min[name] = _f(v, c.rate_rows_per_min[name])
        for name in list(c.min_interval_ms):
            key = f"STOCKHUB_MIN_INTERVAL_{name.upper()}_MS"
            if v := os.environ.get(key):
                c.min_interval_ms[name] = _f(v, c.min_interval_ms[name])
        return c

    def interval_s(self, src: str) -> float:
        return max(0.0, self.min_interval_ms.get(src, 0.0)) / 1000.0


def _f(raw: str, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
