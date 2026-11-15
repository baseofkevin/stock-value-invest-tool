#!/usr/bin/env python3
"""周期弹性初筛工具 —— 命令行入口（README.md「CLI 参考」）。

三种模式：
    单股票   python3 cycle_report.py --stock 600519
    批量     python3 cycle_report.py --from-select 2026-11-13  （读结果库的选股名单）
    批量     python3 cycle_report.py --csv 我的持仓.csv        （读自己的表）

结果只写 results.db，**不再产出 HTML 报告 / 索引页 / CSV**（2026-11-15）：
看结果用 `./run.py web`，要比对历史结论看同一个库。

退出码（§6.5）：
    0   成功
    1   批量中有失败行
    2   参数错误 / 名称歧义 / 代码无法识别
    3   未预期异常
    130 用户中断
"""

import argparse
import datetime as dt
import difflib
import os
import re
import sys

import config
from cycle import batch, build, codes, decide, fetch, fmt, metrics

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_UNEXPECTED = 3
EXIT_INTERRUPT = 130


EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_UNEXPECTED = 3
EXIT_INTERRUPT = 130


# ---------------------------------------------------------------------------
# 名单来源
# ---------------------------------------------------------------------------


def _resolve_date(text):
    """把 `--from-select` 的日期参数转成 YYYY-MM-DD；空串返回 None。

    `--from-select` 不带值时用 None，让结果库退回"最近一次选股运行"——
    这样日常跑不必记日期；要复查某一天时必须显式给。
    """
    if not text:
        return None
    m = re.match(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})", str(text).strip())
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _items_from_csv(path):
    rows = batch.read_csv(path)
    return [(code, name) for code, name in rows]


def build_parser():
    p = argparse.ArgumentParser(
        prog="cycle_report.py",
        description="周期弹性初筛：三个判断、三个指标（是不是周期股 / 弹性 / 周期位置）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 cycle_report.py --stock 600519\n"
            "  python3 cycle_report.py --stock 贵州茅台\n"
            "  python3 cycle_report.py --from-select 2026-11-13 --workers 4\n"
            "  python3 cycle_report.py --csv 我的持仓.csv --workers 4\n"
            "  python3 cycle_report.py --stats --sample 500\n"
        ),
    )

    source = p.add_mutually_exclusive_group()
    source.add_argument("--stock", metavar="代码或名称",
                        help="单股票模式：股票代码（600519 / sh600519）或公司名称")
    source.add_argument("--csv", metavar="路径",
                        help="批量模式：CSV 文件（含代码列，可选名称列），只读不写回")
    source.add_argument("--from-select", metavar="日期", nargs="?", const="", default=None,
                        help="批量模式：直接用结果库里这一天的选股名单（省略日期=最近一次）")

    p.add_argument("--name", default="",
                   help="覆盖报告内公司名（仅单股票模式）")
    p.add_argument("--workers", type=int, default=4,
                   help="批量并发线程数（默认 4）")
    p.add_argument("--force", action="store_true",
                   help="批量模式忽略库里已有结论，全部重算")
    p.add_argument("--stats", action="store_true",
                   help="抽样跑一遍只出统计（校准判断①），不产出报告")
    p.add_argument("--sample", type=int, default=500,
                   help="配合 --stats，随机抽样只数（默认 500）")
    p.add_argument("--seed", type=int, default=None,
                   help="配合 --stats，随机种子（默认随机，会打印出来）")
    p.add_argument("--bundle", default="",
                   help="离线重放 JSON（测试用，单股票模式）")
    p.add_argument("--refresh-names", action="store_true",
                   help="刷新本地股票名称表 data/stock_names.csv")
    p.add_argument("--quiet", action="store_true",
                   help="只输出错误")
    return p


def validate_args(args, parser):
    """参数合法性检查。返回错误信息（None 表示通过）。"""
    if args.stats:
        if args.sample is not None and args.sample <= 0:
            return "--sample 必须 > 0"
        if args.seed is not None and args.seed < 0:
            return "--seed 必须 >= 0"
        return None

    if args.refresh_names:
        return None

    if not args.stock and not args.csv and args.from_select is None:
        return "必须指定 --stock（单股票）、--csv（自己的表）或 --from-select（选股名单）之一"

    if args.workers <= 0:
        return "--workers 必须 > 0"
    if args.bundle and not args.stock:
        return "--bundle 只能与 --stock 一起用"
    return None


# ---------------------------------------------------------------------------
# 名称解析（§6.3）
# ---------------------------------------------------------------------------


