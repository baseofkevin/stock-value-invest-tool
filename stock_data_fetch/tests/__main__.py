"""`python -m stock_data_fetch.tests` —— 跑全部测试，返回非 0 表示有失败。

为什么要有这一个：四个项目重建之后，"全面测试"会变成一个反复出现的动作。
每处都手敲四条命令，早晚会漏一条；漏掉的那条正好是挂掉的那条。
"""

from __future__ import annotations

import importlib
import sys

MODULES = ("test_gates", "test_coverage", "test_fetch", "test_factor", "test_westock",
           "test_doctor", "test_probe", "test_repair", "test_scan", "test_tencent",
           "test_annotation", "test_bulk")


def main() -> int:
    bad: list[str] = []
    total = 0
    for name in MODULES:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        mod = importlib.import_module(f"stock_data_fetch.tests.{name}")
        rc = mod.main() if hasattr(mod, "main") else (0 if True else 1)
        if rc != 0:
            bad.append(name)
        total += 1

    print(f"\n{'=' * 60}")
    if bad:
        print(f"\033[31m{total} 个测试文件中 {len(bad)} 个失败：{', '.join(bad)}\033[0m")
        return 1
    print(f"\033[32m{total} 个测试文件全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
