"""`preflight()` —— 启动闸门，返回 `Hub`。

六道闸（`CONTRACT` §4）：

  1 盘中      交易时段内**不禁止启动**，只是把"能不能联网"标成 false（见下）
  2 库可用    不存在且允许建 → 按 schema.sql 建库
  3 版本      user_version != 1 → 拒绝启动，不自动迁移
  4 PRAGMA    回读 journal_mode 必须是 wal（网络盘会静默退回 rollback journal）
  5 日历      本地自检：文件在、没断档、没过期
  6 空间      只告警，不拦（清理是 GC 的职责）

**闸 1 在 2026-11-14 改过语义：从"启动就拦"改成"联网才拦"。**

原来的写法是盘中直接抛 `MarketOpenError`，于是四个工具在 09:15–15:05 完全不可用——
哪怕它们要的数据昨天就已经在库里、这次一次上游都不用打。这与"按需数据层"的定位冲突：
数据层存在的意义就是"取过一次就不再取"，而"取不取"与"现在能不能取"是两件事。

现在的规则：

| 情形 | 盘中能不能跑 |
|---|---|
| 库里有、且够新（`coverage.hit` 命中） | **能**。全程只读库，一次上游都不打 |
| 缺数据 / 数据过期 → 必须联网 | **不能**。`Hub.check_online()` 在 `api._pull()` 里抛 |
| `allow_intraday=True` | 能，但会在 `meta` 里留痕 |

拦截点从 `preflight()` 挪到 `api._pull()`——那是**唯一**决定"要不要打上游"的地方，
放在那里才和真实意图一致：不是"不许开始"，而是"不许取数"。

**这个函数必须只做本地、离线、毫秒级的判断。** 启动路径一旦依赖网络，上游抖一下四个工具就
都起不来；与 westock `data_trade_calendar` 的跨源对账因此放在 `probe` 里，不在这里。
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .calendar import TradingCalendar, is_market_open, target_date
from .config import Config, SCHEMA_VERSION
from .errors import HubNotReady, MarketOpenError, SchemaMismatch
from .throttle import Throttle

log = logging.getLogger("stock_data_fetch")


@dataclass
class Hub:
    """一次运行期的共享上下文。**只创建一次**，放在工具入口处。"""

    conn: sqlite3.Connection
    cfg: Config
    cal: TradingCalendar
    target: str
    """`target_date` 的结果：这次运行应该取到哪一天为止。"""

    intraday: bool = False
    """现在是交易时段且没被显式放行 → **不许联网**，但读库照旧（见模块头闸 1）。"""

    _throttle: Throttle | None = field(default=None, repr=False)
    _router: object | None = field(default=None, repr=False)

    @property
    def throttle(self) -> Throttle:
        """进程内唯一的节流器。走文件 + flock，所以跨进程也是同一份配额。"""
        if self._throttle is None:
            self._throttle = Throttle(self.cfg)
        return self._throttle

    @property
    def router(self):
        """进程内唯一的 Router。**必须唯一**：`strikes` 跨股票累积才谈得上"连续失败 3 次降级"。

        每次 `ensure()` 现拼一个的话，`strikes` 每只股票清零，降级规则形同不存在。
        """
        if self._router is None:
            from . import router as R
            self._router = R.Router(cfg=self.cfg, throttle=self.throttle, conn=self.conn)
        return self._router

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:            # pragma: no cover
            pass

    def __enter__(self) -> "Hub":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 第 3 步已接通 ----
    def ensure(self, domain: str, symbol: str, need, **kw):
        from . import api
        return api.ensure(self, domain, symbol, need, **kw)

    def ensure_many(self, domain: str, symbols, need, **kw):
        from . import api
        return api.ensure_many(self, domain, symbols, need, **kw)

    def ensure_bulk(self, domain: str, symbols, need, **kw):
        """组级批量预取（见 `api.ensure_bulk`）：整批一次交给支持多码的源。"""
        from . import api
        return api.ensure_bulk(self, domain, symbols, need, **kw)

    def read(self, domain: str, symbol: str, need):
        from . import api
        return api.read(self, domain, symbol, need)

    def finance_full(self, symbol: str, need) -> dict[str, list[dict]]:
        """**只读**：三张报表的完整原文（`{income/balance/cashflow: [行]}`）。

        给 quality-screen / cycle-elastic 用——它们要的 54~80 个字段里，只有 15 个
        在 `finance` 的结构列上，其余全在 `raw_json` 里。这里负责拆包，上层不必
        自己 `json.loads`。与 `read()` 一样**不打上游**，缺就 `ensure()` 补。
        """
        from . import api
        return api.finance_full(self, symbol, need)

    def gbalance_full(self, symbol: str, need) -> dict:
        """**只读**：东财资产负债表补丁（商誉 / 短期借款）。

        返回 `{"periods": [...], "has_balance": bool}`。`None` 的两种含义
        （真的没有 = 0 / 没数据 = unknown）靠 `has_balance` 区分，**调用方必须读它**——
        详见 `api.gbalance_full` 的说明。
        """
        from . import api
        return api.gbalance_full(self, symbol, need)

    def gcashflow_full(self, symbol: str, need) -> dict:
        """**只读**：东财现金流量表补丁（capex）。返回 `{"periods": [...]}`。

        `CONSTRUCT_LONG_ASSET` 是年初至今累计，算年度 capex 只该用 12-31 的行。
        """
        from . import api
        return api.gcashflow_full(self, symbol, need)

    # ---- 第 7 步接通：按日横截面 ----
    def scan(self, day: str, **kw) -> list[dict]:
        """某交易日的全市场横截面（`scan` 表）。库里没有就联网取。

        返回的是**那一天的全部行**（约 5000 行），按需本地过滤。别在工具里自己拼
        `ensure("scan", "*", want.scan(day))`——`symbol` 与 `fp` 这两处任何一处写错，
        症状都是"拿到空列表"而不是报错。
        """
        from . import api
        return api.ensure_scan(self, day, **kw)

    def scan_many(self, days, **kw) -> dict[str, list[dict]]:
        """多天横截面，`{日期: 行}`。任一天取不到就抛——少一天就是少一批候选，不会报错。"""
        from . import api
        return api.ensure_scan_many(self, days, **kw)

    def scan_cached(self, day: str) -> list[dict]:
        """**只读**：库里那天有什么给什么，绝不联网。给"预览/体检"用。"""
        from . import api, need as N
        return api.read(self, "scan", api.SCAN_SYMBOL, N.want.scan(day)).rows

    def prune_scan(self, keep_days: int | None = None) -> int:
        """清掉过期的横截面（默认留 120 天）。返回删了多少行。"""
        from . import api
        return api.prune_scan(self, keep_days)

    def check_online(self, domain: str, symbol: str, need=None) -> None:
        """联网前的最后一道闸。盘中（且未放行）→ 抛 `MarketOpenError`。

        调用点在 `api._pull()` —— 只有"库里不满足、确实要打上游"时才会走到那里。
        所以"盘中止步"与"盘中照常用缓存"这两件事互不干扰。
        """
        if not self.intraday:
            return
        raise MarketOpenError(
            f"{domain} {symbol} 的数据不在库里，或已不够新，这次需要联网取数。"
            f"现在是交易时段（{_hhmm(self.cfg.market_open)}–{_hhmm(self.cfg.market_close)}），"
            "盘中取到的是半成品，请收盘后重跑（或设 STOCKHUB_ALLOW_INTRADAY=1 强行放行）",
            domain=domain, symbol=symbol, need=str(need) if need is not None else None)

    # ---- 以下由后续步骤填充（CONTRACT §16 第 5 步）----
    def factor(self, symbol: str, dates=None, **kw):
        """与 `dates` 一一对应的复权因子。`dates` 省略 = 库里全部 K 线日期。"""
        from . import factor as F
        if dates is None:
            dates = [r[0] for r in self.conn.execute(
                "SELECT date FROM kline WHERE symbol=? ORDER BY date", (symbol,))]
        return F.factor(self.conn, self.cal, symbol, dates, **kw)

    def adjusted(self, symbol: str, rows=None, *, anchor: str | None = None, **kw):
        """前复权后的 K 线行。`rows` 省略 = 直接读库。

        **锚默认在 `self.target`（今天该取到的那一天）**，因为工具要的是"和今天可比"的价格。
        `factor.adjusted()` 的默认是最后一根 bar——两者的差别只在"最后一根 bar 之后又
        发生过分红"，而那正是最容易忘的情形，所以 Hub 这一层刻意选了 target。
        """
        from . import factor as F
        if rows is None:
            rows = [dict(r) for r in self.conn.execute(
                "SELECT * FROM kline WHERE symbol=? ORDER BY date", (symbol,))]
        return F.adjusted(self.conn, self.cal, symbol, rows,
                          anchor=anchor or self.target, **kw)

    def single_quarter(self, symbol: str, field: str, period: str | None = None) -> dict:
        """单季值。`field` 传**累计口径的原名**（如 `OperatingRevenue`）。

        返回 `{"period", "value", "src"}`，`src` ∈ `stored` / `q1_cum` / `derived` / `missing`。
        `value` 为 `None` 表示确实算不出来——**不填 0**，理由见 `quarter.py` 头部。
        """
        from . import quarter as Q
        return Q.value(self.conn, symbol, field, period)


def preflight(*, allow_intraday: bool | None = None,
              db_path: Path | str | None = None,
              create: bool = True,
              now: dt.datetime | None = None,
              free_mb: float | None = None,
              cfg: Config | None = None) -> Hub:
    """过六道闸并返回 `Hub`。

    `now` / `free_mb` / `cfg` 是给测试注入用的，正常调用不要传。
    """
    cfg = cfg or Config.from_env()
    if db_path is not None:
        cfg.db_path = Path(db_path)
    if allow_intraday is None:
        allow_intraday = cfg.allow_intraday
    now = now or dt.datetime.now()

    # ---- 闸 5 的前半：日历必须能加载（闸 1 依赖它）----
    cal = TradingCalendar.load(cfg.calendar_path)

    # ---- 闸 1 盘中：只标记，不拦启动 ----
    intraday = is_market_open(now, cal, cfg.market_open, cfg.market_close)
    if intraday and allow_intraday:
        log.warning("盘中运行被显式放行（allow_intraday）；当日 bar 可能是半成品")
    elif intraday:
        log.info("现在是交易时段（%s–%s）：本次只允许读库，需要联网取数时会被拦下",
                 _hhmm(cfg.market_open), _hhmm(cfg.market_close))

    # ---- 闸 2 库可用 ----
    path: Path = cfg.db_path
    if not path.exists():
        if not create:
            raise HubNotReady(f"库不存在：{path}")
        log.info("库不存在，按 %s 建库：%s", cfg.schema_path.name, path)
        db.create_db(path, cfg.schema_path)

    conn = db.connect(path)

    # ---- 闸 3 版本 ----
    try:
        db.check_version(conn)
    except SchemaMismatch as e:
        cur = db.schema_version(conn)
        if not db.can_upgrade(cur, SCHEMA_VERSION):
            conn.close()
            raise
        # 允许自动升的版本对。v1→v2 是纯新增表（重放 schema.sql 就够）；
        # v2→v3 动了一张表的 CHECK，靠 db.UPGRADE_STEPS 里的表重建完成。
        # 两条路都**不丢数据**（重建是"建新→拷→删旧→改名"，不是删库重建），
        # 所以不需要人工备份。失败就说明迁移写错了，直接抛比带着半个库继续跑安全。
        log.warning("库版本 %d → %d：自动迁移（%s）", cur, SCHEMA_VERSION, e)
        try:
            db.upgrade(conn, cfg.schema_path, from_version=cur)
            db.check_version(conn)
            db.meta_set(conn, "schema_upgraded_at",
                        now.strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception:
            conn.close()
            raise

    # ---- 闸 4 PRAGMA 落实 ----
    problems = db.pragma_problems(conn)
    if problems:
        conn.close()
        raise HubNotReady("库可用但参数没落实（多半在网络盘/只读盘）：" + "；".join(problems))

    # ---- 闸 5 后半：日历自检 ----
    cal_problems = cal.sanity(now.date().isoformat(), cfg.calendar_max_lag_days)
    if cal_problems:
        conn.close()
        raise HubNotReady("交易日历有问题：" + "；".join(cal_problems),
                          calendar=str(cfg.calendar_path))

    # ---- 闸 6 空间（只告警）----
    free = free_mb if free_mb is not None else _free_mb(path.parent)
    if free is not None and free < cfg.min_free_mb:
        log.warning("剩余空间 %.0fMB < %dMB，继续运行；请清理或调 GC 阈值",
                    free, cfg.min_free_mb)

    if allow_intraday:
        try:                                  # 留痕：谁在盘中跑过
            db.meta_set(conn, "intraday_override_at",
                        now.strftime("%Y-%m-%dT%H:%M:%S"))
        except sqlite3.Error:                 # 留痕失败不该阻断运行
            pass

    target = target_date(now, cal, cfg.close_grace)
    log.debug("preflight ok: db=%s target=%s intraday=%s", path, target,
              intraday and not allow_intraday)
    return Hub(conn=conn, cfg=cfg, cal=cal, target=target,
               intraday=intraday and not allow_intraday)


def _free_mb(path: Path) -> float | None:
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:                           # pragma: no cover
        return None


def _hhmm(t: tuple[int, int]) -> str:
    return f"{t[0]:02d}:{t[1]:02d}"
