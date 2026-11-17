-- ============================================================================
-- schema.sql — 按需共享数据层（lazy hub）建库脚本
-- 版本：user_version = 6
-- 生成依据：`讨论纪要-共享数据层.md`（v16）
-- 用法：sqlite3 shared.db < schema.sql
--
-- 全部 DDL 都是 `IF NOT EXISTS`，所以本脚本**幂等**：对已有的 v1 库直接执行一遍
-- 就能升到 v2（`db.upgrade()` 正是这么做的）。加表/加索引这类向后兼容的变更
-- 走这条路径；改列、改主键那种必须删库重建，不在自动迁移的范围内。
--
-- 三条不可违反的不变式（改任何 DDL 之前先读这三条）：
--   1) 只存"不复权"的原始值。复权在消费侧用 dividend + factor 现算。
--   2) 财报与分红是 append-only：只增期、不删期；值可被上游更正覆盖（留 revision）。
--   3) 任何写入必须与 coverage / watermark 在同一事务内完成，否则会误判"数据完整"。
-- ============================================================================

-- ---------------------------------------------------------------- 建库期 PRAGMA
-- 注意：page_size 与 auto_vacuum 只能在"还没有任何表"时生效，必须放在最前。
PRAGMA page_size = 4096;
PRAGMA auto_vacuum = INCREMENTAL;

PRAGMA journal_mode = WAL;        -- 读写不互斥（四个工具可能同时跑）
PRAGMA synchronous = NORMAL;      -- WAL 下够用；掉电最多丢最后一个事务
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;       -- 等锁而不是立刻报 database is locked
PRAGMA temp_store = MEMORY;

