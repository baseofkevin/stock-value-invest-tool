# -*- coding: utf-8 -*-
"""取数层：**全部走共享数据层**（`stock_data_fetch`），不再直连上游。

## 为什么改成这样

原版 `fetch_all()` 每只股票打 **8 次网络**：weStock 5 次（finance / quote /
profile / consensus / rating）+ 东财 3 次（估值 / 主营构成 / capex）。跑 39 只
候选股就是 300 多次请求，中途任何一次限频都要重试，而且同一只股票明天再跑
还要再打一遍。

现在改成向 `stock_data_fetch` 要数据：库里有的直接用，没有的才联网，联网拿到的
**写回库**，下次（乃至其它三个工具）直接复用。财报一季度才变一次，这个差别很大。

## 契约**一个字都没改**

`fetch_all()` 仍然返回：

    {"code": "sh600519", "calls": [{"tool": ..., "params": ..., "response": ...}], "errors": [...]}

所以 `compute.py` / `score.py` / `render.py` 一行都不用动——它们只认这个结构。
这也是敢换取数层的前提：下游 3000 多行计算与渲染代码零风险。

`calls` 里每个元素的 `response` 形状**照抄原版真实返回**，因为下游是照着那个形状
解析的（`compute._data()` 取 `.data`、`_rows()` 取 `.result.data`）。冒充错形状的
后果不是报错，是"某个指标恒为 None"——报告照出，只是结论悄悄变了。

## 数据来源对照

| calls 里的 tool | 原版来源 | 现在来源 |
|---|---|---|
| `data_finance` | weStock | `finance` 域（`hub.finance_full`，20 期三表） |
| `data_quote` | weStock | `quote` 域 |
| `data_profile` | weStock | `profile` 域 |
| `data_consensus` / `data_rating` | weStock | `snapshot` 域的两个子域 |
| `RPT_VALUEANALYSIS_DET` | 东财 | `valuation` 域（周频序列） |
| `RPT_F10_FN_MAINOP` | 东财 | `snapshot/mainop` 子域 |
| `RPT_F10_FINANCE_GCASHFLOW` | 东财 | `snapshot/gcashflow` 子域 |
| `GOODWILL` / `SHORT_LOAN`（新增） | 无（上游不给） | `snapshot/gbalance` 子域 |

最后一行是**新增**的：原版 weStock 的 `data_finance` 根本不返回商誉和短期借款，
所以原版把商誉恒按 0 处理（`compute.py` 里 `goodwill_imputed` 恒为 True）。
现在这两个字段从东财补上了，商誉检查才真的有效。它不冒充原有 tool——用一个新
key `gbalance` 单独挂在 bundle 的 `extra` 里，` compute.py` 不认也不会受影响，
由 `screen/gbalance.py` 显式读取。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT.parent                       # stock-value-invest-tool/

if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from screen import codes                                                    # noqa: E402

try:
    from stock_data_fetch import Hub, preflight
    from stock_data_fetch import need as N
except ImportError as exc:                  # pragma: no cover
    raise SystemExit(
        f"找不到共享数据层 stock_data_fetch（找的是 {PROJECT}）。\n"
        f"原版 quality-screen 自带取数通道，本版改成依赖共享库；\n"
        f"请确认 stock-value-invest-tool 与本目录同级。原始错误：{exc}") from exc


class DataError(RuntimeError):
    pass


# --quiet：重试警告属于进度噪音，静音模式下不打（失败仍会记进数据缺口）。
QUIET = False


def set_quiet(flag: bool) -> None:
    global QUIET
    QUIET = bool(flag)


def normalize_code(raw: str) -> str:
    """600519 / sh600519 / 600519.SH → sh600519；号段规则见 screen/codes.py。"""
    return codes.normalize(raw)


def six(code: str) -> str:
    return codes.six(code)


# ---------------------------------------------------------------- Hub 生命周期
#
# **每线程一个 Hub，不是一个进程一个。**
#
# SQLite 的连接默认 `check_same_thread=True`——一个连接只能在被创建的那个线程里用。
# 批量模式（`--workers 4`）是 4 个线程并发取数，共用一个 Hub 会直接抛
# `SQLite objects created in a thread can only be used in that same thread`，
# 而且**只有第一个线程能成**、其余全部失败（实测 39 只只成了 3 只）。
#
# 为什么不去关 `check_same_thread`：关掉之后多线程共用一个连接，SQLite 的事务
# 边界和游标会互相踩——上一秒 `BEGIN`、下一秒被另一个线程 `COMMIT`。
# 那种错是间歇性的、数据层面的，比这里直接报错难查一百倍。
#
# 每个线程一个 Hub 的代价只是各自加载一次交易日历（几百 KB，毫秒级）。
# Router 的 strikes 和 Throttle 也随之各持一份，但这不要紧：Throttle 走文件 + flock，
# 跨进程都是同一份配额；strikes 按线程独立反而更准确（一个线程的失败不该让
# 另一个线程跟着降级）。
_TLS = threading.local()


def hub() -> Hub:
    """取当前线程的 Hub；没有就建一个。"""
    h = getattr(_TLS, "hub", None)
    if h is None:
        h = preflight()
        _TLS.hub = h
    return h


def close_hub() -> None:
    """关掉**当前线程**的 Hub。批量结束时由 `batch.py` 在每个 worker 里各调一次。"""
    h = getattr(_TLS, "hub", None)
    if h is not None:
        try:
            h.close()
        finally:
            _TLS.hub = None


# ---------------------------------------------------------------- 形状适配

def _ensure_retrying(h: Hub, domain: str, c: str, need, *, tries: int = 4,
                     wait: float = 1.5) -> None:
    """`h.ensure()` + **对"上游返回空表"重试**。

    ## 为什么必须在这里重试，而不是指望共享层

    共享层把「空表」当成**终态**（`api._pull()` 里 `if not rows: raise EmptyResult`）：
    空表可能是次新股、停牌、退市——那都不该重试，重试只是白打上游。
    这个判断在共享层是对的：它没有足够信息区分"真的没数据"和"上游这次抽风"。

    **但实测 weStock 的 `data_finance` 会间歇性返回空**：同一只股票连查 3 次，
    成功次数 0/1/2/3 都见过（`sz000407`、`sz002596` 三次全空，隔一会儿再查就有）。
    39 只票批量跑，第一版成功 26 只、失败 13 只，其中大半是这种抽风。

    所以重试放在**这一层**：`quality-screen` 知道"我在给一只正常交易的股票出报告，
    它的财报应该是有的"，这是共享层不可能知道的信息。4 次尝试覆盖到 1-3 次抽风的绝大多数。

    只重试 `EmptyResult`，**不重试**其它异常——`SourceUnavailable`（接口改名）、
    `PartialData`（限频后仍缺）重试也没用，重试只会让整批更慢。
    """
    from stock_data_fetch.errors import EmptyResult

    last: Exception | None = None
    for i in range(max(1, tries)):
        try:
            h.ensure(domain, c, need)
            return
        except EmptyResult as exc:
            last = exc
            if i + 1 < tries and not QUIET:
                sys.stderr.write(f"      ↻ {c} 的 {domain} 上游返回空，重试 "
                                 f"({i + 1}/{tries - 1})\n")
            if i + 1 < tries:
                time.sleep(wait)
    raise last or DataError(f"{c} 的 {domain} 取不到")


def _finance_call(h: Hub, c: str, periods: int,
                  gbal: dict[str, Any] | None = None) -> dict[str, Any]:
    """`data_finance`：把 `finance_full()` 的三表拼成上游原样。

    `finance_full()` 返回的每行已经带 `period`（上游原本把日期放在外层
    `REPORT_DATE`）。`compute._date()` 认 `EndDate` / `REPORT_DATE` / `date`
    三个键，这里补一个 `EndDate` 最稳——那是 weStock 自己的键，也是原版
    走的一路。`period` 保留着，不碍事。

    ## 为什么要在这里把 `gbal` 并进 balance 行

    weStock 不返回 `GoodWill` / `ShortTermLoan`，而 `compute._period_record()`
    只从 `bal` 里读它们。**并进来**的理由是"下游一行都不用改"：
    `compute.py` 里 3000 多行计算代码，改一处就得重新验证整条链；
    而在这里补两个键，`GoodWill` 就自然有值了，`goodwill_imputed` 也随之变成 False。

    `gbal` 的 `None` 语义**在这里落定**（见 `api.gbalance_full` 的长注释）：
    `has_balance=True` 时 `None` → `0.0`（公司真没有这个科目，如茅台）；
    `has_balance=False` 时**整个键都不写**——那会让 `compute` 走它的
    `goodwill_imputed` 分支，把商誉按 0 处理但**标记为推算值**，
    报告里会提示"接口未返回商誉，按 0 处理"。两种情况的区别必须保留：
    前者是真的没有，后者是查不到。
    """
    need = N.want.finance(h.target, periods=periods, min_points=1)
    try:
        _ensure_retrying(h, "finance", c, need)
    except Exception as exc:                # noqa: BLE001
        raise DataError(f"关键数据 data_finance 获取失败：{exc}") from exc
    full = h.finance_full(c, need)

    # 商誉 / 短借补丁：按报告期索引，只并进"确实有资产负债表"的那些期
    patch: dict[str, dict[str, Any]] = {}
    if gbal and gbal.get("has_balance"):
        for p in gbal.get("periods") or []:
            d = str(p.get("REPORT_DATE") or "")[:10]
            if not d:
                continue
            gw, sl = p.get("GOODWILL"), p.get("SHORT_LOAN")
            patch[d] = {"GoodWill": 0.0 if gw is None else gw,
                        "ShortTermLoan": 0.0 if sl is None else sl}

    sections: dict[str, list[dict]] = {}
    for table in ("income", "balance", "cashflow"):
        rows = []
        for row in full.get(table) or []:
            r = dict(row)
            d = str(r.get("period") or "")[:10]
            r.setdefault("EndDate", r.get("period"))
            if table == "balance" and d in patch:
                r.update(patch[d])
            rows.append(r)
        sections[table] = rows
    return {"tool": "data_finance",
            "params": {"code": c, "fields": "all", "num": periods},
            "response": {"code": 0, "msg": "ok", "data": {c: sections}}}


def _simple_call(h: Hub, domain: str, tool: str, c: str, need,
                 shape: str) -> dict[str, Any]:
    """点域通用：取一行、按上游形状包好。

    `shape` 决定 response 长什么样，**照抄原版**：
    - `"flat"`  → `{code: {...}}`（`data_quote` / `data_profile` 的样子）
    - `"plain"` → 直接是业务字段（`data_consensus` / `data_rating` 的样子）

    ## 列名回译（`quote` 与 `profile`）

    共享库把上游列名归一过（`total_mv` / `trade_date`…），而下游 `compute.py`
    读的是上游原名（`total_market_cap` / `time`…）。这里换回去。

    **为什么在下游适配而不是改下游**：`compute.py` / `score.py` / `render.py`
    加起来近 3300 行，全是照着上游列名写的；在这里做一张对照表是小改动，
    改下游是大改动且每处都要重新验证。而且「归一列名」是共享库的既定约定
    （`SCHEMA` §2），不该为了某个上层工具反向破例。

    漏映射的后果**不报错**：下游 `quote.get("total_market_cap")` 拿到 None，
    市值算不出来，估值分整块变成"数据不足"——报告照出，只是结论没了。
    实测第一版就是这样（茅台估值分 V 为空）。加字段时两边都要看一眼。
    """
    try:
        _ensure_retrying(h, domain, c, need)
    except Exception as exc:                # noqa: BLE001
        raise DataError(f"{tool} 获取失败：{exc}") from exc
    rng = h.read(domain, c, need)
    row = dict(rng.rows[0]) if rng.rows else {}
    row = _back_translate(domain, row)

    # ## `{"ok": true, "data": ...}` 这一层不能省（2026-11-14 修）
    #
    # 上游真实返回（见 `stock-quality-screen-tool/tests/fixtures/mini_bundle.json`）：
    #
    #     data_quote   → {"ok": true, "data": {"sh600519": {...}}}
    #     data_profile → {"ok": true, "data": {"code": ..., "name": ...}}
    #
    # 下游 `compute._quote()` / `_profile()` 走的是 `_data(_find_call(...))`，
    # 而 `_data()` 的实现是 `_response(call).get("data")`——**只认这一层**。
    #
    # 第一版我返回的是 `{c: row}`（缺 `data` 外壳），于是 `_data()` 拿到 None、
    # `_code_data()` 兜底也救不回来，两者都静默返回 `{}`：
    # 报告照出，但公司名变成代码（`sz002157`）、市值算不出来。
    # 症状是"能跑、能出、结论不对"，最难查的一类——和 `_snapshot_plain()`
    # 那个 `{"data": {...}}` 的坑是同一个根因：**少包一层**。
    #
    # `flat`：quote 多套一层 code（上游按 code 索引）；profile 的 data 就是那只票本身。
    if shape == "flat":
        inner: Any = {c: row} if domain == "quote" else row
    else:
        inner = row
    return {"tool": tool, "params": {"code": c},
            "response": {"ok": True, "data": inner}}


# 共享库归一列名 → 上游原名。
#
# **这张表是实测出来的，不是猜的**：`grep 'quote.get("' compute.py score.py`
# 得到下游真正会读的全部字段——`name` / `prev_close` / `price` / `time` /
# `total_market_cap` / `total_shares`，六个。其中三个（`name`/`prev_close`/`price`）
# 共享库用的就是原名，只有下面这些要换。
#
# 加字段时**两边都要看一眼**：漏映射不报错，只是那个指标恒为 None。
_QUOTE_BACK = {
    "trade_date": "time",              # compute._quote 读 quote.get("time")
    "total_mv": "total_market_cap",    # market_cap_yi 读它；漏了市值算不出来
}

_PROFILE_BACK = {
    "symbol": "code",                  # compute._profile 读 profile.get("code")
    "listed_date": "listedDate",
    # `business` / `sector` / `industry` / `name` 共享库用的就是上游原名，不用换。
    # `business` 是 v4 才存进来的（主营业务描述），报告第一节「生意本身」读它。
}


def _back_translate(domain: str, row: dict[str, Any]) -> dict[str, Any]:
    """把归一列名换回上游原名，**同时保留原名**（两个键并存）。

    保留的原因：共享库自己也有一批上层（kline_screen）在用归一列名，
    这个函数只服务于 quality-screen 这一侧，不该假设别人的读法。
    两个键并存最省心——谁也不吃亏。
    """
    table = {"quote": _QUOTE_BACK, "profile": _PROFILE_BACK}.get(domain)
    if not table:
        return row
    out = dict(row)
    for src, dst in table.items():
        if src in out and dst not in out:
            out[dst] = out[src]
    return out


def _snapshot_call(h: Hub, sub: str, tool: str, c: str, need,
                   key: str) -> dict[str, Any]:
    """`snapshot` 子域：取出 payload 里的列表，包成东财 `{result:{data:[...]}}` 形状。

    `mainop` 与 `gcashflow` 都走这里——下游用 `_rows()` 取 `.result.data`。
    """
    try:
        _ensure_retrying(h, "snapshot", c, need)
    except Exception as exc:                # noqa: BLE001
        raise DataError(f"{tool} 获取失败：{exc}") from exc
    rng = h.read("snapshot", c, need)
    rows: list[dict] = []
    if rng.rows:
        payload = rng.rows[0].get("payload")
        if isinstance(payload, str):
            import json
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        if isinstance(payload, dict):
            rows = [r for r in (payload.get(key) or []) if isinstance(r, dict)]
        elif isinstance(payload, list):     # mainop 存的是裸列表
            rows = [r for r in payload if isinstance(r, dict)]
    return {"tool": tool, "params": {"code": c},
            "response": {"result": {"data": rows, "pages": 1}}}


def _valuation_call(h: Hub, c: str) -> dict[str, Any]:
    """`RPT_VALUEANALYSIS_DET`：估值周频序列，转回东财原始列名。

    共享库的 `valuation` 表存的是**归一后**的列（`trade_date` / `pe_ttm` /
    `pb_mrq` / `ps_ttm` / `total_mv`），而 `compute._valuation()` 读的是东财原始
    列名（`TRADE_DATE` / `PE_TTM` / `PB_MRQ` / `PS_TTM`）。这里换回去——
    让下游适配库的列名，比改下游 100 多行渲染代码安全得多。
    """
    cal = h.cal
    # 上市日决定"这家公司到底能有多少周估值"：430 周是老股的理想深度，对 2021 年
    # 上市的公司是永远达不成的门槛，结果是报告里长期挂一条「取数缺口」、估值分位
    # 整节空白，而且每次运行都重取一遍。分位本身按全部可用历史算、渲染层按实际
    # 起始年份写"2019 年以来周频"，所以短历史不算错误（见 `want.valuation`）。
    listed = None
    try:
        row = h.conn.execute("select list_date from stock where symbol=?", (c,)).fetchone()
        listed = row[0] if row else None
    except Exception:                            # noqa: BLE001 - 取不到就按原门槛
        listed = None
    need = N.want.valuation(cal, h.target, listed=listed)
    try:
        _ensure_retrying(h, "valuation", c, need)
    except Exception as exc:                # noqa: BLE001
        raise DataError(f"RPT_VALUEANALYSIS_DET 获取失败：{exc}") from exc
    rng = h.read("valuation", c, need)
    rename = {"trade_date": "TRADE_DATE", "pe_ttm": "PE_TTM", "pb_mrq": "PB_MRQ",
              "ps_ttm": "PS_TTM", "total_mv": "TOTAL_MARKET_CAP",
              "circ_mv": "CIRCULATION_MARKET_CAP"}
    rows = [{rename.get(k, k): v for k, v in dict(r).items()} for r in rng.rows]
    return {"tool": "RPT_VALUEANALYSIS_DET", "params": {"code": c},
            "response": {"result": {"data": rows, "pages": 1}}}


def _optional(h: Hub, c: str, build, errors: list[dict[str, str]]) -> dict | None:
    """可选数据：失败只记 errors，不拖垮整只股票。"""
    try:
        return build()
    except Exception as exc:                # noqa: BLE001
        errors.append({"tool": getattr(build, "__name__", "?"), "error": str(exc)})
        return None


# ---------------------------------------------------------------- 主入口

def fetch_all(code: str, timeout: float = 90.0, retries: int = 0,
              retry_wait: float = 60.0, periods: int = 20,
              extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回 `{code, calls, errors, extra}`。

    `timeout` / `retries` / `retry_wait` 三个参数**保留但不再生效**：它们原本是给
    node 子进程与东财直连用的，现在超时与重试由共享层的 Routet + Throttle 统管。
    留着的唯一原因是调用方（`batch.py` / `stock_report.py` / CLI）还在传，
    删掉会连锁改一堆签名，而多三个无用参数无害。真要调超时改 `STOCKHUB_*` 环境变量。

    `extra` 里放**不冒充原 tool** 的补充数据（目前是 `gbalance`：商誉 + 短期借款）。
    单独一个 key 而不是塞进 `calls`，是为了让"下游不认也不受影响"这件事成立。
    """
    c = normalize_code(code)
    h = hub()
    calls: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    out_extra: dict[str, Any] = dict(extra or {})

    # ---- 先取商誉/短借补丁：它要并进下面的 data_finance ----
    # 放最前面是因为 `_finance_call()` 需要它做入参。它**本身不是必需的**：
    # 取不到就退化成"原版的行为"（商誉按 0 且标记为推算值），报告照出。
    gbal: dict[str, Any] = {"periods": [], "has_balance": False}
    try:
        need_g = N.want.gbalance(h.target, periods)
        # 只重试 2 次：它是可选补丁，取不到会退化成"商誉按 0 并标记推算值"
        # （即原版行为），报告照出。为它花 4 次 × 1.5 秒不值得。
        _ensure_retrying(h, "snapshot", c, need_g, tries=2)
        gbal = h.gbalance_full(c, need_g)
    except Exception as exc:                # noqa: BLE001
        errors.append({"tool": "gbalance", "error": str(exc)})
    out_extra["gbalance"] = gbal

    # ---- 必需：财务三表。取不到就是没有报告，直接抛 ----
    calls.append(_finance_call(h, c, periods, gbal))

    # ---- 必需：行情。评分要用总市值 / 流通市值 ----
    calls.append(_simple_call(h, "quote", "data_quote", c,
                              N.want.quote(h.target), "flat"))

    # ---- 可选：资料 / 一致预期 / 评级，任一失败只记缺口 ----
    for domain, tool, shape in (("profile", "data_profile", "flat"),
                                ("snapshot", "data_consensus", "plain"),
                                ("snapshot", "data_rating", "plain")):
        try:
            if domain == "snapshot":
                sub = tool.replace("data_", "")
                calls.append(_snapshot_plain(h, sub, tool, c))
            else:
                calls.append(_simple_call(h, domain, tool, c,
                                          N.want.profile(), shape))
        except Exception as exc:            # noqa: BLE001
            errors.append({"tool": tool, "error": str(exc)})

    # ---- 可选：东财三件套（估值 / 主营构成 / capex）----
    for builder, tool in ((lambda: _valuation_call(h, c), "RPT_VALUEANALYSIS_DET"),
                          (lambda: _snapshot_call(h, "mainop", "RPT_F10_FN_MAINOP",
                                                  c, N.want.snapshot("mainop",
                                                                     h.target),
                                                  "periods"),
                           "RPT_F10_FN_MAINOP"),
                          (lambda: _snapshot_call(h, "gcashflow",
                                                  "RPT_F10_FINANCE_GCASHFLOW", c,
                                                  N.want.gcashflow(h.target, periods),
                                                  "periods"),
                           "RPT_F10_FINANCE_GCASHFLOW")):
        try:
            calls.append(builder())
        except Exception as exc:            # noqa: BLE001
            errors.append({"tool": tool, "error": str(exc)})

    return {"code": c, "calls": calls, "errors": errors, "extra": out_extra}


