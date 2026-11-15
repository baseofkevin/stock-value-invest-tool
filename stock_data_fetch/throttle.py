"""节流器：令牌桶（按行数）+ 冷却闸（限频反馈）+ 最小间隔。

**为什么不是固定间隔**（`CONTRACT` §7，2026-11-14 实测）：

    批间间隔 0.6s → 10 批只有 5 批完整
    批间间隔 1.0s → 8/10
    批间间隔 1.6s → 7/10
    批间间隔 3.0s → 3/4

**非单调**——间隔大的反而更差。换成"行数"视角就一致了：不管切多大批、隔多久，
大约消耗 2.5–3 万行之后配额就耗尽。所以节流对象是**返回行数**，不是请求次数，
也不该用固定睡眠近似。

跨进程共享
----------
桶状态落 `state/throttle/<src>.json`，用 `flock` 保护整个"读-算-睡-写"过程。
**必须在持有锁的时候睡**：先放锁再睡会让两个进程同时醒来、把桶一次透支，
那样等于没有节流器。代价是同一个 src 的两个进程严格串行——这正是想要的。

（`fcntl` 只有 POSIX 有。缺失时退化成进程内节流并打一次 WARNING：单进程仍然正确，
  只是不能和别的进程共享配额。这里不退化成"完全不节流"，那是最坏的选择。）
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable, Iterator

from .config import Config
from .errors import ThrottleTimeout

log = logging.getLogger("stock_data_fetch.throttle")

try:                                     # pragma: no cover - 平台差异
    import fcntl
    _HAS_FLOCK = True
except ImportError:                      # pragma: no cover - Windows
    fcntl = None                         # type: ignore[assignment]
    _HAS_FLOCK = False


@dataclass
class Bucket:
    """一个源的节流状态。全部字段都能落 JSON。"""

    tokens: float = 0.0
    """桶内余量（行）。首次使用时就满，否则第一跑要先等一个桶。"""

    refill_at: float = 0.0
    """上次补充时刻（epoch 秒）。用墙钟而不是 monotonic——跨进程要同一个时间基准。"""

    last_req: float = 0.0
    """上次真正发出请求的时刻（epoch 秒），用于最小间隔。"""

    cooldown_until: float = 0.0
    """冷却结束时刻。0 表示不在冷却。"""

    cooldown_len: float = 0.0
    """当前冷却长度。连续限频时翻倍，干净返回时减半。"""

    clean: int = 0
    """连续干净返回次数。到 `clean_streak_reset` 就把冷却长度清零。"""

    @classmethod
    def load(cls, path: Path) -> "Bucket":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self)), encoding="utf-8")
            tmp.replace(path)            # 原子替换：半截 JSON 会让下次读回空桶
        except OSError as e:             # pragma: no cover - 磁盘满/只读
            log.warning("节流状态写不进去（%s）：这次只在进程内生效", e)


class Throttle:
    """串行点。所有"要不要等、等多久"的判断都在这里，别处不许 sleep。"""

    def __init__(self, cfg: Config | None = None, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 root: Path | None = None) -> None:
        self.cfg = cfg or Config()
        self.clock = clock
        self._sleep = sleep
        self.root = Path(root or (self.cfg.state_dir / "throttle"))
        self._warned = False

    # ---- 路径与锁 ----

    def _path(self, src: str) -> Path:
        return self.root / f"{src}.json"

    @contextmanager
    def _locked(self, src: str) -> Iterator[None]:
        if not _HAS_FLOCK:               # pragma: no cover - Windows
            if not self._warned:
                log.warning("本平台没有 fcntl，节流状态不能跨进程共享")
                self._warned = True
            yield
            return
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / f"{src}.lock"
        with open(lock, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ---- 速率 ----

    def _rate_per_s(self, src: str) -> float:
        return max(0.0, self.cfg.rate_rows_per_min.get(src, 0.0)) / 60.0

    def _refill(self, st: Bucket, src: str, now: float) -> None:
        rate = self._rate_per_s(src)
        if rate <= 0:                    # 该源不走令牌桶，只保留最小间隔
            st.tokens = 0.0
            st.refill_at = now
            return
        if not st.refill_at:             # 第一次：给满一桶，否则首跑要先等满
            st.tokens = self.cfg.burst_rows
            st.refill_at = now
            return
        st.tokens = min(self.cfg.burst_rows,
                        st.tokens + max(0.0, now - st.refill_at) * rate)
        st.refill_at = now

    # ---- 主入口 ----

    def take(self, src: str, rows: float, budget: float | None = None) -> float:
        """申请 `rows` 行的配额，必要时等待。返回累计等待秒数。

        等待是**预期行为**，不抛异常；只有累计等待超过 `budget` 才抛 `ThrottleTimeout`
        ——"等太久"和"上游坏了"是两件事，混在一起会让工具误报成数据问题。
        """
        budget = self.cfg.throttle_budget if budget is None else budget
        remaining = max(0.0, budget)
        waited = 0.0
        rows = max(0.0, float(rows))
        interval = self.cfg.interval_s(src)

        # 单次申请超过桶容量时，按桶容量计费。
        # 不这么做的话 `tokens >= rows` 永远不成立——桶补到 burst 就封顶，
        # 于是一直循环到 `budget` 耗尽，抛一个看起来像"上游卡住"的 ThrottleTimeout。
        # 正常配置下 burst(12000) ≥ 任何单次请求(≤12000 行)，这条不会触发；
        # 它是给"有人把 burst 调小了"兜底的。
        if self._rate_per_s(src) > 0 and rows > self.cfg.burst_rows:
            log.warning("%s 单次申请 %.0f 行 > 桶容量 %.0f，按桶容量计费",
                        src, rows, self.cfg.burst_rows)
            rows = self.cfg.burst_rows

        with self._locked(src):
            st = Bucket.load(self._path(src))
            while True:
                now = self.clock()
                self._refill(st, src, now)

                # ① 冷却闸：限频后必须等够，别指望"再试一次就好"
                if st.cooldown_until > now:
                    waited += self._wait(st.cooldown_until - now, remaining, src, waited)
                    remaining = max(0.0, remaining - (self.clock() - now))
                    continue

                # ② 最小间隔
                gap = st.last_req + interval - now
                if gap > 0:
                    waited += self._wait(gap, remaining, src, waited)
                    remaining = max(0.0, remaining - (self.clock() - now))
                    continue

                # ③ 令牌桶
                rate = self._rate_per_s(src)
                if rate <= 0 or st.tokens >= rows:
                    st.tokens = max(0.0, st.tokens - rows)
                    st.last_req = now
                    st.save(self._path(src))
                    return waited

                need = (rows - st.tokens) / rate
                waited += self._wait(need, remaining, src, waited)
                remaining = max(0.0, remaining - (self.clock() - now))

    def _wait(self, need: float, remaining: float, src: str, waited: float) -> float:
        """睡 `need` 秒，但不超过剩余预算；超了就抛。返回真正睡掉的秒数。"""
        if need <= 0:
            return 0.0
        if remaining <= 0:
            raise ThrottleTimeout(
                f"等上游配额已超过 {self.cfg.throttle_budget:.0f} 秒仍未轮到，"
                "可能有另一个进程正在大量取数，请稍后重跑",
                src=src, waited=round(waited, 1))
        slept = min(need, remaining)
        self._sleep(slept)
        if slept < need:                 # 睡到预算上限还没够 → 放弃
            raise ThrottleTimeout(
                f"等上游配额 {self.cfg.throttle_budget:.0f} 秒仍不够，请稍后重跑",
                src=src, waited=round(waited + slept, 1))
        return slept

    # ---- 反馈 ----

    def penalize(self, src: str, *, cap: float | None = None) -> float:
        """上游自报限频。首次冷却 `cooldown_base`，连续触发翻倍到上限。返回冷却秒数。

        `cap` 给"证据不足"的场合用（`router` 的瞬断路径）：它把冷却长度**压到基准值**，
        不再翻倍。理由是一次掐连接可能是偶发，也可能是整源被拒——两种都判断不了，
        而"翻倍"只对**有明确限频证据**的情形成立（westock 的 `errorCount > 0`）。
        不压的话，断连三次就把冷却推到 300 秒，之后每批股票都要先干等 5 分钟。
        """
        with self._locked(src):
            st = Bucket.load(self._path(src))
            now = self.clock()
            if st.cooldown_len <= 0:
                st.cooldown_len = self.cfg.cooldown_base
            else:
                st.cooldown_len = min(self.cfg.cooldown_max, st.cooldown_len * 2)
            if cap is not None:
                st.cooldown_len = min(st.cooldown_len, cap)
            st.cooldown_until = now + st.cooldown_len
            st.clean = 0
            st.save(self._path(src))
            log.warning("%s 报限频，冷却 %.0fs（到 %s）", src, st.cooldown_len,
                        time.strftime("%H:%M:%S", time.localtime(st.cooldown_until)))
            return st.cooldown_len

    def reward(self, src: str) -> float:
        """一次干净返回。冷却长度减半；连续 `clean_streak_reset` 次则清零。"""
        with self._locked(src):
            st = Bucket.load(self._path(src))
            st.clean += 1
            if st.clean >= self.cfg.clean_streak_reset:
                st.cooldown_len = 0.0
            elif st.cooldown_len > 0:
                st.cooldown_len = min(st.cooldown_len, st.cooldown_len / 2.0)
            st.save(self._path(src))
            return st.cooldown_len

    # ---- 查询（router / doctor 用）----

    def cooling(self, src: str) -> float:
        """还要冷却多少秒；0 表示不在冷却。router 靠它决定是否跳过这个源。"""
        st = Bucket.load(self._path(src))
        return max(0.0, st.cooldown_until - self.clock())

    def snapshot(self, src: str) -> dict:
        st = Bucket.load(self._path(src))
        self._refill(st, src, self.clock())      # 只算不存：看一眼"现在有多少"
        now = self.clock()
        return {"src": src, "tokens": round(st.tokens, 1), "burst": self.cfg.burst_rows,
                "rate_per_min": self.cfg.rate_rows_per_min.get(src, 0.0),
                "min_interval_ms": self.cfg.min_interval_ms.get(src, 0.0),
                "cooldown_left_s": round(max(0.0, st.cooldown_until - now), 1),
                "cooldown_len_s": round(st.cooldown_len, 1), "clean": st.clean}
