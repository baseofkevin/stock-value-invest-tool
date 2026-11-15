"""kline_screen 命令行参数与入口。

与原版（`stock-kline-screen-tool`）的 CLI 差异：

- 删掉 `--refresh` / `--refresh-industry` / `--prune-kline` / `--no-clean` /
  `--clean-dry-run`：缓存管理全部归共享库（coverage 闸 + GC），
  工具层不再自己管缓存目录。要强制重取，用 hub 的 `repair` / 删库。
- 新增 `--db` / `--state`：透传给 `preflight`，默认走 hub 的环境变量。
"""

from __future__ import annotations

import argparse
import sys

from stock_data_fetch.errors import StockfetchError as HubError
from stock_data_fetch.hub import preflight

from .params import (CHG_HI, CHG_LO, K_VOL, MA60_GAP, TURN_MAX_N, TURN_MIN_N,
                     TURN_PCT)
from .pipeline import CliError, run


def build_parser():
    ap = argparse.ArgumentParser(
        description="K线选股规则 v2.0（共享数据层版，每周选股 K=5）")
    ap.add_argument("--date", help="基准交易日（默认最近已收盘的交易日）")
    ap.add_argument("--n", type=int, default=4,
                    help="新鲜度窗口，必须 = 扫描周期−1（每周 K=5 用 4=默认；"
                         "每3日用 2；每日用 0）")
    ap.add_argument("--k-vol", type=float, default=K_VOL)
    ap.add_argument("--chg-lo", type=float, default=CHG_LO)
    ap.add_argument("--chg-hi", type=float, default=CHG_HI)
    ap.add_argument("--turn-pct", type=float, default=TURN_PCT,
                    help="换手硬过滤保留池内最低比例：默认 0（只排序标注）；"
                         "0.4=只留 Q1–Q2")
    ap.add_argument("--turn-min", type=int, default=TURN_MIN_N,
                    help="名单少于该数时自动放宽一档（默认 5）")
    ap.add_argument("--turn-max", type=int, default=TURN_MAX_N,
                    help="名单多于该数时自动收紧一档（默认 30）")
    ap.add_argument("--ma60-gap", type=float, default=MA60_GAP)
    ap.add_argument("--db", help="共享库路径（默认 STOCKHUB_DB 或 "
                                 "stock-value-invest-tool/shared.db）")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        # preflight 的六道闸在工具入口处过一遍：库不存在会自动建、
        # 版本不符拒绝启动、盘中只标记不拦（联网才拦）。
        hub = preflight(db_path=a.db) if a.db else preflight()
    except HubError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 3
    try:
        with hub:
            return run(a, hub)
    except CliError as e:
        print(str(e), file=sys.stderr)
        return 1
    except HubError as e:
        print(f"\n错误（数据层）：{e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        # run() 里的数据类错误都用 RuntimeError 表达「宁可失败也不出不可信名单」。
        # 不打 traceback：用户要的是那句原因，不是调用栈。
        print(f"\n错误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
