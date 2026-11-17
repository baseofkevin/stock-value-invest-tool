"""`python -m stock_data_fetch.tests` —— 跑全部测试，返回非 0 表示有失败。

为什么要有这一个：四个项目重建之后，"全面测试"会变成一个反复出现的动作。
每处都手敲四条命令，早晚会漏一条；漏掉的那条正好是挂掉的那条。

**每个测试文件必须提供 `main() -> int`**：聚合器只认返回值。没有 `main()`
的文件一律按失败计（见下面 `run_one` 的说明）——这条是 2026-11-17 补的，
在那之前"没有 main 就当通过"，`test_results` 的 22 项因此空转了两天。
"""

from __future__ import annotations

import importlib
import sys

MODULES = ("test_gates", "test_coverage", "test_fetch", "test_factor", "test_westock",
           "test_doctor", "test_probe", "test_repair", "test_scan", "test_tencent",
           "test_annotation", "test_bulk", "test_results")


def run_one(name: str) -> int:
    """跑一个测试文件，返回它的退出码。

    **没有 `main()` 按失败计**（原写法 `mod.main() if hasattr(...) else (0 if True else 1)`
    里那个 `(0 if True else 1)` 恒为 0，等于"没实现也算通过"）。静默通过比失败更坏：
    失败会有人去修，空转只会让人以为覆盖到了。
    """
    mod = importlib.import_module(f"stock_data_fetch.tests.{name}")
    fn = getattr(mod, "main", None)
    if not callable(fn):
        print(f"  [FAIL] {name} 没有 main() -> int，聚合器无法判定它跑没跑")
        return 1
    return fn()


def main() -> int:
    bad: list[str] = []
    for name in MODULES:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        if run_one(name) != 0:
            bad.append(name)

    print(f"\n{'=' * 60}")
    if bad:
        print(f"\033[31m{len(MODULES)} 个测试文件中 {len(bad)} 个失败：{', '.join(bad)}\033[0m")
        return 1
    print(f"\033[32m{len(MODULES)} 个测试文件全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