def resolve_stock(text):
    """代码或名称 → 6 位代码。

    返回值 (code, error, candidates)。
      * 代码形式 → 直接返回
      * 名称唯一命中 → 返回
      * 名称歧义 → error 说明，candidates 给候选
    """
    if codes.is_valid(text):
        return codes.parse(text), None, []

    table = load_name_table()
    if not table:
        return None, (
            f"无法识别 {text!r}，且本地名称表不存在。\n"
            "  先运行：python3 cycle_report.py --refresh-names"
        ), []

    exact = [(c, n) for c, n in table.items() if n == text]
    if len(exact) == 1:
        return exact[0][0], None, []

    # 去掉 ST / *ST / 空格再比一次
    stripped = _strip_prefix(text)
    near = [(c, n) for c, n in table.items() if _strip_prefix(n) == stripped]
    if len(near) == 1:
        return near[0][0], None, []
    if len(near) > 1:
        return None, f"名称 {text!r} 有多个匹配，请改用代码", near[:10]

    # 用 difflib 猜最接近的几个名称。注意 table 的方向是 {代码: 名称}，
    # 所以必须建立 名称→代码 的反向索引才能取回代码 —— 直接 table[名称] 恒为空，
    # 用户输错一个字时永远收不到"你是不是要找"（这条曾是个真 bug）。
    by_name = {name: code for code, name in table.items()}
    names = list(by_name.keys())
    close = difflib.get_close_matches(text, names, n=5, cutoff=0.6)
    candidates = [(by_name[n], n) for n in close]
    if candidates:
        return None, f"没有精确匹配 {text!r}，你是不是要找：", candidates

    return None, f"本地名称表里找不到 {text!r}", []


def _strip_prefix(name):
    text = str(name).strip().replace(" ", "")
    for prefix in ("*ST", "ST", "XD", "XR", "DR"):
        if text.upper().startswith(prefix):
            text = text[len(prefix):]
    return text


def load_name_table():
    """读 data/stock_names.csv，返回 {code: name}。"""
    path = config.STOCK_NAMES_CSV
    if not os.path.exists(path):
        return {}
    table = {}
    with open(path, "r", encoding="utf-8-sig") as fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.replace("\t", ",").split(",")]
            if not parts or not codes.is_valid(parts[0]):
                continue
            if idx == 0 and len(parts) > 1 and parts[1] in ("名称", "股票名称", "name"):
                continue
            name = parts[1] if len(parts) > 1 else ""
            if name:
                table[codes.parse(parts[0])] = name
    return table


# ---------------------------------------------------------------------------
# 单股票模式
# ---------------------------------------------------------------------------


def run_single(args):
    text = args.stock
    code, error, candidates = resolve_stock(text)
    if not code:
        sys.stderr.write(f"错误：{error}\n")
        for cand_code, cand_name in candidates:
            sys.stderr.write(f"  候选：{cand_code} {cand_name}\n")
        # 歧义或无法识别 → 退出码 2，**不生成任何文件**
        return EXIT_USAGE

    timestamp = batch.batch_timestamp()

    try:
        if args.bundle:
            raw = fetch.load_bundle(args.bundle)
        else:
            if not args.quiet:
                sys.stderr.write(f"取数中… {code}\n")
            raw = fetch.fetch_one(code)
    except fetch.DataError as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return EXIT_USAGE

    stock = build.build(raw)
    if not stock["quarters"]:
        sys.stderr.write("错误：没有有效的季度数据\n")
        return EXIT_USAGE

    if args.name:
        stock["name"] = args.name
    if not stock["name"]:
        stock["name"] = resolve_name_nameonly(code)

    m = metrics.compute_all(stock["quarters"], config)
    decision = decide.decide(stock["quarters"], m, stock["industry"], config)
    decision.setdefault("meta", {})["latest"] = str(
        (stock["quarters"][-1] or {}).get("date") or "")[:10]

    # 单只也入库：不落库的话"跑一只看看"就成了看板里看不到的黑数。
    item = batch.ItemResult(code, stock.get("name") or "")
    item.ok, item.decision, item.stock = True, decision, stock
    batch._save_results([item], kind="single")

    if not args.quiet:
        _print_decision(stock, decision, m, "results.db")
    return EXIT_OK


def resolve_name_nameonly(code):
    """名称表里查名字（取数没给 name 时的兜底）。"""
    return load_name_table().get(code, "")


