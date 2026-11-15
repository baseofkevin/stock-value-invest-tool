"""测试公共设施：路径注入、配置替身、序列构造小工具。

所有测试文件的第一件事都是 `import tests.helpers` —— 它负责把项目根目录塞进
`sys.path`，这样 `python3 -m unittest discover -s tests` 和
`python3 tests/test_xxx.py` 两种跑法都能 import 到 `config` / `cycle`。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: E402
from cycle import build, decide, metrics  # noqa: E402


class Cfg:
    """配置替身：默认拷贝 config 的全部大写常量，用例可逐个覆盖。

    为什么不用改 config 模块本身：
        模块级常量一旦被某个用例改掉，后面的用例全受影响（unittest 不保证顺序隔离）。
        用替身对象，每个用例自带一份，互不污染。
    """

    def __init__(self, **overrides):
        for name in dir(config):
            if name.isupper():
                setattr(self, name, getattr(config, name))
        for key, value in overrides.items():
            if not hasattr(self, key):
                raise AttributeError(f"config 里没有常量 {key}")
            setattr(self, key, value)


def quarters_of(gross_margins=None, revenues=None, op_profits=None,
                turnover=None, dates=None, contract_liab=None):
    """直接造 build 风格的 quarters（跳过两表对齐，专供 metrics 用例）。

    参数都是等长列表；都不给时返回空列表。
    """
    arrays = [a for a in (gross_margins, revenues, op_profits, turnover, dates) if a]
    n = len(arrays[0]) if arrays else 0

    def pick(seq, i):
        return None if seq is None else seq[i]

    out = []
    for i in range(n):
        date = dates[i] if dates else f"20{10 + i // 4:02d}-{(i % 4) * 3 + 3:02d}-30"
        out.append({
            "date": date,
            "revenue_q": pick(revenues, i),
            "cost_q": None,
            "op_profit_q": pick(op_profits, i),
            "fixed_asset": None,
            "cip": None,
            "contract_liab": pick(contract_liab, i),
            "gross_margin": pick(gross_margins, i),
            "capacity_turnover": pick(turnover, i),
        })
    return out


def metrics_of(gm_std=None, ratio=None, reliable=None, samples=0,
               pct=None, current=None, pos_samples=0):
    """造 metrics.compute_all 的替身，供 decide 用例。"""
    return {
        "gm_std": gm_std,
        "elasticity": {"ratio": ratio, "reliable": reliable, "samples": samples},
        "position": {"pct": pct, "current": current, "samples": pos_samples},
    }


def pipeline(stock, cfg=None):
    """build 的产物 → (metrics, decision)，一次走完。"""
    cfg = cfg or config
    m = metrics.compute_all(stock["quarters"], cfg)
    d = decide.decide(stock["quarters"], m, stock.get("industry", ""), cfg)
    return m, d


def build_from(stock, cfg=None):
    """fixture 原始数据 → build 产物。"""
    return build.build(stock)


#: 断言用的常量集合（与 README.md「判定阈值」逐字一致）
#:
#: 判断① 有 4 个：绝对通道 GM_STD_CYCLE + 相对通道 GM_CV_MIN
#: 与两个守卫（GM_MEDIAN_MIN / GM_STD_MIN_FOR_CV）。守卫也是数字判定，
#: 改动它们同样会改变标签，所以一并纳入这份契约。
FIVE_THRESHOLDS = {
    "GM_STD_CYCLE": 5.0,
    "GM_CV_MIN": 20.0,
    "GM_MEDIAN_MIN": 5.0,
    "GM_STD_MIN_FOR_CV": 2.0,
    "ELASTIC_RATIO_MIN": 2.0,
    "POSITION_LOW_PCT": 40.0,
    "POSITION_HIGH_PCT": 60.0,
    "REV_MEDIAN_STABLE_MIN": 3.0,
    "ELASTIC_ZERO_CROSS_MAX": 0.3333,
    "CAPACITY_EXPAND_GROWTH": 15.0,
    "CAPACITY_SHRINK_GROWTH": -5.0,
}
