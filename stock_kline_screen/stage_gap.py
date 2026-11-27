"""`[4/6]` K 线之后、`[6/6]` 行情之前的配额恢复等待——**时长自适应**。

为什么需要它（2026-11-27）：K 线批量是 40 只/请求，全市场约 126 批；行情紧跟着再打
123 批。westock 按「每分钟请求数」限频（实测 60~70 次），250 批挤在几分钟内必然击穿
它的滑动窗口——当晚实测行情阶段首轮 3680 只失败，全靠 router 的冷却重试才一只只补回来。

为什么不能写死一个数：真正需要等多久取决于上游那一刻窗口还剩多少、本批要发多少请求。
但每次运行结束都拿得到一个**直接反馈**——行情首轮失败多少只。既然有反馈，就让它自己
学：失败多就加长，干净就缩回，上下都有界，不必由人来猜。

学习节奏：判断在本进程内做，结果跨运行落盘（`state/stage_gap.json`），所以哪怕一周才
跑一次，也是逐周收敛。落盘内容同时记下「上次凭什么调的」，运行日志里直接打出来，
不用翻历史。

`STOCKHUB_STAGE_GAP_S` 一旦设置就当成**人工指定**：既不读学习值，也不再学习。
理由是人明确表态时不该被自动逻辑覆盖；`STOCKHUB_STAGE_GAP_S=0` 因此是可靠的总开关。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT = 90.0
"""首次运行（还没有任何观测）时的等待秒数。跑满一次之后由观测接管。

