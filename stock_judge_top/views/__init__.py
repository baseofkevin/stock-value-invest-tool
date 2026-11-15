"""四因子卡片汇总。"""
from .earnings import earnings_view
from .momentum import momentum_view
from .turnover import turnover_view
from .volume import volume_view


def build_views(r):
    """四个因子各自独立出结果；这里不做加权、不汇总、不判顶。"""
    return [
        momentum_view(r),
        turnover_view(r),
        earnings_view(r),
        volume_view(r),
    ]
