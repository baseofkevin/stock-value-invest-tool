#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四个功能模块的统一入口。

    python3 run.py all             # 全流程：日历 → 选股 → 周期 → 质量（每周五盘后）
    python3 run.py menu            # 交互菜单
    python3 run.py -h              # 帮助
    python3 run.py select          # 只跑选股（stock_kline_screen）
    python3 run.py cycle           # 周期弹性批量（依赖当天的选股 CSV）
    python3 run.py cycle --stock 600519     # 周期弹性单只
    python3 run.py quality         # 质量初筛批量（依赖当天的选股 CSV）
    python3 run.py quality --stock 600519   # 质量初筛单只
    python3 run.py top sh600519    # 顶部判断（stock_judge_top，单只，随时可跑）
    python3 run.py top --csv 选股.csv           # 顶部判断批量：读 CSV 的代码列
    python3 run.py top --from-select            # 顶部判断批量：用当天选股结果

## 为什么要有这个脚本

四个模块各有各的跑法，而且有三处容易踩的坑，脚本把它们收进一处：

1. **`cycle_report.py` / `stock_report.py` 必须在自己目录里跑**（它们 `import config`
   是顶层导入，`python -m stock_cycle_elastic.cycle_report` 直接
   `ModuleNotFoundError: config`）。脚本替你把 cwd 切好。
2. **日历不更新会让下游静默产出上一周的报告**。`hub.target` 由 `target_date()`
   按「今天是交易日且过了 15:05」决定，而它先在日历里查今天；日历只到上周五时，
   `is_trading(今天)` 为假 → 回落到上周五 → 三件套安安静静再产一份上周的报告，
   文件名、目录名都看不出来。所以每一步之前都先**校验目标日在日历里**，
   不在就停下来并提示先跑 `build_calendar`（`--with-calendar` 可以自动更新）。
3. **下游依赖选股 CSV**。选股 0 只时下游没有输入，脚本在交接处就拦下来，
   不让它带着空表跑到一半才炸。

除此之外还有：逐步计时、全程日志（`output/logs/`）、盘前/盘中告警
（收盘宽限 15:05 之前跑等于拿半天的数据算「站上 MA20」）。

**不带参数运行时只打印帮助**，什么也不跑——全流程要三十多分钟，误敲一个回车就
开跑太危险了；想跑什么必须显式说出来（`all` / `select` / `cycle` / …）。

退出码：0 成功 / 1 某步失败 / 2 用法错误 / 130 用户中断。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
KLINE_CSV_DIR = OUTPUT / "kline-screen"
LOG_DIR = OUTPUT / "logs"

CLOSE_GRACE = (15, 5)
"""收盘宽限。与 `stock_data_fetch.config.CLOSE_GRACE` 保持一致：15:05 之前
上游给的是盘中价，用它算「收盘站上 MA20」这类判定本身就是错的。"""

MODULES = {
    "select": ("选股", "stock_kline_screen"),
    "cycle": ("周期弹性", "stock_cycle_elastic"),
    "quality": ("质量初筛", "stock_quality_screen"),
    "top": ("顶部判断", "stock_judge_top"),
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg, flush=True)


def hr(title: str) -> None:
    say("")
    say("=" * 68)
    say(title)
    say("=" * 68)


def load_calendar() -> list[str]:
    """交易日列表。读 `state/trading_calendar.json`（与 hub 用的是同一份）。

    直接解析 JSON 而不是 import `stock_data_fetch.calendar`：入口脚本要能在
    包被改坏时仍然给出「日历没更新」这类诊断，不该和被测代码同生共死。
    """
    path = ROOT / "state" / "trading_calendar.json"
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("days") or [])
    except (OSError, ValueError):
        return []


