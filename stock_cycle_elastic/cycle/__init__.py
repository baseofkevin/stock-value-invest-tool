"""周期弹性初筛工具 —— 计算内核。

模块划分（README.md「项目结构」）：
    codes.py   代码归一化
    util.py    纯函数（num / median / stdev / percentile_rank ...）
    fetch.py   取数（2 次 MCP 调用 + 跨线程节流 + 重试）
    build.py   两张表 → 干净季度序列
    metrics.py 序列 → 三个指标（纯计算）
    decide.py  指标 → 三个标签（5 个阈值）
    render.py  数据 → HTML 报告
    batch.py   CSV → 批量编排
    index_html.py 索引页
"""

__all__ = [
    "codes",
    "util",
    "fetch",
    "build",
    "metrics",
    "decide",
    "render",
    "batch",
    "index_html",
]
