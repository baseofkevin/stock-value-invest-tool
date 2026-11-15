"""四因子卡片汇总（外加可选的第五张「周期位置」）。"""
from .cycle import cycle_view
from .earnings import earnings_view
from .momentum import momentum_view
from .turnover import turnover_view
from .volume import volume_view


def build_views(r):
    """四个因子各自独立出结果；这里不做加权、不汇总、不判顶。

    「周期位置」只在判断①判定为周期股时追加，非周期股的报告与以前完全一样。
    """
    views = [
        momentum_view(r),
        turnover_view(r),
        earnings_view(r),
        volume_view(r),
    ]
    extra = cycle_view(r)
    if extra:
        views.append(extra)
    return views
