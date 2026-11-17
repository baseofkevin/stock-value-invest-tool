"""coverage：命中的唯一依据。

`hit()` 判的是**区间覆盖 + 点数 + 无阻塞洞 + 口径指纹**，绝不判"表里有没有这只股票的行"。
有行不等于覆盖够：`finance` 可能只有 20 期（够 quality，不够 cycle），`kline` 可能只有 207 根
（够 kline-screen，不够 judge）。判错了不报错，只会安静地输出一份"看起来正常"的报告。

**与 `SCHEMA` §5 的一处差异（有意）**：`hit` 不要求 `cov.start <= need.start`（`dividend` 除外）。
理由是次新股：一只 2025 年上市的票永远凑不满"回溯 300 根"，按 `start` 判会让它每次运行都去
重取一次、永远不命中，把配额全烧在一只票上。深度改由 `n >= min_points` 表达——"够不够用"
本来就是这个意思，`start` 只是**取数范围**的起点。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field, replace

from .calendar import TradingCalendar
from .errors import HoleUnreachable, InsufficientDepth, PartialData
from .need import Need

# 各域落在哪张表、按哪个日期列算覆盖、**按什么节拍算洞**
#
# 第三列（cadence）是 2026-11-14 补的，补得很晚，因为它的缺席不会报错，只会让功能假死：
#
#   day    → 每个交易日都应有一条，缺了就是一个洞（只有 kline）
#   period → 季度序列，法定披露期才是"应有点"，日历上的空档全是正常的
#   week   → 周频，一个 ISO 周一条
#   event  → 事件序列（除权日），"没有事件"是合法状态，洞这个概念不存在
#
# 早先的实现对所有域一律调 `recount_holes(cal, ...)`，拿交易日历去衡量季度序列：
# `finance` 十多年的 43 个报告期落在 ~2500 个交易日里，于是算出一个覆盖全程的巨型洞，
# `blocking > 0` → `hit()` 永远 False → **每次都重取，永远不命中**。
# 之所以一直没暴露，是因为现有测试只测了"分红 0 条"——那时 `end` 还是 None，
# `recount_holes` 提前返回了。第 5 步一接上真数据就会全线假死。
DOMAIN_SPEC: dict[str, tuple[str, str, str]] = {
    "kline": ("kline", "date", "day"),
    "finance": ("finance", "period", "period"),
    "dividend": ("dividend", "ex_date", "event"),
    "valuation": ("valuation", "trade_date", "week"),
}

POINT_DOMAINS = frozenset({"quote", "snapshot", "profile", "scan"})
"""一股一份、没有区间概念的域。它们走 `merge_point()`，不走 `merge()`：

- `quote`   当期行情，过期判据是"存的 trade_date 是否等于目标交易日"；
- `snapshot` mainop / consensus / rating，按 `domain` 再分一层；
- `profile`  写的是 `stock` 表（元数据），连日期列都没有；
- `scan`     某天的全市场横截面。「一份」是"一天"而不是"一只股票"——
  它的 `symbol` 恒为 `"*"`（`api.SCAN_SYMBOL`），`fp` 里钉死日期。
  放进点域是因为它同样是"整份取、取到就够"，没有"补一段"这回事。
