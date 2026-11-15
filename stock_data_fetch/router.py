"""路由：这次用谁、失败了怎么降级、组级重试。

规则（`SOURCES` §7）：

1. 该域所有可用源按 `priority` 升序；
2. 主源冷却中（`throttle.cooling > 0`）→ 本次先用别的，全都冷却才等；
3. 主源连续失败 `cfg.degrade_after` 次 → **本次进程内**降级，写 `meta['source_degraded']`；
4. 主源可用但返回 partial → **不降级**，走组级重试；
5. 主源连断三次（`ThrottleTimeout`）→ **有备源就降级**，没备源才抛（见 `fetch()` 的说明）。

第 4 条最容易搞错：**限频不是"源坏了"**。换源等于把压力转给另一个源，同时把数据换成
另一家的口径——`finance` / `valuation` 换源就是要作废旧数据的（`SOURCES` §4.1）。
只有"不可达 / 鉴权失败 / 接口变更"才降级。

组级重试（`CONTRACT` §7.3）也在这里：上游按 10 只一组独立判定，失败组在 `errors[]`
里自报。所以重取只针对那几只，且**先冷却再取**——kline-screen 现在是"丢码就拆半立刻
重取"，实测这会加重限频（连发 6 批全败）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from . import db, sources
from .config import Config
from .errors import SourceUnavailable, ThrottleTimeout
from .need import Need
from .throttle import Throttle

log = logging.getLogger("stock_data_fetch.router")


@dataclass
class Router:
    cfg: Config
    throttle: Throttle
    conn: object | None = None
    """给了就写 `meta['source_degraded']`；不给也能工作（只影响留痕）。"""

    strikes: dict[tuple[str, str], int] = field(default_factory=dict)
    """(domain, src) → 连续失败次数。进程内，不落盘——重启后可以再试一次。"""

    dead: set[tuple[str, str]] = field(default_factory=set)

    # ---- 选源 ----

    def order(self, domain: str, need: Need | None = None) -> list[sources.SourceSpec]:
        """可用源，按 priority 升序；冷却中的排到后面。

        `need` 会透传给 `sources.available()`，让它去问适配器的 `supports()`——
        `snapshot` 域靠这个区分 `mainop`（东财）与 `consensus`/`rating`（westock）。
        不传 `need` 就只能拿到"声明支持该域"的全部源，排第一的可能根本不认得这个子域。

        **冷却 ≠ 不可用**。早先的写法是"冷却就跳过，全都跳过就报没有可用源"，
        于是主源一进 300 秒冷却，整批股票就集体失败——实测 20 只票在第一只吃满冷却后
        全线报"没有可用源"。正确语义是：还有能用的就先换一个；**全都冷却就等**
        （`throttle.take()` 会睡到冷却结束，这段等待是预期行为，不是错误）。
        """
        alive = [s for s in sources.available(domain, need)
                 if (domain, s.name) not in self.dead]
        if not alive:
            why = "；".join(f"{s.name}: {r}" for s, r in sources.disabled(domain))
            raise SourceUnavailable(f"{domain} 域没有可用源。{why or '全部已降级'}")
        ready = [s for s in alive if self.throttle.cooling(s.name) <= 0]
        if ready:
            skipped = [s.name for s in alive if s not in ready]
            if skipped:
                log.info("%s 冷却中，本次先用 %s", ",".join(skipped), ready[0].name)
            return ready
        log.info("%s 域所有源都在冷却，等 %s（冷却最长 %d 秒）",
                 domain, alive[0].name, int(max(self.throttle.cooling(s.name) for s in alive)))
        return alive

    # ---- 取数 ----

    def fetch(self, domain: str, codes: list[str], need: Need, *,
              rounds: int = 3) -> tuple[sources.Got, sources.SourceSpec]:
        """按顺序试源，返回 (Got, 用的源)。有组级失败不抛，交给 `api` 判 `PartialData`。

        **`ThrottleTimeout` 有备源时降级，没备源时才抛**（2026-11-14 深夜改的）。
        原规则是"断连三次就抛，绝不换源"，那条规则的成立前提是"换源会把压力转给另一家、
        还要作废旧数据"。但真实场景打穿了它：东财 `push2his` 突发配额用完后**整段拒连**
        （0.2 秒关连接，持续 20 分钟以上），而 K 线与它同值的备份（腾讯 web 通道）就闲在旁边。
        按旧规则，4300 只的候选池会变成"每隔 30 秒冷却到期就有一批股票失败"——
        工具拿到的是一份**少了若干只却不报错**的报告，而这正是最坏的结局。

        `_call` 里已经重发过一次、冷却过一次、连试三次了，"这个源此刻不可用"的证据足够硬，
        所以直接标死并降级；`meta['source_degraded']` 留痕。若本域没有别的可用源，
        仍然抛 `ThrottleTimeout`——那种情况下"稍后重跑"才是正确动作。
        """
        codes = list(codes)
        last: Exception | None = None
        specs = self.order(domain, need)
        for i, spec in enumerate(specs):
            fn = sources.fetch_fn(spec.name)
            try:
                got = self._fetch_with_retry(spec, fn, domain, codes, need, rounds)
                self.strikes[(domain, spec.name)] = 0
                return got, spec
            except ThrottleTimeout as e:
                if i == len(specs) - 1:
                    raise
                last = e
                self.dead.add((domain, spec.name))
                self._note_degrade(domain, spec, self.cfg.degrade_after, e)
                log.error("%s 连续 %d 次连接中断，改用备源 %s（本进程内不再使用它）",
                          spec.name, 3, specs[i + 1].name)
                continue
            except SourceUnavailable as e:
                last = e
                self._strike(domain, spec, e)
                continue
        raise SourceUnavailable(f"{domain} 域所有源都失败：{last}")

    def _call(self, spec, fn, domain: str, codes: list[str], need: Need, *,
              tries: int = 3) -> sources.Got:
        """节流 + 调一次适配器。**连接中断按瞬断处理：先立刻重发一次，仍断才冷却。**

        东财实测会在连续取数后直接掐连接（`RemoteDisconnected`，不是 4xx）。
        这和"源坏了"是两件事——换源会把压力转给另一家、还把口径换成另一家的。

        **为什么要"先无冷却重发一次"**（2026-11-14 冒烟实测改的）：
        一次掐连接的成本只是那一个 0.4 秒的请求，而原来每掐一次就罚
        `cooldown_base`（30s），连掐两次翻倍到 60s、三次 120s。10 只票的 K 线
        因此跑了 336.5 秒，其中 240 秒纯粹在等冷却——而实际取数只要 4 秒
        （干净状态下每只 0.35–0.44s）。掐连接是**偶发**的（同一批里 8 只连续成功），
        偶发事件不该触发指数级退避。

        代价说清楚：如果上游是真的限频，这次重发会被再掐一次，白白多花一个请求；
        然后才进入冷却，损失有界。而"限频时连发"的老坑（连发 6 批全败）说的是
        **重试整批**，这里重发的还是同一批、只多一次，不构成那个问题。
        """
        last: Exception | None = None
        for i in range(max(1, tries)):
            self.throttle.take(spec.name, self._expect(spec, need, codes))
            try:
                return fn(domain, codes, need)
            except SourceUnavailable as e:
                if not e.ctx.get("transient"):
                    raise
                last = e
                if i == 0:
                    log.warning("%s 瞬断，立即重发一次（不冷却）：%s", spec.name, e)
                    continue
                # `cap=cooldown_base`：瞬断的证据不如"上游自报限频"硬，别让它把冷却翻到 300 秒。
                # 不设这个上限的实测后果：东财被整段拒连时，每批股票都先干等 5 分钟，
                # 4300 只的候选池根本跑不完（`SOURCES` §5.7 那轮的 336 秒就是这么来的）。
                self.throttle.penalize(spec.name, cap=self.cfg.cooldown_base)
                log.warning("%s 连接中断（第 %d/%d 次），冷却后重试：%s",
                            spec.name, i + 1, tries, e)
        raise ThrottleTimeout(
            f"{spec.name} 连续 {tries} 次连接中断，最后一次：{last}。请稍后重跑",
            src=spec.name)

    def _fetch_with_retry(self, spec, fn, domain: str, codes: list[str],
                          need: Need, rounds: int) -> sources.Got:
        got = self._call(spec, fn, domain, codes, need)

        for i in range(max(0, rounds - 1)):
            failed = list(dict.fromkeys(got.failed_symbols))   # 去重且保序
            if not failed:
                break
            log.warning("%s 第 %d 轮：%d 只失败（%s），先冷却再只重取这几只",
                        spec.name, i + 1, len(failed), ",".join(failed[:5]))
            self.throttle.penalize(spec.name)          # 必须先冷却，不能立刻重取
            more = self._call(spec, fn, domain, failed, need)
            got.extend(more)
            # 归一：每轮结束后失败清单只保留"确实还没拿到行"的那几只，
            # 不然上一轮的 BatchError 会留在 errors 里，下一轮把同一只请求两次。
            still = [s for s in failed if s not in more.rows]
            got.errors = [sources.BatchError(still)] if still else []
            if not still:
                break

        if got.errors:
            return got                                    # 规则 4：限频不降级
        self.throttle.reward(spec.name)
        return got

    def _strike(self, domain: str, spec: sources.SourceSpec, e: Exception) -> None:
        key = (domain, spec.name)
        n = self.strikes.get(key, 0) + 1
        self.strikes[key] = n
        if n >= self.cfg.degrade_after:
            self.dead.add(key)
            self._note_degrade(domain, spec, n, e)
            log.error("%s 对 %s 连续失败 %d 次，本次进程内不再使用：%s",
                      spec.name, domain, n, e)
        else:
            log.warning("%s 失败（第 %d/%d 次）：%s", spec.name, n,
                        self.cfg.degrade_after, e)

    def _note_degrade(self, domain: str, spec: sources.SourceSpec, n: int,
                      e: Exception) -> None:
        if self.conn is None:
            return
        try:
            # 用 `sources.available()` 而不是 `self.order()`：此刻 `spec` 刚被塞进 `dead`，
            # `order()` 若因此抛"没有可用源"，留痕就永远写不进去——降级本身反而没留下痕迹。
            nxt = [s.name for s in sources.available(domain) if s.name != spec.name]
            db.meta_set(self.conn, "source_degraded", json.dumps({
                "domain": domain, "from": spec.name, "to": nxt[0] if nxt else None,
                "reason": str(e)[:200], "strikes": n,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False))
        except Exception as err:                # 留痕失败不该阻断取数
            log.debug("降级留痕写不进去：%s", err)

    @staticmethod
    def _expect(spec: sources.SourceSpec, need: Need, codes: list[str]) -> float:
        """估算这次要花掉多少行配额。

        **宁多勿少**：估多了只是慢一点，估少了会打穿上游（`CONTRACT` §7.1）。
        """
        per = {"kline": need.points or 300,
               "finance": (need.extra.get("periods") or 43) * 3,
               "valuation": need.points or 520,
               "scan": need.points or 5400,
               # `quote` 是"一只一行"的快照：按默认的 100 行/只估，
               # 4300 只的池子会被算成 43 万行，一次就把 12000 行的桶打穿
               # → `throttle.take()` 按桶容量计费并整桶冷却（实测报了
               # "单次申请 327100 行 > 桶容量 12000"），白白等一分钟。
               "quote": 1,
               "dividend": 20}.get(need.domain, 100)
        return float(per) * max(1, len(codes))


def remember_source(conn, domain: str, upstream: str) -> None:
    """记"本域当前源"。`finance` / `valuation` 换源要作废旧数据，靠这条比对（`SOURCES` §4.1）。"""
    db.meta_set(conn, f"domain_src:{domain}", upstream)


def current_source(conn, domain: str) -> str | None:
    return db.meta_get(conn, f"domain_src:{domain}")


__all__ = ["Router", "remember_source", "current_source"]
