"""共享取数层（按需、单文件、无 daemon）。包名 `stock_data_fetch`，项目名 `stock_data_fetch`。

四个工具的唯一数据入口。用法只有四行：

    from stock_data_fetch import preflight, want
    hub = preflight()
    hub.ensure("kline", "sh600519", want.kline(hub.cal, hub.target, bars=300, min_bars=250))
    qfq = hub.adjusted("sh600519")          # 前复权价，纯计算不取数

当前进度（`CONTRACT` §16）：
  第 1 步 ✅ errors / config / db / calendar / hub（六道闸）
  第 2 步 ✅ need（need→区间换算）/ coverage（hit / merge / holes / verdict）
  第 3 步 ✅ throttle / sources / router / api.ensure（kline）
  第 4 步 ✅ factor（因子 / qfq / 幂等 / 缓存）
  第 5 步 ✅ sources/westock、eastmoney 的 valuation/snapshot、quarter、七个域的写库
  第 5b 步 ✅ 真上游冒烟（10 只 × 6 域）+ build_calendar（12 年交易日历）
  第 6 步 ✅ doctor（只读体检）/ probe（跨源对账）/ repair（分红漏记修复）
  下一步   四个工具接入，按 kline → quality → cycle → judge 逐个重建
"""

from __future__ import annotations

from .config import Config
from .errors import (EmptyResult, FactorError, HoleUnreachable, HubNotReady,
                     InsufficientDepth, MarketOpenError, PartialData,
                     SchemaMismatch, SourceUnavailable, StockfetchError,
                     ThrottleTimeout)
from .hub import Hub, preflight
from .need import (FP, MIN_VALUATION_WEEKS, Need, fingerprint,
                   latest_report_period, want)

__version__ = "0.1.0"

__all__ = [
    "preflight", "Hub", "Config",
    "want", "Need", "FP", "fingerprint", "latest_report_period",
    "MIN_VALUATION_WEEKS",
    "StockfetchError", "MarketOpenError", "HubNotReady", "SchemaMismatch",
    "InsufficientDepth", "PartialData", "EmptyResult", "ThrottleTimeout",
    "FactorError", "HoleUnreachable", "SourceUnavailable",
]
