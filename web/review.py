"""兼容层：综合评估的规则已经搬到 `stock_review/`（与 `stock_kline_screen` 平级）。

留着这个文件只有一个原因——`tests/test_web.py` 里 19 个用例写的是
`from web import review`，模板和 `web/app.py` 也按这个名字引。新代码请直接
`from stock_review import build`，别再往这里加逻辑。
"""
from __future__ import annotations

from stock_review.review import *          # noqa: F401,F403
from stock_review.review import build, evaluate          # noqa: F401
from stock_review.config import (                        # noqa: F401
    BUY_CUT, ELASTIC_POINTS, Q_HARD, SCORE_MAX, TIERS, TREND_PENALTY,
)
from stock_review.gates import check                     # noqa: F401
