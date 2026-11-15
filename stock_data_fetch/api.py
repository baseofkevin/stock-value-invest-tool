"""`ensure` / `ensure_many` / `read` —— 唯一会"决定要不要打上游"的地方。

流程严格照 `CONTRACT` §6：

    read_coverage → hit? → 返回
    flock((域,股票))                     ← 分片锁，不是全局锁
      双检（等锁期间别人可能已经填好）
      清掉到期可重试的 hole 重试标记（force 时全清）
      miss = missing_segments(...)       ← 可能多段
      for seg in miss: 节流 → fetch → 组级重试 → 一个事务里写数据 + 写覆盖
    verdict(cov, need)                   ← 要么返回覆盖，要么抛异常，没有第三种

**写数据与写覆盖必须在同一个事务里**（`SCHEMA` §1 不变式 3）。分开写会出现
"数据进去了、覆盖没更新"或反过来，而两种都会让下一次运行做出错误判断：
前者白取一遍，后者把不完整的数据当成完整的用。

第 5 步起七个域全部接通（`kline` / `finance` / `dividend` / `valuation` / `quote` /
`snapshot` / `profile`）。**两类域的收尾动作不一样**，`_pull` 里按 `coverage.POINT_DOMAINS`
分派：区间域走 `merge()`（按日期重算覆盖与洞），点域走 `merge_point()`（一股一份，
记"这是哪一天的"）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator

from . import coverage as C
from . import db, router as R
from .config import KLINE_KEEP_BARS, SCAN_RETENTION_DAYS
from .errors import EmptyResult, InsufficientDepth, MarketOpenError, PartialData
from .need import Need, want
from .throttle import Throttle

log = logging.getLogger("stock_data_fetch.api")

SCAN_SYMBOL = "*"
"""`scan` 域的"股票代码"。横截面的粒度是**一天**，不是一只股票，覆盖表却按
`(domain, symbol, fp)` 存，只好借这一列放一个不会与真实代码冲突的值。