-- ---------------------------------------------------------------- 证券主表
-- 定位：universe 不进共享库，但每只股票的"元数据"要有一处权威副本。
--       profile 域（quality 的金融股判定、cycle 的名称/行业）落在这里。
CREATE TABLE IF NOT EXISTS stock (
  symbol       TEXT PRIMARY KEY,                  -- sh600519 / sz000858 / bj430047
  name         TEXT NOT NULL,
  exch         TEXT NOT NULL CHECK (exch IN ('SH','SZ','BJ')),
  setcode      INTEGER NOT NULL,                  -- 通达信市场码：1=沪 0=深 2=北
  industry     TEXT,                              -- 申万一级
  sector       TEXT,                              -- v4：上游 sector，industry 缺失时的备用
  business     TEXT,                              -- v4：主营业务描述，报告第一节「生意本身」
  is_financial INTEGER NOT NULL DEFAULT 0,        -- quality 用于切换估值口径
  list_date    TEXT,                              -- YYYY-MM-DD，判新股/次新
  updated_at   TEXT NOT NULL,
  last_access_at TEXT                             -- 最近一次被 ensure() 读到；**只写不读，股票级 GC 未实现**
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 日 K（域 kline）
-- 存 raw（不复权）。深度 300 根，滚动。可整段丢弃后重取（可再生）。
CREATE TABLE IF NOT EXISTS kline (
  symbol TEXT NOT NULL,
  date   TEXT NOT NULL,          -- YYYY-MM-DD
  open   REAL,
  high   REAL,
  low    REAL,
  close  REAL,
  volume REAL NOT NULL DEFAULT 0,-- 单位：股（已从通达信的"手"换算，×100）
  amount REAL NOT NULL DEFAULT 0,-- 单位：元
  src    TEXT NOT NULL DEFAULT 'tdx',
  PRIMARY KEY (symbol, date)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 财报（域 finance）
-- 三表合一：一期一行，同一报告期的利润表/资产负债表/现金流量表字段并排。
-- 好处：cycle 要 income+balance 同期对齐、judge 要 6 期，都是"按期取三表"，一次查询即可。
--
-- 口径：**全部存上游原值，库里不含任何我们自己派生的量。**
--   累计值  OperatingRevenue       （年初至今）
--   单季值  OperatingRevenue_Q     （上游直接给，已实测：本年各季 _Q 之和 = 年末累计）
--   TTM     OperatingRevenueTTM    （上游直接给，已实测：等于"累计+去年年报-去年同季"，差 0%）
-- 存 _Q/TTM 而不是自己差分：差分要求相邻期齐全，43 期零余量下缺一期就崩；
-- 而上游给的与差分结果一致（实测差 0.01 亿，纯四舍五入），没有引入新口径。
--
-- 金额单位：**元**（实测 sh600519 2026-09-30 累计营收 130,970,743,649 元 = 1309.7 亿元）。
--   其他源（如 data_consensus）的万元口径必须在入库前换算成元，`unit` 列只做标识。
--
-- 列名保持上游字段原名（零映射风险）；未提列的字段仍在 raw_json 里，可随时补列而无需重取。
CREATE TABLE IF NOT EXISTS finance (
  symbol                   TEXT NOT NULL,
  period                   TEXT NOT NULL,      -- 报告期末 YYYY-MM-DD
  OperatingRevenue         REAL,               -- 累计
  OperatingRevenue_Q       REAL,               -- 单季
  OperatingRevenueTTM      REAL,
  OperatingCost            REAL,
  OperatingCost_Q          REAL,
  OperatingCostTTM         REAL,
  OperatingProfit          REAL,
  OperatingProfit_Q        REAL,
  OperatingProfitTTM       REAL,
  NPParentCompanyOwners    REAL,               -- 归母净利润，累计
  NPParentCompanyOwners_Q  REAL,
  NPParentCompanyOwnersTTM REAL,
  TotalFixedAsset          REAL,               -- 资产负债表项，无单季/TTM 概念
  CIP                      REAL,               -- 在建工程
  ContractLiability        REAL,               -- 合同负债
  unit                     TEXT NOT NULL DEFAULT 'CNY',
  raw_json                 TEXT,               -- 只保留最近 2 期，更早的置 NULL（省 5GB+）
  src                      TEXT NOT NULL,      -- eastmoney / derived / tdx
  updated_at               TEXT NOT NULL,
  PRIMARY KEY (symbol, period)
) WITHOUT ROWID;

-- 上游更正留痕：只在真有差异时写一行（相对差 > 1e-6）。量极小，<1MB。
CREATE TABLE IF NOT EXISTS finance_revision (
  symbol      TEXT NOT NULL,
  period      TEXT NOT NULL,
  field       TEXT NOT NULL,
  old_value   TEXT,
  new_value   TEXT,
  detected_at TEXT NOT NULL,
  src         TEXT,
  PRIMARY KEY (symbol, period, field, detected_at)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 分红送转（域 dividend）
-- append-only。除权日是既成事实，永不修改。复权因子的唯一来源。
-- 字段单位：一律"每股"（上游给的是每 10 股，入库时 /10）。
CREATE TABLE IF NOT EXISTS dividend (
  symbol             TEXT NOT NULL,
  ex_date            TEXT NOT NULL,                 -- 除权除息日 YYYY-MM-DD
  cash_per_share     REAL NOT NULL DEFAULT 0,       -- 每股派现（税前）
  bonus_per_share    REAL NOT NULL DEFAULT 0,       -- 每股送股（10送3 → 0.3）
  transfer_per_share REAL NOT NULL DEFAULT 0,       -- 每股转增（10转5 → 0.5）
  rights_per_share   REAL NOT NULL DEFAULT 0,       -- 每股配股比例
  rights_price       REAL,                          -- 配股价
  plan_text          TEXT,                          -- 上游原文（10派39.740元送8.000股转12.000股）
  src                TEXT,
  updated_at         TEXT NOT NULL,
  PRIMARY KEY (symbol, ex_date)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 估值（域 valuation）
-- 唯一需要长历史的域：10 年周频（520 周）。入库即周频——trade_date 取该 ISO 周的最后一个交易日。
-- quality 的 _valuation() 正是"按 ISO 周取最后交易日"，与此逐点等价。
--
-- 列名带口径后缀，因为三种口径混用会让 quality 的 V 分静默漂移：
--   pe_ttm  ← 上游 PE_TTM
--   pb_mrq  ← 上游 PB_MRQ（最新报告期净资产）
--   ps_ttm  ← 上游 PS_TTM
-- ⚠️ pb_mrq 的分母会随新财报重算，所以**本表历史值不是永不变**：
--    允许整段覆盖刷新，coverage 不承诺"旧值永久有效"。
CREATE TABLE IF NOT EXISTS valuation (
  symbol     TEXT NOT NULL,
  trade_date TEXT NOT NULL,      -- 该 ISO 周最后一个交易日
  pe_ttm     REAL,
  pb_mrq     REAL,
  ps_ttm     REAL,
  total_mv   REAL,               -- 元
  circ_mv    REAL,               -- 元
  src        TEXT,
  PRIMARY KEY (symbol, trade_date)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 快照型（域 quote）
-- 当期一份。过期判据 = stored.trade_date <> target（按交易日相等判，不按时间差）。
CREATE TABLE IF NOT EXISTS quote (
  symbol       TEXT PRIMARY KEY,
  trade_date   TEXT NOT NULL,
  price        REAL,
  prev_close   REAL,
  pct_chg      REAL,
  volume       REAL,             -- 股
  amount       REAL,             -- 元
  turnover     REAL,             -- 换手率 %
  float_shares REAL,             -- 流通股（股）——judge 换手率用当期值近似
  total_shares REAL,
  total_mv     REAL,
  circ_mv      REAL,
  pe_ttm       REAL,
  pb           REAL,
  src          TEXT,
  updated_at   TEXT NOT NULL
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 快照型（通用）
-- 字段不稳定、只被某一家工具消费的域放这里：mainop / consensus / rating。
-- 用 payload JSON 而不是建三张表：这些域字段随上游变，建表等于给自己挖迁移坑。
--
-- as_of 的语义**按 domain 固定**（同一个 domain 永远只用一种含义）：
--   mainop    → 报告期末（2025-12-31），因为主营构成按报告期披露
--   gbalance / gcashflow → 报告期末（补丁表都按报告期披露）
--   consensus / rating → 目标交易日
-- 两个含义混在一列是刻意的：拆成两列会让每行一半是 NULL，查询要写 COALESCE。
CREATE TABLE IF NOT EXISTS snapshot (
  symbol  TEXT NOT NULL,
  domain  TEXT NOT NULL CHECK (domain IN ('mainop','consensus','rating','gbalance','gcashflow')),
  as_of   TEXT,                  -- mainop/gbalance/gcashflow=报告期末；consensus/rating=目标交易日
  payload TEXT NOT NULL,         -- JSON
  src     TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, domain)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 横截面（域 scan）
-- 每个交易日一份「全市场当日有价的股票」，一股一行。**按日聚簇**（主键 date 在前），
-- 因为唯一的访问方式是「给我 2026-11-13 那天全市场」，不是「给我这只票的历史」。
--
-- 为什么必须有这张表（第 7 步接 kline-screen 时才发现）：
-- 四个工具的粗筛都要「某天全市场哪些股票涨幅达标」，而 hub 原有七个域全是**按股票**的
-- （`ensure(domain, symbol, need)`）。没有按日的横截面，每个工具就得自己去打一次
-- `screen condition`——那正是共享层要消灭的重复请求。
--
-- 口径选择：存**全市场当日有价**（约 5031 只 / 天），不存「涨幅 ∈ [0.5, 20.5] 的那 566 只」。
-- 存过滤后的结果等于把 kline-screen 的规则焊进共享层，quality/cycle/judge 每家门槛
-- 都不一样，改一次规则就得清一次库。存全市场则任何人都能在本地过滤。
--
-- 只带 `close` / `pct_chg` / `name` 三列，**不带市值与净流入**——不是不想省一次请求，
-- 而是上游 `tool_filter` 有个约 2 秒的硬超时，"条件数 × 返回行数"一乘就挂：
-- 全市场 + 5 个条件超时，全市场 + 1 个条件 2.4 秒拿回 5031 行（详见 `config.SCAN_EXPR`）。
-- 市值/净流入的消费方只要几百只，走 `quote` 域按批取，比牺牲"能不能拿全"划算。
--
-- 容量：5031 行/天 × 约 60 字节 ≈ 0.3MB/天。**只留最近 `scan_retention_days` 天**
-- （默认 120），GC 之后稳定在 60 万行 / 60MB 左右。这是本层唯一一张要盯容量的表：
-- 横截面是「当期快照」，没有十年分位那种需求。
--
-- 停牌股不在这张表里（上游按 `ClosePrice > 0` 过滤后就不返回它）：这对四个工具是
-- **等价**的——停牌当日涨幅为 0，任何「涨幅达标」的粗筛都不可能把它选进来。
CREATE TABLE IF NOT EXISTS scan (
  date        TEXT NOT NULL,             -- YYYY-MM-DD
  symbol      TEXT NOT NULL,             -- sh600519 / sz000858 / bj430047
  name        TEXT,                      -- 当日上游给的名称（省一次 profile 查询）
  close       REAL,                      -- 元
  pct_chg     REAL,                      -- **百分数**（10.03 = 涨 10.03%），与 tools 的阈值同口径
  src         TEXT,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (date, symbol)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 覆盖度（命中的唯一依据）
-- 规则：命中判"区间覆盖"，不判"文件/表里有没有行"。
-- 判据 = start_date <= need.start AND end_date >= need.end AND holes = 0
--        AND partial = 0 AND fp 相等
-- fp（口径指纹）：含 type / fq / fields 等"形态"参数；不含 num / limit 等"深度"参数。
CREATE TABLE IF NOT EXISTS coverage (
  domain      TEXT NOT NULL,               -- kline / finance / dividend / valuation / quote / ...
  symbol      TEXT NOT NULL,
  fp          TEXT NOT NULL DEFAULT '',    -- 口径指纹；口径单一的域留空串
  start_date  TEXT,
  end_date    TEXT,
  n           INTEGER NOT NULL DEFAULT 0,  -- 行数（期数或 bar 数）
  holes       INTEGER NOT NULL DEFAULT 0,  -- 区间内的缺口个数（含 permanent）
  hole_detail TEXT,                        -- 缺口明细 JSON，形态见 CONTRACT §6.3：
                                           -- [{"from":"2026-03-02","to":"2026-03-05","n":4,
                                           --   "retry":2,"next_try_at":"...","cls":"retry",
                                           --   "reason":"no_data"}]
                                           -- 只存 JSON 不建表：缺口的生命周期由重试状态机管，
                                           -- 建表要多一张只增不删的小表，且要额外的 GC。
  partial     INTEGER NOT NULL DEFAULT 0,  -- 1 = 上一次取数被限频截断，不可信
  src         TEXT,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (domain, symbol, fp)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 水位线（漏跑自愈）
-- target 从独立交易日历推，绝不从 MAX(kline.date) 反推（会自我锁定）。
CREATE TABLE IF NOT EXISTS watermark (
  domain       TEXT NOT NULL,
  scope        TEXT NOT NULL DEFAULT '',   -- 空串=全局；也可按板块/分组
  last_ok_date TEXT,
  cursor       TEXT,                       -- 分页游标 JSON（如 tdx 的 startxh）
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (domain, scope)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 元数据
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 索引
-- v6 删掉了 `ix_kline_date`（曾占 153MB 库的 22%，而全仓没有任何查询用到它：
-- 横截面走 `scan`，kline 侧只有"按 symbol 取一段"，主键已按 (symbol,date) 聚簇）。
-- 真要按日扫全市场，临时建索引再删比常驻划算——常驻的代价是每次写入都要维护它。
-- 财报按报告期反查（"哪些股票缺 2023 年报"）。
CREATE INDEX IF NOT EXISTS ix_finance_period ON finance(period);
-- 修订记录按股票+期查。
CREATE INDEX IF NOT EXISTS ix_rev_symbol_period ON finance_revision(symbol, period);
-- 横截面反查（"这只票最近 20 天的日涨幅"），也供 GC 按日期范围删。
CREATE INDEX IF NOT EXISTS ix_scan_symbol ON scan(symbol);

-- ---------------------------------------------------------------- 人工标注（域 annotation）
-- 定位：**不是**上游取来的数据，是人写的判断。所以它不进 `snapshot`——
--       那张表的语义是"从某个源取来的一份快照"，有 `src`、可能被 GC 按访问时间回收；
--       标注必须长期留存，且"改动"就是改动本身，不存在"重新取一次"。
--
-- 与 `stock` 表**不设外键**：标注可以比 profile 先存在。您完全可能先把自选股 113 只
-- 标注一遍，之后才逐个取数。加外键会让"先录标注"直接失败，那正好挡住了最自然的用法。
--
-- `kind` 而不是每类标注一张表：将来加"质量控制""风险提示"等标注，加一个 kind 值就够。
CREATE TABLE IF NOT EXISTS annotation (
  symbol     TEXT NOT NULL,                  -- sh601958 / sz000629
  kind       TEXT NOT NULL DEFAULT 'moat',   -- 标注类型；目前只有 moat（垄断性）
  moat_type  TEXT,                           -- 枚举见 config.MOAT_TYPES；kind='moat' 时有值
  evidence   TEXT,                           -- 一句话依据
  controller TEXT,                           -- 控股股东 / 实际控制人
  sources    TEXT,                           -- JSON 数组：可点击的核实来源
  verified   TEXT,                           -- YYYY-MM-DD 核实日期；超 12 个月报告显示「待复核」
  note       TEXT,                           -- 自由备注，扩展用
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, kind)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 初始化
PRAGMA user_version = 6;

INSERT OR IGNORE INTO meta(key, value) VALUES
  ('schema_version', '2'),
  ('created_at',     strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
  ('hub_note',       'raw-only; append-only for finance/dividend; coverage-gated'),
  ('kline_depth',    '300'),
  ('finance_depth',  '43'),
  ('valuation_years','10'),
  ('scan_retention_days', '120');