取 90 而不是 60：2026-11-27 那次**完全没有等待**，行情首轮就挂了 3680/5030。
60 秒能不能压住没有实测过，而赌错的代价是那一周多花二三十分钟靠冷却重试硬补；
多等 30 秒的代价可以忽略。学习者会自己往下摸，所以这个初值宁可偏保守。"""

MIN_GAP = 20.0
"""下限。再短就等于没等——上游窗口按分钟计，低于 20 秒基本无意义。"""

MAX_GAP = 180.0
"""上限。3 分钟还压不住说明不是「等得不够」，而是别的问题（配额被别处吃掉、
或上游整体降级），继续加长只会让每周那次跑得更慢。"""

STEP_UP = 30.0
STEP_DOWN = 15.0
"""上调比下调步子大：宁可多等 30 秒，也不要再出现上千只失败后靠重试硬补。"""

UP_RATIO = 0.05
"""首轮失败率高于 5% 判「这条路挤爆了」→ 加长。"""

DOWN_RATIO = 0.01
"""低于 1% 判「窗口够用」→ 缩回。留这段死区是因为总有一小撮票本来就没行情
（停牌、退市整理、上游缺字段），它们不该被算成「等得不够」。"""

MIN_SAMPLE = 200
"""本次真去取数的只数少于这个数就不学：12 只里挂 1 只就是 8%，
样本太小会把它误判成限频，一路把等待加上去。"""

# 为什么 `bad` 只升不降（曾经写过「连续干净 4 次就放宽下界」，删了）：
# 放宽等于**主动再去试一次已知不够的等待值**，代价是那一周重演上千只失败、
# 靠冷却重试硬补（实测多花二三十分钟）；收益只是每周少等十几秒。
# 这笔账明显不划算。真要把下界调回去，删掉 `state/stage_gap.json` 或设
# `STOCKHUB_STAGE_GAP_S` 都行——那是「我知道条件变了」的人工判断，
# 比让程序定期盲试更靠谱。


@dataclass
class Last:
    """上一次运行在行情阶段的观测——只用于解释「凭什么调」。"""

    date: str = ""
    tried: int = 0
    """本次真去取数的只数（命中缓存的不算，它们没发请求）。"""
    failed: int = 0
    gap_used: float = 0.0
    action: str = "hold"
    next_gap: float = 0.0
    note: str = ""
    """补充说明（比如「再缩就回到 60s，那档已知不够」）。"""

    def ratio(self) -> float:
        return (self.failed / self.tried) if self.tried else 0.0


@dataclass
class State:
    gap: float = DEFAULT
    bad: float | None = None
    """**已知不够**的最大等待值。缩时长时不许缩到这一档或以下——
    否则会变成「降下去、又失败、再升回来」的来回振荡（第一版实测就是这样）。
    只升不降，理由见 `MIN_SAMPLE` 下面那段。"""
    last: Last = field(default_factory=Last)

    # ---- 落盘 ----

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()                 # 没有/坏了都从默认值起，不值得报错
        st = cls()
        try:
            st.gap = float(raw.get("gap", DEFAULT))
        except (TypeError, ValueError):
            st.gap = DEFAULT
        bad = raw.get("bad")
        try:
            st.bad = float(bad) if bad is not None else None
        except (TypeError, ValueError):
            st.bad = None
        last = raw.get("last") or {}
        known = {f.name for f in Last.__dataclass_fields__.values()}
        st.last = Last(**{k: v for k, v in last.items() if k in known})
        st.gap = clamp(st.gap)
        return st

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(path)            # 原子替换：半截 JSON 会让下次读回默认值
        except OSError as e:             # pragma: no cover - 磁盘满/只读
            log.warning("阶段等待状态写不进去（%s）：下次仍用默认值", e)


def clamp(gap: float) -> float:
    return max(MIN_GAP, min(MAX_GAP, float(gap)))


def observe(state: State, *, tried: int, failed: int, date: str = "",
            gap_used: float | None = None) -> Last:
    """拿一次行情首轮的结果更新等待时长，返回本次观测（同时写进 `state.last`）。

    `gap_used` 是本次实际等的秒数；`None` 表示本次没等（比如阶段 4 全部命中缓存），
    这类运行不该参与学习——没等也就谈不上「等得够不够」。
    """
    lap = Last(date=date, tried=int(tried), failed=int(failed),
               gap_used=float(state.gap if gap_used is None else gap_used))
    if gap_used is None or tried < MIN_SAMPLE:
        lap.action, lap.next_gap = "skip", state.gap
        state.last = lap
        return lap

    ratio = lap.ratio()
    if ratio > UP_RATIO:
        # 这一档不够。记下来，后面缩时长时不许再缩回这里。
        state.bad = max(state.bad or 0.0, lap.gap_used)
        lap.action = "up"
        lap.next_gap = clamp(lap.gap_used + STEP_UP)
        if lap.next_gap <= lap.gap_used:
            # 已经在上限：再加长无从谈起。问题不在等待时长，别让它看起来像调过了。
            lap.action = "hold"
            lap.note = f"已在上限 {MAX_GAP:.0f}s 仍失败，不是等待时长的问题"
            log.warning("行情首轮仍失败 %d/%d，但配额等待已达上限 %.0fs："
                        "建议查上游配额，或用 STOCKHUB_MIN_INTERVAL_WESTOCK_MS "
                        "把请求节奏压下来", failed, tried, MAX_GAP)
    elif ratio < DOWN_RATIO:
        cand = lap.gap_used - STEP_DOWN
        if state.bad is not None and cand <= state.bad:
            # 再缩就回到「已知不够」的那一档，停在这里。
            lap.action = "hold"
            lap.next_gap = lap.gap_used
            lap.note = f"再缩就回到 {state.bad:.0f}s，那档已知不够"
        else:
            lap.action = "down"
            lap.next_gap = clamp(cand)
    else:
        lap.action = "hold"
        lap.next_gap = lap.gap_used

    state.gap = lap.next_gap
    state.last = lap
    return lap


def describe(lap: Last) -> str:
    """把一次观测写成一行人话，直接进日志。"""
    if lap.action == "skip":
        if lap.tried:
            return f"行情首轮 {lap.failed}/{lap.tried} 只失败，样本太小，等待时长保持不变"
        return "行情全部命中缓存，无需取数，等待时长保持不变"
    pct = lap.ratio() * 100
    head = f"行情首轮 {lap.failed}/{lap.tried} 只失败（{pct:.1f}%）"
    if lap.action == "up":
        return f"{head} → 配额等待 {lap.gap_used:.0f}s 上调到 {lap.next_gap:.0f}s"
    if lap.action == "down":
        return f"{head} → 配额等待 {lap.gap_used:.0f}s 缩到 {lap.next_gap:.0f}s"
    if lap.note:
        return f"{head} → 配额等待保持 {lap.gap_used:.0f}s（{lap.note}）"
    return f"{head} → 配额等待保持 {lap.gap_used:.0f}s"
