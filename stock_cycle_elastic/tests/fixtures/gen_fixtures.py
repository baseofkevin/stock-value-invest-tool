#!/usr/bin/env python3
"""生成 `tests/fixtures/*.json` 离线快照。

用法（在项目根目录）：
    python3 -m tests.fixtures.gen_fixtures

产出的是 **--bundle 格式**（含 calls.data_finance / calls.data_profile），
这样测试既可以直接喂 build()，也可以通过 fetch.replay() 走一遍解析路径。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.fixtures import ON_DISK, FIXTURE_DIR, make  # noqa: E402
from tests.fixtures import scenarios  # noqa: E402


def main():
    for name in ON_DISK:
        stock = scenarios.SCENARIOS[name]()
        payload = make.bundle(
            stock["code"], stock["income"], stock["balance"],
            name=stock["name"], industry=stock["industry"],
        )
        path = os.path.join(FIXTURE_DIR, name + ".json")
        make.dump(payload, path)
        n_income = len(stock["income"])
        n_bal = len(stock["balance"])
        size = os.path.getsize(path)
        print(f"{name:28s} income={n_income:3d} balance={n_bal:3d} {size/1024:7.1f}KB")
    print(f"\n共 {len(ON_DISK)} 个 fixture → {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