真实代码最长 8 位（`sh600519`），`"*"` 不可能是任何一只票，`stock` 表里也不会出现，
所以 `_touch()` 对它是 0 行更新——无害且不需要特判。
"""

try:                                     # pragma: no cover - 平台差异
    import fcntl
except ImportError:                      # pragma: no cover - Windows
    fcntl = None                         # type: ignore[assignment]


@dataclass(frozen=True)
class Range:
    """`ensure()` / `read()` 的返回。`rows` 按日期升序，键 = 表列名。"""

    domain: str
    symbol: str
    rows: list[dict]
    n: int
    start: str | None
    end: str | None
    permanent_holes: list[C.Hole] = field(default_factory=list)
    fetched: bool = False

    def series(self) -> dict[str, list]:
        """列式形态（`CONTRACT` §11）。K 线永远整列用，所以给列式最省事。"""
        cols: dict[str, list] = {}
        for r in self.rows:
            for k, v in r.items():
                cols.setdefault(k, []).append(v)
        return cols


@dataclass
class Batch:
    ok: dict[str, Range] = field(default_factory=dict)
    failed: dict[str, Exception] = field(default_factory=dict)

    def raise_if_any(self) -> None:
        if self.failed:
            sym, exc = next(iter(self.failed.items()))
            raise exc


# ---------------------------------------------------------------- 锁

@contextmanager
def _locked(state_dir: Path, name: str) -> Iterator[None]:
    """按 (域, 股票) 分片加锁。两个工具跑同一只股票时，第二个等第一个填完再双检。"""
    if fcntl is None:                    # pragma: no cover - Windows
        yield
        return
    d = Path(state_dir) / "locks"
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    with open(d / f"{safe}.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------- ensure

def ensure(hub, domain: str, symbol: str, need: Need, *,
           force: bool = False, now: dt.datetime | None = None) -> Range:
    """保证"区间覆盖 + 无阻塞洞 + 够点数"，否则抛。"""
    now = now or dt.datetime.now()
    conn = hub.conn
    throttle = getattr(hub, "throttle", None) or Throttle(hub.cfg)
    rtr = _router(hub, throttle)

    cov = C.read(conn, domain, symbol, need.fp)
    if C.hit(cov, need):
        _touch(conn, symbol)
        return _range(conn, domain, symbol, need, fetched=False)

    with _locked(hub.cfg.state_dir, f"{domain}__{symbol}"):
        cov = C.read(conn, domain, symbol, need.fp)          # 双检
        if C.hit(cov, need):
            _touch(conn, symbol)
            return _range(conn, domain, symbol, need, fetched=False)

        if force:
            with db.tx(conn):
                C.clear_holes(conn, domain, symbol, need.fp)
            cov = C.read(conn, domain, symbol, need.fp)

        segs = C.missing_segments(cov, need, hub.cal, now)
        if not segs:
            # 覆盖到 `end` 却仍不命中（点数不足 / 口径刚被换源作废）→ 整段重取一次。
            # 这里不判 `need.start`：`finance` 与 `dividend` 本来就有一个端点是 None。
            segs = [(need.start, need.end)]

        attempts: dict[tuple, int] = {}
        fetched = False

        if domain in C.POINT_DOMAINS:
            # 一股一份：**一次就够**。既没有"补一段"，也没有"同一段重试两次"——
            # 取到就是取到，取不到下次运行再来。放在重试循环里会把同一次请求打两遍，
            # 而点域的重试并不会让上游多给一份数据（停牌股问两次还是停牌）。
            fetched = _pull(hub, rtr, domain, symbol, need, (need.start, need.end),
                            first=cov is None)
            cov = C.read(conn, domain, symbol, need.fp)
        else:
            for _ in range(3):
                todo = [s for s in C.missing_segments(cov, need, hub.cal, now)
                        if attempts.get(s, 0) < 2]
                if not todo:
                    break
                for seg in todo:
                    attempts[seg] = attempts.get(seg, 0) + 1
                    fetched |= _pull(hub, rtr, domain, symbol, need, seg, first=cov is None)
                cov = C.read(conn, domain, symbol, need.fp)
                if C.hit(cov, need):
                    break

        if cov is not None and cov.blocking:
            twice = [s for s, n in attempts.items() if n >= 2]
            if twice:
                with db.tx(conn):
                    C.settle(conn, domain, symbol, need.fp, twice, now=now)
                cov = C.read(conn, domain, symbol, need.fp)

    cov = C.read(conn, domain, symbol, need.fp)
    C.verdict(cov, need)                                     # 不满足就抛
    _touch(conn, symbol)
    return _range(conn, domain, symbol, need, fetched=fetched)


def _pull(hub, rtr: R.Router, domain: str, symbol: str, need: Need,
          seg: tuple[str | None, str | None], *, first: bool = False) -> bool:
    """取一段并落库，返回是否真的打了上游。

    `first=True` 表示这是库里第一次取这只股票——此时空结果只能是"代码有问题/已退市"，
    直接报 `EmptyResult`；已经有数据的股票某一段为空，多半是停牌，返回 False 让
    调用方在试够次数后 settle 成永久洞。

    **这里也是"盘中闸"真正生效的地方**（`hub.check_online`）：走到 `_pull` 就说明库里的
    数据不满足这次需求、非联网不可了。盘中（且没放行）在这里抛，而不是在 `preflight()`
    里——那样会让"库里有数据"的盘中运行也一起跑不了。
    """
    check = getattr(hub, "check_online", None)
    if callable(check):
        check(domain, symbol, need)

    sub = replace(need, start=seg[0], end=seg[1])
    got, spec = rtr.fetch(domain, [symbol], sub)

    if got.errors:
        with db.tx(hub.conn):
            C.mark_partial(hub.conn, domain, symbol, need.fp)
        raise PartialData(
            f"{symbol} 的 {domain} 在上游限频后 3 轮重试仍缺。已取到的部分已落库，"
            "下次运行会继续补齐",
            domain=domain, symbol=symbol)

    rows = got.rows.get(symbol) or []
    if not rows:
        reason = got.empty_reason or "no_data"
        if first:
            if reason == "code_moved":
                raise EmptyResult(
                    f"{symbol} 的代码在上游已迁移（返回「已切换」且无数据），"
                    "请更新 universe 里的代码后重跑", domain=domain, symbol=symbol)
            raise EmptyResult(
                f"上游对 {symbol} 的 {domain} 返回空表（{reason}）。"
                "若这不是次新股/停牌，请用 STOCKHUB_LOG=DEBUG 复查请求参数",
                domain=domain, symbol=symbol)
        log.info("%s %s 的 %s..%s 无数据（多半停牌），本次先不报错", domain, symbol, *seg)
        return True

    with db.tx(hub.conn):
        _write_rows(hub.conn, domain, symbol, rows, spec.upstream)
        if domain in C.POINT_DOMAINS:
            # 一股一份：写"哪一天的这一份"，不是"覆盖了哪一段"。
            # `n` 传真实行数：`scan` 一天 5026 行、`quote` 一张 1 行，
            # 都写死 1 的话"只回来 3 只票"和"回来 5026 只"在 doctor 里长得一模一样。
            C.merge_point(hub.conn, domain=domain, symbol=symbol,
                          as_of=_as_of(domain, rows, sub), src=spec.upstream,
                          fp=need.fp, n=len(rows))
        else:
            # 这里**不传 `fetched`**：单只路径有自己的收敛机制——同一段在同一次
            # `ensure` 里取两次仍空就 `settle()` 成永久洞（见 `coverage.settle`
            # 的说明）。在这里 bump 会把洞推入退避窗口，第二次取数被跳过，
            # 于是 `attempts` 到不了 2、永远 settle 不了，反而退化成"每次运行都
            # 报 InsufficientDepth"。批量路径（`ensure_bulk`）没有这个重试环，
            # 才需要靠 bump 收敛。
            C.merge(hub.conn, cal=hub.cal, need=need, symbol=symbol, src=spec.upstream)
        _watermark(hub.conn, domain, rows)
    R.remember_source(hub.conn, domain, spec.upstream)
    log.info("%s %s：取到 %d 行（%s）", domain, symbol, len(rows), spec.name)
    return True


def _as_of(domain: str, rows: list[dict], need: Need) -> str:
    """点域这一份数据"算哪一天的"，写进覆盖记录。

    三个子域三种答案，因为它们回答的是三个不同的问题：

    | 域 | 用什么 | 为什么 |
    |---|---|---|
    | `quote` | 行里的 `trade_date` | **停牌股**上游给的还是停牌前那天。用 `need.end` 会谎报"今天已到手"，下次 `hit()` 放行，工具于是永远用着停牌前的价 |
    | `snapshot` | `need.end` | `mainop` 的 `as_of` 是**报告期末**（内容属性），拿它跟交易日比会永远"末端不足"，每次运行都重取；新鲜度该用"哪一天问的"表达 |
    | `scan` | `need.end` | 同 `snapshot`：横截面的"新鲜度"是"哪天问的"，而它的内容日期由 `fp` 里的 `date=` 钉死。用行里的 `date` 也行（行里就是那天），但那样"上游错给了一天"会静默改掉覆盖记录 |
    | `profile` | `need.end`（通常是 None） | 元数据没有日期概念，`None` 表示"取过就够，别再问" |
    """
    if domain == "quote" and rows:
        day = str(rows[0].get("trade_date") or "").strip()
        if day:
            return day
    if domain in ("snapshot", "scan") and need.end:
        return need.end
    if rows:                                  # snapshot 没有 need.end 时的兜底
        day = str(rows[0].get("as_of") or "").strip()
        if day:
            return day
    return need.end or ""


def _router(hub, throttle: Throttle) -> R.Router:
    """一个进程一个 `Router` —— `strikes` 必须跨股票累积。

    早先每次 `ensure()` 都新建一个，`strikes` 于是在每只股票之间被清空，
    `degrade_after=3`（连续失败 3 次降级）这条规则**从来没有触发过**：主源坏掉时，
    4000 只股票就是 4000 次"第 1/3 次失败"，一次也不会降级。
    现在优先拿 `Hub.router`（Hub 里缓存一份），拿不到才现拼。
    """
    r = getattr(hub, "router", None)
    if isinstance(r, R.Router):
        return r
    return R.Router(cfg=hub.cfg, throttle=throttle, conn=hub.conn)


def ensure_many(hub, domain: str, symbols: list[str], need: Need, *,
                force: bool = False, now: dt.datetime | None = None) -> Batch:
    """批量版。**不抛单只失败**——四个工具对"某一只失败"的取舍不一样。

    但必须显式消费 `Batch.failed`：非空且不调用 `raise_if_any()` 也不看它，
    `doctor` 会警告，收盘汇总里也必须打印失败只数（`CONTRACT` §3.2）。
    """
    out = Batch()
    for sym in symbols:
        try:
            out.ok[sym] = ensure(hub, domain, sym, need, force=force, now=now)
        except MarketOpenError:
            # 盘中要联网：这不是"这一只失败"，而是整批都不必再试。
            # 收进 failed 的话，4000 只股票就是 4000 条"需要联网"，第一条之后全是噪音，
            # 而调用方还得自己从失败清单里认出这是同一个原因。
            raise
        except Exception as e:                  # noqa: BLE001 - 故意收全，交给调用方
            log.warning("%s %s 失败：%s", domain, sym, e)
            out.failed[sym] = e
    return out


def ensure_bulk(hub, domain: str, symbols: list[str], need: Need, *,
                now: dt.datetime | None = None) -> Batch:
    """组级批量预取：把**整批**一次交给源（westock MCP 的 `data_kline` 40 只/请求）。

    为什么需要它（2026-11-15）：`ensure` 的语义是"单只股票的覆盖/补洞/加锁"，
    `ensure_many` 只是把这只循环 N 遍——`SourceSpec.batch=40` 声明的组级批量
    从未被用上。4304 只的粗筛池走逐只路径被压在 ~1 只/秒（还要吃东财断连与
    腾讯 WAF 的冷却），而原版 kline-screen-tool 走的就是 westock MCP 多码通道，
    同样的池子几分钟跑完。

    语义（与 `ensure_many` 的差异，都是**有意**的）：

    - 不做补洞重试、不做点数门槛判定、不加单只锁——就是"能取多少取多少，
      逐只独立事务落库并写 coverage"；
    - 已命中 coverage 的股票直接跳过（不浪费上游配额）；
    - **按「每只缺哪一段」分组取**（2026-11-15 修）：与 `_pull` 一致地走
      `missing_segments()`，所以"只缺最新一天"的票只请求那一天，不是整段 300 根。
      改之前这里直接拿 `need` 整段去取，日增量因此放大 300 倍
      （4304 行 → 1,291,200 行），顺带把 `router._expect` 的配额估算也放大 300 倍，
      一次批量就打穿 12000 行的桶。回归测试见 `tests/test_bulk.py`；
    - 返回 `Batch`：`ok` = 本次取到数据的（`Range`，与 `ensure` 同形状）；
      `failed` = 上游这次没给的。**`failed` 不是错误**——停牌/次新/限频都在
      里面，调用方应把剩余的再走一遍 `ensure_many` 拿正式的异常分类
      （`InsufficientDepth` / `HoleUnreachable` vs 真失败）；
    - 盘中闸照走：要联网时先过 `hub.check_online`（与 `_pull` 同一道闸）。

    适用域：`kline`（westock 批量）。其它域传进来也能跑，但只有支持多码的
    源才有收益——单码源（腾讯 HTTP）会被适配器拆成 N 次单只请求，与
    `ensure_many` 等价。
    """
    now = now or dt.datetime.now()
    conn = hub.conn
    throttle = getattr(hub, "throttle", None) or Throttle(hub.cfg)
    rtr = _router(hub, throttle)

    todo = [s for s in symbols
            if not C.hit(C.read(conn, domain, s, need.fp), need)]
    out = Batch()
    if not todo:
        return out

    check = getattr(hub, "check_online", None)
    if callable(check):
        check(domain, todo[0], need)                 # 盘中闸：一次，整批共用

    # ---- 按「每只缺哪一段」分组，而不是整批共用同一个 `need` ----
    #
    # 这是本函数与 `_pull`（单只路径）的语义差异所在，2026-11-15 补齐：
    # 原来直接 `rtr.fetch(domain, todo, need)`，于是「只缺今天一根」的票也按
    # `need.start..need.end`（300 根）请求。实测 4304 只的日增量因此要回
    # 129 万行，而实际只需要 4304 行 —— 放大 300 倍，且 `router._expect` 按
    # `need.points` 计配额，一次批量就把配额桶（12000 行）打穿。
    # 原版 kline-screen-tool 快的原因正是这条：每只只补「最后一根 + 1 天」。
    groups: dict[tuple[str | None, str | None], list[str]] = {}
    for sym in todo:
        cov = C.read(conn, domain, sym, need.fp)
        segs = C.missing_segments(cov, need, hub.cal, now)
        if not segs:
            # **覆盖已到末端、只是根数不够** → 直接判深度不足，不打上游。
            #
            # 这是次新股（上市才 40 个交易日）和长期停牌股。它们缺的是"上市之前
            # 的 K 线"，再取一遍 300 根，上游还是只给那 40 根，`n` 一点不变，
            # 下一轮照样不命中 —— 于是**每次运行都白打一遍**（实测 24 只 × 300 根）。
            # 更糟的是这类请求会把整批拖成一次大调用，上游偶发不回就整跑失败。
            #
            # 为什么不会误伤"被 GC 裁浅了、其实能补回来"的票：只有 `n < min_points`
            # 才会走到这里，而 `prune_kline` 的默认保留深度（`KLINE_KEEP_BARS=400`）
            # 远大于任何工具的 `min_points`（最严 250），裁完也仍然命中。
            if cov and cov.end and need.end and cov.end >= need.end:
                out.failed[sym] = InsufficientDepth(
                    f"{sym} 的 {domain} 只有 {cov.n} 根，达不到 {need.min_points} 根"
                    f"（次新股 / 长期停牌，上游给不出更多）",
                    domain=domain, symbol=sym, n=cov.n, need=need.min_points)
                continue
            # 其余情形（覆盖记录缺失 / 口径刚被换源作废）→ 整段重取，
            # 与 `ensure()` 的兜底（`if not segs:` 那段）保持一致。
            groups.setdefault((need.start, need.end), []).append(sym)
            continue
        for seg in segs:                             # 可能多段：末端延伸 + 到期缺口
            groups.setdefault(seg, []).append(sym)

    # ---- 小分组并批：**服务端按「每分钟请求数」限频，不是按行数** ----
    #
    # 缺口（`holes`）是按 symbol 记的，每只票缺的日期段各不相同，于是天然一个 symbol
    # 一组、一次请求。历史上被截断过的一批票（本次实测 117 只）就会变成 117 次
    # 连续的 1 只请求——结果是 westock 直接回"限频"，冷却 30s → 60s → 120s 递增，
    # 一次运行卡在重试里出不来（2026-11-15 真跑撞到）。
    #
    # 而一次 **40 只 × 300 根** 的请求（实测 24 只 1077 行）完全不会被限。
    # 所以当"小分组"多起来时，把这些票并进一个整段批次：行数多花（本地令牌桶按
    # 行限流，会自己等够配额），请求数从上百次降到个位数。一次并批把历史缺口全补上，
    # 之后就不会再出现。
    #
    # 阈值 8：正常运行只有零星几只缺口，不该为它们整段重取；只有"成片缺"才值得换。
    small = [k for k, v in groups.items() if len(v) <= 2]
    if len(small) > 8:
        merged: list[str] = []
        for k in small:
            merged.extend(groups.pop(k))
        whole = (need.start, need.end)
        merged.extend(groups.get(whole, []))
        groups[whole] = sorted(set(merged))
        log.info("%s 小分组 %d 个 → 并成 1 个整段批次（%d 只），省 %d 次请求",
                 domain, len(small), len(groups[whole]), len(small) - 1)

    used: str | None = None
    src_names: list[str] = []
    for (seg_start, seg_end), codes in groups.items():
        # `sub` 只用于**取数**：`points` 改成这一段真正要回的根数，配额估算才诚实。
        # 落库后的覆盖重算仍用原始 `need`（`merge()` 按库里的实际范围重数，不看 need.start）。
        #
        # 只有 `kline`/`valuation` 的 `points` 是"行数"，可以按段缩；
        # `scan`/`quote`/`snapshot` 的 `points` 是"整份多少行"（scan 一天 5000 行），
        # 缩了会让 `_expect` 少算三个数量级的配额 —— 那几个域保持原值。
        if domain in ("kline", "valuation"):
            sub = replace(need, start=seg_start, end=seg_end,
                          points=_span_bars(hub.cal, seg_start, seg_end, need))
        else:
            sub = replace(need, start=seg_start, end=seg_end)
        try:
            got, spec = _fetch_split(rtr, domain, codes, sub)
        except Exception as e:                       # noqa: BLE001 - 单组失败收进 failed
            # **不能让它抛出去**（2026-11-15 踩到）：`ensure_bulk` 的契约是
            # "能取多少取多少"，调用方（kline_screen 的 [4/6] 阶段）靠 `failed`
            # 分类，不接异常。上游一次偶发不回码就会让 4304 只的整跑直接中止——
            # 实测就这么失败过一次，而同一批重试立刻成功。
            log.warning("%s 批量取数失败（%d 只，%s..%s）：%s",
                        domain, len(codes), seg_start, seg_end, e)
            for s in codes:
                out.failed[s] = e
            continue
        used = used or spec.upstream
        if spec.name not in src_names:
            src_names.append(spec.name)
        for sym in codes:
            rows = got.rows.get(sym) or []
            if not rows:
                out.failed[sym] = EmptyResult(
                    f"批量预取未返回 {sym} 的 {domain} 数据（停牌/次新/限频均可能）",
                    domain=domain, symbol=sym)
                continue
            with db.tx(conn):
                _write_rows(conn, domain, sym, rows, spec.upstream)
                if domain in C.POINT_DOMAINS:
                    C.merge_point(conn, domain=domain, symbol=sym,
                                  as_of=_as_of(domain, rows, need), src=spec.upstream,
                                  fp=need.fp, n=len(rows))
                else:
                    C.merge(conn, cal=hub.cal, need=need, symbol=sym, src=spec.upstream,
                            fetched=(seg_start, seg_end))
                _watermark(conn, domain, rows)
            out.ok[sym] = _range(conn, domain, sym, need, fetched=True)

    if used:
        R.remember_source(conn, domain, used)
    # 日志里要打**源**的名字。注意别打 `spec.upstream`：`westock` 的上游数据商恰好
    # 也叫 `tencent`（`SourceSpec("westock","tencent","mcp",...)`），而 `src` 列记的是
    # 数据商、不是源——两者同名会让人以为"跑的是腾讯 HTTP 备源"，把整条改动看反。
    log.info("%s 批量预取：%d/%d 只落库（%d 组，源=%s）", domain, len(out.ok), len(todo),
             len(groups), src_names or "-")
    return out


def _span_bars(cal, start, end, need: Need) -> int:
    """这一段真正要回多少根 —— 给 `router._expect` 算配额，也给适配器算 `limit`。

    **不能"数不出来就退回 `need.points`"**（第一版就是那么写的，然后在本函数最主要的
    使用场景上失效）：日历末端不足时 `cal.between(start, end)` 返回空列表，而那恰好是
    "日历没更新、只补最后一两天"的常态——退回去就等于又按 300 根算配额，一次 4304 只的
    批量照样申请 129 万行，前面所有增量改动全部白做（实测被 `test_bulk` 抓出来）。

    所以：日历数不出来就按**周内工作日**估（非交易日上游本来也不返回数据，周末多估
    一两根无害），连日期都解析不了才退回 `need.points`。
    """
    if not start or not end or start > end:
        return int(need.points or 300)
    days = cal.between(start, end)
    if days:
        return max(1, len(days))
    try:
        d0 = dt.date.fromisoformat(start)
        d1 = dt.date.fromisoformat(end)
    except ValueError:
        return int(need.points or 300)
    n = sum(1 for i in range((d1 - d0).days + 1)
            if (d0 + dt.timedelta(days=i)).weekday() < 5)
    return max(1, n)


def _fetch_split(rtr: R.Router, domain: str, codes: list[str], need: Need,
                 *, depth: int = 0):
    """整批取一次；**大面积丢码**时把缺的拆半再取（最多 2 层）。

    为什么不是"整批重试 + 冷却"（`router._fetch_with_retry` 的做法）：服务端限频的
    表现不是整批失败，而是「60 只只回 10 只」——此时把**同一批**再发一次，往往还是
    回 10 只；而拆成 30/15 只一批，一次就全回（原版 `_kline_fetch` 的实测结论）。

    为什么只对"大面积"拆：少数几只没回多半是停牌/次新，再问一次还是空。
    不设比例阈值的话，3000 只停牌股会变成 3000 次重取，比不重试更糟。
    """
    got, spec = rtr.fetch(domain, codes, need)
    missing = [c for c in codes if c not in got.rows]
    if not missing or depth >= 2 or len(missing) * 2 < len(codes):
        return got, spec
    half = max(1, len(missing) // 2)
    for i in range(0, len(missing), half):
        part, _ = _fetch_split(rtr, domain, missing[i:i + half], need, depth=depth + 1)
        got.extend(part)
    return got, spec


# ---------------------------------------------------------------- 横截面快捷入口

def ensure_scan(hub, day: str, *, force: bool = False,
                now: dt.datetime | None = None) -> list[dict]:
    """保证"某天的全市场横截面"在库里，返回那一天的行。

    这是 `scan` 域的唯一入口。四个工具的粗筛都走这里，**不要**自己拼
    `ensure("scan", ...)`——`symbol` 必须恒为 `SCAN_SYMBOL`、`need` 的 `fp` 必须带日期，
    这两条漏一条就会静默返回错误的那一天（或空列表），而不是报错。

    取不到就抛（`ensure` 的语义）：某天的横截面缺失时，"今天没有合格股"和
    "接口挂了"必须能被区分开，否则工具会照常产出一份空名单。

    `force=True` 是**真的重取**：先删掉那天的覆盖记录，让它必然 miss。
    不能只把 `force` 透传给 `ensure()`——那个 `force` 的语义是"清掉重试标记后
    **再判一次命中**"，命中就直接返回库里的旧数据，对"上游补发了、我要刷新"
    这个场景等于什么都没做（第 7 步实测踩到，症状是 `force=True` 之后上游调用数 +0）。
    """
    need = want.scan(day)
    if force:
        with db.tx(hub.conn):
            hub.conn.execute(
                "DELETE FROM coverage WHERE domain='scan' AND symbol=? AND fp=?",
                (SCAN_SYMBOL, need.fp))
    ensure(hub, "scan", SCAN_SYMBOL, need, now=now)
    return read(hub, "scan", SCAN_SYMBOL, need).rows


def ensure_scan_many(hub, days, *, force: bool = False,
                     now: dt.datetime | None = None) -> dict[str, list[dict]]:
    """多天横截面。逐日 `ensure`，任一天失败就抛。

    **不做"跳过失败的那天"**：粗筛池是"这 K+1 天里涨幅达标过的股票并集"，
    少一天就少一批候选，而少掉的那批不会以任何形式出现在结果里——
    名单看起来完全正常，只是漏了。宁可这一次跑不出来。
    """
    out: dict[str, list[dict]] = {}
    for day in days:
        out[day] = ensure_scan(hub, day, force=force, now=now)
    return out


def prune_scan(hub, keep_days: int | None = None,
               today: str | None = None) -> int:
    """删掉 `scan` 里早于 `keep_days` 的横截面，返回删了多少行。

    **只有这张表需要例行清理**：其余域都是"按股票、滚动 300 根"，天然有界；
    `scan` 是"按日、全市场"，不清理就是每年 +130 万行 / +130MB。

    保留期从 `meta['scan_retention_days']` 取，缺省用 `config.SCAN_RETENTION_DAYS`
    （30 天）。`today` 用 `hub.target` 而不是 `date.today()`——补跑历史时
    "今天是哪天"是相对的，用真实今天会把补跑的窗口一起清掉。

    失败不抛：清理是运维动作，不该让一次选股因为删不掉旧行而失败。
    """
    keep = keep_days
    if keep is None:
        keep = int(db.meta_get(hub.conn, "scan_retention_days") or SCAN_RETENTION_DAYS)
    if keep <= 0:
        return 0
    base = today or hub.target or dt.date.today().isoformat()
    cutoff = (dt.date.fromisoformat(base) - dt.timedelta(days=keep)).isoformat()
    try:
        with db.tx(hub.conn):
            cur = hub.conn.execute("DELETE FROM scan WHERE date < ?", (cutoff,))
            # 覆盖记录跟着删，否则下次 `hit()` 会以为那天还在库里
            hub.conn.execute(
                "DELETE FROM coverage WHERE domain='scan' AND end_date < ?", (cutoff,))
        n = cur.rowcount or 0
    except Exception as e:                   # noqa: BLE001 - 清理失败不该阻断运行
        log.warning("scan 清理失败（不影响本次结果）：%s", e)
        return 0
    if n:
        log.info("scan 清理：删掉 %s 之前的 %d 行", cutoff, n)
    return n


def prune_kline(hub, keep_bars: int | None = None,
                today: str | None = None) -> int:
    """每只股票只留最近 `keep_bars` 根日 K，返回删了多少行。

    **为什么需要它（2026-11-15）**：`kline` 是唯一一张"股票 × 交易日"双维度的表，
    而全仓**没有任何地方删过它**（`grep "DELETE FROM kline"` 零命中）。
    `coverage.merge()` 的注释写着"K 线是会滚动删除的"，但那个"会"从来没兑现——
    实测已经是 122 万行 / 104 MB（占库 68%），再跑一年就是 +140 MB。
    共享层的初衷是"按需取、不要囤"，没有裁剪的"按需"迟早变成"全量囤"。

    **按全市场统一日期切，不按每只票各自的第 N 根**：同一只票停牌会让"第 N 根"
    对应的日期跳变，逐票计算要 5000 次查询、而且 `DELETE` 会退化成逐行删。
    统一日期只有一条 `DELETE ... WHERE date < ?`，一次扫表就完事（这操作一天最多一次，
    而且 `date` 上现在**故意没有索引**——常驻索引的代价是每次写入都要维护它）。

    **覆盖记录跟着改**：删掉旧 bar 后 `cov.start` 会指向一个已经不存在的日期。
    不处理的话，`missing_segments` 的"到期缺口"分支（读 `hole_detail`）还会去补
    那些刚被删掉的日期，天天白打上游。所以同一事务里把 `start_date` / `n` 按实际
    重数，并把 `hole_detail` 清空——真的缺口会在下一次 `merge()` 里被 `recount_holes`
    重新发现，不需要我们在这里维护。

    `keep_bars` 缺省用 `config.KLINE_KEEP_BARS`（400）。`today` 用 `hub.target`：
    补跑历史时"保留最近 N 根"是相对那个日期的，用真实今天会误删补跑窗口。

    失败不抛，理由同 `prune_scan`。
    """
    keep = keep_bars if keep_bars is not None else KLINE_KEEP_BARS
    if keep <= 0:
        return 0
    cal = getattr(hub, "cal", None)
    if cal is None:
        return 0
    base = today or hub.target or cal.end
    days = [d for d in cal.days if not base or d <= base]
    if len(days) <= keep:
        return 0                               # 还没有攒够 keep 根，不用裁
    cutoff = days[-keep]
    try:
        with db.tx(hub.conn):
            cur = hub.conn.execute("DELETE FROM kline WHERE date < ?", (cutoff,))
            hub.conn.execute(
                "UPDATE coverage SET "
                "  start_date = (SELECT MIN(date) FROM kline k WHERE k.symbol = coverage.symbol),"
                "  n          = (SELECT COUNT(*)   FROM kline k WHERE k.symbol = coverage.symbol),"
                "  holes      = 0, hole_detail = '[]' "
                "WHERE domain = 'kline' AND start_date IS NOT NULL AND start_date < ?",
                (cutoff,))
        n = cur.rowcount or 0
    except Exception as e:                   # noqa: BLE001 - 清理失败不该阻断运行
        log.warning("kline 清理失败（不影响本次结果）：%s", e)
        return 0
    if n:
        log.info("kline 清理：删掉 %s 之前的 %d 行（每只保留最近 %d 根）", cutoff, n, keep)
    return n


# ---------------------------------------------------------------- read

# 域 → (表, 日期列)。`profile` 写的是元数据表 `stock`，没有日期列。
_TABLES: dict[str, tuple[str, str | None]] = {
    "kline": ("kline", "date"),
    "finance": ("finance", "period"),
    "dividend": ("dividend", "ex_date"),
    "valuation": ("valuation", "trade_date"),
    "quote": ("quote", "trade_date"),
    "snapshot": ("snapshot", "as_of"),
    "scan": ("scan", "date"),
    "profile": ("stock", None),
}

# 点域里**仍要按日期精确取一份**的那些。其余点域（quote / profile / snapshot）
# 是"当期一份"，不带日期条件。
#
# `scan` 必须在这里——但它在 `read()` 里走的是单独一条分支（见那里），
# 因为它的表是"一份 = 多行"，连 `symbol=?` 都不能用。这个集合只用于
# "知道它是按日分片的"，别的地方别再拿它当查询条件。
_POINT_BY_DATE = frozenset({"scan"})


def read(hub, domain: str, symbol: str, need: Need) -> Range:
    """**只读**：不满足条件也照样返回现状，绝不因为参数写大了就去打上游。"""
    conn = hub.conn
    try:
        table, dcol = _TABLES[domain]
    except KeyError:
        raise NotImplementedError(f"域 {domain} 的读法还没定义") from None

    if domain == "scan":
        # ⚠️ `scan` 是**唯一**一个"一份 = 多行"的域：它的 `symbol` 列存的是真实代码
        # （一天 5000 行，每行一只票），`*` 只用来定位覆盖记录。所以这里**不能**
        # 按 `symbol=?` 过滤——按 `*` 查会一行都查不到，而症状是"拿到空列表"，
        # 不是报错。写这条注释是因为第一版就这么错了，测试里 11 条断言同时变红。
        sql = f"SELECT * FROM {table}"
        args = []
        if need.end:
            sql += f" WHERE {dcol}=?"
            args.append(need.end)
        sql += f" ORDER BY {dcol}, symbol"
    else:
        sql = f"SELECT * FROM {table} WHERE symbol=?"
        args = [symbol]

        if domain in C.POINT_DOMAINS:
            # 一股一份：不按日期过滤（`need.end` 是"哪天的快照"，不是"取哪段区间"）。
            # `snapshot` 还多一层子域，不加这个条件会把 mainop 与 consensus 混成一行。
            if domain == "snapshot" and need.extra.get("snapshot_domain"):
                sql += " AND domain=?"
                args.append(need.extra["snapshot_domain"])
        else:
            if need.start:
                sql += f" AND {dcol} >= ?"
                args.append(need.start)
            if need.end:
                sql += f" AND {dcol} <= ?"
                args.append(need.end)

        if dcol:
            sql += f" ORDER BY {dcol}"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    if domain == "kline" and need.points and len(rows) > need.points:
        rows = rows[-need.points:]              # 只要最近 N 根

    cov = C.read(conn, domain, symbol, need.fp)
    first, last = (rows[0].get(dcol), rows[-1].get(dcol)) if (rows and dcol) else (None, None)
    return Range(domain=domain, symbol=symbol, rows=rows, n=len(rows),
                 start=first, end=last,
                 permanent_holes=[h for h in (cov.detail if cov else []) if h.permanent],
                 fetched=False)


_STATEMENTS = ("income", "balance", "cashflow")


def finance_full(hub, symbol: str, need: Need) -> dict[str, list[dict]]:
    """**只读**：把 `finance.raw_json` 拆回三张表的原文，按报告期返回。

    返回 `{"income": [...], "balance": [...], "cashflow": [...]}`，
    每个列表里的元素是上游那一期的原始行（字段名是上游列名，没改过），
    且**每行都带 `period`**——上游原本把它放在外层（`data_finance` 的
    `REPORT_DATE`），这里补齐，否则调用方拿到的 60 行分不清哪行是哪期。

    为什么需要这个入口：`finance` 表只存了 15 个结构列，而 quality-screen /
    cycle-elastic 的计算层要读 54~80 个字段（商誉、存货、货币资金、有息负债、
    经营现金流、研发、ROIC……）。那些字段全在 `raw_json` 里，但让每个上层工具
    自己 `json.loads` + 合并 + 补 `period`，等于把同一段解析逻辑抄四遍——
    抄错一次的症状是"某个指标恒为 None"，不报错，只是结论悄悄变了。

    保留期数由 `RAW_JSON_KEEP` 决定。某期 `raw_json` 为 NULL（历史遗留的老行，
    或那次上游只回了部分表）时**静默跳过**——调用方按 `period` 自己对齐，
    缺表就是缺表，这里不猜。

    和 `read()` 一样**绝不打上游**：库里是什么就给什么。要补齐得先 `ensure()`。
    """
    rng = read(hub, "finance", symbol, need)
    out: dict[str, list[dict]] = {t: [] for t in _STATEMENTS}
    seen: dict[str, dict] = {}
    for row in rng.rows:
        raw = row.get("raw_json")
        if not raw:
            continue
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            log.warning("%s %s 的 raw_json 解析失败，跳过该期", symbol, row.get("period"))
            continue
        period = row.get("period")
        for table in _STATEMENTS:
            part = obj.get(table)
            if not isinstance(part, dict):
                continue
            rec = seen.setdefault(f"{table}|{period}", dict(part))
            rec.setdefault("period", period)
            # 同一期可能出现两次（老行 + 新行），后到的补齐缺失字段，不覆盖已有的数
            rec.update({k: v for k, v in part.items() if rec.get(k) in (None, "")})
    for key, rec in seen.items():
        out[key.split("|", 1)[0]].append(rec)
    for table in _STATEMENTS:
        out[table].sort(key=lambda r: r.get("period") or "")
    return out


def gbalance_full(hub, symbol: str, need: Need) -> dict:
    """**只读**：东财资产负债表补丁（商誉 / 短期借款），按报告期返回。

    返回 `{"periods": [...], "has_balance": bool}`，`periods` 最近一期在前。
    每期是东财原始行，日期已归一成 `YYYY-MM-DD`（`REPORT_DATE`）。

    ## `None` 怎么读——这是本函数存在的唯一理由

    weStock 的 `data_finance` 根本不返回 `GoodWill` / `ShortTermLoan`，而 quality-screen
    的「商誉/净资产」「现金短债比」两项检查正好要它们。原版只能把商誉按 0 处理，
    于是"商誉风险"这一项对所有公司恒为通过。

    东财这边 `None` 有两种意思，**必须靠 `TOTAL_ASSETS` 分开**：

    - `has_balance=True` 且科目为 `None` → 该公司**确实**没有这个科目 → 读作 **0**。
      茅台就没有商誉、也没有短期借款，报成 unknown 会让它永远卡在一个假警报上。
    - `has_balance=False` → 连资产负债表都没拿到 → 读作 **unknown**，绝不当 0。
      当 0 的后果是"商誉/净资产 = 0%"被判定为安全——一个查不出来的风险被报成通过。

    **调用方必须自己按上面两条解释 `None`**，不要把 `has_balance` 丢掉。
    这里不做转换，因为"0"和"unknown"在下游是两种不同的展示与评分，替调用方拍板
    会让它对不上自己的其它字段。
    """
    rng = read(hub, "snapshot", symbol, need)
    if not rng.rows:
        return {"periods": [], "has_balance": False}
    raw = rng.rows[0].get("payload")
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        log.warning("%s 的 gbalance payload 解析失败", symbol)
        return {"periods": [], "has_balance": False}
    if not isinstance(obj, dict):
        return {"periods": [], "has_balance": False}
    return {"periods": obj.get("periods") or [],
            "has_balance": bool(obj.get("has_balance"))}


def gcashflow_full(hub, symbol: str, need: Need) -> dict:
    """**只读**：东财现金流量表补丁（capex），按报告期返回。

    返回 `{"periods": [...]}`，最近一期在前，日期已归一成 `YYYY-MM-DD`。

    `CONSTRUCT_LONG_ASSET` 是**年初至今累计**：同一年的 03-31 / 06-30 / 09-30 / 12-31
    递增。算年度 capex 只能用 `12-31` 年报行——混用会把 capex 低估、FCF 虚高。
    这里不做筛选（那属于口径，不属于取数），只保证日期和排序一致。

    **不打上游**，与其它 `*_full()` 一致。
    """
    rng = read(hub, "snapshot", symbol, need)
    if not rng.rows:
        return {"periods": []}
    raw = rng.rows[0].get("payload")
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        log.warning("%s 的 gcashflow payload 解析失败", symbol)
        return {"periods": []}
    if not isinstance(obj, dict):
        return {"periods": []}
    return {"periods": obj.get("periods") or []}


# ---------------------------------------------------------------- 写库

_WRITERS = {}          # 在文件末尾填，避免函数定义顺序把这张表挤到中间

FIN_COLS = (
    "OperatingRevenue", "OperatingRevenue_Q", "OperatingRevenueTTM",
    "OperatingCost", "OperatingCost_Q", "OperatingCostTTM",
    "OperatingProfit", "OperatingProfit_Q", "OperatingProfitTTM",
    "NPParentCompanyOwners", "NPParentCompanyOwners_Q", "NPParentCompanyOwnersTTM",
    "TotalFixedAsset", "CIP", "ContractLiability",
)
"""`finance` 表里"由上游填"的 15 个列。`unit`/`raw_json`/`src`/`updated_at` 由本模块补。"""

RAW_JSON_KEEP = 43
"""`finance.raw_json` 保留最近多少期（43 = 上游能给的全部）。

