"""路径、抓取参数、闸门时间。**可调数值只在这里**，别的模块不许出现魔法数字。

与 `stock_data_fetch/config.py` 的关系：**没有关系**。这个包是独立工具，
刻意不 import 数据层的任何模块——它只有 6 个上游请求，不值得为此拖进
hub / router / coverage / throttle 那一大套，也不该被那边的 schema 版本闸卡住。
两边唯一共享的是**文件路径约定**（同一仓库根、同样的环境变量名）。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 仓库根（本包的上一级）。所有默认路径都相对它。
ROOT = Path(__file__).resolve().parents[1]

#: 抓取分页。实测全市场 5579 只 / 1000 行一页 = 6 页。
PAGE_SIZE = 1000

#: 翻页上限。真翻到 30 页说明返回的已经不是"一天的全市场"了，宁可报错也不闷头取。
MAX_PAGES = 30

#: 单次 HTTP 超时（秒）。
TIMEOUT = 20

#: 收盘宽限：过了这个时点才认为"今天的数据已经发布"。
#: 上游是按交易日算的，收盘（15:00）后还要几分钟落库，留 5 分钟。
CLOSE_GRACE = (15, 5)

#: 环境变量名。与数据层同名，是为了"拿副本调试"这一招在两个工具里用起来一样。
ENV_SHARED_DB = "STOCK_SHARED_DB"
ENV_RESULTS_DB = "STOCK_RESULTS_DB"
ENV_CALENDAR = "STOCKHUB_CALENDAR"


def shared_db(explicit: str | Path | None = None) -> Path:
    """原始层：只放 `price` 一张表。"""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_SHARED_DB)
    return Path(env).expanduser() if env else ROOT / "shared.db"


def results_db(explicit: str | Path | None = None) -> Path:
    """结果层：`price` + `price_stat`，看板读这里。

    默认值与 `stock_data_fetch.results.DEFAULT_PATH` 指向同一个文件——
    这是**约定**，不是依赖：两边各自算路径，不 import 对方。
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_RESULTS_DB)
    return Path(env).expanduser() if env else ROOT / "results.db"


def calendar_path(explicit: str | Path | None = None) -> Path:
    """交易日历。

    默认取仓库内的 `state/trading_calendar.json`（数据层的 `build_calendar` 写的），
    而不是本机某个别的项目路径——这个包要能在一台没装 kline-screen 的机器上跑。
    文件不存在时不算错：`--date` 照样能用，只是不给日期时定不出目标日。
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_CALENDAR)
    return Path(env).expanduser() if env else ROOT / "state" / "trading_calendar.json"
