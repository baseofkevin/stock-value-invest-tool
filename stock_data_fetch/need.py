"""need：一次取数需求。

**区间与点数必须分开**（`CONTRACT` §5）：

- `start` / `end` 决定"取多长"，属于**取数范围**；
- `points` / `min_points` 决定"够不够用"，属于**门槛**。

混在一起就会重犯老坑：20 期刚好过 `MIN_QUARTERS_REQUIRED=20`，静默降级出一份看起来正常的报告。
所以门槛由**调用方声明**——quality 传 20、cycle 传 40、judge 传 6，谁也不会被谁卡住，谁也不许偷偷降。

`hit()` 判的是 `end` 与 `n`，**不是 `start`**（除 `dividend` 外）。原因见 `coverage.hit` 的注释：
次新股的 `start` 永远到不了 300 根之前，按 `start` 判会让它每次运行都去重取一次，永远不命中。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .calendar import TradingCalendar

# 各域的口径指纹（`SCHEMA` §4.4）。**不含 src**——换源作废靠 meta，不靠 fp（`SOURCES` §4.1）。
# `snapshot` 不在这张表里：它的 fp 由子域现拼（`sub=mainop` / `sub=consensus`）。
# 三个子域是三种不同的数据，共用一条覆盖记录会互相顶掉——写完 `consensus` 之后
# `mainop` 的 `hit()` 会返回 True，于是永远拿不到主营构成，而日志里一句错都没有。
#
# `scan` 也不在这张表里，理由**同一个但方向相反**：它是"一天一份全市场"，
# fp 必须带日期（`date=2026-11-13`）。共用一条 fp 的话，取完周五再要周四，
# `hit()` 看到 `cov.end=2026-11-13 >= 2026-11-12` 就放行，然后查询查出 0 行——
# 又是一次"看起来正常的空名单"。这是本层最贵的一类 bug，两处都栽过。
FP = {
    "kline": "fq=raw",
    "finance": "type=all;fields=all",
    "valuation": "freq=week",
    "dividend": "",
    "quote": "",
    "profile": "",
}

# 法定披露截止日：报告期 → (当年, 月, 日) 的截止；年报顺延到次年 4-30。
_REPORT_DEADLINES = (("03-31", "04-30"), ("06-30", "08-31"),
                     ("09-30", "10-31"), ("12-31", "04-30"))

MIN_VALUATION_WEEKS = 430
"""估值深度门槛（周）。430 周 ≈ 8.3 年。

**这是被上游卡出来的数，不是拍的**：东财 `RPT_VALUEANALYSIS_DET` 最早只到
2018-01-02（2026-11-14 实测，2150 个交易日 = 454 周），任何高于 454 的门槛
都不可达，只会让 `ensure` 每次都以 `InsufficientDepth` 收场。
留 24 周余量给停牌多、上市晚的票。