"""

# 退避序列（小时）。超过就标 permanent（`CONTRACT` §6.3）
BACKOFF_HOURS = (1, 6, 24, 72, 168)


# ---------------------------------------------------------------- Hole

@dataclass(frozen=True)
class Hole:
    """连续缺口。连续段算**一个** hole，不是一个交易日一个。"""

    start: str
    end: str
    n: int = 0
    retry: int = 0
    next_try_at: str | None = None
    cls: str = "retry"            # retry / permanent
    reason: str = "no_data"       # no_data / upstream_skip / suspended

    @property
    def permanent(self) -> bool:
        return self.cls == "permanent"

    def to_dict(self) -> dict:
        return {"from": self.start, "to": self.end, "n": self.n, "retry": self.retry,
                "next_try_at": self.next_try_at, "cls": self.cls, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> "Hole":
        return cls(start=d.get("from") or d.get("start") or "",
                   end=d.get("to") or d.get("end") or "",
                   n=int(d.get("n") or 0), retry=int(d.get("retry") or 0),
                   next_try_at=d.get("next_try_at"), cls=d.get("cls") or "retry",
                   reason=d.get("reason") or "no_data")

    def bump(self, now: dt.datetime) -> "Hole":
        """重试一次之后的形态。超过退避序列末尾 → permanent。"""
        r = self.retry + 1
        if r > len(BACKOFF_HOURS):
            return replace(self, retry=r, cls="permanent", next_try_at=None)
        at = (now + dt.timedelta(hours=BACKOFF_HOURS[r - 1])).isoformat(timespec="seconds")
        return replace(self, retry=r, next_try_at=at)


def hole_is_waiting(h: Hole, now: dt.datetime) -> bool:
    """还在退避窗口内：**不许**去打上游。

    这与"阻不阻塞命中"是两件事：命中只问"数据完整吗"（`blocking`），这里问"现在能重试吗"。
    混在一起会让"再等 1 小时"被当成"数据已完整"。
    """
    if h.permanent or not h.next_try_at:
        return False
    try:
        return now < dt.datetime.fromisoformat(h.next_try_at)
    except ValueError:                              # pragma: no cover - 脏数据就当到期
        return False


# ---------------------------------------------------------------- Coverage

@dataclass
class Coverage:
    domain: str
    symbol: str
    fp: str
    start: str | None = None
    end: str | None = None
    n: int = 0
    holes: int = 0
    detail: list[Hole] = field(default_factory=list)
    partial: int = 0
    src: str | None = None
    updated_at: str | None = None

    @property
    def blocking(self) -> int:
        """阻塞命中的洞数。`permanent` 不阻塞——否则停牌股永远不命中，每次跑白打一次上游。"""
        return sum(1 for h in self.detail if not h.permanent)

    def waiting(self, now: dt.datetime) -> list[Hole]:
        return [h for h in self.detail if hole_is_waiting(h, now)]

    def due(self, now: dt.datetime) -> list[Hole]:
        return [h for h in self.detail if not h.permanent and not hole_is_waiting(h, now)]


def read(conn: sqlite3.Connection, domain: str, symbol: str, fp: str | None) -> Coverage | None:
    row = conn.execute(
        "SELECT domain,symbol,fp,start_date,end_date,n,holes,hole_detail,partial,src,updated_at "
        "FROM coverage WHERE domain=? AND symbol=? AND fp=?",
        (domain, symbol, "" if fp is None else fp)).fetchone()
    if row is None:
        return None
    detail: list[Hole] = []
    if row["hole_detail"]:
        try:
            raw = json.loads(row["hole_detail"])
            detail = [Hole.from_dict(x) for x in raw if isinstance(x, dict)]
        except (ValueError, TypeError):              # 脏 JSON 不该让整次运行崩
            detail = []
    return Coverage(domain=row["domain"], symbol=row["symbol"], fp=row["fp"],
                    start=row["start_date"], end=row["end_date"], n=int(row["n"]),
                    holes=int(row["holes"]), detail=detail, partial=int(row["partial"]),
                    src=row["src"], updated_at=row["updated_at"])


# ---------------------------------------------------------------- 命中

def hit(cov: Coverage | None, need: Need) -> bool:
    """库里数据够不够这次用。**不看 `start`**（`dividend` 除外），理由见模块头。"""
    if cov is None or cov.fp != need.fp or cov.partial:
        return False
    if need.end and (cov.end is None or cov.end < need.end):
        return False
    if cov.blocking:
        return False
    if need.domain == "dividend":
        # 分红只看"查到那么早了没有"，不看条数：十年不分红是合法状态
        if need.start and (cov.start is None or cov.start > need.start):
            return False
        return True
    return cov.n >= need.min_points


def miss_reasons(cov: Coverage | None, need: Need) -> list[str]:
    """命不中时给出原因，用于日志与异常文案——别让人对着 `hit() == False` 猜。"""
    if cov is None:
        return ["无覆盖记录"]
    out: list[str] = []
    if cov.fp != need.fp:
        out.append(f"口径不符（库 {cov.fp} / 需 {need.fp}）")
    if cov.partial:
        out.append("上次取数被限频截断")
    if need.end and (cov.end is None or cov.end < need.end):
        out.append(f"末端不足（库 {cov.end} / 需 {need.end}）")
    if cov.blocking:
        out.append(f"{cov.blocking} 个待重试缺口")
    if need.domain == "dividend":
        if need.start and (cov.start is None or cov.start > need.start):
            out.append(f"起点不足（库 {cov.start} / 需 {need.start}）")
    elif cov.n < need.min_points:
        out.append(f"点数不足（库 {cov.n} / 需 {need.min_points}）")
    return out


def verdict(cov: Coverage | None, need: Need) -> Coverage:
    """命不中时抛最具体的那个异常（`CONTRACT` §10）。

    调用方在 `ensure` 的最后一步调它：**要么拿到覆盖，要么拿到异常**，没有第三种返回。
    这条边界一旦放宽，工具就开始出"缺几只股但看起来正常"的报告。

    判定顺序按"信息量从多到少"：

      1. `partial`        → `PartialData`（限频截断，已取到的部分留库，下次接着补）
      2. 有阻塞洞          → `InsufficientDepth`（用例 5：中间删一天就只能算 299 根，不能当 300 根用）
      3. 只剩永久洞 + 点数不够 → `HoleUnreachable`（**唯一**允许调用方逐股跳过）
      4. 其余（点数/末端/口径） → `InsufficientDepth`
    """
    if hit(cov, need) and cov is not None:      # hit 已保证非 None，这里只是让类型收窄
        return cov

    reasons = "；".join(miss_reasons(cov, need)) or "未知原因"
    where = f"{need.domain} {cov.symbol if cov else '-'}"

    if cov is None:
        raise InsufficientDepth(f"{where}：库里没有覆盖记录", domain=need.domain)
    if cov.partial:
        raise PartialData(f"{where}：{reasons}", domain=need.domain, symbol=cov.symbol)
    if cov.blocking:
        holes = "、".join(f"{h.start}..{h.end}" for h in cov.detail if not h.permanent)
        raise InsufficientDepth(f"{where}：区间内有缺口（{holes}），{reasons}",
                                domain=need.domain, symbol=cov.symbol)
    if cov.detail and cov.n < need.min_points:
        raise HoleUnreachable(f"{where}：{reasons}",
                              domain=need.domain, symbol=cov.symbol)
    raise InsufficientDepth(f"{where}：{reasons}",
                            domain=need.domain, symbol=cov.symbol)


# ---------------------------------------------------------------- 缺口重算

def recount_holes(cal: TradingCalendar, have: set[str], start: str | None,
                  end: str | None, old: list[Hole] | None = None,
                  fetched: tuple[str | None, str | None] | None = None,
                  now: dt.datetime | None = None
                  ) -> tuple[int, list[Hole]]:
    """用交易日历重算缺口：应有一天都没少，少了就按**连续段**合并。

    **不许增量累加**。增量在"重取同一段"时会算成两倍，而错误不会立刻暴露——
    它只在三个月后表现为"明明够 300 根却报不够"。

    保留旧缺口的重试计数：同一段（起止相同）沿用 `retry` / `next_try_at`，
    否则退避永远从头开始，等于没有退避。

    `fetched`：这一次**实际向上游要过**的区间 `(start, end)`。落在这个区间里的缺口
    说明"刚重取过、还是没有"，于是 `bump()` 一次（退避 1h→6h→24h→72h→168h，
    第 6 次标 `permanent`）。

    **这一条以前没人调用**（2026-11-15 修）：`Hole.bump` 只在测试里出现过，
    生产路径上一次都没走过，于是缺口永远停在 `retry=0`、永远 `due`、永远阻塞
    `hit()` —— 实测 278 只票的 351 个缺口（绝大多数是停牌日，上游根本没有那些 bar）
    在**每次运行**都要重取一遍整段 300 根，一次 34,500 行，且永远收敛不了。
    """
    if not start or not end or start > end:
        return 0, []
    expected = cal.between(start, end)
    if not expected:
        return 0, []
    have_set = have
    missing = [d for d in expected if d not in have_set]
    if not missing:
        return 0, []

    pos = {d: i for i, d in enumerate(expected)}
    segs: list[list[str]] = []
    for d in missing:
        if segs and pos[d] == pos[segs[-1][1]] + 1:
            segs[-1][1] = d
        else:
            segs.append([d, d])

    old_by_key = {(h.start, h.end): h for h in (old or [])}
    fs, fe = fetched if fetched else (None, None)
    detail: list[Hole] = []
    for a, b in segs:
        prev = old_by_key.get((a, b))
        if prev is None:
            detail.append(Hole(a, b, n=cal.count(a, b)))
        elif fs and fe and fs <= a and b <= fe and not prev.permanent:
            detail.append(prev.bump(now or dt.datetime.now()))   # 刚要过这一段，还是缺
        else:
            detail.append(prev)
    return len(detail), detail


# ---------------------------------------------------------------- 待补区间

def _next_trading(cal: TradingCalendar, day: str) -> str | None:
    """`day` 之后的下一个交易日。

    **日历末端不够时退回"自然日 +1"**（2026-11-15 修）。原写法 `return cal.on_or_after(nxt)`
    在日历没更新（`cal.end` 正好等于库里最后一天）时返回 `None`，于是 `missing_segments`
    返回空列表 → `ensure()` 的兜底退化成"整段重取 300 根"（每只票、每一天），
    而 `ensure_bulk()` 更是一律取整段 —— 这是"每天重下 129 万行"的一个隐藏放大器。

    退回落成自然日为什么安全：段的两端是 `(nxt, need.end)`，适配器会按区间取；
    非交易日上游本来就不返回数据，多带一天周六/周日是无害的空区间，
    而真正的交易日（哪怕日历还不知道）照样能取到。
    """
    nxt = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    return cal.on_or_after(nxt) or nxt


def _prev_trading(cal: TradingCalendar, day: str) -> str | None:
    return cal.prev(day)


def missing_segments(cov: Coverage | None, need: Need, cal: TradingCalendar,
                     now: dt.datetime | None = None) -> list[tuple[str, str | None]]:
    """这次该补哪几段。可能多段：末端延伸 + 起点前补 + 到期的缺口。

    两处"没有下界"的域要单独照顾，否则它们**永远不取数**（2026-11-14 修）：

    - `finance` 的 `need.start` 是 `None`（"最近 43 期"，起点由上游决定），
      老写法 `if need.start and need.end` 会返回空列表，`ensure` 的兜底同样要求 `start`，
      于是首次运行一次都不打上游，直接报"库里没有覆盖记录"。
    - `dividend` 的 `need.end` 是 `None`（除权日没有上界），同理返回空列表。
    """
    now = now or dt.datetime.now()

    if need.domain in POINT_DOMAINS:
        # 一股一份：要么整份重取，要么不用取（`hit` 已经判过了）。没有"补一段"这回事。
        # `end` 为 None（`profile`）时也要返回一段——返回空列表会让调用方以为"不用取"。
        return [(need.start, need.end)]

    if cov is None:
        if need.domain == "dividend":
            return [(need.start, None)] if need.start else []
        return [(need.start, need.end)] if need.end else []

    segs: list[tuple[str, str | None]] = []

    # ① 末端延伸
    if need.end and (cov.end is None or cov.end < need.end):
        s = _next_trading(cal, cov.end) if cov.end else need.start
        if s and s <= need.end:
            segs.append((s, need.end))

    # ② 起点前补：只有分红关心（K 线往更早取没有意义，反正会滚掉）
    if need.domain == "dividend" and need.start and cov.start and cov.start > need.start:
        e = _prev_trading(cal, cov.start)
        if e and need.start <= e:
            segs.append((need.start, e))

    # ③ 到期可重试的缺口
    for h in cov.due(now):
        segs.append((h.start, h.end))

    segs.sort()
    return segs


# ---------------------------------------------------------------- 写回

def merge(conn: sqlite3.Connection, *, cal: TradingCalendar, need: Need, symbol: str,
          src: str | None, now: dt.datetime | None = None,
          fetched: tuple[str | None, str | None] | None = None) -> Coverage:
    """重算并写回覆盖记录。**必须在调用方的事务里**（数据与覆盖同进同出，`SCHEMA` §1 不变式 3）。

    `n` 与 `holes` 一律重数 / 重算，不做增量。

    **区间取值按域分两种**（与 `CONTRACT` §6.2 的"只增不减"有一处有意差异）：

    - `dividend` 的 `start` 记的是"我们查过的最早一天"，不是"最早一条分红"。
      十年不分红是合法状态，按数据定起点会让它永远不命中、每次运行都白问一遍上游，
      所以 `start = min(数据最早, need.start, 旧 start)`。
    - 其余域（`kline` / `finance` / `valuation`）的区间就是**库里数据本身的范围**。
      不保留旧边界：K 线是会滚动删除的，保留旧 `start` 会在 GC 之后重算出一个
      "老数据被删了"的假缺口，且这个假缺口要重试 6 次才变 permanent。
    """
    now = now or dt.datetime.now()
    stamp = now.isoformat(timespec="seconds")
    spec = DOMAIN_SPEC.get(need.domain)
    if spec is None:
        raise ValueError(f"域 {need.domain} 没有区间覆盖规则（quote/snapshot 用 merge_point）")
    table, dcol, cadence = spec

    old = read(conn, need.domain, symbol, need.fp)
    rows = conn.execute(
        f"SELECT {dcol} AS d FROM {table} WHERE symbol=? AND {dcol} IS NOT NULL",
        (symbol,)).fetchall()
    dates = {r["d"] for r in rows}
    n = len(dates)

    start = min(dates) if dates else None
    end = max(dates) if dates else None
    if need.domain == "dividend":
        start = _earliest(start, need.start, old.start if old else None)
        end = _latest(end, old.end if old else None)

    if cadence == "day":
        holes, detail = recount_holes(cal, dates, start, end, old.detail if old else None,
                                      fetched=fetched, now=now)
    else:
        # 非日频域不按日历算洞（理由见 DOMAIN_SPEC 上面的注释）。
        # 完整性交给两条硬判据：`end` 推到 need.end，`n >= min_points`。
        # `dividend` 更特殊——十年不分红是合法状态，连点数门槛都是 0。
        holes, detail = 0, []
    cov = Coverage(domain=need.domain, symbol=symbol, fp=need.fp, start=start, end=end, n=n,
                   holes=holes, detail=detail, partial=0, src=src, updated_at=stamp)
    write(conn, cov)
    return cov


def _earliest(*vals: str | None) -> str | None:
    got = [v for v in vals if v]
    return min(got) if got else None


def _latest(*vals: str | None) -> str | None:
    got = [v for v in vals if v]
    return max(got) if got else None


def merge_point(conn: sqlite3.Connection, *, domain: str, symbol: str, as_of: str,
                src: str | None, fp: str = "", n: int = 1,
                now: dt.datetime | None = None) -> Coverage:
    """快照型（quote / snapshot / scan）：一股一份，覆盖即完整，没有区间概念。

    `n` 默认 1（"这一份拿到了"），`scan` 传真实行数——`doctor` 的深度检查与
    `probe` 的横截面探针都要看它，写死 1 会让"只回来 3 只票"看起来和 5026 只一样正常。
    """
    stamp = (now or dt.datetime.now()).isoformat(timespec="seconds")
    cov = Coverage(domain=domain, symbol=symbol, fp=fp, start=as_of, end=as_of,
                   n=max(1, int(n)), holes=0, detail=[], partial=0, src=src, updated_at=stamp)
    write(conn, cov)
    return cov


def write(conn: sqlite3.Connection, cov: Coverage) -> None:
    """写回一行覆盖。`updated_at` 缺省填当下——调用方不必为了一个时间戳去拼 `Coverage`。"""
    stamp = cov.updated_at or dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO coverage(domain,symbol,fp,start_date,end_date,n,holes,hole_detail,"
        "partial,src,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(domain,symbol,fp) DO UPDATE SET "
        "start_date=excluded.start_date, end_date=excluded.end_date, n=excluded.n, "
        "holes=excluded.holes, hole_detail=excluded.hole_detail, partial=excluded.partial, "
        "src=excluded.src, updated_at=excluded.updated_at",
        (cov.domain, cov.symbol, cov.fp, cov.start, cov.end, cov.n, cov.holes,
         json.dumps([h.to_dict() for h in cov.detail], ensure_ascii=False) if cov.detail else None,
         cov.partial, cov.src, stamp))


def mark_partial(conn: sqlite3.Connection, domain: str, symbol: str, fp: str,
                 now: dt.datetime | None = None) -> None:
    """记"上次被限频截断"。**不推进 end_date**——推进了下次就会被当成完整段。"""
    cov = read(conn, domain, symbol, fp)
    if cov is None:
        cov = Coverage(domain=domain, symbol=symbol, fp=fp)
    cov.partial = 1
    cov.updated_at = (now or dt.datetime.now()).isoformat(timespec="seconds")
    write(conn, cov)


def clear_holes(conn: sqlite3.Connection, domain: str, symbol: str, fp: str) -> None:
    """`force=True` 时清掉全部重试标记，让下次重头试一次（`CONTRACT` §6.3）。"""
    cov = read(conn, domain, symbol, fp)
    if cov is None:
        return
    cov.detail = []
    cov.holes = 0
    write(conn, cov)


def settle(conn: sqlite3.Connection, domain: str, symbol: str, fp: str,
           segs: list[tuple[str, str]], reason: str = "no_data",
           now: dt.datetime | None = None) -> int:
    """把落在 `segs` 范围内的缺口直接判 `permanent`，返回改了几段。

    **为什么不能等退避走完 6 次**（实现时发现，见纪要 §22.4）：缺一天的最常见原因是
    **停牌**——那不是"上游抽风"，重试 6 次（1/6/24/72/168h）之后才认账，意味着这只股票
    在长达 11 天里每次运行都 `blocking > 0` → 不命中 → 拒绝服务。实测 20 只票里有 3 只
    因此完全不可用，而它们只是各停过一天牌。

    所以规则改成：**同一次运行里对同一段取两次仍空，就认账**（仍是"永久洞"，
    仍计入 `coverage.holes`，仍出现在 `doctor` 与 `Range.permanent_holes` 里，
    只是不再阻塞命中、不再为它反复打上游）。真要有"上游漏一天"的情况，
    由 `probe` 的停牌判定（`SOURCES` §8 探针 6）抓出来，而不是靠反复重试。
    """
    cov = read(conn, domain, symbol, fp)
    if cov is None or not cov.detail:
        return 0
    changed = 0
    out: list[Hole] = []
    for h in cov.detail:
        inside = any(a <= h.start and h.end <= b for a, b in segs)
        if inside and not h.permanent:
            out.append(replace(h, cls="permanent", next_try_at=None, reason=reason))
            changed += 1
        else:
            out.append(h)
    if changed:
        cov.detail = out
        cov.updated_at = (now or dt.datetime.now()).isoformat(timespec="seconds")
        write(conn, cov)
    return changed


def touch_access(conn: sqlite3.Connection, symbol: str, now: dt.datetime | None = None) -> None:
    """记一次访问，写 `stock.last_access_at`。

    **这一列目前只写不读**（2026-11-17 核实）。设计里那句"`last_access_at`
    早于 90 天 → 删该股的 `kline`/`valuation`"从未实现过：全项目没有一处
    `DELETE FROM stock`，也没有任何代码按这个时间回收数据。所以它现在只是
    留痕，别当作"已经在 GC"来理解。

    真要实现时注意两件事：删 `kline` 必须**同事务**重算 coverage，否则
    `hit()` 会拿着一条指向空表的覆盖记录去放行；`prune_kline` 那套按根数
    滚动裁剪已经够用，股票级回收只在"几千只票的库里绝大多数再也不会被看"
    时才划算。见 `stock_data_fetch/README.md` §10「已知遗留」。
    """
    stamp = (now or dt.datetime.now()).isoformat(timespec="seconds")
    conn.execute("UPDATE stock SET last_access_at=? WHERE symbol=?", (stamp, symbol))