**这个数由四个工具里需求最长的那个定，取 43**：

- quality-screen 要 20 期做 5 年趋势（`config.TREND_QUARTERS`）——43 期已覆盖；
- cycle-elastic 要 **43 期**：判断③ 产能周转率分位要 40 个样本，而周转率是
  「最近 4 季营收之和 ÷ 当季(固资+在建)」，**前 3 期凑不满 4 季滚动和**，
  所以 40 + 3 = 43 是硬下限，一期都不能少。少一期，「10 年分位」就变成
  「9.75 年分位」，且窗口最左端会缺一个真实的历史低点/高点。

**43 不是选出来的，是上游的上限**：weStock `data_finance` 无论要 44 还是 60，
都只回 43 期（2026-11-14 实测，`sh601899` 最早 2016-03-31）。所以：
- 设 44 → 拿不到，只是让 `HISTORY_QUARTERS=44` 的请求值看起来对得上；
- 设 43 → 与事实上限一致，`_trim_raw_json` 不会误删任何一期。

原先写 20（当时只有 quality-screen 一个消费方）。换 20 的代价是 cycle 的判断③
静默降级：分位窗口从 10 年缩到 4 年，而报告标题仍写「10 年分位」——
**症状是结论看起来正常、只是位置标签偏了**，这类 bug 比报错难查得多。

