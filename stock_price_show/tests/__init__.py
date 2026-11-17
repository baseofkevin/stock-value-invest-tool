"""测试包。跑法（在仓库根）：

    python3 -m stock_price_show.tests            # 全部
    python3 -m stock_price_show.tests.test_price_show

**全部离线**：上游抓取整个换掉（`source.fetch_day` 的 `get=` 注入 / monkeypatch），
一个真实网络请求都不打。
"""

__all__: list[str] = []
