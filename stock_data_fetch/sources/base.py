"""适配器契约：三层之间**唯一**的接口。

    sources/<name>.py  →  fetch(domain, codes, need) -> Got     （把上游返回变成行）
    router.py          →  pick(domain) / fetch(...)             （这次用谁、失败了怎么降级）
    api.py             →  ensure / read                         （要不要取、取多少、写不写）

三层互不知道对方的细节。加一个源 = 写一个 `fetch()` + 注册表加一行。

`Got` 而不是"直接返回行"的理由：**"没取到"有三种**，上层必须能分开。
参数写错返回空表、该股停牌、真没这只股票——三者的后续动作完全不同（报错 / 记永久洞 / 什么都不做）。
只返回行的话，这三种在类型上长得一模一样。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from ..need import Need


@dataclass(frozen=True)
class Quota:
    """一个源的配额形态。两种至少有一个。"""

    rows_per_min: float | None = None
    """按返回行数计（westock / 东财）。实测这才是真正的限制口径，见 `CONTRACT` §7。"""

    min_interval_ms: float | None = None
    """按请求数计（无批量能力的源，如 tdx）。"""


@dataclass(frozen=True)
class SourceSpec:
    """注册表里的一行。`upstream` 才是 `fp` 里的那个"源"，`name` 只是适配器名。"""

    name: str
    """适配器名：westock / eastmoney / tdx / baostock / akshare_lg / efinance"""

    upstream: str
    """上游：tencent / eastmoney / tdx / baostock / legulegu。换源判等看的是它。"""

    transport: str
    """mcp / http / lib"""

    domains: tuple[str, ...]
    priority: int
    """同域内 0 = 主源，数字越大越靠后。"""

    batch: int
    """单次最大 codes 数；1 = 无批量，只能逐只。"""

    quota: Quota = field(default_factory=Quota)

    optional: bool = False
    """True = 依赖缺失时静默摘除（三个开源源）。"""

    reason: str = ""
    """不可用的原因。非空 → 注册表过滤掉它，`doctor` 原样打印。"""


@dataclass
class BatchError:
    """上游自报的组级失败。`symbols` 就是上游 `errors[].symbol` 拆出来的那几只。"""

    symbols: list[str]
    code: str = "BATCH_ERROR"
    message: str = ""

    def to_dict(self) -> dict:
        return {"symbols": self.symbols, "code": self.code, "message": self.message}


@dataclass
class Got:
    """一次 `fetch()` 的结果。`rows` 按 symbol 分行，字段名 = 表列名。"""

    rows: dict[str, list[dict]] = field(default_factory=dict)
    errors: list[BatchError] = field(default_factory=list)
    empty_reason: str | None = None
    """`None`（没空）/ `no_data`（该股确实没有）/ `code_moved`（代码已迁移）/
    `param_error`（多半是参数或接口变了，要当错误）。"""

    meta: dict = field(default_factory=dict)

    # ---- 派生 ----

    @property
    def symbols(self) -> list[str]:
        return list(self.rows)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.rows.values())

    @property
    def failed_symbols(self) -> list[str]:
        return [s for e in self.errors for s in e.symbols]

    @property
    def partial(self) -> bool:
        """有组级失败，或请求的有一部分既没返回行也不在失败清单里。"""
        return bool(self.errors)

    def extend(self, other: "Got") -> "Got":
        """把重取回来的结果并进来。同 symbol 的行**按日期覆盖**，不重复。"""
        for sym, rows in other.rows.items():
            cur = self.rows.setdefault(sym, [])
            if not cur:
                cur.extend(rows)
                continue
            by_date = {_key(r): i for i, r in enumerate(cur)}
            for r in rows:
                i = by_date.get(_key(r))
                if i is None:
                    cur.append(r)
                else:
                    cur[i] = r
        self.errors.extend(other.errors)
        if other.empty_reason and not self.empty_reason:
            self.empty_reason = other.empty_reason
        self.meta.update(other.meta)
        return self

    def drop_error_symbols(self, still_failed: Iterable[str]) -> None:
        """重试成功后把对应 symbol 从失败清单里摘掉。"""
        keep = set(still_failed)
        out: list[BatchError] = []
        for e in self.errors:
            left = [s for s in e.symbols if s in keep]
            if left:
                out.append(BatchError(left, e.code, e.message))
        self.errors = out


def _key(row: Mapping) -> str:
    """一行的唯一键：K 线按 date，财报按 period，分红按 ex_date。"""
    for k in ("date", "period", "ex_date", "trade_date", "as_of"):
        if k in row:
            return str(row[k])
    return repr(sorted(row.items()))


# ---- 适配器要实现的东西 ------------------------------------------------

FetchFn = Callable[[str, Sequence[str], Need], Got]
"""`sources/<name>.fetch(domain, codes, need) -> Got`。

要求：
1. **不许 sleep**。节流由 `throttle` 负责，适配器里睡会绕过令牌桶。
2. **不许写库**。写库是 `api` 的事，且必须在覆盖记录同一个事务里。
3. **空表必须给 `empty_reason`**。六个源里有四个参数写错时返回空表而不报错
   （`CONTRACT` §7.4 已实测两种），所以"空"的原因只能由适配器判断。
4. **单位换算在适配器里做完**。库里只存"元 / 股 / 每股"，调用方不该再猜。
"""