磁盘代价（实测单期 4.2KB）：500 只 × 43 期 ≈ 88MB，不构成问题。
注意这只影响**之后**写入的行：已被置 NULL 的旧行要 `force` 重取才补得回来。
"""


def _write_rows(conn, domain: str, symbol: str, rows: list[dict], src: str) -> None:
    """按域分发。`rows` 的键 = 表列名（适配器保证），这里不做字段改名。"""
    fn = _WRITERS.get(domain)
    if fn is None:
        raise NotImplementedError(f"域 {domain} 还没有写入实现（{sorted(_WRITERS)} 有）")
    fn(conn, symbol, rows, src)


def _write_kline(conn, symbol: str, rows: list[dict], src: str) -> None:
    """K 线落库。**换源不许覆盖已有 bar**（`SOURCES` §4.1）：

    `ON CONFLICT ... WHERE kline.src = excluded.src` —— 同源可以纠正（上游改数），
    异源只补空缺。不加这个条件，一次应急换源就会把已有 300 根整段换成另一家的分位。

    `amount` 的 `or 0.0` 是**有意的**：`schema.sql` 里它是 `NOT NULL DEFAULT 0`，而
    `tencent` 那条通道根本没有成交额字段（无鉴权备源，见 `sources/tencent.py`）。
    于是 `amount = 0` 有两种含义——上游给了 0 / 这行来自 tencent；靠 `src` 区分。
    不拿"量×价"补：那个数看着像真值，会被下游当成交额用。
    """
    conn.executemany(
        "INSERT INTO kline(symbol,date,open,high,low,close,volume,amount,src) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
        "volume=excluded.volume, amount=excluded.amount, src=excluded.src "
        "WHERE kline.src = excluded.src",
        [(symbol, r["date"], r.get("open"), r.get("high"), r.get("low"), r.get("close"),
          r.get("volume") or 0.0, r.get("amount") or 0.0, src) for r in rows])


def _write_finance(conn, symbol: str, rows: list[dict], src: str) -> None:
    """财报：append-only（只增期、不删期），上游更正 → 覆盖 + 留痕。

    两条容易写错、且**错了不报错**的规则：

    1. **新值是 `None` 时绝不覆盖旧值。** 上游某次只回了利润表 + 现金流量表，合并出来的
       行里资产负债表那几列全是 `None`；照写就会把已有的 `TotalFixedAsset` / `CIP`
       静默抹成 NULL，而 cycle 的"在建工程 / 固定资产"拐点会因此整段消失，报告照出。
    2. **相对差 > 1e-6 才算"更正"。** 否则每次重取都会往 `finance_revision` 里灌一堆
       "差 0.0000001"的噪音，真正要紧的那次更正反而被淹掉。
    """
    stamp = _now()
    old = {r["period"]: dict(r) for r in conn.execute(
        f"SELECT period,{','.join(FIN_COLS)},raw_json FROM finance WHERE symbol=?", (symbol,))}

    for row in rows:
        period = row.get("period")
        if not period:
            continue
        prev = old.get(period)
        given = [c for c in FIN_COLS if row.get(c) is not None]

        if prev is None:
            conn.execute(
                f"INSERT INTO finance(symbol,period,{','.join(FIN_COLS)},unit,raw_json,src,"
                f"updated_at) VALUES(?,?,{','.join('?' * len(FIN_COLS))},?,?,?,?)",
                (symbol, period, *[row.get(c) for c in FIN_COLS], row.get("unit") or "CNY",
                 row.get("raw_json"), src, stamp))
            continue

        changed = [(c, prev.get(c), row[c]) for c in given
                   if prev.get(c) is not None and _rel_diff(prev[c], row[c]) > 1e-6]

        # **数值没变也要看 raw_json。** 这是 2026-11-14 补的：
        # 原先 `if not changed: continue` 只看 15 个结构列，于是"库里有这期、数值也对、
        # 但 raw_json 是 NULL"的行永远补不上——`force=True` 重取一遍也一样，
        # 因为数值确实没变，代码在那一步就 return 了。症状是 `finance_full()` 只返回
        # 最近 2 期（那 2 期是早期插入的），而 `finance` 表看起来 20 期齐全。
        # 实测：茅台 20 期里 18 期 raw_json 为 NULL，重取后仍是 18 期。
        new_raw = row.get("raw_json")
        need_raw = bool(new_raw) and new_raw != prev.get("raw_json")

        if not changed and not need_raw:
            continue                              # 真的一模一样就不写，省一次 UPDATE 与一次刷盘

        if changed:
            sets = ", ".join(f"{c}=?" for c in given)
            conn.execute(
                f"UPDATE finance SET {sets}, unit=?, raw_json=COALESCE(?,raw_json), src=?, "
                "updated_at=? WHERE symbol=? AND period=?",
                (*[row[c] for c in given], row.get("unit") or "CNY", new_raw,
                 src, stamp, symbol, period))
            conn.executemany(
                "INSERT OR REPLACE INTO finance_revision(symbol,period,field,old_value,"
                "new_value,detected_at,src) VALUES(?,?,?,?,?,?,?)",
                [(symbol, period, c, _sv(a), _sv(b), stamp, src) for c, a, b in changed])
            log.info("%s %s 的财报被上游更正 %d 个字段：%s", symbol, period, len(changed),
                     ",".join(c for c, _, _ in changed))
        else:
            # 只补 raw_json，不碰 `updated_at` 之外的任何东西——数值一个字没变，
            # 这不是"财报被更正"，不该在 `finance_revision` 里留痕。
            conn.execute("UPDATE finance SET raw_json=? WHERE symbol=? AND period=?",
                         (new_raw, symbol, period))

    _trim_raw_json(conn, symbol)


def _trim_raw_json(conn, symbol: str) -> None:
    """`raw_json` 保留最近 `RAW_JSON_KEEP` 期（默认 20），更早的置 NULL。

    **这是 2026-11-14 改的，原先是"只留最近 2 期"。** 改动的原因不是省心，
    是 quality-screen 这类工具**必须**从 `raw_json` 读全字段：

    `finance` 表只有 15 个结构列（营收/成本/营业利润/净利润的各口径 + 固定资产/
    在建工程/合同负债），而 quality-screen 的计算层读 **54 个财务字段**——商誉、
    存货、货币资金、有息负债、经营现金流、研发费用、速动比率、ROIC……缺 39 个。
    这些字段并没有丢：`_merge_statements` 把上游三张表的原文整份塞进了 `raw_json`
    （实测 income 58 / balance 52 / cashflow 22 个字段）。原先的问题只是**早于最近
    2 期的全被清掉了**，于是"最近 2 期能算、更早的算不了"，同比与趋势全废。

    为什么敢留 20 期：实测单期 `raw_json` 约 4.6KB，20 期 = 90KB/只。
    `SCHEMA` §3 当初算的"省 5GB+"是按 43 期 × 5000 只（全市场）估的；
    四个工具的真实用法是"只查几十只"（先由 kline-screen 选出候选，再逐只出报告），
    500 只 × 90KB ≈ 45MB，不构成问题。

    要更长历史（比如 43 期做十年趋势）改 `RAW_JSON_KEEP` 即可——它是个常量，
    不是散落的魔法数字。注意这只会影响**之后**写入的行：已经被置 NULL 的旧行
    需要 `force` 重取才会补回来。
    """
    keep = [r[0] for r in conn.execute(
        "SELECT period FROM finance WHERE symbol=? ORDER BY period DESC LIMIT ?",
        (symbol, RAW_JSON_KEEP))]
    if keep:
        conn.execute("UPDATE finance SET raw_json=NULL WHERE symbol=? AND period<?",
                     (symbol, min(keep)))


def _write_dividend(conn, symbol: str, rows: list[dict], src: str) -> None:
    """分红：**真 append-only**，已有的行一条都不改。

    除权日是既成事实——两家源不会给出两个日期，也不该出现"上游把 2020 年的除权日改了"
    这种事。真出现了，改它比留它更危险：复权因子的历史会跟着变，而所有回测结论都建立在
    那个历史之上。所以只在数值不一致时**打一条 warning**，不动库。
    """
    stamp = _now()
    for r in rows:
        ex = r.get("ex_date")
        if not ex:
            continue
        cur = conn.execute(
            "SELECT cash_per_share,bonus_per_share,transfer_per_share FROM dividend "
            "WHERE symbol=? AND ex_date=?", (symbol, ex)).fetchone()
        if cur is None:
            conn.execute(
                "INSERT INTO dividend(symbol,ex_date,cash_per_share,bonus_per_share,"
                "transfer_per_share,rights_per_share,rights_price,plan_text,src,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (symbol, ex, r.get("cash_per_share") or 0.0, r.get("bonus_per_share") or 0.0,
                 r.get("transfer_per_share") or 0.0, r.get("rights_per_share") or 0.0,
                 r.get("rights_price"), r.get("plan_text"), src, stamp))
            continue
        drift = max(_rel_diff(cur["cash_per_share"], r.get("cash_per_share") or 0.0),
                    _rel_diff(cur["bonus_per_share"], r.get("bonus_per_share") or 0.0),
                    _rel_diff(cur["transfer_per_share"], r.get("transfer_per_share") or 0.0))
        if drift > 1e-6:
            log.warning("%s %s 的分红与库里不一致（库里 cash=%.4f/送%.4f/转%.4f，"
                        "上游 cash=%.4f/送%.4f/转%.4f）。按 append-only 保留库里的值",
                        symbol, ex, cur["cash_per_share"], cur["bonus_per_share"],
                        cur["transfer_per_share"], r.get("cash_per_share") or 0.0,
                        r.get("bonus_per_share") or 0.0, r.get("transfer_per_share") or 0.0)


def _write_scan(conn, symbol: str, rows: list[dict], src: str) -> None:
    """横截面落库：**整天整份替换**，不是逐行 upsert。

    某一天的"全市场"是一个原子事实。上游这次只回了 5000 只、上次 5026 只，
    逐行 upsert 会把上一次多出来的 26 只留在库里，于是粗筛池凭空多出 26 只
    当天根本没成交的股票——而它们每一只都会往下走到 `find_start`，
    最后表现为"名单里混进了不该有的票"，不报任何错。

    `symbol` 参数（`"*"`）不用，行里自带真实代码；这里保留它是因为
    `_write_rows()` 的分发签名对所有域统一。
    """
    stamp = _now()
    days = sorted({str(r.get("date") or "") for r in rows} - {""})
    if not days:
        return
    conn.executemany("DELETE FROM scan WHERE date=?", [(d,) for d in days])
    conn.executemany(
        "INSERT INTO scan(date,symbol,name,close,pct_chg,src,updated_at) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(date,symbol) DO UPDATE SET name=excluded.name, "
        "close=excluded.close, pct_chg=excluded.pct_chg, src=excluded.src, "
        "updated_at=excluded.updated_at",
        [(r["date"], r["symbol"], r.get("name"), r.get("close"), r.get("pct_chg"),
          src, stamp) for r in rows if r.get("date") and r.get("symbol")])


def _write_quote(conn, symbol: str, rows: list[dict], src: str) -> None:
    """行情快照：一股一行，**整行覆盖**。快照没有"历史值"概念，覆盖即正确。"""
    stamp = _now()
    for r in rows:
        conn.execute(
            "INSERT INTO quote(symbol,trade_date,price,prev_close,pct_chg,volume,amount,"
            "turnover,float_shares,total_shares,total_mv,circ_mv,pe_ttm,pb,src,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "trade_date=excluded.trade_date, price=excluded.price, "
            "prev_close=excluded.prev_close, pct_chg=excluded.pct_chg, "
            "volume=excluded.volume, amount=excluded.amount, turnover=excluded.turnover, "
            "float_shares=excluded.float_shares, total_shares=excluded.total_shares, "
            "total_mv=excluded.total_mv, circ_mv=excluded.circ_mv, "
            "pe_ttm=excluded.pe_ttm, pb=excluded.pb, src=excluded.src, "
            "updated_at=excluded.updated_at",
            (symbol, r.get("trade_date") or "", r.get("price"), r.get("prev_close"),
             r.get("pct_chg"), r.get("volume"), r.get("amount"), r.get("turnover"),
             r.get("float_shares"), r.get("total_shares"), r.get("total_mv"),
             r.get("circ_mv"), r.get("pe_ttm"), r.get("pb"), src, stamp))


def _write_profile(conn, symbol: str, rows: list[dict], src: str) -> None:
    """元数据写 `stock` 表。几处讲究：

    - `industry` / `sector` / `business` 都用 `COALESCE(excluded.x, stock.x)`：
      这次上游没给的字段，不能把上一次的值抹成 NULL。后果不只是显示变空——
      quality 靠 `industry` 判"是不是同一个行业"、靠 `business` 渲染报告的
      「生意本身」一节，抹一次就等于这些结论全部降级。上游偶发少给一个字段
      是常事，这种"降级"完全不可见，所以在这里挡住；
    - **不碰 `last_access_at`**：那是 GC 的存活标记，被"刷新元数据"重置等于永不回收。
    """
    stamp = _now()
    for r in rows:
        conn.execute(
            "INSERT INTO stock(symbol,name,exch,setcode,industry,sector,business,"
            "is_financial,list_date,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "name=excluded.name, exch=excluded.exch, setcode=excluded.setcode, "
            "industry=COALESCE(excluded.industry, stock.industry), "
            "sector=COALESCE(excluded.sector, stock.sector), "
            "business=COALESCE(excluded.business, stock.business), "
            "is_financial=excluded.is_financial, "
            "list_date=COALESCE(excluded.list_date, stock.list_date), "
            "updated_at=excluded.updated_at",
            (symbol, r.get("name") or symbol, r.get("exch") or "SH",
             int(r.get("setcode") or 0), r.get("industry"), r.get("sector"),
             r.get("business"),
             int(r.get("is_financial") or 0), r.get("list_date"), stamp))


def _write_snapshot(conn, symbol: str, rows: list[dict], src: str) -> None:
    """`mainop` / `consensus` / `rating`：整份 JSON 覆盖，字段随上游变也不怕。"""
    stamp = _now()
    for r in rows:
        domain = r.get("domain")
        if not domain:
            continue
        conn.execute(
            "INSERT INTO snapshot(symbol,domain,as_of,payload,src,updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(symbol,domain) DO UPDATE SET as_of=excluded.as_of, "
            "payload=excluded.payload, src=excluded.src, updated_at=excluded.updated_at",
            (symbol, domain, r.get("as_of"), r.get("payload") or "{}", src, stamp))


def _write_valuation(conn, symbol: str, rows: list[dict], src: str) -> None:
    """估值周频序列。**允许整段覆盖**：`pb_mrq` 的分母随新财报重算，历史值不是永不变。"""
    conn.executemany(
        "INSERT INTO valuation(symbol,trade_date,pe_ttm,pb_mrq,ps_ttm,total_mv,circ_mv,src) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,trade_date) DO UPDATE SET "
        "pe_ttm=excluded.pe_ttm, pb_mrq=excluded.pb_mrq, ps_ttm=excluded.ps_ttm, "
        "total_mv=excluded.total_mv, circ_mv=excluded.circ_mv, src=excluded.src",
        [(symbol, r["trade_date"], r.get("pe_ttm"), r.get("pb_mrq"), r.get("ps_ttm"),
          r.get("total_mv"), r.get("circ_mv"), src) for r in rows if r.get("trade_date")])


_WRITERS.update({
    "kline": _write_kline, "finance": _write_finance, "dividend": _write_dividend,
    "quote": _write_quote, "profile": _write_profile, "snapshot": _write_snapshot,
    "valuation": _write_valuation, "scan": _write_scan,
})


def _watermark(conn, domain: str, rows: list[dict]) -> None:
    """记"上次取到哪一天"。漏跑自愈用，**不从 MAX(kline.date) 反推**（`SCHEMA` §7）。"""
    last = max((r.get("date") or r.get("period") or r.get("trade_date")
                or r.get("ex_date") or r.get("as_of") or "") for r in rows)
    if not last:
        return
    conn.execute(
        "INSERT INTO watermark(domain,scope,last_ok_date,updated_at) VALUES(?,'',?,?) "
        "ON CONFLICT(domain,scope) DO UPDATE SET "
        "last_ok_date=excluded.last_ok_date, updated_at=excluded.updated_at "
        "WHERE excluded.last_ok_date > COALESCE(watermark.last_ok_date,'')",
        (domain, last, _now()))


# ---------------------------------------------------------------- 小工具

def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _rel_diff(a, b) -> float:
    """相对差。两个都不是数时按"相等/不等"退化成 0/1。"""
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return 0.0 if a == b else 1.0
    d = abs(fb - fa)
    if d == 0.0:
        return 0.0
    base = max(abs(fa), abs(fb))
    return d / base if base else 1.0


def _sv(v) -> str:
    return "" if v is None else str(v)


# ---------------------------------------------------------------- 杂

def _range(conn, domain: str, symbol: str, need: Need, *, fetched: bool) -> Range:
    r = read(_HubLike(conn), domain, symbol, need)
    return replace(r, fetched=fetched)


class _HubLike:
    """`read()` 只需要 `.conn`。给它一个最小的壳，省得为了一次读再拼一个 Hub。"""

    def __init__(self, conn) -> None:
        self.conn = conn


def _touch(conn, symbol: str) -> None:
    try:
        C.touch_access(conn, symbol)
    except Exception:                        # noqa: BLE001 - 记账失败不该阻断运行
        pass
