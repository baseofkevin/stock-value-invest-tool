#!/usr/bin/env python3
"""周期弹性初筛工具 —— 命令行入口（README.md「CLI 参考」）。

两种模式：
    单股票   python3 cycle_report.py --stock 600519
    批量     python3 cycle_report.py --csv 池.csv --workers 4

输出目录不开放命令行参数，统一由 `_out_dir_for()` 决定，四个工具都落在
`stock-value-invest-tool/output/<工具名>/` 下（见 output/README.md）：

    批量  output/cycle-elastic/<批次日期>/   （批次日期取自 CSV 文件名或「启动日」列）
    单股  output/cycle-elastic/              （没有批次概念，直接落根下）

原因：原版 `--out-dir` 默认 `PROJECT_ROOT/html-report`，又允许随意覆盖，
于是同一个工具的报告散在源码目录、`/tmp/初筛` 好几处，「哪份是最新的」
只能靠翻 mtime 猜。取消这个参数后产物位置是可预测的。

退出码（§6.5）：
    0   成功（含"历史不足但已出报告"）
    1   批量中有失败行
    2   参数错误 / 名称歧义 / 代码无法识别
    3   未预期异常
    130 用户中断
"""

import argparse
import difflib
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import config
from cycle import batch, build, codes, decide, fetch, index_html, metrics, render

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_UNEXPECTED = 3
EXIT_INTERRUPT = 130


# ---------------------------------------------------------------------------
# 输出目录
# ---------------------------------------------------------------------------
#
# 批次日期长这样：2026-11-13 / 2026-11-13T09:30 / 2026/11/13。
# CSV 文件名（选股_2026-11-13.csv）和「启动日」列都是这个形状。
#
# 与 quality-screen 的 `_out_dir_for()` 同构——两个工具的产物目录规则必须一致，
# 否则 output/ 下面一半带日期层、一半不带，等于没约定。

_DATE_RE = re.compile(r"(\d{4})[-/.](\d{2})[-/.](\d{2})")


def _date_from_name(path):
    """从文件名里抠出 YYYY-MM-DD；抠不到返回 None。

    取**最后一个**匹配：文件名可能是 `选股_2026-11-13_备份-2026-11-14.csv`，
    最后那个才是想表达的日期（也是重命名时最可能改的）。
    """
    hits = _DATE_RE.findall(Path(path).stem)
    if not hits:
        return None
    y, m, d = hits[-1]
    return f"{y}-{m}-{d}"


def _date_from_rows(rows, column="启动日"):
    """从「启动日」列取批次日期：取出现最多的那个非空值。

    用众数而不是第一行：CSV 可能混进别的交易日（拼接两次选股结果），
    第一行恰好是异类时整批报告会落到错的日期目录里。
    """
    counts = {}
    for row in rows or []:
        m = _DATE_RE.search(str(row.get(column) or ""))
        if not m:
            continue
        key = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: (counts[k], k))


def _csv_rows(path):
    """为算批次日期，把 CSV 读成 `[{列名: 值}]`。

    **为什么不复用 `batch.read_csv()`**：它只返回 `CsvRow(line_no, code, name)`，
    刻意不带其它列（它只关心"有几只票"）。批次日期要读「启动日」列，
    那是它不认的东西。这里独立读一份，读失败就返回空列表——
    拿不到日期只是退化成「用今天」，不该让整批任务因此中止。
    """
    try:
        import csv as _csv
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            return [dict(r) for r in _csv.DictReader(fh)]
    except Exception:                       # noqa: BLE001
        return []