def pick_date(explicit: str | None, days: list[str]) -> str:
    """定下这次跑哪一天。

    `explicit` 为 "today"/None 时按「交易日 + 过了 15:05」取今天，否则取上一个交易日
    （与 `calendar.target_date` 同一套规则，避免入口和库内算法各说各话）。
    显式给的日期**必须是日历里的交易日**，否则直接报错——猜一个"最近的交易日"
    正是上一周那份静默报告的来源。
    """
    if explicit and explicit not in ("today", "latest"):
        day = explicit
        if days and day not in set(days):
            raise SystemExit(
                f"错误：{day} 不在交易日历里（日历 {days[0]} .. {days[-1]}）。\n"
                f"      周五 15:10 之后先跑：python3 -m stock_data_fetch.build_calendar")
        return day

    if not days:
        raise SystemExit("错误：没有交易日历（state/trading_calendar.json）。\n"
                         "      先跑：python3 -m stock_data_fetch.build_calendar")

    now = dt.datetime.now()
    today = now.date().isoformat()
    is_trading_today = today in set(days)
    if is_trading_today and now.time() >= dt.time(*CLOSE_GRACE):
        return today

    # 盘中（未过收盘宽限）要**排除今天**：否则 14:00 跑会拿今天的半日数据当基准日，
    # 而「收盘站上 MA20」这类判定在盘中根本不成立。
    prev = [d for d in days if (d < today if is_trading_today else d <= today)]
    if not prev:
        raise SystemExit(f"错误：交易日历早于 {today}，无法确定目标交易日。")

    chosen = prev[-1]
    if is_trading_today:
        say(f"⚠️  现在 {now:%H:%M}，还没过收盘宽限 15:05——今天的数据是盘中的，"
            f"改用上一个交易日 {chosen}。")
    else:
        say(f"⚠️  今天 {today} 不是交易日，本次按 {chosen} 跑。")
        # 周末跑是正常的（复盘），不该提示「去更新日历」；工作日却不是交易日，
        # 才可能是日历没更新。
        if now.weekday() < 5 and days[-1] < today:
            say(f"⚠️  日历最后一天是 {days[-1]}。若今天其实是交易日，"
                f"先跑：python3 -m stock_data_fetch.build_calendar")
    return chosen


