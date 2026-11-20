"""路径、超时、UA。**可调数值只在这里**（阈值在 `rules.py`）。

路径约定与 `stock_data_fetch` / `stock_extreme_point` 共用同一套环境变量名，
是为了"拿一份副本库调试"这一招在本仓库里处处一样。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 仓库根（本包的上一级）。
ROOT = Path(__file__).resolve().parents[1]

ENV_SHARED_DB = "STOCK_SHARED_DB"
ENV_RESULTS_DB = "STOCK_RESULTS_DB"

#: 单次 HTTP 超时（秒）。四个源都很快，20 秒还没回来说明网络有问题。
TIMEOUT = 20

#: 中证指数那个接口一次回 970KB，20 秒不够（实测偶发 30s+），单独放宽。
PERF_TIMEOUT = 90

#: 单个源最多试几次。第二次是给"上游偶发断连"的，不是给限流的。
RETRY = 2

#: 上游都认 UA。默认的 `Python-urllib/3.13` 在乐咕上直接 403。
UA = "Mozilla/5.0 (compatible; stock_market_trend; +local)"

#: 10Y 国债只取近十年——ERP 分位窗口就是 10 年，更早的用不上。
BOND_START = "2015-01-01"


def shared_db(explicit: str | Path | None = None) -> Path:
    """原件层。本项目里它已经放着 kline / valuation / quote，这一层再加三张表。"""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_SHARED_DB)
    return Path(env).expanduser() if env else ROOT / "shared.db"


def results_db(explicit: str | Path | None = None) -> Path:
    """结果层。看板只读这一个库。"""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_RESULTS_DB)
    return Path(env).expanduser() if env else ROOT / "results.db"