def _print_decision(stock, decision, m, path):
    cycle = decision["cycle"]
    elastic = decision["elastic"]
    position = decision["position"]

    def show(value, digits=2, suffix=""):
        return "—" if value is None else f"{value:.{digits}f}{suffix}"

    # σ / 分位 / 比值都走 fmt 的格式化：阈值边界的显示一致性只在这一个地方守。
    # 终端里同样不能出现「毛利率σ 5.0 pp」配「否」这种自相矛盾的读数。
    lines = [
        "",
        f"  {stock['name'] or stock['code']}  {stock['code']}"
        + (f"  {stock['industry']}" if stock["industry"] else ""),
        f"  {'-' * 46}",
        f"  是不是周期股    {_cycle_text(cycle['label']):6s} 毛利率σ "
        + fmt.fmt_pp(cycle["gm_std"], lower=config.GM_STD_CYCLE),
        f"  弹性            {_elastic_text(elastic['label']):6s} 比值 "
        + fmt.fmt_num(elastic["ratio"], 2, lower=config.ELASTIC_RATIO_MIN),
        f"  周期位置        {_position_text(position['label']):6s} 分位 "
        + fmt.fmt_pct(position["pct"],
                         lower=config.POSITION_LOW_PCT,
                         upper=config.POSITION_HIGH_PCT),
    ]
    if decision["flags"]["financial"]:
        lines.append("  （金融企业：产能周转率不适用）")
    if decision["flags"]["insufficient_history"]:
        lines.append(f"  （历史不足：{decision['meta']['quarters']} 季）")

    if cycle.get("channel") == "cv":
        lines.append(
            f"  （判断① 走相对波动通道：σ {cycle['gm_std']:.2f}pp 未到 "
            f"{config.GM_STD_CYCLE:g}pp，但 CV {cycle['gm_cv']:.0f}% ≥ "
            f"{config.GM_CV_MIN:g}%，毛利率中位 {cycle['gm_median']:.1f}%）")

    if elastic.get("reason") == "near_breakeven":
        zc = (m.get("elasticity") or {}).get("zero_cross", 0)
        total = (m.get("elasticity") or {}).get("samples", 0)
        lines.append(f"  （弹性不可信：{zc}/{total} 季营业利润跨零，贴近盈亏平衡点）")
    if position.get("capacity_direction") in ("expand", "shrink"):
        lines.append(
            f"  （产能方向：{decide.CAPACITY_DIRECTION_TEXT[position['capacity_direction']]}，"
            f"近 {config.CAPACITY_TREND_QUARTERS} 季资产 "
            f"{position['asset_growth']:+.0f}%、营收 "
            f"{(position['revenue_growth'] or 0):+.0f}%）")

    badges = decide.badges(decision)
    if badges:
        lines.append("  标签  " + " · ".join(text for _cls, text in badges))

    lines.append(f"  {'-' * 46}")
    lines.append(f"  结果  {path}（./run.py web 查看）")
    lines.append("")
    sys.stdout.write("\n".join(lines) + "\n")


def _cycle_text(label):
    return {decide.CYCLE_YES: "是", decide.CYCLE_NO: "否"}.get(label, "—")


def _elastic_text(label):
    return {
        decide.ELASTIC_STRONG: "强",
        decide.ELASTIC_WEAK: "弱",
        decide.ELASTIC_UNSTABLE: "不稳定",
    }.get(label, "—")


def _position_text(label):
    return {
        decide.POSITION_LOW: "低位",
        decide.POSITION_NEUTRAL: "中性",
        decide.POSITION_HIGH: "高位",
    }.get(label, "—")


# ---------------------------------------------------------------------------
# 批量模式
# ---------------------------------------------------------------------------


def run_batch(args):
    if args.from_select is not None:
        target_date = _resolve_date(args.from_select)
        items = batch.read_results(target_date)
        source = f"结果库 选股 {target_date or '最近一次'}"
    else:
        if not os.path.exists(args.csv):
            sys.stderr.write(f"错误：CSV 不存在：{args.csv}\n")
            return EXIT_USAGE
        try:
            items = _items_from_csv(args.csv)
        except ValueError as exc:
            sys.stderr.write(f"错误：{exc}\n")
            return EXIT_USAGE
        # 和 quality 一样：CSV 模式也要有基准日，Ctrl-C 后重跑才能复用已算完的票。
        target_date = dt.date.today().isoformat()
        source = os.path.basename(args.csv)

    if not items:
        sys.stderr.write("错误：名单是空的，没有可算的股票\n")
        return EXIT_USAGE

    total = [0]
    has_error = [False]

    def progress(done, total_n, code, ok, message):
        total[0] = total_n
        if not ok:
            has_error[0] = True
        if args.quiet:
            return
        mark = "ok  " if ok else "FAIL"
        extra = "" if ok else f"  {message}"
        sys.stderr.write(f"\r[{done}/{total_n}] {mark} {code}{extra}\n")

    try:
        results = batch.run_batch(
            items,
            workers=args.workers,
            force=args.force,
            progress=progress,
            target_date=target_date,
        )
    except KeyboardInterrupt:
        sys.stderr.write("\n中断：已算出的结果已经写进 results.db。\n")
        return EXIT_INTERRUPT
    except ValueError as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return EXIT_USAGE

    # 重复行既不算成功也不算失败 —— 名单里同一只股票写两遍是**笔误**，
    # 不是取数失败。把它算进 fail 会让退出码无端变成 1，脚本据此判断就会误报。
    ok_count = sum(1 for r in results if r.ok)
    dup_count = sum(1 for r in results if r.duplicate)
    fail_count = len(results) - ok_count - dup_count

    if not args.quiet:
        sys.stdout.write("\n")
        detail = f"  完成 {ok_count} 只，失败 {fail_count} 只"
        if dup_count:
            detail += f"，重复跳过 {dup_count} 只"
        reused = sum(1 for r in results if r.reused)
        if reused:
            detail += f"，库里已有跳过 {reused} 只"
        sys.stdout.write(detail + "\n")
        sys.stdout.write(f"  名单  {source}\n")
        sys.stdout.write("  结果  results.db（./run.py web 查看）\n\n")

    return EXIT_OK if fail_count == 0 else EXIT_PARTIAL