def _snapshot_plain(h: Hub, sub: str, tool: str, c: str) -> dict[str, Any]:
    """`consensus` / `rating`：一个子域一份 payload，包成**上游原样**返回。

    ## `{"data": {code: payload}}` 这一层不能省

    下游 `compute._forecast()` 走的是 `_code_data(_data(call), code)`：
    `_data()` 取 `.data`，`_code_data()` 再按 `code` 取一层。

    真实上游返回（`mini_bundle.json` 实测）是 **单层**：

        {"ok": true, "data": {"code": "sh600519", "name": ..., "targetPrice": ...,
                              "forecastInstitutions": 4, ...}}

    原版单元测试的 `payload_call()` 构造的是 `{"data": {CODE: payload}}`（多套一层
    code），`_code_data()` 先试 `payload[code]`、失败再 `return payload` 兜底——
    所以两种形状都能工作，但**单层才是上游真样子**，这里照真的来。

    踩过两次的坑都记在这里：
    1. 第一版直接返回 payload 本体（`response` 就是业务字段，连 `data` 都没包）→
       `_data()` 拿到 None，报"机构覆盖 0 家"、估值分整块为 None；
    2. `{"data": {c: body}}` 能跑，但和上游不一致，属于"靠兜底活着"。
    """
    need = N.want.snapshot(sub, h.target)
    _ensure_retrying(h, "snapshot", c, need)
    rng = h.read("snapshot", c, need)
    body: dict = {}
    if rng.rows:
        payload = rng.rows[0].get("payload")
        if isinstance(payload, str):
            import json
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if isinstance(payload, dict):
            body = payload
    return {"tool": tool, "params": {"code": c},
            "response": {"ok": True, "data": body}}


def load_bundle(path: str | Path) -> dict[str, Any]:
    """读取旧采集包 / 测试 fixture；只作为输入，不生成新 JSON。

    **原样保留**：`--bundle` 离线重放是排查"昨天那份报告为什么这么算"的唯一手段，
    换取数层不该把它一起丢掉。旧包没有 `extra`，补一个空的，下游不用判 None。
    """
    import json
    p = Path(path)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"采集包读取失败：{exc}") from exc
    calls = obj.get("calls")
    if not isinstance(calls, list) or not calls:
        raise DataError("采集包里没有 calls")
    code = obj.get("code") or ""
    for call in calls:
        params = call.get("params") or {}
        if not code and params.get("code"):
            code = params["code"]
    if not code:
        raise DataError("采集包里没有代码")
    return {"code": normalize_code(code), "calls": calls,
            "errors": obj.get("errors") or [],
            "extra": obj.get("extra") or {}}
