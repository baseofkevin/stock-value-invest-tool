"""每日「冷热摘要」的口径：涨跌家数 / 涨跌停家数 / 三个估值的中位数。

**这是唯一一处定义口径的地方。** `store.write_day()` 用它写库，看板「冷热时点」页
只读 `results.db.price_stat` 里那一行，页面本身不算任何东西。

把口径单独放一个文件而不是塞进 `store.py`：它是**纯函数**（一堆行 → 一个 dict），
不碰连接、不碰网络，测试可以直接喂假行、不用建库。

## 涨停怎么算（这件事没有上游帮忙，只能自己定）

`price` 表只有收盘价与涨跌幅，没有"是否涨停"这个标记——东财的估值横截面不提供。
所以按**板块的涨跌幅限制**判，并留 0.2 个点吸收四舍五入（上游 `CHANGE_RATE` 是
多位小数，但 10.00% 的涨停偶尔会算出 9.9987）：

| 板块 | 判据 |
|---|---|
| 主板（`sh60*` / `sz00*`） | `pct_chg >= 9.8` |
| 创业板（`sz30*`）/ 科创板（`sh68*`） | `pct_chg >= 19.8` |
| 北交所（`bj*`） | `pct_chg >= 29.8` |
| ST / *ST（名称含 `ST`） | `pct_chg >= 4.8`（ST 的限制是 5%） |

**ST 的判断放在板块之前**：ST 股在主板也按 5% 走，先后顺序反了就会把一只涨停的
ST 股算成"普通上涨"，冷热摘要于是系统性偏冷。名称里带 `ST`（含 `*ST`）的才判。
北交所例外：那里没有 5% 的限制，所以 `bj` 先返回 29.8%，不进 ST 分支。

## 中位数为什么只取正值

亏损股的 `PE_TTM` 是负数（万科 -0.43、和展能源 -1.4）。把负值混进中位数，
全市场 PE 中位数会被拖到个位数附近，看起来像"遍地便宜"，而实际是"一半公司在亏"。
所以三个 median 都只统计**正值**，`n_pe` / `n_pb` / `n_ps` 记录参与计算的只数，
页面上要能说清"这个中位数是基于多少只票算的"。
"""

from __future__ import annotations

#: 涨跌停判据（留 0.2 个点余量）。改这里就是改全站口径。
BOARD_LIMIT = {"main": 9.8, "gem": 19.8, "star": 19.8, "bj": 29.8}
ST_LIMIT = 4.8

#: 平盘阈值：|涨跌幅| 小于它就当平盘。`CHANGE_RATE` 是浮点，严格等于 0 的票极少。
FLAT_EPS = 0.005


def board_of(symbol: str) -> str:
    """`sh600519` → `main` / `gem` / `star` / `bj`。

    **不按前缀白名单**判主板（`sh60` 之外还有 `sh601/603/605`、`sz000/001/002/003`），
    而是先认几个"特殊板"，剩下的都算主板——交易所新增代码段时不会把新股判错。
    """
    s = str(symbol or "").lower()
    if s.startswith("bj"):
        return "bj"
    if s.startswith("sz30"):            # 创业板 300/301
        return "gem"
    if s.startswith("sh68"):            # 科创板 688/689
        return "star"
    return "main"


def limit_pct(symbol: str, name: str | None = None) -> float:
    """这只票的涨停阈值（百分数）。跌停用同一个数的负值。"""
    if board_of(symbol) == "bj":
        return BOARD_LIMIT["bj"]
    if "ST" in str(name or "").upper():
        return ST_LIMIT
    return BOARD_LIMIT[board_of(symbol)]


def _median(values: list[float]) -> float | None:
    """中位数。空列表给 `None`（不是 0——0 会被读成"极便宜"）。"""
    if not values:
        return None
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _pos(values) -> list[float]:
    """只留正值（`None` 与负数都丢掉）。见模块头「中位数为什么只取正值」。"""
    out = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out


def summarize(rows: list[dict], *, src: str | None = None,
              updated_at: str | None = None) -> dict:
    """一组 `price` 行 → `price_stat` 的一行。

    `rows` 的键与 `price` 表列名一致（`pct_chg` 是**百分数**）。
    返回的 dict 键与 `price_stat` 的列名一一对应，直接 `INSERT` 即可。
    """
    n_up = n_down = n_flat = up_limit = down_limit = 0
    for r in rows:
        chg = r.get("pct_chg")
        if chg is None:
            continue                       # 停牌/无涨跌幅：不计入涨跌家数
        chg = float(chg)
        lim = limit_pct(r.get("symbol") or "", r.get("name"))
        if chg >= lim:
            up_limit += 1
        if chg <= -lim:
            down_limit += 1
        if chg > FLAT_EPS:
            n_up += 1
        elif chg < -FLAT_EPS:
            n_down += 1
        else:
            n_flat += 1

    pe = _pos(r.get("pe_ttm") for r in rows)
    pb = _pos(r.get("pb_mrq") for r in rows)
    ps = _pos(r.get("ps_ttm") for r in rows)
    mv = [float(r["total_mv"]) for r in rows
          if r.get("total_mv") is not None and float(r["total_mv"]) > 0]

    days = {str(r.get("date") or "") for r in rows} - {""}
    if len(days) > 1:
        # 摘要的粒度是"一天"。混了多天还硬算，算出来的"涨跌家数"没有含义，
        # 而且会静默覆盖掉其中一天的摘要——报错比这个结果有用。
        raise ValueError(f"summarize() 只接受同一天的行，收到 {sorted(days)}")
    return {
        "date": next(iter(days), ""),
        "n": len(rows),
        "n_up": n_up,
        "n_down": n_down,
        "n_flat": n_flat,
        "n_limit_up": up_limit,
        "n_limit_down": down_limit,
        "n_pe": len(pe),
        "n_pb": len(pb),
        "n_ps": len(ps),
        "pe_median": _median(pe),
        "pb_median": _median(pb),
        "ps_median": _median(ps),
        "mv_sum": sum(mv) if mv else None,
        "src": src,
        "updated_at": updated_at,
    }


#: `price_stat` 里由本模块算出来的列（写库时按这个顺序拼 SQL）。
COLUMNS = ("date", "n", "n_up", "n_down", "n_flat", "n_limit_up", "n_limit_down",
           "n_pe", "n_pb", "n_ps", "pe_median", "pb_median", "ps_median",
           "mv_sum", "src", "updated_at")

__all__ = ["summarize", "limit_pct", "board_of", "COLUMNS", "BOARD_LIMIT",
           "ST_LIMIT", "FLAT_EPS"]