class Tee:
    """同时写屏幕和日志文件。每一步的输出都留底，事后能查是哪一步慢。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("w", encoding="utf-8")

    def line(self, text: str) -> None:
        say(text)
        self.fh.write(text + "\n")
        self.fh.flush()

    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass


LAST_SPENT = {"s": 0.0}
"""最近一次 `run_step` 的耗时，给汇总表用（省得每个 step_* 都返回元组）。"""


def run_step(tee: Tee, title: str, argv: list[str], cwd: Path) -> tuple[int, float]:
    """跑一步，实时转发输出并计时。返回 (退出码, 秒)。"""
    hr(title)
    tee.line(f"$ {' '.join(argv)}    (cwd={cwd.relative_to(ROOT) if cwd != ROOT else '.'})")
    tee.line("")
    t0 = time.time()
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            errors="replace", bufsize=1)
    assert proc.stdout is not None
    for raw in proc.stdout:
        tee.line(raw.rstrip("\n"))
    code = proc.wait()
    spent = time.time() - t0
    LAST_SPENT["s"] = spent

    tee.line("")
    if code == 0:
        tee.line(f"✅ {title} 完成（{spent:.1f}s）")
    else:
        tee.line(f"❌ {title} 失败（退出码 {code}，{spent:.1f}s）")
    return code, spent


# ---------------------------------------------------------------------------
# 各模块
# ---------------------------------------------------------------------------

def py_module(name: str) -> list[str]:
    return [sys.executable, "-m", name]


def csv_for(day: str) -> Path:
    return KLINE_CSV_DIR / f"选股_{day}.csv"


def csv_rows(path: Path) -> int:
    """CSV 的**数据行数**（不含表头）。文件不存在返回 -1。"""
    if not path.exists():
        return -1
    with path.open(encoding="utf-8-sig") as fh:
        return max(0, sum(1 for _ in fh) - 1)


def step_select(tee: Tee, day: str, extra: list[str]) -> int:
    argv = py_module("stock_kline_screen") + ["--date", day] + extra
    code, _ = run_step(tee, f"① 选股 stock_kline_screen（{day}）", argv, ROOT)
    if code != 0:
        return code
    path = csv_for(day)
    n = csv_rows(path)
    if n < 0:
        tee.line(f"❌ 选股退出码 0，但没有产物：{path}")
        return 1
    if n == 0:
        tee.line(f"⚠️  {path.name} 只有表头——今天没有股票通过初筛。")
        tee.line("    下游（周期/质量）没有输入，流程到此为止。")
        return 3
    tee.line(f"📄 {path}  共 {n} 只")
    return 0


def step_cycle(tee: Tee, day: str, extra: list[str], stock: str | None = None) -> int:
    """周期弹性。`stock` 给了就是单只模式（不依赖当天 CSV）。"""
    if stock:
        argv = [sys.executable, "cycle_report.py", "--stock", stock] + extra
        code, _ = run_step(tee, f"② 周期弹性 stock_cycle_elastic（{stock} 单只）",
                           argv, ROOT / "stock_cycle_elastic")
        return code
    csv = csv_for(day)
    if csv_rows(csv) <= 0:
        tee.line(f"❌ 找不到可用的选股结果：{csv}")
        tee.line(f"    先跑选股：python3 run.py select --date {day}")
        return 2
    argv = [sys.executable, "cycle_report.py", "--csv", str(csv)] + extra
    code, _ = run_step(tee, f"② 周期弹性 stock_cycle_elastic（{day}）",
                       argv, ROOT / "stock_cycle_elastic")
    return code


def step_quality(tee: Tee, day: str, extra: list[str], stock: str | None = None) -> int:
    """质量初筛。`stock` 给了就是单只模式（不依赖当天 CSV）。"""
    if stock:
        argv = [sys.executable, "stock_report.py", "--stock-code", stock] + extra
        code, _ = run_step(tee, f"③ 质量初筛 stock_quality_screen（{stock} 单只）",
                           argv, ROOT / "stock_quality_screen")
        return code
    csv = csv_for(day)
    if csv_rows(csv) <= 0:
        tee.line(f"❌ 找不到可用的选股结果：{csv}")
        tee.line(f"    先跑选股：python3 run.py select --date {day}")
        return 2
    argv = [sys.executable, "stock_report.py", "--csv", str(csv)] + extra
    code, _ = run_step(tee, f"③ 质量初筛 stock_quality_screen（{day}）",
                       argv, ROOT / "stock_quality_screen")
    return code


def note_single_date(tee: Tee, day: str, wanted: str | None) -> int:
    """单只模式的基准日由程序自己算，`--date` 传不进去。

    `cycle_report.py --stock` 与 `stock_report.py --stock-code` 都没有日期参数，
    它们内部走 `hub.target`（与 `pick_date` 同一套规则：今天是交易日且过了 15:05
    就用今天，否则上一个交易日）。所以这里只做一致性检查：如果用户显式要的日期
    和程序会用的那一天不一样，直接说清楚，别让他以为跑的是那一天。
    """
    if wanted and wanted not in ("today", "latest") and wanted != day:
        tee.line(f"❌ 单只模式的基准日由程序自己决定（本次为 {day}），"
                 f"不接受 --date {wanted}。")
        tee.line("    要按历史某天出报告，只能走批量模式（--csv）。")
        return 2
    tee.line(f"ℹ️  单只模式按 {day} 出报告（程序内部取「今天或上一个交易日」）。")
    return 0


def step_top(tee: Tee, day: str, codes: list[str], extra: list[str],
             csv_path: Path | None = None, workers: int = 4) -> int:
    """顶部判断：**随时可跑**，单只或多只，也可以整份 CSV 批量。

    批量走 `stock_judge_top --csv`（程序自己读代码列、自己并发），比在这里
    一只只 fork 快得多，失败也不会中断其余的。
    """
    if csv_path is not None:
        if csv_rows(csv_path) <= 0:
            tee.line(f"❌ 找不到可用的股票清单：{csv_path}")
            return 2
        argv = py_module("stock_judge_top") + [
            "--csv", str(csv_path), "--date", day, "--workers", str(workers)] + extra
        rc, _ = run_step(tee, f"④ 顶部判断 stock_judge_top（批量 {csv_rows(csv_path)} 只 @ {day}）",
                         argv, ROOT)
        return rc

    if not codes:
        tee.line("❌ 顶部判断需要至少一个股票代码（或用 --csv / --from-select 批量）。")
        return 2
    failed = []
    for code in codes:
        argv = py_module("stock_judge_top") + [code, "--date", day] + extra
        code_rc, _ = run_step(tee, f"④ 顶部判断 stock_judge_top（{code} @ {day}）",
                              argv, ROOT)
        if code_rc not in (0,):
            failed.append(code)
    if failed:
        tee.line(f"❌ 失败的代码：{'、'.join(failed)}")
        return 1
    return 0


def step_calendar(tee: Tee) -> int:
    code, _ = run_step(tee, "⓪ 更新交易日历 build_calendar",
                       py_module("stock_data_fetch.build_calendar"), ROOT)
    return code


def ensure_calendar(tee: Tee, day_wanted: str | None, auto: bool) -> tuple[str, int]:
    """必要时更新日历，再定下这次的目标日。

    先看「日历最后一天是否 >= 今天」：不够就说明上游可能有新的交易日，
    这正是每周五必须先跑的那一步。
    """
    days = load_calendar()
    now = dt.datetime.now()
    today = now.date().isoformat()
    stale = (not days) or days[-1] < today
    # 周末不自动更新：周六周日跑一次只会白打一次上游，而日历本来就该停在上周五。
    weekday = now.weekday() < 5
    if stale and auto and weekday:
        rc = step_calendar(tee)
        if rc != 0:
            tee.line("❌ 日历更新失败。没有日历，下游会回落到上周五并静默产出旧报告。")
            return "", rc
        days = load_calendar()
    elif stale and days and weekday:
        tee.line(f"⚠️  日历最后一天是 {days[-1]}，今天 {today}。")
        tee.line("    如果是交易日且已收盘，请先跑：python3 -m stock_data_fetch.build_calendar")
        tee.line("    （否则下游会回落到上一个交易日，产出一份旧报告）")

    try:
        return pick_date(day_wanted, days), 0
    except SystemExit as exc:
        tee.line(str(exc))
        return "", 1


def summary(tee: Tee, day: str, steps: list[tuple[str, int, float]]) -> None:
    hr(f"汇总（{day}）")
    for name, rc, spent in steps:
        mark = "✅" if rc == 0 else ("⚠️" if rc == 3 else "❌")
        tee.line(f"  {mark} {name:<28s} {spent:6.1f}s   退出码 {rc}")
    tee.line("")
    tee.line(f"  选股 CSV    {csv_for(day)}")
    tee.line(f"  周期报告    {OUTPUT / 'cycle-elastic' / day}")
    tee.line(f"  质量报告    {OUTPUT / 'quality-screen' / day}")
    tee.line(f"  顶部判断    {OUTPUT / 'judge-top'}")
    tee.line(f"  本次日志    {tee.fh.name}")


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_all(args, tee: Tee) -> int:
    day, rc = ensure_calendar(tee, args.date, auto=not args.no_calendar)
    if not day:
        return rc or 1

    steps: list[tuple[str, int, float]] = []
    rc = step_select(tee, day, args.select_args)
    steps.append(("① 选股", rc, LAST_SPENT["s"]))
    if rc != 0:
        summary(tee, day, steps)
        return rc

    if args.skip_cycle:
        tee.line("（按 --skip-cycle 跳过周期弹性）")
    else:
        rc = step_cycle(tee, day, args.cycle_args)
        steps.append(("② 周期弹性", rc, LAST_SPENT["s"]))
        if rc != 0 and not args.keep_going:
            summary(tee, day, steps)
            return rc

    if args.skip_quality:
        tee.line("（按 --skip-quality 跳过质量初筛）")
    else:
        rc = step_quality(tee, day, args.quality_args)
        steps.append(("③ 质量初筛", rc, LAST_SPENT["s"]))
        if rc != 0 and not args.keep_going:
            summary(tee, day, steps)
            return rc

    summary(tee, day, steps)
    return 0


def cmd_single(action: str):
    def run(args, tee: Tee) -> int:
        day, rc = ensure_calendar(tee, args.date, auto=not args.no_calendar)
        if not day:
            return rc or 1
        if action == "select":
            return step_select(tee, day, args.extra)
        if action in ("cycle", "quality"):
            stock = getattr(args, "stock", None)
            if stock:
                rc = note_single_date(tee, day, args.date)
                if rc:
                    return rc
                return (step_cycle if action == "cycle" else step_quality)(
                    tee, day, args.extra, stock)
            return (step_cycle if action == "cycle" else step_quality)(
                tee, day, args.extra)
        if action == "top":
            csv_path = None
            if getattr(args, "from_select", False):
                csv_path = csv_for(day)
            elif getattr(args, "csv", None):
                csv_path = Path(args.csv).expanduser()
                if not csv_path.is_absolute():
                    csv_path = ROOT / csv_path
            return step_top(tee, day, args.codes or [], args.extra,
                            csv_path=csv_path, workers=getattr(args, "workers", 4))
        raise AssertionError(action)
    return run


def cmd_calendar(args, tee: Tee) -> int:
    rc = step_calendar(tee)
    days = load_calendar()
    if days:
        tee.line(f"日历：{days[0]} .. {days[-1]}（{len(days)} 个交易日）")
    return rc


# ---------------------------------------------------------------------------
# 交互菜单
# ---------------------------------------------------------------------------

MENU = """
  1) 全流程：日历 → 选股 → 周期弹性 → 质量初筛     ← 每周五盘后
  2) 只跑选股            stock_kline_screen
  3) 周期弹性            stock_cycle_elastic     （回车=批量，或填单只代码）
  4) 质量初筛            stock_quality_screen    （回车=批量，或填单只代码）
  5) 顶部判断            stock_judge_top         （单只/多只；回车=当天选股批量）
  6) 只更新交易日历      build_calendar
  0) 退出
