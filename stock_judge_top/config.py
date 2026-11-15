"""项目路径与运行开关。

迁移说明（2026-11-15，原 stock-judge-top-tool → 共享数据层版）
------------------------------------------------------------
原来这一层还管 westock 令牌（`WESTOCK_TOKEN_FILE` / `WESTOCK_MCP_URL` /
`WESTOCK_BIN` / `JUDGE_USE_EXTERNAL_WESTOCK`）与缓存目录（`JUDGE_CACHE_DIR`）。
现在取数全部走共享库 hub（`stock_data_fetch`），令牌、重试、配额、缓存
都归 hub 管，这里只剩 HTML 输出目录。

环境变量
--------
JUDGE_OUT_DIR     HTML 输出目录，默认 <项目根>/output/judge-top
STOCKHUB_DB       共享库路径（透传给 hub.preflight，hub 自己解析默认值）
"""
import os

# 项目根 = stock-value-invest-tool/（本包的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dir(env, default):
    return os.path.abspath(os.path.expanduser(os.environ.get(env) or default))


OUT_DIR = _dir("JUDGE_OUT_DIR", os.path.join(ROOT, "output", "judge-top"))
