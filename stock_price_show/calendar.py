"""只读的交易日历：定"今天算不算今天"，以及"上一个交易日是哪天"。

日历是**规则**不是数据，所以不进库（进库就要维护更新，反而多一个失效点）。
它只用到这里需要的三件事：

1. `--date` 不给时定目标日（今天过了收盘宽限且是交易日 → 今天，否则上一交易日）；
2. 判断给的是不是交易日（不是就直接退出，别去打上游浪费请求）；
3. 出错时给出"上一个交易日是 X"的提示。

文件来自数据层的 `build_calendar`（`state/trading_calendar.json`，`{span,end,days[]}`）。
**文件不存在不算错**：`--date` 照样能用，只是没法定"今天"。
"""

from __future__ import annotations

import datetime as dt
import json
from bisect import bisect_right
from pathlib import Path

from . import config


class Calendar:
    """不可变交易日集合。所有日期都是 `YYYY-MM-DD` 字符串（字典序即时序）。"""

    __slots__ = ("days", "_set", "end", "source")

    def __init__(self, days: list[str], end: str = "", source: str = "") -> None:
        self.days = sorted({str(d) for d in days if d})
        self._set = frozenset(self.days)
        self.end = end or (self.days[-1] if self.days else "")
        self.source = source

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Calendar | None":
        """读日历；不存在/格式不对就给 `None`（调用方降级，不抛）。"""
        p = Path(path or config.calendar_path())
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(raw, list):                       # 容忍纯数组形态
            raw = {"days": raw}
        days = raw.get("days") or []
        if not isinstance(days, list) or not days:
            return None
        return cls(days, str(raw.get("end") or ""), source=str(p))

    def is_trading(self, day: str) -> bool:
        return day in self._set

    def on_or_before(self, day: str) -> str | None:
        i = bisect_right(self.days, day) - 1
        return self.days[i] if i >= 0 else None


def target(cal: Calendar, now: dt.datetime | None = None) -> str:
    """不给 `--date` 时要取哪一天。

    与四个工具同一套规则：今天过了收盘宽限（15:05）且今天是交易日，才算今天；
    其余一律退到**上一个交易日**——包括"今天还没收盘"（那时上游还没算出来）、
    "今天不是交易日"、"日历还没更新到今天"三种情形。
    宁可少取一天，也别去问上游一个还没发布的日子（只会得到"返回数据为空"）。
    """
    now = now or dt.datetime.now()
    today = now.date().isoformat()
    grace = now.replace(hour=config.CLOSE_GRACE[0], minute=config.CLOSE_GRACE[1],
                        second=0, microsecond=0)
    if cal.is_trading(today) and now >= grace:
        return today
    # 注意这里要的是"今天**之前**"：直接 `on_or_before(today)` 在今天本身是交易日时
    # 会原样返回今天——那正是要避开的那个还没发布的日子。
    before = (now.date() - dt.timedelta(days=1)).isoformat()
    return cal.on_or_before(before) or today


__all__ = ["Calendar", "target"]
