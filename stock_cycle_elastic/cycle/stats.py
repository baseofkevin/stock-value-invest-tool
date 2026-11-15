"""`--stats` 校准（README.md「参数全表」）。

抽样跑一遍，只输出分布与命中率，**不产出任何报告**。
用途：校准判断① 的 `GM_STD_CYCLE = 5.0pp`（季度口径通常比文献的年度口径偏高）。

可复现性：打印 seed，同 seed 同样本、同结果。
数据来源：本地 `data/stock_names.csv`（由 `--refresh-names` 生成）。
"""

import os
import random

import config
from cycle import batch, build, codes, decide, fetch, metrics


def load_universe():
    """从本地名称表读全市场代码列表。

    文件格式：`代码,名称`（首行可为表头）。
    没有文件时抛 ValueError，提示先生成。
    """
    path = config.STOCK_NAMES_CSV
    if not os.path.exists(path):
        raise ValueError(
            f"没有本地名称表 {path}\n"
            "  先运行：python3 cycle_report.py --refresh-names\n"
            "  （--stats 需要全市场代码列表才能抽样）"
        )

    out = []
    with open(path, "r", encoding="utf-8-sig") as fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.replace("\t", ",").split(",")]
            if idx == 0 and not codes.is_valid(parts[0]):
                continue                       # 表头
            if not parts or not codes.is_valid(parts[0]):
                continue
            name = parts[1] if len(parts) > 1 else ""
            out.append((codes.parse(parts[0]), name))
    return out


def run(sample_size=500, seed=None, progress=None):
    """跑统计，返回结果 dict。"""
    universe = load_universe()
    if seed is None:
        seed = random.randrange(1, 10 ** 6)
    rng = random.Random(seed)
    picked = universe if sample_size >= len(universe) else rng.sample(universe, sample_size)

    gm_stds = []
    gm_cvs = []
    ratios = []
    pcts = []
    labels = {"cycle_yes": 0, "elastic_strong": 0, "low": 0, "high": 0}
    cv_channel_hits = [0]           # 判断① 通道 B 单独贡献的命中数
    failed = []
    total = len(picked)

    for idx, (code, name) in enumerate(picked, start=1):
        try:
            raw = fetch.fetch_one(code)
            stock = build.build(raw)
            if not stock["quarters"]:
                failed.append((code, name, "无有效季度"))
                continue
            m = metrics.compute_all(stock["quarters"], config)
            d = decide.decide(stock["quarters"], m, stock["industry"], config)

            if m["gm_std"] is not None:
                gm_stds.append(m["gm_std"])
            if (m.get("gm_cv") or {}).get("cv") is not None:
                gm_cvs.append(m["gm_cv"]["cv"])
                if d["cycle"]["channel"] == "cv":
                    cv_channel_hits[0] += 1
            if m["elasticity"]["ratio"] is not None:
                ratios.append(m["elasticity"]["ratio"])
            if m["position"]["pct"] is not None:
                pcts.append(m["position"]["pct"])

            if d["cycle"]["label"] == decide.CYCLE_YES:
                labels["cycle_yes"] += 1
            if d["elastic"]["label"] == decide.ELASTIC_STRONG:
                labels["elastic_strong"] += 1
            if d["position"]["label"] == decide.POSITION_LOW:
                labels["low"] += 1
            elif d["position"]["label"] == decide.POSITION_HIGH:
                labels["high"] += 1
        except fetch.DataError as exc:
            failed.append((code, name, str(exc)[:80]))
        except KeyboardInterrupt:
            raise
        except Exception as exc:                     # 单只异常不中断
            failed.append((code, name, f"异常：{exc}"))

        if progress:
            progress(idx, total, code, True, "")

    return {
        "seed": seed,
        "sampled": total,
        "universe": len(universe),
        "failed": len(failed),
        "failed_samples": failed[:10],
        "gm_std": _describe(gm_stds),
        "gm_cv": _describe(gm_cvs),
        "cv_channel_hits": cv_channel_hits[0],
        "ratio": _describe(ratios),
        "position": _describe(pcts),
        "hit_rates": {
            "cycle_yes": _rate(labels["cycle_yes"], total),
            "elastic_strong": _rate(labels["elastic_strong"], total),
            "low": _rate(labels["low"], total),
            "high": _rate(labels["high"], total),
        },
        "thresholds": {
            "GM_STD_CYCLE": config.GM_STD_CYCLE,
            "ELASTIC_RATIO_MIN": config.ELASTIC_RATIO_MIN,
            "POSITION_LOW_PCT": config.POSITION_LOW_PCT,
            "POSITION_HIGH_PCT": config.POSITION_HIGH_PCT,
        },
    }


