"""CSV 行组装与终端摘要。

相对原版（`kline_screen/output.py`）的可见变化：

- CSV 少一列「二级行业」（共享库没有申万二级，见 `params.CSV_HEADER`）；
- **不再产出 `result_<S>.json`**（2026-11-15 按要求删）：那份明细里的参数、
  剔除统计、逐股去向，终端摘要里已经打印了要看的部分。少一个文件就少一处
  "两边不一致"的可能——CSV 与 JSON 的名单必须同步排序、同步筛选，
  之前是同一个 `final` 列表喂两份产物，删掉 JSON 这层耦合就没了。
"""

from __future__ import annotations


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


def print_summary(st):
    """终端摘要：剔除统计、行业分布、名单表与产物路径。"""
    final = st["final"]
    print(f"\n粗筛池 {st['pool_n']} 只 → 入选 {len(final)} 只\n")
    print("剔除明细（按原因聚合）：")
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
