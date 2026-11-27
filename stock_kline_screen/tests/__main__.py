"""`python -m stock_kline_screen.tests` —— 跑全部测试，返回非 0 表示有失败。

和 `stock_data_fetch.tests` 同一个理由：重建四个工具时"全面测试"会反复做，
手敲四条命令早晚漏一条，而漏掉的正好是挂掉的那条。
"""

from __future__ import annotations

import importlib
import sys

MODULES = ("test_rules", "test_turnover", "test_turnaround", "test_stage_gap",
           "test_pipeline", "test_cli")


def main() -> int:
    bad: list[str] = []
    for name in MODULES:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        mod = importlib.import_module(f"stock_kline_screen.tests.{name}")
        if (mod.main() if hasattr(mod, "main") else 0) != 0:
            bad.append(name)

    print(f"\n{'=' * 60}")
    if bad:
        print(f"\033[31m{len(MODULES)} 个测试文件中 {len(bad)} 个失败：{', '.join(bad)}\033[0m")
        return 1
    print(f"\033[32m{len(MODULES)} 个测试文件全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
