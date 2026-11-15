"""取数层：**全部走共享数据层**（`stock_data_fetch`），不再直连上游。

## 为什么改成这样

原版每只股票打 **2 次网络**（`data_finance` + `data_profile`，经 node 转发 MCP），
而且自己维护一套跨线程节流闸门、退避分档、抖动防惊群。跑 100 只就是 200 次请求，
同一只股票明天再跑还要再打一遍。

现在改成向 `stock_data_fetch` 要数据：库里有的直接用，没有的才联网，联网拿到的
**写回库**，下次（乃至其它三个工具）直接复用。财报一季度才变一次，差别很大。

## 契约**一个字都没改**

`fetch_one()` 仍然返回：

    {"code", "name", "industry", "income": [...], "balance": [...], "data_gaps": [...]}

`build.py` / `metrics.py` / `decide.py` / `render.py` 一行都不用动——它们只认这个结构。
这是敢换取数层的前提：下游 2000 多行计算与渲染代码零风险。

## 字段来源对照

| build 要的字段 | 原版来源 | 现在来源 |
|---|---|---|
| `OperatingRevenue_Q` | `data_finance` income | `finance.raw_json`（`hub.finance_full`） |
| `OperatingCost_Q` | 同上 | 同上 |
| `OperatingProfit_Q` | 同上 | 同上 |
| `TotalFixedAsset` | `data_finance` balance | 同上 |
| `TConstruInProcess` | 同上 | 同上 |
| `ContractLiability` | 同上（仅展示） | 同上 |
| `name` / `industry` | `data_profile` | `profile` 域（`stock` 表） |

## 三个必须注意的地方

**1. 期数下限是 43，不是 4 的整数倍。**
判断③ 要 40 个周转率样本 = 10 年分位；而周转率是「最近 4 季之和」，
前 3 期凑不满 → 需要 40 + 3 = **43 期**。共享库 `RAW_JSON_KEEP` 已设为 43
（见 `api.py` 那段长注释）。少要一期，分位窗口就短一截，而报告标题仍写
「10 年分位」——**症状是标签偏了但不报错**，是最难查的一类。

**2. `finance_full()` 返回的行带 `period`，不带 `EndDate`。**
`build._row_date()` 认 `EndDate` / `date` / `REPORT_DATE` / `EndDateStr`，**不认 `period`**。
所以这里必须补一个 `EndDate`——否则每一行都被判「日期格式异常，已跳过」，
最终 `quarters=0`，报告出一张空表。这是迁移时最容易踩的坑。

**3. `_ensure_retrying()` 要重试「上游返回空」。**
共享层把空表当**终态**（`api._pull()` 里 `if not rows: raise EmptyResult`）——
这个判断在共享层是对的：它没有足够信息区分「真没数据」和「上游这次抽风」。
但实测 weStock 的 `data_finance` 会间歇性返回空（同一只票连查 3 次，
成功 0/1/2/3 次都见过）。cycle-elastic 知道「我在给一只正常交易的股票出报告，
它的财报应该是有的」，这是共享层不可能知道的信息，所以重试放在这一层。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

# `stock_cycle_elastic/cycle/` → `stock-value-invest-tool/`，让 `stock_data_fetch` 可 import
_HERE = Path(__file__).resolve().parent          # .../stock_cycle_elastic/cycle/
_HUB = _HERE.parent.parent                       # .../stock-value-invest-tool/
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

import config                                    # noqa: E402
from cycle import codes                          # noqa: E402

try:
    from stock_data_fetch import Hub, preflight
    from stock_data_fetch import need as N
except ImportError as exc:                       # pragma: no cover
    raise SystemExit(
        f"找不到共享数据层 stock_data_fetch（找的是 {_HUB}）。\n"
        f"原版 cycle-elastic 自带 node 取数通道，本版改成依赖共享库；\n"
        f"请确认 stock-value-invest-tool 与本目录同级。原始错误：{exc}") from exc


class DataError(Exception):
    """取数失败。CLI 捕获后退出码 2。"""


# --quiet：重试警告属于进度噪音，静音模式下不打（失败仍会记进 data_gaps）。
QUIET = False


def set_quiet(flag):
    global QUIET
    QUIET = bool(flag)


# ---------------------------------------------------------------------------
# Hub 生命周期
# ---------------------------------------------------------------------------
#
# **每线程一个 Hub，不是一个进程一个。**
#
# SQLite 连接默认 `check_same_thread=True`——一个连接只能在创建它的线程里用。
# 批量模式（`--workers 4`）是 4 个线程并发取数，共用一个 Hub 会直接抛
# `SQLite objects created in a thread can only be used in that same thread`，
# 而且**只有第一个线程能成**、其余全部失败。
#
# 为什么不去关 `check_same_thread`：关掉之后多线程共用一个连接，SQLite 的事务
# 边界和游标会互相踩——上一秒 BEGIN、下一秒被另一个线程 COMMIT。那种错是
# 间歇性的、数据层面的，比这里直接报错难查一百倍。
#
# 每个线程一个 Hub 的代价只是各自加载一次交易日历（几百 KB，毫秒级）。
# Throttle 走文件 + flock，跨进程都是同一份配额，所以并发不会超配额。
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


# ---------------------------------------------------------------------------
# 带重试的 ensure
# ---------------------------------------------------------------------------

#: 重试「上游返回空」时的等待秒数。
#:
#: 单列成模块常量而不是写死 `_ensure_retrying(wait=1.5)`：测试要把它压到 0，
#: 否则每个重试用例白等 1.5 秒（`test_fetch` 里有 4 个这样的用例）。
_RETRY_WAIT_DEFAULT = 1.5


def _ensure_retrying(h: Hub, domain, c, need, *, tries: int = 4,
                     wait: float | None = None) -> None:
    """`h.ensure()` + **对「上游返回空表」重试**。

    只重试 `EmptyResult`，**不重试**其它异常——`SourceUnavailable`（接口改名）、
    `PartialData`（限频后仍缺）重试也没用，重试只会让整批更慢。

    为什么需要（详见模块 docstring 第 3 点）：共享层把空表当终态是对的，
    但它不知道「这只票财报一定存在」。4 次尝试覆盖 1-3 次上游抽风的绝大多数。
    """
    from stock_data_fetch.errors import EmptyResult

    if wait is None:
        wait = _RETRY_WAIT_DEFAULT
    last = None
    for i in range(max(1, tries)):
        try:
            h.ensure(domain, c, need)
            return
        except EmptyResult as exc:
            last = exc
            if i + 1 < tries:
                if not QUIET:
                    sys.stderr.write(f"      ↻ {c} 的 {domain} 上游返回空，重试 "
                                     f"({i + 1}/{tries - 1})\n")
                time.sleep(wait)
    raise last or DataError(f"{c} 的 {domain} 取不到")


# ---------------------------------------------------------------------------
# 业务取数
# ---------------------------------------------------------------------------

def _symbol(code) -> str:
    """`600519` / `600519.SH` → `sh600519`。共享库的 symbol 一律带前缀。"""
    six = codes.parse(code)
    return codes.with_prefix(six) if not six.startswith(("sh", "sz", "bj")) else six


def fetch_finance(code, num=None):
    """取利润表 + 资产负债表，返回 `(income列表, balance列表)`。

    返回的行是**上游原始字段名**（`OperatingRevenue_Q` 等），并且补了
    `EndDate`——`build._row_date()` 靠它认日期，只给 `period` 会被当成脏数据跳过。

    没有超时/重试参数：重试由 `_ensure_retrying` 接管（只对空表重试 4 次），
    节流与超时由共享库的 Throttle 接管。原版的 `retries` / `retry_wait` /
    `timeout` / `interval_ms` 已删除 —— 留着不生效的参数比删掉更误导人。
    """
    h = hub()
    periods = config.HISTORY_QUARTERS if num is None else num
    symbol = _symbol(code)

    # `min_points=1` 而不是 40：期数不够由 `decide` 标「历史不足」并照常出报告
    # （README「次新股必然缺 10 年数据，直接报错等于放弃一整个股票池」），
    # 不该在这里抛 `InsufficientDepth` 把整只股票判死。
    # 这也与原版行为一致：原版 `num=44` 拿回几期算几期。
    need = N.want.finance(h.target, periods=periods, min_points=1)
    try:
        _ensure_retrying(h, "finance", symbol, need)
    except DataError:
        raise
    except Exception as exc:                # noqa: BLE001
        # `_ensure_retrying` 重试用尽后抛的是共享库的 `EmptyResult`，
        # 它和本模块的 `DataError` 是两个类。这里统一翻译：CLI 只认 `DataError`
        # （退出码 2 + 一条人话），漏翻译会让栈回溯直接吐到用户脸上。
        raise DataError(f"{code} 的财报取不到：{exc}") from exc

    full = h.finance_full(symbol, need)
    income = [_with_end_date(r) for r in (full.get("income") or [])]
    balance = [_with_end_date(r) for r in (full.get("balance") or [])]

    if not income:
        # 空表等于没取到。`_ensure_retrying` 已经重试过 4 次，到这里就是真没有。
        raise DataError(
            f"{code} 的利润表为空（库里无 raw_json 且上游多次返回空，"
            f"通常为限频/服务端降级，稍后重试）")

    # 只保留最近 num 期：库里可能有 43 期，但调用方只要 20 期时不该把它全塞给 build
    # ——build 按日期升序处理，前面的期数只是多余计算，不影响结果，但慢。
    if num is not None and len(income) > num:
        income = income[-num:]
        balance = balance[-num:] if balance else balance
    return income, balance


def _with_end_date(row: dict) -> dict:
    """补 `EndDate`。

    `finance_full()` 每行带 `period`（上游原本把日期放在外层 `REPORT_DATE`）。
    `build._row_date()` 认的四个键里**没有 `period`**，所以这里补一个 `EndDate`。
    不补的话：每一行都被判「日期格式异常，已跳过」，`quarters=0`，报告出空表。
    """
    r = dict(row)
    period = r.get("period")
    if period and not any(r.get(k) for k in ("EndDate", "date", "REPORT_DATE")):
        r["EndDate"] = period
    return r


def fetch_profile(code):
    """取公司名与行业。失败返回空 dict（不中断流程，调用方记 data_gaps）。

    名称/行业是可选数据，所以失败吞掉——一只票没有行业名，
    不该让整份报告作废（`build` 只是少一个表头字段）。
    """
    h = hub()
    symbol = _symbol(code)

    need = N.want.profile()
    try:
        _ensure_retrying(h, "profile", symbol, need, tries=2, wait=1.0)
    except Exception:                       # noqa: BLE001
        return {}

    rng = h.read("profile", symbol, need)
    if not rng.rows:
        return {}
    row = dict(rng.rows[0])

    return {
        "name": (row.get("name") or "").strip(),
        "industry": (row.get("industry") or "").strip(),
        "code": row.get("symbol") or symbol,
    }


def _pick(mapping, keys):
    for k in keys:
        v = mapping.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def fetch_one(code):
    """取一只股票的完整原始数据，返回
    `{code, name, industry, income, balance, data_gaps}`。"""
    data_gaps = []
    income, balance = fetch_finance(code)

    name, industry = "", ""
    try:
        profile = fetch_profile(code)
        name = _pick(profile, ("name", "orgName", "org_name", "secuAbbr", "cname"))
        industry = _pick(profile, ("industry", "induName", "industryName"))
    except DataError as exc:
        # 名称/行业是可选数据：失败不能让整只股票作废
        data_gaps.append(f"data_profile 失败：{exc}")

    return {
        "code": codes.parse(code),
        "name": name or "",
        "industry": industry or "",
        "income": income,
        "balance": balance,
        "data_gaps": data_gaps,
    }


# ---------------------------------------------------------------------------
# 离线重放（--bundle，测试与排障用）
# ---------------------------------------------------------------------------
#
# 这一段**原样保留**：它不碰网络，只读 JSON 文件，是测试跑得快的基础
# （tests/ 全程离线靠它）。改取数层不该动它。

def load_bundle(path):
    """从 JSON 文件读取一份离线数据包，格式：

        {
          "code": "601899",                      # 可省，缺省时从 calls 里取
          "name": "紫金矿业",                     # 可省
          "industry": "有色金属",                  # 可省
          "calls": {
            "data_finance": { ...原始返回... },
            "data_profile": { ...原始返回... }     # 可省
          }
        }

    也接受最简形式：文件本身就是 data_finance 的原始返回（旧格式兼容）。
    """
    if not os.path.exists(path):
        raise DataError(f"bundle 文件不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise DataError(f"bundle 不是合法 JSON：{exc}")

    if not isinstance(raw, dict):
        raise DataError("bundle 顶层必须是对象")

    if "calls" in raw:
        calls = raw.get("calls") or {}
        if not isinstance(calls, dict):
            raise DataError("bundle.calls 必须是对象")
        fin = calls.get("data_finance")
        prof = calls.get("data_profile")
        code = raw.get("code") or _code_from_calls(fin) or _code_from_calls(prof)
        if not code:
            raise DataError("bundle 里没有 code，也没有可从 params 推断的调用")
        debt = {
            "code": codes.parse(code),
            "name": raw.get("name") or "",
            "industry": raw.get("industry") or "",
            "income": [],
            "balance": [],
            "data_gaps": [],
        }
        if fin is not None:
            node = _dig_finance(fin, codes.with_prefix(code), code)
            debt["income"] = node.get("income") or []
            debt["balance"] = node.get("balance") or []
        if prof is not None:
            node = prof.get("data") if isinstance(prof.get("data"), dict) else prof
            debt["name"] = debt["name"] or _pick(node, ("name", "orgName"))
            debt["industry"] = debt["industry"] or _pick(node, ("industry", "induName"))
        return debt

    # 旧格式：文件本身就是 data_finance 的返回
    code = raw.get("code") or _code_from_calls(raw)
    if not code:
        raise DataError("bundle 缺少 code（旧格式需要在顶层写 code）")
    node = _dig_finance(raw, codes.with_prefix(code), code)
    return {
        "code": codes.parse(code),
        "name": raw.get("name") or "",
        "industry": raw.get("industry") or "",
        "income": node.get("income") or [],
        "balance": node.get("balance") or [],
        "data_gaps": [],
    }


def _dig_finance(payload, mcp_code, code):
    """从 data_finance 的嵌套结构里挖出 {income, balance, cashflow}。

    实测结构：{"ok":true,"data":{"code":0,"msg":"...","data":{"sh600519":{...}}}}
    但接口历史上变过嵌套层数，所以这里做成"逐层找"而不是死写路径。

    **只在 `--bundle` 重放旧快照时用**。实时取数走 `finance_full()`，不经过这里。
    """
    seen = []
    node = payload
    for _ in range(4):
        if not isinstance(node, dict):
            break
        for key in (mcp_code, mcp_code.lower(), codes.parse(code)):
            if key in node and isinstance(node[key], dict):
                inner = node[key]
                if "income" in inner or "balance" in inner:
                    return inner
        node = node.get("data")
        seen.append(node)

    for node in seen:
        if isinstance(node, dict) and ("income" in node or "balance" in node):
            return node
    raise DataError(f"{code} 的返回里找不到 income/balance（结构可能变了）")


def _code_from_calls(payload):
    """从 {"params": {"code": "sh600519"}} 里兜出代码（兼容旧 bundle）。"""
    if not isinstance(payload, dict):
        return ""
    params = payload.get("params")
    if isinstance(params, dict):
        for key in ("code", "symbol", "secuCode"):
            val = params.get(key)
            if isinstance(val, str) and val.strip():
                try:
                    codes.parse(val)
                    return val.strip()
                except ValueError:
                    continue
    return ""


def replay(calls, code, name="", industry=""):
    """用一组"按工具名索引的返回"直接构造 build 的输入，供测试替身使用。"""
    fin = calls.get("data_finance")
    if fin is None:
        raise DataError("calls 里缺少 data_finance")
    node = _dig_finance(fin, codes.with_prefix(code), code)
    prof = calls.get("data_profile") or {}
    pnode = prof.get("data") if isinstance(prof.get("data"), dict) else prof
    return {
        "code": codes.parse(code),
        "name": name or _pick(pnode, ("name", "orgName")),
        "industry": industry or _pick(pnode, ("industry", "induName")),
        "income": node.get("income") or [],
        "balance": node.get("balance") or [],
        "data_gaps": [],
    }


# ---------------------------------------------------------------------------
# 已删除接口的显式桩
# ---------------------------------------------------------------------------
#
# 原版 `fetch.py` 导出过 `call_tool` / `call_with_retry` / `reset_throttle` /
# `set_interval_ms`，`tests/test_fetch.py` 直接测它们（跨线程节流、退避分档、
# 纯文本判失败、抖动防惊群）。这些机制现在由共享库承担，本层不再有对应实现。
#
# 不删掉桩、也不假装实现：留一个**显式报错**的桩，让"这个测试在测已经不存在的东西"
# 一眼可见，而不是变成一个 ImportError 让人翻半天。

def _gone(name):
    raise NotImplementedError(
        f"`{name}` 是原版 node 取数通道的接口，迁移到共享数据层后已不存在。\n"
        f"重试与节流现在由 stock_data_fetch 负责（见模块 docstring）。\n"
        f"如果测试还在调它，那个测试测的是已经被删掉的机制，应当一并改写或删除。")


def call_tool(*a, **kw):
    _gone("call_tool")


def call_with_retry(*a, **kw):
    _gone("call_with_retry")


def reset_throttle(*a, **kw):
    _gone("reset_throttle")


def set_interval_ms(*a, **kw):
    _gone("set_interval_ms")
