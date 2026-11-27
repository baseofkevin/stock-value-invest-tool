"""测试共用的环境隔离：把结果库指到临时目录。

为什么单独一个模块：这个设置原来只写在 `tests/__init__.py` 里，而它**只有
把 tests 当包导入时才执行**（`python3 -m stock_kline_screen.tests`）。
直接跑某个文件（`python3 stock_kline_screen/tests/test_pipeline.py`）会绕过
`__init__.py`，端到端用例就把假运行写进真实的 `results.db` 里——2026-11-15
就这么污染过 5 条（好票/冷好票、基准日 2026-10-15）。每个用例文件都导入
这个模块，两种跑法就都被隔离了。
"""

import os as _os
import tempfile as _tempfile

_os.environ.setdefault(
    "STOCK_RESULTS_DB",
    _os.path.join(_tempfile.mkdtemp(prefix="results-test-"), "results.db"))

# 阶段间配额恢复等待（`pipeline.STAGE_GAP_S`）在测试里必须是 0。
# `test_pipeline` 的 FakeHub 会让 `ensure_bulk.ok` 非空，不关掉就要真睡 60 秒，
# 整套测试会直接跑成 SIGKILL（2026-11-27 实测）。
_os.environ.setdefault("STOCKHUB_STAGE_GAP_S", "0")
