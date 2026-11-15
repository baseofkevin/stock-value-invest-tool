"""纯函数工具集 —— 无 IO、无全局状态、不打印。

刻意只用标准库 `statistics`，不引 numpy/pandas：
本工具的计算量极小（几十个数），装第三方包不值得。

关键约定（README.md「测试」）：
  * **缺失一律返回 None，绝不返回 0 或 False**。调用方必须用 `is None` 判断。
  * `stdev` 用样本标准差（ddof=1），与 `statistics.stdev` 一致；
    样本数 < 2 返回 None（样本标准差在 n=1 时无定义）。
"""

import statistics


def num(value):
    """把接口返回的字符串数字转成 float。

    接口大量使用字符串（"2084485744.75"）和空串表示缺失。
    返回 None 表示"没有这个数"，而不是 0。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool 是 int 的子类，但语义上不该当数字用
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def safe_div(numerator, denominator):
    """除法，分母为 0 / None 时返回 None（不抛 ZeroDivisionError）。

    不做四舍五入 —— 精度由展示层决定，计算层保留原始精度。
    """
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def median(values):
    """中位数。剔除 None；空列表返回 None。

    偶数个时取中间两值均值（与 statistics.median 一致）。
    """
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return statistics.median(clean)


def stdev(values):
    """样本标准差（ddof=1）。剔除 None；有效样本 < 2 返回 None。"""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    return statistics.stdev(clean)


def percentile_rank(series, current):
    """中位秩法计算 `current` 在 `series` 中的分位（0~100）。

    公式：`(小于 current 的个数 + 0.5 × 等于 current 的个数) / 总数 × 100`

    性质（必须被测试钉住）：
      * 全部相等 → 恰好 50.0（中位秩的关键性质，也是为什么用它）
      * 当前值是唯一最大值 → 接近 100 但不等于 100
      * series 里的 None 被剔除，分母是有效个数

    `series` 应包含 current 本身。空序列或 current 为 None 返回 None。
    """
    if current is None:
        return None
    clean = [v for v in series if v is not None]
    if not clean:
        return None
    below = sum(1 for v in clean if v < current)
    equal = sum(1 for v in clean if v == current)
    return (below + 0.5 * equal) / len(clean) * 100.0


def pct_change(now, prev):
    """变化率（百分数）。

    分母用 `abs(prev)`，所以：
      * prev = -100 → now = -50 得 +50%（亏损收窄记为正，符合直觉）
      * prev = 0    → 返回 None（除零无意义）
    """
    if now is None or prev is None:
        return None
    if prev == 0:
        return None
    return (now - prev) / abs(prev) * 100.0


def clip(value, low, high):
    """把 value 夹到 [low, high]。value 为 None 时原样返回 None。"""
    if value is None:
        return None
    return max(low, min(high, value))


def mean(values):
    """算术平均。剔除 None；空列表返回 None。"""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)
