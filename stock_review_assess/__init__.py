"""综合评估：四条硬门槛 + 赔率分，给一批候选票排优先级。

和 `stock_kline_screen` / `stock_quality_screen` / `stock_cycle_elastic` /
`stock_judge_top` 平级——都是顶层包，都不 import `web`，也都不自己连库：

    stock_review_assess.config   全部阈值（改一个数刷新页面就生效）
    stock_review_assess.gates    四条硬门槛（纯函数）
    stock_review_assess.review   赔率分 + 分档 + build()

数据从哪来：`web/db.py` 读好 `results.db` 的合并行、`shared.db` 的财务与
`annotation`，再交给 `build()`。这一层只吃字典、不碰文件，所以能单测。

用法（在别的代码里）：

    from stock_review_assess import build
    report = build(rows, moats, financials)
    report["buy"]          # 优先研究
    report["gate_stats"]   # 四条门槛各自的通过/不过/未核实只数
"""
from __future__ import annotations

from . import config, gates
from .gates import check as check_gates
from .review import build, evaluate

__version__ = "1.0.0"

__all__ = ["build", "evaluate", "check_gates", "config", "gates",
           "__version__"]