要更长的估值历史必须**换源**（通达信/理杏仁），不是调大这个数——
调大只会把"数据源不够"变成一句看不懂的报错。
"""


def fingerprint(domain: str) -> str:
    return FP.get(domain, "")


@dataclass(frozen=True)
class Need:
    """一次取数需求。冻结：它要在日志、异常与跨模块调用里原样传递，绝不允许中途被改写。"""

    domain: str
    fp: str
    end: str | None
    """要覆盖到的最后一天。`None` 表示不设上界（只要不早于 start 就算够）。"""
    start: str | None
    """要覆盖到的第一天。对 `dividend` 是命中条件；其余域只是取数范围的起点。"""
    points: int
    """期望点数，用来估算上游配额（`expect_rows`）。"""
    min_points: int
    """门槛：库里点数少于它就 `InsufficientDepth`，绝不降级。"""
    extra: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return (f"{self.domain}({self.start or '-'}..{self.end or '-'}, "
                f"min={self.min_points})")


# ---------------------------------------------------------------- 构造器

class _Want:
    """`want.kline(...)` 这一族。都要求传日历，因为"深度"只能靠日历换成日期。"""

    @staticmethod
    def kline(cal: TradingCalendar, end: str, bars: int = 300,
              min_bars: int = 250) -> Need:
        """日 K。`bars` 决定回头多少根，`min_bars` 是门槛（judge 250 / kline-screen 71）。"""
        start = cal.start_for_bars(end, bars)
        return Need("kline", fingerprint("kline"), end=end, start=start,
                    points=bars, min_points=min_bars)

    @staticmethod
    def finance(target: str, periods: int = 43, min_points: int = 40) -> Need:
        """财报三表。没有下界——"最近 N 期"，到底几期由上游决定（当前上限 43）。

        `end` 取**法定披露截止日**推出来的最新应有报告期，这样"新季报出来了"能自动让
        旧覆盖失效，而不用等谁去手动刷。
        """
        return Need("finance", fingerprint("finance"), end=latest_report_period(target),
                    start=None, points=periods, min_points=min_points,
                    extra={"periods": periods})

    @staticmethod
    def valuation(cal: TradingCalendar | None, target: str, years: int = 10,
                  min_weeks: int = MIN_VALUATION_WEEKS) -> Need:
        """估值序列，周频。

        `min_weeks` 默认 430（8.3 年），**不是 520**：东财 `RPT_VALUEANALYSIS_DET`
        对茅台最早一行是 2018-01-02（2150 个交易日，454 周），这是它的历史上限。
        钉死 480 的结果是每次运行都以 `InsufficientDepth` 结束——2026-11-14 冒烟
        实测 3/3 全败，且报错写的是"库 205 / 需 480"，看起来像数据缺失。

        `start` **不夹到日历首日**。原来写的是 `cal.on_or_after(day) or cal.days[0]`，
        而 `on_or_after("2016-11-15")` 在只有 4 年日历时返回 `2022-11-14`（二分查找到
        插入点 0），于是 `need.start` 被静默改成 4 年前，取回来 205 周、门槛 480——
        错误信息把"日历太短"伪装成"这份数据只有 4 年"。估值是**取数范围**，不是
        交易日集合，日历够长才用它把起点对齐到交易日，不够长就直接用自然日。
        """
        day = (dt.date.fromisoformat(target) - dt.timedelta(days=365 * years + 3)).isoformat()
        start = day
        if cal is not None and cal.days and cal.days[0] <= day:
            start = cal.on_or_after(day) or day
        return Need("valuation", fingerprint("valuation"), end=target, start=start,
                    points=years * 52, min_points=min_weeks)

    @staticmethod
    def dividend(kline_start: str, pad_days: int = 365) -> Need:
        """分红送转。起点比 K 线再往前一年（防御 + 审计用途，算 qfq 其实窗口外会抵消）。"""
        since = (dt.date.fromisoformat(kline_start) - dt.timedelta(days=pad_days)).isoformat()
        return Need("dividend", fingerprint("dividend"), end=None, start=since,
                    points=0, min_points=0)

    @staticmethod
    def quote(target: str) -> Need:
        return Need("quote", fingerprint("quote"), end=target, start=target,
                    points=1, min_points=1)

    @staticmethod
    def profile() -> Need:
        """元数据（`stock` 表）：名称 / 行业 / 是否金融 / 上市日。

        **不带上界**（`end=None`）。元数据取一次就够用，`hit()` 里"没有上界"等于"不看过期"，
        于是第二次运行不会再问上游。要强制刷新就 `ensure(..., force=True)`。

        为什么不能像 `quote` 那样按每个交易日判过期：一只股票的行业一年也变不了一次，
        按日判意味着 quality 每次跑都把候选池的 `profile` 全重取一遍——那正是
        "按需数据层"最不该做的事。真改了名/换了行业，重取一次也就 1 个请求。
        """
        return Need("profile", fingerprint("profile"), end=None, start=None,
                    points=1, min_points=1)

    @staticmethod
    def gbalance(target: str, periods: int = 20) -> Need:
        """东财资产负债表补丁（商誉 + 短期借款），按报告期。

        **为什么单独一个函数、不复用 `snapshot()`**：`snapshot()` 把 `points` 钉成 1、
        `start`/`end` 钉成同一天，那是"当期一份"的语义（主营构成、一致预期、评级）。
        `gbalance` 是**多期**的——quality-screen 要拿它对齐每一期的财报，
        只给最新一期就没法和 `finance` 的 20 期并排。语义不同就别共用一个入口，
        否则某天有人给它传 `periods` 会发现参数被静默吞掉。

        `end` 取法定披露截止日推出来的最新应有报告期（与 `finance()` 同源），
        `start=None` 表示没有下界——"最近 N 期"，到底几期由上游决定。
        """
        return Need("snapshot", f"sub=gbalance;p={periods}",
                    end=latest_report_period(target), start=None,
                    points=periods, min_points=1,
                    extra={"snapshot_domain": "gbalance", "periods": periods})

    @staticmethod
    def gcashflow(target: str, periods: int = 20) -> Need:
        """东财现金流量表补丁（capex：购建固定资产等支付的现金），按报告期。

        与 `gbalance()` 同构，理由也一样：weStock 的现金流表里没有这一项，
        而 FCF = 经营现金流 - capex 必须要有它。`points=periods` 只为估上游配额，
        `min_points=1` 才是门槛——有一期就够派上用场，缺的年份下游自会断线。
        """
        return Need("snapshot", f"sub=gcashflow;p={periods}",
                    end=latest_report_period(target), start=None,
                    points=periods, min_points=1,
                    extra={"snapshot_domain": "gcashflow", "periods": periods})

    @staticmethod
    def snapshot(domain: str, as_of: str) -> Need:
        """`mainop` / `consensus` / `rating` 三个子域。

        **fp 必须带子域**（`sub=mainop` 而不是空串）：覆盖表的主键是
        `(domain, symbol, fp)`，三个子域共用 `domain="snapshot"`，fp 再不分就没法区分。
        实测症状：写完 `consensus` 之后再要 `mainop`，`hit()` 直接返回 True，
        查询按 `domain=mainop` 过滤得到 0 行，调用方拿到一份空数据且没有任何告警。
        """
        return Need("snapshot", f"sub={domain}", end=as_of, start=as_of,
                    points=1, min_points=1, extra={"snapshot_domain": domain})

    @staticmethod
    def scan(day: str) -> Need:
        """某交易日的全市场横截面（`scan` 表）。

        **`fp` 里钉死日期**，理由见 `FP` 上面那段：这是"一天一份"的域，
        覆盖记录必须一天一条，否则取了最新的那天之后，往前的每一天都会被误判成"已覆盖"。

        `points` 给的是**预计返回行数**（5026 行），不是"期望几个点"——它只被
        `router._expect()` 用来估上游配额。`min_points=1` 才是门槛：横截面只要有一行
        就算这次取到了（真正"空"的情况由适配器报 `empty_reason`，不该靠点数猜）。

        `symbol` 不在这里定：它是 `api.SCAN_SYMBOL`（`"*"`），由 `ensure_scan()` 填。
        `Need` 是纯粹的"要什么"，"给谁"是调用方的参数。
        """
        from .config import SCAN_POINTS
        return Need("scan", f"date={day}", end=day, start=day,
                    points=SCAN_POINTS, min_points=1)


want = _Want()


# ---------------------------------------------------------------- 财报期

def latest_report_period(day: str, grace_days: int = 1) -> str:
    """按法定披露期限，`day` 这天"应该已经能看到"的最新报告期。

    A 股定期报告披露上限：一季报 4-30、半年报 8-31、三季报 10-31、年报次年 4-30。
    很多公司卡在最后一天披露，所以给 1 天宽限，否则 4-30 当天会来取一份还没出的年报。

    **年报与一季报的截止日同为 4-30**，所以不能只比 `deadline`——那样会按迭代顺序留下年报
    （`2025-12-31`），而 5-1 之后正确的最新报告期是一季报（`2026-03-31`）。
    比 `(deadline, period)` 二元组，即"截止日相同则报告期更晚的胜出"。
    """
    d = dt.date.fromisoformat(day)
    year = d.year
    best: tuple[str, str] | None = None          # (deadline, period)
    for yy in (year - 1, year):
        for period_suffix, deadline_suffix in _REPORT_DEADLINES:
            period = f"{yy}-{period_suffix}"
            dy = yy + 1 if (period_suffix == "12-31" and deadline_suffix == "04-30") else yy
            deadline = f"{dy}-{deadline_suffix}"
            if dt.date.fromisoformat(deadline) + dt.timedelta(days=grace_days) <= d:
                if best is None or (deadline, period) > best:
                    best = (deadline, period)
    if best is None:                              # pragma: no cover - 只有 2000 年以前才会
        return f"{year - 2}-12-31"
    return best[1]
