"""阶段等待自适应测试：`stage_gap` 的调整规则与落盘。

跑法：cd ~/MyData/stock_value_invest_tool && python3 -m stock_kline_screen.tests.test_stage_gap

重点不是「60 秒对不对」，而是三条不变量：
1. 有界——再怎么调都落在 [MIN_GAP, MAX_GAP] 里，别把周跑拖成小时级；
2. 有死区——零星几只本来就没行情的票（停牌/退市）不该被当成限频；
3. 不乱学——样本太小、本次没等、人工指定，这三种情况都不许改等待时长。
"""

from __future__ import annotations

try:
    from . import _env  # noqa: F401  （作为包导入）
except ImportError:  # pragma: no cover - 直接跑文件
    import _env  # noqa: F401

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

from stock_kline_screen import stage_gap as G  # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def run(state, *, tried, failed, gap_used=None, date=""):
    return G.observe(state, tried=tried, failed=failed, gap_used=gap_used, date=date)


def test_direction():
    st = G.State(gap=60)
    lap = run(st, tried=5030, failed=3680, gap_used=60)
    check("73% 失败 → 加长", lap.action == "up" and st.gap == 90, f"{lap}")

    st = G.State(gap=90)
    lap = run(st, tried=5030, failed=0, gap_used=90)
    check("一只没挂 → 缩回", lap.action == "down" and st.gap == 75, f"{lap}")

    st = G.State(gap=60)
    lap = run(st, tried=5030, failed=100, gap_used=60)
    check("2% 落在死区 → 不变", lap.action == "hold" and st.gap == 60,
          f"ratio={lap.ratio():.3f} {lap}")

    # 死区上界：4.9% 仍不变，5.1% 才动。停牌票通常在 1~3%。
    st = G.State(gap=60)
    run(st, tried=1000, failed=49, gap_used=60)
    check("4.9% 仍在死区", st.gap == 60, f"gap={st.gap}")
    st = G.State(gap=60)
    run(st, tried=1000, failed=51, gap_used=60)
    check("5.1% 越过上界 → 加长", st.gap == 90, f"gap={st.gap}")


def test_no_oscillation():
    """第一版的缺陷：一干净就往回缩，缩到底又失败，在两端来回跳。"""
    st = G.State()
    run(st, tried=5030, failed=3680, gap_used=60)          # 60 不够
    check("60s 判不够 → 记下已知不够的值", st.bad == 60 and st.gap == 90, f"{st}")

    run(st, tried=5030, failed=0, gap_used=90)             # 90 干净
    check("90s 干净 → 缩到 75", st.gap == 75, f"gap={st.gap}")

    lap = run(st, tried=5030, failed=0, gap_used=75)       # 75 干净，但再缩就是 60
    check("75s 干净 → 停在 75，不退到已知不够的 60",
          st.gap == 75 and lap.action == "hold", f"gap={st.gap} action={lap.action}")
    check("停下时说明理由", "已知不够" in lap.note, lap.note)

    # 收敛：干净运行不再「每次都往下挪一格」，而是停在 75——而且不会过几周又
    # 自己跑回去重试 60（那等于定期主动失败一次）。
    for _ in range(8):
        run(st, tried=5030, failed=0, gap_used=st.gap)
    check("连续 8 次干净仍稳在 75（不振荡、不回头试 60）",
          st.gap == 75 and st.bad == 60, f"gap={st.gap} bad={st.bad}")

    # 到上限还失败：不许说「上调」，要说清是别的问题。
    st = G.State(gap=G.MAX_GAP)
    lap = run(st, tried=5030, failed=3000, gap_used=G.MAX_GAP)
    check("已在上限仍失败 → 标记为保持并说明原因",
          lap.action == "hold" and "上限" in lap.note, f"{lap}")
    check("上限那一档不再自诩「已上调」", "上调" not in G.describe(lap), G.describe(lap))


def test_bounds():
    st = G.State(gap=G.MAX_GAP - 5)
    run(st, tried=5000, failed=5000, gap_used=st.gap)
    check("上调不越上限", st.gap == G.MAX_GAP, f"gap={st.gap}")

    st = G.State(gap=G.MIN_GAP + 5)
    run(st, tried=5000, failed=0, gap_used=st.gap)
    check("下调不破下限", st.gap == G.MIN_GAP, f"gap={st.gap}")

    # 收敛：连续干净运行一路缩到下限就停住，不会变负。
    st = G.State()
    for _ in range(20):
        run(st, tried=5000, failed=0, gap_used=st.gap)
    check("连续 20 次干净后停在下限", st.gap == G.MIN_GAP, f"gap={st.gap}")


def test_no_learn():
    st = G.State(gap=60)
    lap = run(st, tried=15, failed=3, gap_used=60)
    check("样本 15 只（<200）→ 不学", lap.action == "skip" and st.gap == 60,
          f"{lap}")

    st = G.State(gap=60)
    lap = run(st, tried=5030, failed=2000, gap_used=None)
    check("本次没等 → 不学", lap.action == "skip" and st.gap == 60, f"{lap}")

    st = G.State(gap=60)
    lap = run(st, tried=0, failed=0, gap_used=60)
    check("全部命中缓存 → 不学", lap.action == "skip" and st.gap == 60, f"{lap}")


def test_persist():
    d = Path(tempfile.mkdtemp(prefix="stage-gap-"))
    p = d / "stage_gap.json"

    check("文件不存在时回默认值", G.State.load(p).gap == G.DEFAULT)

    st = G.State(gap=90)
    run(st, tried=5030, failed=3680, date="2026-11-27", gap_used=90)
    st.save(p)
    back = G.State.load(p)
    check("落盘后读回 gap", back.gap == 120, f"gap={back.gap}")
    check("落盘后读回上次观测", back.last.tried == 5030 and back.last.date == "2026-11-27",
          f"{back.last}")

    p.write_text("{ 半截", encoding="utf-8")
    check("坏 JSON 回默认值，不抛", G.State.load(p).gap == G.DEFAULT)

    p.write_text('{"gap": 9999}', encoding="utf-8")
    check("文件里的越界值读回时夹住", G.State.load(p).gap == G.MAX_GAP,
          f"gap={G.State.load(p).gap}")


def test_describe():
    st = G.State()
    lap = run(st, tried=5030, failed=3680, gap_used=60)
    s = G.describe(lap)
    check("加长的说明带「上调」和失败数", "上调" in s and "3680" in s, s)

    st = G.State()
    lap = run(st, tried=5030, failed=0, gap_used=60)
    check("缩回的说明带「缩到」", "缩到" in G.describe(lap), G.describe(lap))


def main() -> int:
    test_direction()
    test_no_oscillation()
    test_bounds()
    test_no_learn()
    test_persist()
    test_describe()
    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
