"""源注册表：显式列表 + 可用性过滤 + 子域过滤。

不搞插件自动发现——源就六七个，显式列表更好审查：想知道"这次到底会用谁"，
读这一个文件就够，不用去翻各模块的导入副作用。

## 三条 2026-11-14 改过的地方

**① `kline` 主源是 eastmoney，不是 tdx**（纪要 §22）：tdx 需要 OAuth 令牌而独立进程拿不到，
东财 `fqt=0` 无鉴权且 raw 值逐点相同，所以 **eastmoney 主 / tdx 备（有令牌时才启用）**。
tdx 没令牌时被 `reason` 过滤掉，router 不会白打 401。

**② 每个源只声明它真的实现了的域。** 早先 eastmoney 声明了 `finance` / `snapshot` 却只有
`kline` 的实现，于是每只股票的 `ensure("finance")` 都先打一次东财、抛一个
`SourceUnavailable`、记一次 strike、打一条 warning——4000 只就是 4000 条噪音，
而且 `degrade_after=3` 一满足就把东财在 `finance` 上"降级"掉，日志里看起来像真出了故障。
**声明与实际不符，比少声明一个域危险得多。**

**③ `snapshot` 是复合域，靠 `supports()` 再分一层。** 它下面有三个子域：
`mainop` 只有东财有（`RPT_F10_FN_MAINOP`），`consensus` / `rating` 只有 westock 有。
`SourceSpec` 一个源只有一个 `priority`，没法表达"这个域我只有一半"，所以加了可选钩子
`supports(domain, need) -> bool`：适配器自己说了算，注册表只负责把它接上。

**④ `kline` 补了第二条腿 `tencent`（无鉴权直连 HTTP）。** 东财 `push2his` 突发配额用完会
**整段拒连**（`SOURCES` §5.7，实测持续 20 分钟以上），而 K 线原先只有它一条腿——
一封就四个工具全停。`tencent` 给的是**真原材料**（`fq` 留空，与 tdx/东财逐点同值），
所以它当备源不作废任何旧数据；`amount` 它没有，留 NULL（见 `tencent.py` 模块头）。
"""

from __future__ import annotations

from . import eastmoney, tdx, tencent, westock
from .base import BatchError, Got, Quota, SourceSpec

SOURCES: list[SourceSpec] = [
    SourceSpec("eastmoney", "eastmoney", "http", ("kline", "valuation", "snapshot"),
               priority=0, batch=1, quota=Quota(rows_per_min=15000.0)),
    SourceSpec("westock", "tencent", "mcp",
               ("kline", "finance", "dividend", "quote", "profile", "snapshot", "scan"),
               priority=0, batch=40, quota=Quota(rows_per_min=15000.0)),
    SourceSpec("tdx", "tdx", "mcp", ("kline", "quote", "profile"),
               priority=1, batch=1, quota=Quota(min_interval_ms=300.0)),
    SourceSpec("tencent", "tencent", "http", ("kline",),
               priority=2, batch=1,
               quota=Quota(rows_per_min=15000.0, min_interval_ms=200.0)),
    SourceSpec("baostock", "baostock", "lib", ("kline", "valuation", "calendar"),
               priority=9, batch=1, quota=Quota(min_interval_ms=200.0), optional=True,
               reason="未接入（本机 bs.login 返回 10001011 黑名单用户）"),
]

MODULES = {"eastmoney": eastmoney, "tdx": tdx, "westock": westock,
           "tencent": tencent, "baostock": None}


def _reason(spec: SourceSpec) -> str:
    """不可用的原因；空串表示可用。"""
    if spec.reason:
        return spec.reason
    mod = MODULES.get(spec.name)
    if mod is None:
        return "适配器未实现"
    probe = getattr(mod, "available", None)
    if callable(probe):
        ok, why = probe()
        return "" if ok else why
    return ""


def _handles(spec: SourceSpec, domain: str, need) -> bool:
    """适配器可选钩子 `supports(domain, need)`。没实现就当"这个域我全管"。"""
    if need is None:
        return True
    probe = getattr(MODULES.get(spec.name), "supports", None)
    if not callable(probe):
        return True
    try:
        return bool(probe(domain, need))
    except Exception:                       # noqa: BLE001 - 钩子写坏了不该阻断路由
        return True


def available(domain: str | None = None, need=None) -> list[SourceSpec]:
    """可用源，按 (priority, name) 排好序。

    `domain` 给了就只留支持该域的；`need` 也给了就再过一遍 `supports()`——
    `snapshot` 那种"我只管一半"的域靠它，不然每一次都会先挑错源再失败重挑。
    """
    out = [s for s in SOURCES
           if (domain is None or domain in s.domains)
           and not _reason(s)
           and (domain is None or _handles(s, domain, need))]
    return sorted(out, key=lambda s: (s.priority, s.name))


def disabled(domain: str | None = None) -> list[tuple[SourceSpec, str]]:
    """不可用的源及其原因。`doctor` 直接打印这张表。"""
    return [(s, _reason(s)) for s in SOURCES
            if _reason(s) and (domain is None or domain in s.domains)]


def get(name: str) -> SourceSpec:
    for s in SOURCES:
        if s.name == name:
            return s
    raise KeyError(f"没有这个源：{name}")


def fetch_fn(name: str):
    """取适配器模块的 `fetch()`。没实现就报清楚，别让调用方拿到 None 才崩。"""
    mod = MODULES.get(name)
    if mod is None or not hasattr(mod, "fetch"):
        raise KeyError(f"源 {name} 的适配器还没有 fetch()")
    return mod.fetch


__all__ = ["SOURCES", "available", "disabled", "get", "fetch_fn",
           "SourceSpec", "Quota", "Got", "BatchError"]
