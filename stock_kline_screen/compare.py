"""把新程序的产物与原版（v1.10）同一基准日的结果逐行对照。

用法：

    python3 -m stock_kline_screen.compare <基准日>

两边读的是：
- 新版：`stock-value-invest-tool/output/kline-screen/选股_<S>.csv`
- 原版：`~/MyData/stock-kline-screen-tool/csv_output/选股_<S>.csv`

只比**共有列**（原版 16 列里有「二级行业」，新版没有；新版没有净流入列），
其余逐格比较并给出差异原因分类。不通过时退出码 1。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from stock_data_fetch.config import HUB_ROOT
from stock_kline_screen.params import OUT_DIR_NAME

# 新版路径从共享库配置推导，不写死 `~/MyData/xxx`：
# 2026-11-15 项目从 stock-data-hub 改名成 stock-value-invest-tool，写死的路径当场失效。
# 目录名取 `params.OUT_DIR_NAME` 而不是再抄一遍字符串：
# 两边各写一份，改目录名时必然只改对一半。
# 这里与 `pipeline._out_dir()` 用同一个口径（state_dir 的父目录下 output/）。
NEW_DIR = HUB_ROOT / "output" / OUT_DIR_NAME
OLD_DIR = Path.home() / "MyData" / "stock-kline-screen-tool" / "csv_output"

# 共有列（列名两版一致的那些）
SHARED = ["代码", "名称", "一级行业", "启动日", "距启动交易日", "启动日量能倍数",
          "启动日涨幅%", "距MA20%", "距MA60%", "毛利率改善", "20日换手%",
          "池内换手分位", "分位星级", "最新收盘价", "总市值（亿）"]


def read(path: Path) -> dict[str, dict]:
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r["代码"]: r for r in csv.DictReader(f)}


def num(v: str):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def close(a, b, tol: float) -> bool:
    x, y = num(a), num(b)
    if x is None or y is None:
        return (a or "") == (b or "")
    return abs(x - y) <= tol


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit(__doc__)
    S = argv[1]
    new = read(NEW_DIR / f"选股_{S}.csv")
    old = read(OLD_DIR / f"选股_{S}.csv")

    only_new = sorted(set(new) - set(old))
    only_old = sorted(set(old) - set(new))
    both = sorted(set(new) & set(old))

    print(f"基准日 {S}")
    print(f"  原版 v1.10 : {len(old)} 只")
    print(f"  新版 v2.0  : {len(new)} 只")
    print(f"  共有       : {len(both)} 只")
    print(f"  只在原版   : {len(only_old)} 只  {only_old[:10]}")
    print(f"  只在新版   : {len(only_new)} 只  {only_new[:10]}")

    print(f"\n共有股票的逐列差异（容差：价格/涨幅/距MA 0.02，换手 0.01，市值 0.5 亿）：")
    tol = {"最新收盘价": 0.02, "启动日涨幅%": 0.02, "距MA20%": 0.02,
           "距MA60%": 0.02, "启动日量能倍数": 0.02, "20日换手%": 0.01,
           "总市值（亿）": 0.5}
    tally: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for col in SHARED:
        bad = []
        for code in both:
            a, b = new[code].get(col, ""), old[code].get(col, "")
            if col in tol:
                ok = close(a, b, tol[col])
            else:
                ok = (a or "").strip() == (b or "").strip()
            if not ok:
                bad.append(f"{code}: 新={a!r} 旧={b!r}")
        tally[col] = len(bad)
        if bad:
            samples[col] = bad[:5]

    width = max(len(c) for c in SHARED)
    for col in SHARED:
        n = tally[col]
        flag = "✅" if n == 0 else "❌"
        print(f"  {flag} {col:<{width}}  差异 {n}/{len(both)}")
        for s in samples.get(col, [])[:3]:
            print(f"        {s}")

    print("\n行业口径差异检查（沪深两市一级行业应完全一致）：")
    ind_bad = [f"{c}: 新={new[c].get('一级行业')!r} 旧={old[c].get('一级行业')!r}"
               for c in both
               if (new[c].get("一级行业") or "").strip()
               != (old[c].get("一级行业") or "").strip()]
    print(f"  {'✅ 无差异' if not ind_bad else '❌ ' + str(len(ind_bad)) + ' 只不同'}")
    for s in ind_bad[:8]:
        print(f"        {s}")

    # 名单集合差异是「取数/口径」而非「规则」的差异，只展开口径一致的子集判定通过与否
    ok = (len(only_old) == 0 and len(only_new) == 0
          and not ind_bad and all(v == 0 for v in tally.values()))
    print(f"\n{'通过：与原版逐格一致' if ok else '未通过：上面标 ❌ 的项需要解释'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
