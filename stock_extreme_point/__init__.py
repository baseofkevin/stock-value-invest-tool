"""`stock_extreme_point` —— 全市场价格/估值快照（`price`）与每日冷热摘要（`price_stat`）。

这个包**只管一件事**：把某个交易日的全市场价格 + PE + PB + PS 取回来，
落到两个库里，再算出那一天的冷热摘要。四个工具、共享数据层、看板都不在这里。

    python3 -m stock_extreme_point --date 2026-11-16     # 取一天
    ./run.py extreme --date 2026-11-16              # 日常入口（带进度条与日志）
    ./run.py extreme --code 002096 --date 2026-11-03 # 单只补数（只改这一只）

数据流向（2026-11-17 定，见 README）：

    shared.db   price                     原始横截面，从上游原样落库
    results.db  price + price_stat        同一天的数据 + 派生摘要，看板只读这个库

为什么原始和摘要分在两个库
--------------------------
`shared.db` 是"上游数据"（可重取的原始层），`results.db` 是"给人看的结果"。
看板只读 `results.db` 之后，上游怎么变、抓取怎么失败都不影响看图；
反过来 `shared.db` 被 `maintain` VACUUM/清理时，也不会把页面上的历史时点一起带走。
`price` 两边都有，是**刻意的冗余**：一份是原件，一份是看板要用的复制品。
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