def _out_dir_for(csv_path=None, rows=None):
    """本次运行该往哪个目录写报告。

    - 批量（给了 csv_path）：`output/cycle-elastic/<批次日期>/`，
      日期先看 CSV 文件名，再看「启动日」列，都没有才退化成今天。
    - 单股（csv_path=None）：`output/cycle-elastic/`，没有批次可言，不加日期层。

    没有「用户显式指定」这一路：`--out-dir` 参数已删除，见模块文档。
    """
    base = Path(os.path.expanduser(config.DEFAULT_OUT_DIR)).resolve()
    if csv_path is None:
        return str(base)
    batch_date = _date_from_name(csv_path) or _date_from_rows(rows)
    if not batch_date:
        batch_date = f"{datetime.now():%Y-%m-%d}"
    return str(base / batch_date)

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_UNEXPECTED = 3
EXIT_INTERRUPT = 130


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="cycle_report.py",
        description="周期弹性初筛：三个判断、三个指标（是不是周期股 / 弹性 / 周期位置）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 cycle_report.py --stock 600519\n"
            "  python3 cycle_report.py --stock 贵州茅台\n"
            "  python3 cycle_report.py --csv 池.csv --workers 4\n"
            "  python3 cycle_report.py --stats --sample 500\n"
        ),
    )

    source = p.add_mutually_exclusive_group()
    source.add_argument("--stock", metavar="代码或名称",
                        help="单股票模式：股票代码（600519 / sh600519）或公司名称")
    source.add_argument("--csv", metavar="路径",
                        help="批量模式：CSV 文件（含代码列，可选名称列）")

    p.add_argument("--name", default="",
                   help="覆盖报告内公司名（仅单股票模式）")
    p.add_argument("--workers", type=int, default=4,
                   help="批量并发线程数（默认 4）")
    p.add_argument("--force", action="store_true",
                   help="批量模式忽略已有报告，全部重算")
    p.add_argument("--no-index", action="store_true",
                   help="批量模式不生成索引页")
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

    if not args.stock and not args.csv:
        return "必须指定 --stock（单股票）或 --csv（批量）之一"

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


def run_single(args, out_dir):
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
    charts = metrics.series_for_chart(stock["quarters"], config)
    html = render.render(stock, decision, m, charts)

    batch.ensure_dir(out_dir)
    display_name = stock["name"] or code
    path = os.path.join(out_dir, batch.report_filename(display_name, code, timestamp))
    batch.write_atomic(path, html)

    if not args.quiet:
        _print_decision(stock, decision, m, path)
    return EXIT_OK


def resolve_name_nameonly(code):
    """名称表里查名字（取数没给 name 时的兜底）。"""
    return load_name_table().get(code, "")


def _print_decision(stock, decision, m, path):
    cycle = decision["cycle"]
    elastic = decision["elastic"]
    position = decision["position"]

    def fmt(value, digits=2, suffix=""):
        return "—" if value is None else f"{value:.{digits}f}{suffix}"

    # σ / 分位 / 比值都走 render 的格式化：精度与阈值边界一致性跟 HTML 报告同源。
    # 终端里同样不能出现「毛利率σ 5.0 pp」配「否」这种自相矛盾的读数。
    lines = [
        "",
        f"  {stock['name'] or stock['code']}  {stock['code']}"
        + (f"  {stock['industry']}" if stock["industry"] else ""),
        f"  {'-' * 46}",
        f"  是不是周期股    {_cycle_text(cycle['label']):6s} 毛利率σ "
        + render.fmt_pp(cycle["gm_std"], lower=config.GM_STD_CYCLE),
        f"  弹性            {_elastic_text(elastic['label']):6s} 比值 "
        + render.fmt_num(elastic["ratio"], 2, lower=config.ELASTIC_RATIO_MIN),
        f"  周期位置        {_position_text(position['label']):6s} 分位 "
        + render.fmt_pct(position["pct"],
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
    lines.append(f"  报告  {path}")
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


def run_batch(args, out_dir):
    if not os.path.exists(args.csv):
        sys.stderr.write(f"错误：CSV 不存在：{args.csv}\n")
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
        results, index_path = batch.run_batch(
            args.csv,
            out_dir=out_dir,
            workers=args.workers,
            force=args.force,
            progress=progress,
            build_index=not args.no_index,
        )
    except KeyboardInterrupt:
        sys.stderr.write("\n中断：已完成的报告已保留，索引页未生成。\n")
        return EXIT_INTERRUPT
    except ValueError as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return EXIT_USAGE

    # 重复行既不算成功也不算失败 —— CSV 里同一只股票写两遍是**用户的笔误**，
    # 不是取数失败。把它算进 fail 会让退出码无端变成 1，脚本据此判断就会误报。
    ok_count = sum(1 for r in results if r.ok)
    dup_count = sum(1 for r in results if getattr(r.row, "duplicate", False))
    fail_count = len(results) - ok_count - dup_count

    if not args.quiet:
        sys.stdout.write("\n")
        detail = f"  完成 {ok_count} 只，失败 {fail_count} 只"
        if dup_count:
            detail += f"，重复跳过 {dup_count} 只"
        sys.stdout.write(detail + "\n")
        if index_path:
            sys.stdout.write(f"  索引页  {index_path}\n")
        sys.stdout.write(f"  报告目录  {out_dir}\n\n")

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
            return run_single(args, _out_dir_for())
        # 批量：output/cycle-elastic/<批次日期>/，日期从 CSV 文件名或「启动日」列取
        return run_batch(args, _out_dir_for(args.csv, _csv_rows(args.csv)))
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