"""


def interactive() -> int:
    days = load_calendar()
    span = f"{days[0]} .. {days[-1]}" if days else "（无日历）"
    say(f"股票研究工具箱 —— 交易日历：{span}")

    while True:
        say(MENU)
        try:
            choice = input("选择 [0-6]: ").strip()
        except (EOFError, KeyboardInterrupt):
            say("")
            return 130
        if choice in ("0", "q", ""):
            return 0
        if choice not in {"1", "2", "3", "4", "5", "6"}:
            say("请输入 0-6。")
            continue

        day_hint = input(f"基准日（回车=自动，今天 {dt.date.today()}）: ").strip() or None

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        tee = Tee(LOG_DIR / f"run-{stamp}.log")
        try:
            if choice == "1":
                args = argparse.Namespace(date=day_hint, no_calendar=False,
                                          select_args=[], cycle_args=["--workers", "2"],
                                          quality_args=["--workers", "2"],
                                          skip_cycle=False, skip_quality=False,
                                          keep_going=False)
                rc = cmd_all(args, tee)
            elif choice == "2":
                args = argparse.Namespace(date=day_hint, no_calendar=False, extra=[])
                rc = cmd_single("select")(args, tee)
            elif choice in ("3", "4"):
                who = "周期弹性" if choice == "3" else "质量初筛"
                code = input(f"{who}——股票代码（回车=批量跑当天选股 CSV）: ").strip()
                args = argparse.Namespace(date=day_hint, no_calendar=False,
                                          stock=code or None,
                                          extra=[] if code else ["--workers", "2"])
                rc = cmd_single("cycle" if choice == "3" else "quality")(args, tee)
            elif choice == "5":
                raw = input("股票代码（可多个，空格分隔；回车=用当天选股结果批量跑）: ").split()
                args = argparse.Namespace(date=day_hint, no_calendar=False,
                                          codes=raw, csv=None,
                                          from_select=not raw, workers=4, extra=[])
                rc = cmd_single("top")(args, tee)
            else:
                args = argparse.Namespace()
                rc = cmd_calendar(args, tee)
        except KeyboardInterrupt:
            say("\n已中断。")
            return 130
        finally:
            tee.close()

        say("")
        say(f"—— 退出码 {rc}；日志 {tee.fh.name}")
        try:
            input("回车回到菜单…")
        except (EOFError, KeyboardInterrupt):
            say("")
            return 0


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="四个功能模块的统一入口。不带参数只打印帮助；"
                    "跑全流程用 all，想选着跑用 menu。",
        epilog="""示例：
  ./run.py all                            # 周五盘后全流程（日历→选股→周期→质量）
  ./run.py menu                           # 交互菜单
  ./run.py select --date 2026-11-20       # 只跑选股，指定基准日
  ./run.py cycle   --stock 600519         # 周期弹性，单只
  ./run.py quality --stock 600519         # 质量初筛，单只
  ./run.py top     600519                 # 顶部判断

