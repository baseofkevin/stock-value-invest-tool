"""`python3 -m stock_judge_top.tests` —— 跑全部测试，返回非 0 表示有失败。

与 `stock_kline_screen.tests` 同款入口：全面测试会反复做，手敲多条命令
早晚漏一条，而漏掉的正好是挂掉的那条。
"""

from __future__ import annotations

import importlib
import sys

MODULES = ("test_indicators", "test_finance", "test_judgement",
           "test_views", "test_render", "test_evaluate",
           "test_judge", "test_hubdata")


def main() -> int:
    bad: list[str] = []
    for name in MODULES:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}", flush=True)
        mod = importlib.import_module(f"stock_judge_top.tests.{name}")
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        if not result.wasSuccessful():
            bad.append(name)

    print(f"\n{'=' * 60}")
    if bad:
        print(f"\033[31m{len(MODULES)} 个测试文件中 {len(bad)} 个失败：{', '.join(bad)}\033[0m")
        return 1
    print(f"\033[32m{len(MODULES)} 个测试文件全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
