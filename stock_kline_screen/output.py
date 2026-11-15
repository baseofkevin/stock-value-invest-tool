"""CSV 行组装、result JSON 与终端摘要。

相对原版（`kline_screen/output.py`）的可见变化：

- CSV 少一列「二级行业」（共享库没有申万二级，见 `params.CSV_HEADER`）；
- result JSON 的「参数」节删掉了净流入 / 市值比相关条目——这两个展示量在
  共享库版里**不存在**（`scan` 域不取 MainNetFlow，见 hub `config.SCAN_EXPR`
  的超时实测），留着条目只会让人以为还有这个数。
"""

from __future__ import annotations

import os

from .params import C2_TOL, C3_RATIO, RESULT_VERSION, TURN_GM_REQUIRE_YOY
from .rules import REJ_STAGE


def turn_star(rank):
    """换手分位的星级标记（只影响展示，不参与排序/裁决）。

    Q1（0 ≤ rank < 20）→ `***`，Q2（20 ≤ rank < 40）→ `**`，
    Q3（40 ≤ rank < 60）→ `*`；Q4/Q5 与分位为空（数据不足）→ 留空。
    """
    if rank is None:
        return ""
    if rank < 20:
        return "***"
    if rank < 40:
        return "**"
    if rank < 60:
        return "*"
    return ""


def turn_cell(label, desc):
    """把独立判断与证据拼成一格：是/否/无数据 + 全角冒号 + 证据。"""
    if not label:
        return ""
    if not desc or label == "不适用":
        return label
    return f"{label}：{desc}"


def r2(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return round(v, 2)


def csv_row(x):
    """一只入选股的 CSV 行，顺序与 CSV_HEADER 严格对应（15 列）。"""
    return [
        x["code"], x.get("name") or "",
        x.get("sw1") or "未分类",
        x["d_star"], x["age"], round(x["vcross"], 2), x.get("chg"),
        x["r20"], x["r60"],
        turn_cell(x.get("turn_p1", ""), x.get("turn_gm", "")),
        x.get("turn20"), x.get("turn20_rank"),
        turn_star(x.get("turn20_rank")),
        r2(x.get("price")),
        r2(x.get("total_cap_yi")),
    ]


def build_result(st):
    """组装 result_<S>.json 的完整运行明细。"""
    a = st["a"]
    turn = st["turn"]
    csv_path, res_path = st["csv_path"], st["res_path"]
    stats = dict(sorted(st["rej"].items(), key=lambda kv: -kv[1]))
    return {
        "版本": RESULT_VERSION,
        "基准日": st["S"], "扫描周期K": a.n + 1, "新鲜度窗口N": a.n,
        "覆盖区间": st["need"],
        "参数": {"K_VOL": a.k_vol, "涨幅区间": [a.chg_lo, a.chg_hi],
                 "MA60上限": a.ma60_gap,
                 "换手过滤": st["turn_meta"],
                 "换手分位口径": (f"分母 = 当天粗筛池里算得出 20 日换手的股票"
                                  f"（{st.get('turn_pool_n') or 0} 只），"
                                  f"不是入选名单"),
                 "C2容忍": C2_TOL, "C3比例": C3_RATIO,
                 "次新股口径": f"stock.list_date，上市未满 3 年（profile 域）",
                 "总市值列": "quote 域当期 total_mv/1e8，仅展示不参与裁决",
                 # 这两列与 CSV 主体**不同源**是设计如此（README §1.5）：
                 # 主体是当日横截面/日K派生量，这两列分别是元数据和财务，
                 # 一季或一年才变一次。它们回答的是日K答不了的问题，不是冗余。
                 "行业归属": "stock.industry（profile 域，上游一级口径），只展示不参与裁决",
                 "毛利率改善列": ("finance 域单季列本地算 (营收−成本)/营收，"
                                  "只展示不参与裁决；金融股按 is_financial 判「不适用」"),
                 "收盘价列": "scan 域基准日 close，与基准日严格同日"},
        "业绩转变参数": {"判定方式": "①毛利率环比季改善单项，不合成总分",
                         "①毛利率环比季数": 2,
                         "①营收环比不为负": True,
                         "①最新单季毛利率高于上年同季": TURN_GM_REQUIRE_YOY,
                         "数据是否可用": bool(turn),
                         "缺失代码": st["turn_failed"]},
        "粗筛池": st["pool_n"], "入选数": len(st["final"]),
        "剔除非": stats,
        "剔除统计": stats,
        "剔除明细": dict(sorted(st["rej_raw"].items(), key=lambda kv: -kv[1])),
        "剔除代码": st["rej_code"],
        "剔除阶段": REJ_STAGE,
        "剔除口径": ("统计=数字归一化聚合（仅用于分组展示）；"
                     "明细=原文，保留真实 N/量能比/age/日期；"
                     "代码=逐股去向 [阶段码, 原因]"),
        "产物": {"csv": os.path.basename(csv_path), "csv行数": st["n_csv"],
                 "json": os.path.basename(res_path)},
        "名单": st["final"],
    }


def print_summary(st):
    """终端摘要：剔除统计、行业分布、名单表与产物路径。"""
    final = st["final"]
    print(f"\n粗筛池 {st['pool_n']} 只 → 入选 {len(final)} 只\n")
    print("剔除明细（按原因聚合；逐股原因见 result JSON 的「剔除代码」）：")
    for k, v in sorted(st["rej"].items(), key=lambda kv: -kv[1]):
        print(f"    {k}: {v}")
    if final:
        cnt = {}
        for x in final:
            k = x.get("sw1") or "未分类"
            cnt[k] = cnt.get(k, 0) + 1
        print("\n行业分布（上游一级口径，只展示、不参与裁决）：" + " ｜ ".join(
            f"{k} {v} 只"
            for k, v in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))))

        def _mark(v):
            return {"是": "是", "否": "否", "无数据": "无",
                    "不适用": "不"}.get(v, "-")
        print(f"\n{'代码':10s}{'名称':9s}{'启动日':12s}{'距启动':>6s}"
              f"{'量能':>7s}{'换手%':>7s}{'池内分位':>7s}"
              f"{'距MA20':>8s}{'距MA60':>8s}"
              f"{'流通市值亿':>11s}{'①':>3s}")
        for x in final:
            t20 = f"{x['turn20']:7.2f}" if x.get("turn20") is not None else "      -"
            tr = (f"{x['turn20_rank']:7.1f}"
                  if x.get("turn20_rank") is not None else "      -")
            print(f"{x['code']:10s}{(x.get('name') or '')[:8]:9s}"
                  f"{x['d_star']:12s}{x['age']:6d}{x['vcross']:7.2f}"
                  f"{t20}{tr}"
                  f"{x['r20']:8.2f}{x['r60']:8.2f}"
                  f"{str(x.get('float_cap_yi') or '-'):>11s}"
                  f"{_mark(x.get('turn_p1')):>3s}")
    print(f"\nCSV    : {st['csv_path']}")
    print(f"明细   : {st['res_path']}")
