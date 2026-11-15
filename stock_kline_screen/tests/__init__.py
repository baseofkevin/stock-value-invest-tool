# 测试**不许**写真实的 results.db：有些用例会走工具的端到端路径（CLI / 批量），
# 那条路径现在会自动落库。不隔离的话每跑一次测试就往结果库里塞十几条假运行，
# 过几天再看"这只票历史结论怎么变的"全是测试数据。
import os as _os
import tempfile as _tempfile

_os.environ["STOCK_RESULTS_DB"] = _os.path.join(
    _tempfile.mkdtemp(prefix="results-test-"), "results.db")

"""stock_kline_screen 的测试（全部离线，不打上游）。"""
