"""K 线选股规则 v2.0 —— 共享数据层（stock_data_fetch）接入版。

由 `stock-kline-screen-tool`（v1.10，westock CLI 直连 + 本地 JSON 缓存）重建：
规则七条一字未动，数据全部改走共享库。与原版的口径差异见 `README.md` 的对照表。

用法：

    cd /path/to/stock-value-invest-tool
    python3 -m stock_kline_screen            # 默认基准日 = 最近交易日，K=5
    python3 -m stock_kline_screen --date 2026-11-06
"""

__version__ = "2.0.0"
