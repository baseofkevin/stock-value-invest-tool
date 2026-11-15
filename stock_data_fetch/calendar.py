"""交易日历。

日历从 kline-screen 的 `database/trading_calendar.json` 加载（13KB，`{end, span, days[]}`），
**不进库**：日历是"规则"不是"数据"，进库就要维护更新，反而多一个失效点（`SCHEMA` §4.4）。

它承担三件事，任何一件算错都会连锁：
  1. 盘中闸——判断"今天是不是交易日"；
  2. `target_date()`——决定"要到哪一天为止"，**绝不从 `MAX(kline.date)` 反推**（会自我锁定）；
  3. hole 判定——把区间内的应有交易日与实际日期做差集。

`preflight` 只做**本地自检**（闸 5）：文件存在、覆盖到当前、没有明显断档。
与 westock `data_trade_calendar` 的**跨源对账**放在 `probe` 里，不放在启动路径上——
启动一旦依赖网络，上游抖一下四个工具就都起不来。
"""

from __future__ import annotations

import datetime as dt
import json
from bisect import bisect_left, bisect_right
from pathlib import Path

from .errors import HubNotReady


class TradingCalendar:
    """不可变交易日集合。所有日期都是 `YYYY-MM-DD` 字符串（字典序即时序）。"""

    __slots__ = ("days", "_set", "span", "end", "source")

    def __init__(self, days: list[str], span: str, end: str, source: str = "") -> None:
        self.days: list[str] = sorted(set(days))
        if not self.days:
            raise HubNotReady("交易日历为空", source=source)
        self._set = frozenset(self.days)
        self.span = span or self.days[0]
        self.end = end or self.days[-1]
        self.source = source

    # ---- 构造 ----
    @classmethod
    def load(cls, path: Path) -> "TradingCalendar":
        p = Path(path)
        if not p.exists():
            raise HubNotReady(
                f"交易日历不存在：{p}。请确认 kline-screen 项目路径，"
                "或用 STOCKHUB_CALENDAR 指向 trading_calendar.json")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise HubNotReady(f"交易日历无法解析：{p}（{e}）") from e
        if isinstance(raw, list):                       # 容忍纯数组形态
            raw = {"days": raw}
        days = raw.get("days") or []
        if not isinstance(days, list):
            raise HubNotReady(f"交易日历格式不对：{p}（days 不是数组）")
        return cls(days, raw.get("span", ""), raw.get("end", ""), source=str(p))

    # ---- 查询 ----
    def is_trading(self, day: str) -> bool:
        return day in self._set

    def on_or_before(self, day: str) -> str | None:
        i = bisect_right(self.days, day) - 1
        return self.days[i] if i >= 0 else None

    def on_or_after(self, day: str) -> str | None:
        i = bisect_left(self.days, day)
        return self.days[i] if i < len(self.days) else None

    def prev(self, day: str) -> str | None:
        """严格早于 `day` 的最后一个交易日。"""
        i = bisect_left(self.days, day) - 1
        return self.days[i] if i >= 0 else None

    def between(self, start: str, end: str) -> list[str]:
        """闭区间 `[start, end]` 内的交易日。"""
        lo = bisect_left(self.days, start)
        hi = bisect_right(self.days, end)
        return self.days[lo:hi]

    def count(self, start: str, end: str) -> int:
        return max(0, bisect_right(self.days, end) - bisect_left(self.days, start))

    def start_for_bars(self, end: str, bars: int) -> str | None:
        """要凑够 `bars` 根、截止到 `end`，起点该是哪天。

        返回闭区间起点。日历不够长时返回日历第一天，由调用方按 `min_bars` 判深度。
        """
        hi = bisect_right(self.days, end)
        if hi <= 0:
            return None
        lo = max(0, hi - bars)
        return self.days[lo]

    # ---- 自检（闸 5）----
    def sanity(self, today: str, max_lag_days: int = 30) -> list[str]:
        """返回问题清单，空表示通过。只查"能本地判断"的事。"""
        problems: list[str] = []
        if self.end < today:
            lag = (dt.date.fromisoformat(today) - dt.date.fromisoformat(self.end)).days
            if lag > max_lag_days:
                problems.append(
                    f"日历最后一天是 {self.end}，距今 {lag} 天，超过 {max_lag_days} 天，"
                    f"请更新 {self.source}")
        # 单日跨度 > 12 天的"交易日断档"在 A 股只有春节/国庆+调休，超过就是缺数据
        for a, b in zip(self.days, self.days[1:]):
            gap = (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
            if gap > 12:
                problems.append(f"日历在 {a} → {b} 之间断了 {gap} 天，疑似缺数据")
                break
        # 今年已过 12 月的话，交易日应在 240 上下；不足 200 说明这一年没更新全
        year = today[:4]
        if self.end >= f"{year}-12-01":
            n = sum(1 for d in self.days if d.startswith(year))
            if n < 200:
                problems.append(f"{year} 年交易日只有 {n} 天，明显偏少")
        return problems


# ---------------------------------------------------------------- 时间判定

def _t(hhmm: tuple[int, int]) -> dt.time:
    return dt.time(hhmm[0], hhmm[1])


def is_market_open(now: dt.datetime, cal: TradingCalendar,
                   open_at=(9, 15), close_at=(15, 5)) -> bool:
    """处在盘中窗口内（含集合竞价）。非交易日永远为 False。"""
    day = now.date().isoformat()
    if not cal.is_trading(day):
        return False
    return _t(open_at) <= now.time() <= _t(close_at)


def target_date(now: dt.datetime, cal: TradingCalendar,
                close_grace=(15, 5)) -> str:
    """应该取到哪一天为止。

    规则只有一条：**当日只有在"已过收盘宽限"时才算今天，否则退到上一个交易日**。
    宽限必须与盘中闸的上界同一个值（`config.CLOSE_GRACE == MARKET_CLOSE`），
    否则会出现"闸放行了、但目标日还是昨天"——那会静默产出一份少了一天的报告。
    """
    today = now.date().isoformat()
    if cal.is_trading(today) and now.time() >= _t(close_grace):
        return today
    got = cal.prev(today) if cal.is_trading(today) else cal.on_or_before(today)
    if got is None:
        raise HubNotReady(f"交易日历早于 {today}，无法确定目标交易日", calendar=cal.source)
    return got