def _describe(values):
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def q(frac):
        idx = min(len(ordered) - 1, max(0, int(round(frac * (len(ordered) - 1)))))
        return round(ordered[idx], 2)

    return {
        "n": len(ordered),
        "min": round(ordered[0], 2),
        "p20": q(0.20),
        "p50": q(0.50),
        "p80": q(0.80),
        "max": round(ordered[-1], 2),
    }


def _rate(hit, total):
    if not total:
        return 0.0
    return round(hit / total * 100.0, 1)


def format_report(result):
    """把统计结果排版成终端可读的文本。"""
    lines = []
    lines.append("=" * 62)
    lines.append("周期弹性初筛 · 抽样统计（--stats）")
    lines.append("=" * 62)
    lines.append(f"全市场代码数 {result['universe']}  抽样 {result['sampled']}  seed={result['seed']}")
    lines.append(f"失败 {result['failed']}")
    lines.append("")

    labels = (
        ("gm_std", "判断① 毛利率标准差 (pp)"),
        ("gm_cv", "判断① 毛利率变异系数 CV (%)"),
        ("ratio", "判断② 弹性比值"),
        ("position", "判断③ 产能分位 (%)"),
    )
    for key, title in labels:
        d = result[key]
        lines.append(f"{title}")
        if d.get("n"):
            lines.append(
                f"    n={d['n']}  min={d['min']}  p20={d['p20']}  "
                f"p50={d['p50']}  p80={d['p80']}  max={d['max']}")
        else:
            lines.append("    （无有效样本）")
        lines.append("")

    lines.append(f"判断① 中由通道 B（相对波动）单独贡献的命中：{result.get('cv_channel_hits', 0)}")
    lines.append("")
    lines.append("标签命中率")
    names = (
        ("cycle_yes", "周期股"),
        ("elastic_strong", "弹性强"),
        ("low", "低位"),
        ("high", "高位"),
    )
    for key, text in names:
        lines.append(f"    {text:6s} {result['hit_rates'][key]:6.1f}%")
    lines.append("")

    check = result["hit_rates"]["cycle_yes"]
    lines.append("判断① 校准建议（目标命中率 20–40%）")
    if not result["sampled"]:
        lines.append("    样本为空，无法判断")
    elif check > 50:
        lines.append(f"    当前 {check:.1f}% 偏高 → 门槛过松，考虑上调 GM_STD_CYCLE")
    elif check < 10:
        lines.append(f"    当前 {check:.1f}% 偏低 → 门槛过紧，考虑下调 GM_STD_CYCLE")
    else:
        lines.append(f"    当前 {check:.1f}% 落在区间内，可沿用 GM_STD_CYCLE = {result['thresholds']['GM_STD_CYCLE']:g}")

    if result["failed_samples"]:
        lines.append("")
        lines.append("失败样本（前 10）")
        for code, name, err in result["failed_samples"]:
            lines.append(f"    {code} {name} — {err}")
    lines.append("")
    lines.append(f"复现命令：python3 cycle_report.py --stats --sample {result['sampled']} --seed {result['seed']}")
    return "\n".join(lines)
