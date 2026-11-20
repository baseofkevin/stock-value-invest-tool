"""`python3 -m stock_extreme_point.tests` —— 跑本包全部测试。

现在只有一个测试文件，仍然保留这个入口：以后加文件时不必改 `run.py`、
也不必让人记住"哪些文件算测试"。
"""

from __future__ import annotations

import importlib
import sys

MODULES = ("test_extreme_point",)


def main() -> int:
    bad: list[str] = []
    for name in MODULES:
        mod = importlib.import_module(f"stock_extreme_point.tests.{name}")
        rc = mod.main() if hasattr(mod, "main") else 0
        if rc != 0:
            bad.append(name)
    if bad:
        print(f"\033[31m{len(MODULES)} 个测试文件中 {len(bad)} 个失败：{', '.join(bad)}\033[0m")
        return 1
    print(f"\033[32m{len(MODULES)} 个测试文件全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
