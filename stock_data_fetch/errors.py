"""异常树。

设计约束：异常只回答"发生了什么"，不回答"该怎么办"。
该怎么办写在 `CONTRACT-stock_data_fetch.md` §10 的那张表里，由**调用方**决定——代码里不做策略判断，
否则四个工具会各自拿到一个"已经替它做过决定"的异常，而它们的取舍本来就不一样。

每个异常带一个 `code`（进程退出码），工具可以直接 `sys.exit(e.code)`。
"""

from __future__ import annotations


class StockfetchError(Exception):
    """所有异常的基类。"""

    code = 1

    def __init__(self, message: str, **ctx: object) -> None:
        super().__init__(message)
        self.message = message
        self.ctx = ctx

    def __str__(self) -> str:  # pragma: no cover - 只影响展示
        if not self.ctx:
            return self.message
        extra = " ".join(f"{k}={v}" for k, v in self.ctx.items())
        return f"{self.message}（{extra}）"


class MarketOpenError(StockfetchError):
    """处于交易时段。半成品 bar 不能当收盘价，所以整体拦下。"""

    code = 2


class HubNotReady(StockfetchError):
    """库打不开、建不了、或 PRAGMA 没落实（多半在网络盘/只读盘）。"""

    code = 3


class SchemaMismatch(StockfetchError):
    """`user_version` 与代码期望不符。不自动迁移，只报错让人重建。"""

    code = 3


class InsufficientDepth(StockfetchError):
    """区间够但点数不够。**绝不降级返回部分数据**——那会产出一份看起来正常的错报告。"""

    code = 4


class PartialData(StockfetchError):
    """上游限频，组级重试 3 轮后仍缺。已取到的部分已经落库，下次运行会继续补。"""

    code = 5


class EmptyResult(StockfetchError):
    """上游返回空表，且不属于"次新股 / 停牌"这两种合法空。

    实测：参数名写错（`codes` 而不是逗号 `code`）不报错，会拼成 `[sh600519` 并返回空 `nodes`。
    所以空表必须当错，不能静默写 0 行。
    """

    code = 5


class ThrottleTimeout(StockfetchError):
    """等配额或冷却超时。"""

    code = 6


class FactorError(StockfetchError):
    """复权数据自相矛盾（如 `P_ref <= 0`）。不允许跳过，跳过会让 qfq 静默偏高。"""

    code = 7


class HoleUnreachable(StockfetchError):
    """永久缺口导致点数不足。**唯一允许调用方逐股跳过的异常。**"""

    code = 8


class SourceUnavailable(StockfetchError):
    """源不可达：缺令牌、鉴权失败、接口变更、连续空表。

    **只在 `router` 内部流动，不该到达四个工具**——router 会把它翻译成"降级到备源"或
    "`PartialData`"。给退出码 9 只是为了让它在日志里有个明确标识；工具端不需要处理它。
    """

    code = 9
