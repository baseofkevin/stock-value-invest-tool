"""换手层测试：`turnover20` / `annotate_turnover` / `apply_turnover`。

跑法：cd ~/MyData/stock-value-invest-tool && python3 -m stock_kline_screen.tests.test_turnover

相对原版（test_screen.py 的换手节）删掉了**单位换算整节**：共享库的
`kline.volume` 已是股，`infer_unit` / `unit_from_prefix` 那套「科创板是股、
其余是手」的两可补丁在新版里不存在，也就无从测起。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

from stock_kline_screen.turnover import (apply_turnover, annotate_turnover,  # noqa: E402
                                         turnover20)

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


def bars(n=25, amount=1e8, *, no_amount=False):
    """n 根 bar，volume 单位是股、amount 是元。"""
    out = []
    for i in range(n):
        out.append({"date": f"2026-10-{i+1:02d}", "close": 10.0, "volume": 1e7,
                    "amount": None if no_amount else amount})
    return out


def test_turnover20() -> None:
    print("\nturnover20")
    cal20 = [f"2026-10-{i+1:02d}" for i in range(20)]
    t, n = turnover20("sh600000", bars(25), cal20, 100e8)
    check("正常计算：日均 1 亿 / 100 亿 = 1%", t == 1.0, str(t))
    check("覆盖 20 天", n == 20, str(n))

    t, n = turnover20("sh600000", bars(25, no_amount=True),
                      [f"2026-10-{i+1:02d}" for i in range(25)], 100e8)
    check("amount 缺失 → close×volume 兜底（10×1e7=1亿）", t == 1.0, str(t))

    # 覆盖不足
    t, n = turnover20("sh600000", bars(10), [f"2026-10-{i+1:02d}" for i in range(25)],
                      100e8)
    check("覆盖 < 15 天 → (None, 10)", t is None and n == 10, f"{t} {n}")

    t, n = turnover20("sh600000", bars(25), [f"2026-10-{i+1:02d}" for i in range(25)],
                      0)
    check("流通市值为 0 → None", t is None, str(t))
    check("空 bars → (None, 0)", turnover20("x", [], [], 1e9) == (None, 0))

    # 窗口内停牌（cal 有 25 天、bars 只有 20 根，其中 5 天缺失按停牌跳过）
    t, n = turnover20("sh600000", bars(20), [f"2026-10-{i+1:02d}" for i in range(25)],
                      100e8)
    check("窗口内停牌 5 天：覆盖 20 天照样能算", t == 1.0 and n == 20, f"{t} {n}")


def test_annotate() -> None:
    print("\nannotate_turnover（池内分位）")
    cal20 = [f"2026-10-{i+1:02d}" for i in range(20)]
    km = {"a": bars(25, amount=1e8), "b": bars(25, amount=2e8),
          "c": bars(25, amount=5e8), "d": bars(25, amount=5e7)}
    caps = {"a": 100e8, "b": 100e8, "c": 100e8, "d": 100e8}
    hits = [{"code": "a"}, {"code": "c"}, {"code": "d"}]
    pool_t = annotate_turnover(hits, list(km), km, caps, cal20)
    check("池内 4 只都能算", len(pool_t) == 4, str(pool_t))
    by_code = {x["code"]: x for x in hits}
    check("换手值正确（a=1.0/c=5.0/d=0.5）",
          by_code["a"]["turn20"] == 1.0 and by_code["c"]["turn20"] == 5.0
          and by_code["d"]["turn20"] == 0.5, str({k: v['turn20'] for k, v in by_code.items()}))
    # 分位 = 池内严格小于该值的比例。池内值 [0.5,1,2,5]：
    check("a 的分位 = 1/4 = 25", by_code["a"]["turn20_rank"] == 25.0,
          str(by_code["a"]["turn20_rank"]))
    check("d（最冷）分位 = 0", by_code["d"]["turn20_rank"] == 0.0,
          str(by_code["d"]["turn20_rank"]))
    check("c（最热）分位 = 75", by_code["c"]["turn20_rank"] == 75.0,
          str(by_code["c"]["turn20_rank"]))
    # 数据不足的票：覆盖 <15 → turn20 None、分位 None，绝不静默丢弃
    hits2 = [{"code": "short"}, {"code": "a"}]
    km2 = dict(km, short=bars(10))
    caps2 = dict(caps, short=100e8)
    annotate_turnover(hits2, list(km2), km2, caps2, cal20)
    check("数据不足 → turn20=None 且保留在名单里",
          hits2[0]["turn20"] is None and hits2[0]["turn20_rank"] is None)


def mk_pool(sizes, *, n_bars=25):
    """`{code: (bars, cap)}` 的池：`sizes` 给每只的日均成交额（亿）。"""
    km, caps = {}, {}
    for code, amt in sizes.items():
        km[code] = bars(n_bars, amount=amt * 1e8)
        caps[code] = 100e8                    # 统一 100 亿流通市值
    return km, caps


def test_apply() -> None:
    print("\napply_turnover（默认标注 / 硬过滤 / 自适应）")
    cal20 = [f"2026-10-{i+1:02d}" for i in range(20)]
    sizes = {f"c{i:02d}": 1 + i for i in range(30)}     # 1..30 亿，30 只池
    km, caps = mk_pool(sizes)
    hits = [{"code": c} for c in sizes]
    annotate_turnover(hits, list(km), km, caps, cal20)

    kept, meta, dropped = apply_turnover(hits, list(km), km, caps, cal20, 0.0, 5, 30)
    check("pct=0：只标注不剔除", len(kept) == 30 and not dropped, str(len(kept)))
    check("pct=0 的 meta 说明是排序模式", meta["启用"] is False and "只参与排序" in meta["原因"],
          str(meta))

    kept, meta, dropped = apply_turnover(hits, list(km), km, caps, cal20, 0.4, 5, 30)
    check("pct=0.4：留池内最低 40% 段（cut 取整后 13 只）", len(kept) == 13,
          f"kept={len(kept)}")
    check("硬过滤的 meta 带剔除线", meta["启用"] is True and "剔除线%" in meta, str(meta))

    # 自适应放宽：只给 3 只池、min_n=5 → 放宽到 60%
    small = {"a": 1, "b": 2, "c": 3}
    km2, caps2 = mk_pool(small)
    hits2 = [{"code": c} for c in small]
    annotate_turnover(hits2, list(km2), km2, caps2, cal20)
    kept2, meta2, _ = apply_turnover(hits2, list(km2), km2, caps2, cal20, 0.4, 5, 30)
    check("样本 <20 只 → 分位不稳定，退化为只标注",
          meta2["启用"] is False and "分位不稳定" in meta2["原因"], str(meta2))


def main() -> int:
    test_turnover20()
    test_annotate()
    test_apply()
    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
