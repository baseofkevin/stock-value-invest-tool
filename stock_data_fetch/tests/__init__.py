"""测试包。

**为什么 `tests/` 放在包里而不是项目根**（2026-11-14 决定）：
测试与它测的代码同版本、同发布、同一次移动。放在包外时，`pip install` 之后
测试就找不到了；放在包内则可以 `python -m stock_data_fetch.tests.test_gates`。
代价是每个测试文件要多一句 `sys.path.insert`，把项目根加回来。

跑法（三个都能单独跑，也都能一起跑）：

    cd /home/kevin/MyData/stock_value_invest_tool/stock_data_fetch
    python3 -m stock_data_fetch.tests.test_gates
    python3 -m stock_data_fetch.tests.test_coverage
    python3 -m stock_data_fetch.tests.test_fetch

也可以直接 `python3 stock_data_fetch/tests/test_gates.py`——每个文件自己把
项目根塞进 `sys.path`，两种跑法等价。
"""
