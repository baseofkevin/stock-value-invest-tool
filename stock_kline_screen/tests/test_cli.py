"""CLI 退出码：数据层错误必须退 1，不能占用 3（3 = 选股 0 只）。

2026-11-20 的故障里，取数失败被 `cli.py` 映射成 3，`run.py` 于是把它
当成"选股 0 只"，汇总还写成了"其余步骤完成"。这个测试钉住映射，
避免下次再混。
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from stock_data_fetch.errors import HubNotReady  # noqa: E402
from stock_kline_screen import cli  # noqa: E402


class _Hub:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CliExitCodeTest(unittest.TestCase):
    def test_preflight_data_error_is_1(self) -> None:
        with mock.patch.object(cli, "preflight",
                               side_effect=HubNotReady("库打不开")):
            with redirect_stderr(io.StringIO()) as err:
                rc = cli.main(["--date", "2026-11-20"])
        self.assertEqual(rc, 1)
        self.assertIn("库打不开", err.getvalue())

    def test_run_hub_error_is_1(self) -> None:
        with mock.patch.object(cli, "preflight", return_value=_Hub()), \
                mock.patch.object(cli, "run",
                                  side_effect=HubNotReady("上游超时")):
            with redirect_stderr(io.StringIO()) as err:
                rc = cli.main(["--date", "2026-11-20"])
        self.assertEqual(rc, 1)
        self.assertIn("错误（数据层）", err.getvalue())


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