# ---------------------------------------------------------------------------
# 统计 / 名称表
# ---------------------------------------------------------------------------


def run_stats(args):
    from cycle import stats as stats_mod
    try:
        result = stats_mod.run(
            sample_size=args.sample,
            seed=args.seed,
            progress=None if args.quiet else _stats_progress,
        )
    except ValueError as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        sys.stderr.write("\n中断。\n")
        return EXIT_INTERRUPT

    sys.stdout.write(stats_mod.format_report(result) + "\n")
    return EXIT_OK


def _stats_progress(done, total, code, ok, message):
    sys.stderr.write(f"\r[{done}/{total}] {code}    ")


def run_refresh_names():
    """拉全市场代码表写到 data/stock_names.csv。

    原版调 `data_stocklist`（一次返回全市场约 5000 只）。迁移后改走共享库的
    `scan` 域——那本来就是"某交易日的全市场横截面"，`symbol` + `name` 两列
    正是名称表要的东西，不必为一个衍生物再引入一个上游接口。

    `scan` 有多个交易日时取**最新那一天**：名称会随重组/改名变，
    用旧日期等于把改名前的老名字写进表里。
    """
    sys.stderr.write("取全市场代码表…\n")
    try:
        h = fetch.hub()
        # 用最新的一个交易日；日历里 `target` 就是"这次该取到哪天"
        rows = h.scan(h.target)
    except Exception as exc:                # noqa: BLE001
        sys.stderr.write(f"错误：{exc}\n")
        return EXIT_USAGE

    pairs = []
    seen = set()
    for r in rows:
        code = (r.get("symbol") or "").strip()
        name = (r.get("name") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        pairs.append((codes.parse(code) if code.startswith(("sh", "sz", "bj")) else code, name))

    if not pairs:
        sys.stderr.write("错误：横截面里没有可用的代码与名称\n")
        return EXIT_USAGE

    os.makedirs(os.path.dirname(config.STOCK_NAMES_CSV), exist_ok=True)
    with open(config.STOCK_NAMES_CSV, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("代码,名称\n")
        for code, name in pairs:
            fh.write(f"{code},{name}\n")

    sys.stdout.write(f"已写入 {len(pairs)} 只 → {config.STOCK_NAMES_CSV}\n")
    return EXIT_OK


# `_extract_stocklist()` 已删除：它解析的是 `data_stocklist` 那个上游接口的
# JSON 形状（一层层 walk 找 code/name）。现在名称表直接来自共享库 `scan` 表
# 的两个列，返回的是规整的 dict 列表，不需要"从任意嵌套里挖"。
# 留着它等于留一段"看起来还能用"的死代码，下一个人会以为还有别的调用方。


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    error = validate_args(args, parser)
    if error:
        sys.stderr.write(f"错误：{error}\n")
        parser.print_usage(sys.stderr)
        return EXIT_USAGE

    try:
        if args.stats:
            return run_stats(args)
        if args.refresh_names:
            return run_refresh_names()
        if args.stock:
            # 单股：output/cycle-elastic/，没有批次概念
            return run_single(args)
        return run_batch(args)
    except KeyboardInterrupt:
        sys.stderr.write("\n中断。\n")
        return EXIT_INTERRUPT
    except Exception as exc:
        sys.stderr.write(f"未预期异常：{exc}\n")
        import traceback
        traceback.print_exc()
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