不带参数只打印本帮助，不跑任何程序（全流程要三十多分钟，免得误敲回车就开跑）。

下游（cycle/quality）认的是 output/kline-screen/选股_<基准日>.csv，
所以它们的 --date 必须和那天的选股一致；不给就按「今天（过了 15:05）」算。""")
    sub = p.add_subparsers(dest="cmd")

    def add_date(sp):
        sp.add_argument("--date", default=None,
                        help="基准交易日 YYYY-MM-DD；today=自动（默认）")
        sp.add_argument("--no-calendar", action="store_true",
                        help="即使日历过期也不要自动更新")

    sp = sub.add_parser("all", aliases=["weekly"],
                        help="全流程：日历 → 选股 → 周期 → 质量")
    add_date(sp)
    sp.add_argument("--skip-cycle", action="store_true", help="跳过周期弹性")
    sp.add_argument("--skip-quality", action="store_true", help="跳过质量初筛")
    sp.add_argument("--keep-going", action="store_true", help="某一步失败也继续往下跑")
    sp.add_argument("--select-args", default="", help="透传给 stock_kline_screen 的参数")
    sp.add_argument("--cycle-args", default="--workers 2", help="透传给 cycle_report.py")
    sp.add_argument("--quality-args", default="--workers 2", help="透传给 stock_report.py")

    sp = sub.add_parser("select", help="只跑选股 stock_kline_screen（只能批量）")
    add_date(sp)
    sp.add_argument("extra", nargs="*", default=[], help="透传给 stock_kline_screen 的参数")

    for name, helptext, flag in (
            ("cycle", "只跑周期弹性 stock_cycle_elastic", "--stock"),
            ("quality", "只跑质量初筛 stock_quality_screen", "--stock-code")):
        sp = sub.add_parser(name, help=helptext + "（批量或单只）")
        add_date(sp)
        sp.add_argument("--stock", metavar="代码", default=None,
                        help=f"单只模式：只跑这一只（转成 {flag}），不需要当天的选股 CSV")
        sp.add_argument("extra", nargs="*", default=[], help="透传给该程序的参数")

    sp = sub.add_parser("top", help="顶部判断 stock_judge_top（单只/多只/CSV 批量）")
    add_date(sp)
    sp.add_argument("codes", nargs="*", default=[],
                    help="股票代码，如 sh600519 600519 600519.SH（与 --csv 二选一）")
    sp.add_argument("--csv", metavar="CSV", default=None,
                    help="批量：从该 CSV 读代码列（代码/股票代码/证券代码/code）")
    sp.add_argument("--from-select", action="store_true",
                    help="批量：直接用当天选股结果 output/kline-screen/选股_<基准日>.csv")
    sp.add_argument("--workers", type=int, default=4, help="批量并发线程数，默认 4")
    sp.add_argument("extra", nargs="*", default=[], help="透传给 stock_judge_top 的参数")

    sp = sub.add_parser("calendar", help="只更新交易日历")
    sub.add_parser("menu", help="交互菜单（0-6 选择）")
    return p


def split_passthrough(text: str) -> list[str]:
    return [x for x in text.split() if x]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # 裸跑只给帮助，**绝不**顺手跑点什么：全流程三十多分钟，误敲回车就开跑
        # 比"少敲一个词"的代价大得多。
        build_parser().print_help()
        say("")
        say("提示：跑全流程用 ./run.py all；想选着跑用 ./run.py menu。")
        return 2

    # `parse_known_args`：单只/单模块模式下要能透传未知参数（--force、--workers…）。
    # 用 `nargs="*"` 接不住以 `-` 开头的参数，argparse 会直接报 unrecognized。
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2
    if args.cmd in ("all", "weekly"):
        if unknown:
            say(f"❌ weekly 不认识这些参数：{' '.join(unknown)}")
            say("   透传请用 --select-args / --cycle-args / --quality-args。")
            return 2
    else:
        args.extra = list(getattr(args, "extra", []) or []) + list(unknown)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    tee = Tee(LOG_DIR / f"run-{stamp}.log")
    try:
        if args.cmd == "menu":
            tee.close()
            return interactive()
        if args.cmd in ("all", "weekly"):
            args.select_args = split_passthrough(getattr(args, "select_args", ""))
            args.cycle_args = split_passthrough(getattr(args, "cycle_args", ""))
            args.quality_args = split_passthrough(getattr(args, "quality_args", ""))
            return cmd_all(args, tee)
        if args.cmd == "calendar":
            return cmd_calendar(args, tee)
        handler = cmd_single(args.cmd)
        return handler(args, tee)
    except KeyboardInterrupt:
        say("\n已中断。")
        return 130
    finally:
        tee.close()


if __name__ == "__main__":
    sys.exit(main())
