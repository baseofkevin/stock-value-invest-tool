"""fixtures 包。

`tests/fixtures/*.json` 是**离线快照**（由 `gen_fixtures.py` 生成），
测试优先读 JSON；JSON 不存在时回退到 `scenarios.py` 现造，保证测试永远能跑。
"""

import json
import os

FIXTURE_DIR = os.path.dirname(os.path.abspath(__file__))

#: 落盘的 fixture 名单（与 scenarios.SCENARIOS 的 key 对齐，另加两个不落盘的）
ON_DISK = (
    "full_40q",
    "short_12q",
    "balance_missing",
    "zero_revenue",
    "neg_profit",
    "huge_ratio",
    "flat_margin",
    "flat_margin_equal_turnover",
    "financial",
    "no_cl",
)


def load(name):
    """读一个 fixture 的 **build 入参**（{code,name,industry,income,balance}）。

    优先读 `tests/fixtures/<name>.json`；不存在则用 scenarios 现造。
    """
    path = os.path.join(FIXTURE_DIR, name + ".json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        # 落盘格式是 bundle（含 calls），这里拆成 build 入参
        if "calls" in raw:
            from cycle import fetch
            return fetch.replay(raw["calls"], raw["code"],
                                name=raw.get("name", ""), industry=raw.get("industry", ""))
        return raw

    from tests.fixtures import scenarios
    return scenarios.SCENARIOS[name]()


def exists(name):
    return os.path.exists(os.path.join(FIXTURE_DIR, name + ".json"))
