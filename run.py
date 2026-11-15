#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四个功能模块的统一入口。

    python3 run.py all             # 全流程：日历 → 选股 → 周期 → 质量 → 顶部 → 看板
    python3 run.py menu            # 交互菜单
    python3 run.py -h              # 帮助
    python3 run.py select          # 只跑选股（stock_kline_screen）
    python3 run.py cycle           # 周期弹性批量（读结果库里当天的选股名单）
    python3 run.py cycle --stock 600519     # 周期弹性单只
    python3 run.py quality         # 质量初筛批量（读结果库里当天的选股名单）
    python3 run.py quality --stock 600519   # 质量初筛单只
    python3 run.py top sh600519    # 顶部判断（stock_judge_top，单只，随时可跑）
    python3 run.py top --from-select            # 顶部判断批量：读结果库的名单
    python3 run.py web                          # 打开看板（Flask + Chart.js）

## 为什么要有这个脚本

四个模块各有各的跑法，而且有三处容易踩的坑，脚本把它们收进一处：

1. **`cycle_report.py` / `stock_report.py` 必须在自己目录里跑**（它们 `import config`
   是顶层导入，`python -m stock_cycle_elastic.cycle_report` 直接
   `ModuleNotFoundError: config`）。脚本替你把 cwd 切好。
2. **日历不更新会让下游静默产出上一周的报告**。`hub.target` 由 `target_date()`
   按「今天是交易日且过了 15:05」决定，而它先在日历里查今天；日历只到上周五时，
   `is_trading(今天)` 为假 → 回落到上周五 → 三件套安安静静再产一份上周的报告，
   文件名、目录名都看不出来。所以每一步之前都先**校验目标日在日历里**，
   不在就停下来并提示先跑 `build_calendar`（`all` 默认会自动更新）。
3. **下游依赖选股名单**。选股 0 只时下游没有输入，脚本在交接处就拦下来，
   不让它带着空表跑到一半才炸。

除此之外还有：逐步计时、全程日志（`logs/`）、盘前/盘中告警
（收盘宽限 15:05 之前跑等于拿半天的数据算「站上 MA20」）。

**不带参数运行时只打印帮助**，什么也不跑——全流程要三十多分钟，误敲一个回车就
开跑太危险了；想跑什么必须显式说出来（`all` / `select` / `cycle` / …）。

日历没有单独的子命令：它只是全流程的第 0 步，`all` 会自己按需更新
（日历最后一天早于今天就先跑 `build_calendar`）。真需要单独更新时，直接用
`python3 -m stock_data_fetch.build_calendar`——这条命令四个工具和 README 里都写着，
比在入口再挂一个 `calendar` 子命令少一处迷惑。

退出码：0 成功 / 1 某步失败 / 2 用法错误 / 130 用户中断。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
#: 运行日志。原来放在 `output/logs/`，而 `output/` 下只剩这一个用处，
#: 于是把目录本身删掉、日志直接落在仓库根的 `logs/`（文件名带时间戳，好翻）。
LOG_DIR = ROOT / "logs"
DB_PATH = ROOT / "results.db"
"""派生结果库：四个工具的结论落在这里，看板也读它。"""

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


#: 逐只失败的播报行。同样的信息紧接着就有聚合汇总（「145 只历史不足」、
#: 「完成 35 只，失败 3 只」），一行一只股就会把真正的结果挤出屏幕——
#: 2026-11-15 那次全流程里，这类行占了全部输出的一半以上。
#: 控制台省掉，**日志文件里一行不少**；要现场看就加 `--verbose`。
_NOISE = (
    re.compile(r"^kline \S+ 失败："),      # stock_data_fetch.api 逐只 K 线失败
    re.compile(r"^\[\d+/\d+\] FAIL "),   # cycle/quality 逐只
    re.compile(r"^\[FAIL\]"),
)


def _is_noise(text: str) -> bool:
    return any(p.match(text) for p in _NOISE)


class Tee:
    """同时写屏幕和日志文件。每一步的输出都留底，事后能查是哪一步慢。

    屏幕走"关键信息"路线（噪声行只计数、不打印），日志文件保持全量。
    """

    def __init__(self, path: Path, verbose: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("w", encoding="utf-8")
        self.verbose = verbose
        self.muted = 0          # 控制台省掉的行数（带 --verbose 恒为 0）
        self._blank = False     # 上一条是不是空行（折叠连续空行）

    def line(self, text: str) -> None:
        self.fh.write(text + "\n")
        self.fh.flush()
        if not self.verbose and _is_noise(text):
            self.muted += 1
            return
        if not text and self._blank:
            return              # 连续空行压成一行
        self._blank = not text
        say(text)

    def close(self) -> None:
        """可重复调用：`all` 起看板前要先把日志收掉（后面是长驻进程）。"""
        fh = getattr(self, "fh", None)
        if fh is None:
            return
        try:
            fh.close()
        except OSError:
            pass
        self.fh = None


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
    tee.muted = 0
    for raw in proc.stdout:
        tee.line(raw.rstrip("\n"))
    muted, tee.muted = tee.muted, 0
    code = proc.wait()
    spent = time.time() - t0
    LAST_SPENT["s"] = spent

    if muted:
        tee.line(f"    （省掉 {muted} 行逐只失败播报，日志里有；要看就加 --verbose）")
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


def select_count(day: str) -> int:
    """当天选股名单的只数（读 results.db 里基准日为 day 的那次运行）。

    原来是数 `选股_<day>.csv` 的行数。CSV 取消后，名单只在结果库里——
    交接判断从"文件在不在"变成"这次运行有没有留下记录"，更贴近事实：
    即使选股跑了、但一只都没选中，也是 0 行，而不是"文件缺失"。
    读不到库（还没跑过）返回 -1，与原来"文件不存在"同一套语义。
    """
    try:
        from stock_data_fetch import results as results_db
        return len(results_db.symbols_for("select", day))
    except Exception:                              # noqa: BLE001
        return -1


def step_stats(tool: str, day: str) -> tuple[int, int]:
    """该批次最新一次运行的 (成功, 失败)；读不到给 (-1, -1)。"""
    try:
        from stock_data_fetch import results as results_db
        info = results_db.run_stats(tool, day, kind="batch")
    except Exception:                              # noqa: BLE001
        return -1, -1
    return int(info.get("n_ok", -1)), int(info.get("n_fail", -1))


def judge_step(tee: Tee, title: str, tool: str, day: str, rc: int,
               strict: bool) -> bool:
    """这一步要不要中断整条流程（True = 可以往下走）。

    判据是**有没有产出**，不是"退出码是不是 0"。上游对个别小票会返回空
    （利润表为空、K 线历史不足、停牌退市），周期弹性因此以退出码 1 结束，
    但它已经把 35 只写进了库。这时停掉整条链路，等于让 3 只票的数据缺口
    把每周的批量流程废掉——而这 3 只下周多半还是缺。

    反过来，一只都没成功（例如名单对不上、库被锁住）必须停：再往下跑
    只会产出一份缺一条腿的结果。`--strict` 可以恢复"任何失败都停"。
    """
    if rc == 0:
        return True
    n_ok, n_fail = step_stats(tool, day)
    if strict or n_ok <= 0:
        return False
    tee.line("")
    tee.line(f"⚠️  {title} 有 {n_fail} 只失败，但已写入 {n_ok} 只 → 继续往下跑")
    tee.line("    失败的多是上游缺数据（利润表为空 / K 线历史不足），看板里这几只留空。")
    tee.line("    要「有任何失败就停」，加 --strict。")
    return True


def step_select(tee: Tee, day: str, extra: list[str]) -> int:
    argv = py_module("stock_kline_screen") + ["--date", day] + extra
    code, _ = run_step(tee, f"① 选股 stock_kline_screen（{day}）", argv, ROOT)
    if code != 0:
        return code
    n = select_count(day)
    if n <= 0:
        tee.line(f"⚠️  {day} 没有股票通过初筛（结果库里 0 行）。")
        tee.line("    下游（周期/质量）没有输入，流程到此为止。")
        return 3
    tee.line(f"📄 results.db  select_result  共 {n} 只")
    return 0


def step_cycle(tee: Tee, day: str, extra: list[str], stock: str | None = None) -> int:
    """周期弹性。`stock` 给了就是单只模式（不依赖当天名单）。"""
    if stock:
        argv = [sys.executable, "cycle_report.py", "--stock", stock] + extra
        code, _ = run_step(tee, f"② 周期弹性 stock_cycle_elastic（{stock} 单只）",
                           argv, ROOT / "stock_cycle_elastic")
        return code
    n = select_count(day)
    if n <= 0:
        tee.line(f"❌ 结果库里没有 {day} 的选股名单")
        tee.line(f"    先跑选股：python3 run.py select --date {day}")
        return 2
    argv = [sys.executable, "cycle_report.py", "--from-select", day] + extra
    code, _ = run_step(tee, f"② 周期弹性 stock_cycle_elastic（{day}）",
                       argv, ROOT / "stock_cycle_elastic")
    return code


def step_quality(tee: Tee, day: str, extra: list[str], stock: str | None = None) -> int:
    """质量初筛。`stock` 给了就是单只模式（不依赖当天名单）。"""
    if stock:
        argv = [sys.executable, "stock_report.py", "--stock-code", stock] + extra
        code, _ = run_step(tee, f"③ 质量初筛 stock_quality_screen（{stock} 单只）",
                           argv, ROOT / "stock_quality_screen")
        return code
    n = select_count(day)
    if n <= 0:
        tee.line(f"❌ 结果库里没有 {day} 的选股名单")
        tee.line(f"    先跑选股：python3 run.py select --date {day}")
        return 2
    argv = [sys.executable, "stock_report.py", "--from-select", day] + extra
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
    """顶部判断：**随时可跑**，单只或多只，也可以整份名单批量。

    批量走 `stock_judge_top --from-select <day>`（程序自己读结果库、自己并发），
    比在这里一只只 fork 快得多，失败也不会中断其余的。
    """
    if csv_path is not None:
        n = select_count(day)
        if n <= 0:
            tee.line(f"❌ 结果库里没有 {day} 的选股名单")
            return 2
        argv = py_module("stock_judge_top") + [
            "--from-select", day, "--date", day, "--workers", str(workers)] + extra
        if tee.verbose and "--verbose" not in argv and "-v" not in argv:
            argv.append("--verbose")
        rc, _ = run_step(tee, f"④ 顶部判断 stock_judge_top（批量 {n} 只 @ {day}）",
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
        # 3 = 选股 0 只（交接处拦下），1 = 有失败但可能已部分写入
        mark = "✅" if rc == 0 else ("⚠️" if rc in (1, 3) else "❌")
        tee.line(f"  {mark} {name:<28s} {spent:6.1f}s   退出码 {rc}")
    tee.line("")
    tee.line(f"  结果库      {DB_PATH}（看板：./run.py web）")
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
        if not judge_step(tee, "② 周期弹性", "cycle", day, rc, args.strict) \
                and not args.keep_going:
            summary(tee, day, steps)
            return rc

    if args.skip_quality:
        tee.line("（按 --skip-quality 跳过质量初筛）")
    else:
        rc = step_quality(tee, day, args.quality_args)
        steps.append(("③ 质量初筛", rc, LAST_SPENT["s"]))
        if not judge_step(tee, "③ 质量初筛", "quality", day, rc, args.strict) \
                and not args.keep_going:
            summary(tee, day, steps)
            return rc

    if args.skip_top:
        tee.line("（按 --skip-top 跳过顶部判断）")
    else:
        rc = step_top(tee, day, [], args.top_args,
                      csv_path="results", workers=args.top_workers)
        steps.append(("④ 顶部判断", rc, LAST_SPENT["s"]))
        if not judge_step(tee, "④ 顶部判断", "top", day, rc, args.strict) \
                and not args.keep_going:
            summary(tee, day, steps)
            return rc

    summary(tee, day, steps)

    # 最后一步是"看结果"：起看板。它是长驻进程，所以放在最后、也是唯一
    # 需要人按 Ctrl-C 才结束的一步。自动化里用 --no-web 跳过。
    if args.no_web:
        tee.line("")
        tee.line("（按 --no-web 不起看板；随时用 ./run.py web 打开）")
        return 0
    return step_web(args, tee)


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
                csv_path = "results"          # 只是一个"走批量"的开关
            elif getattr(args, "csv", None):
                csv_path = Path(args.csv).expanduser()
                if not csv_path.is_absolute():
                    csv_path = ROOT / csv_path
            return step_top(tee, day, args.codes or [], args.extra,
                            csv_path=csv_path, workers=getattr(args, "workers", 4))
        raise AssertionError(action)
    return run


# ---------------------------------------------------------------------------
# 看板
# ---------------------------------------------------------------------------

def cmd_web(args) -> int:
    """起本地看板。

    **不套 Tee**：Tee 是给"跑完就退出"的四步用的，看板是长驻进程，
    套上它反而让 Ctrl-C 退出后日志文件留着半截。Flask 自己的访问日志够用。
    """
    try:
        from web.app import create_app
    except ModuleNotFoundError as exc:
        say(f"❌ 起不来看板：{exc}")
        say("   Flask 不是标准库，需要装一次：")
        say(f"   {sys.executable} -m pip install flask")
        return 2
    app = create_app(DB_PATH)
    say(f"看板：http://{args.host}:{args.port}   结果库 {DB_PATH}")
    say("       Ctrl-C 退出")
    app.run(host=args.host, port=args.port, debug=getattr(args, "debug", False))
    return 0


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------

#: 备份文件名形如 `shared.db.20261115-143000`。
#: 保留策略**只认这个格式**：手工改名的（如 `shared.db.20261115-before-v6`）
#: 是老版本留下的回滚点，一律不删——删掉别人手写的文件不是"清理"，是事故。
BACKUP_RE = re.compile(r"^(shared|results)\.db\.(\d{8}-\d{6})$")
DB_FILES = (("shared", "shared.db"), ("results", "results.db"))


def _backup_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    return ROOT / "backup"


def _existing_backups(where: Path, name: str) -> list[Path]:
    """某库已有的、由本命令生成的备份（按时间戳升序）。"""
    if not where.is_dir():
        return []
    out = []
    for p in where.iterdir():
        m = BACKUP_RE.match(p.name)
        if m and m.group(1) == name and p.is_file():
            out.append(p)
    return sorted(out, key=lambda p: p.name)


def _backup_one(tee: Tee, name: str, src: Path, where: Path, stamp: str,
                keep: int) -> int:
    """VACUUM INTO 一份备份，校验完整性与表数，然后按 keep 清理旧份。

    为什么用 `VACUUM INTO` 而不是 `cp`：
      · shared.db 是 WAL 模式，直接 cp 可能拷到一个"缺最新提交"的库，
        `-wal` 文件不在或已被 checkpoint 掉时，拷出来的库会缺数据；
      · VACUUM INTO 走 SQLite 的一致性读，且顺带整理碎片（体积小很多）。

    为什么要校验：备份存在的意义是"能恢复"。拷完不做 `integrity_check`
    就等于把发现损坏的时间推迟到真出事那天。
    """
    dest = where / f"{src.name}.{stamp}"
    if dest.exists():
        tee.line(f"⚠️  {dest.name} 已存在，跳过")
        return 0
    tee.line(f"⏳ 备份 {src.name}（{src.stat().st_size / 1024 / 1024:.0f} MB）→ {dest}")
    src_conn = dest_conn = None
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        src_conn.execute("vacuum into ?", (str(dest),))
    except Exception as e:                          # noqa: BLE001
        if dest.exists():
            dest.unlink(missing_ok=True)
        tee.line(f"❌ 备份失败（{src.name}）：{e}")
        return 1
    finally:
        if src_conn is not None:
            src_conn.close()

    try:
        dest_conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        ok = dest_conn.execute("pragma integrity_check").fetchone()
        ok = bool(ok and ok[0] == "ok")
        n_tab = dest_conn.execute(
            "select count(*) from sqlite_master where type='table'").fetchone()[0]
        ver = dest_conn.execute("pragma user_version").fetchone()[0]
    except Exception as e:                          # noqa: BLE001
        ok, n_tab, ver, err = False, 0, 0, str(e)
    else:
        err = ""
    finally:
        if dest_conn is not None:
            dest_conn.close()

    if not ok:
        dest.unlink(missing_ok=True)
        tee.line(f"❌ 备份校验失败，已删除：{err or 'integrity_check 不通过'}")
        return 1
    size = dest.stat().st_size / 1024 / 1024
    tee.line(f"✅ {dest.name}  {size:.0f} MB · {n_tab} 张表 · schema v{ver} · 完整性 ok")

    olds = _existing_backups(where, name)
    if keep > 0 and len(olds) > keep:
        for old in olds[:len(olds) - keep]:
            old.unlink(missing_ok=True)
            tee.line(f"   清理旧备份 {old.name}")
    return 0


def cmd_backup(args, tee: Tee) -> int:
    where = _backup_dir(args.dir)
    keep = max(0, args.keep)
    todo = [(n, ROOT / f) for n, f in DB_FILES if args.only in (None, n)]
    if args.list_only:
        tee.line(f"备份目录：{where}")
        found = False
        for name, _ in DB_FILES:
            for p in _existing_backups(where, name):
                found = True
                tee.line(f"  {p.name}  {p.stat().st_size / 1024 / 1024:.0f} MB")
        if not found:
            tee.line("  （还没有由本命令生成的备份）")
        return 0

    where.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    rc = 0
    done = 0
    for name, src in todo:
        if not src.exists():
            tee.line(f"⚠️  {src.name} 不存在，跳过")
            continue
        if _backup_one(tee, name, src, where, stamp, keep) != 0:
            rc = 1
        else:
            done += 1
    if done == 0 and rc == 0:
        tee.line("没有可备份的库。")
    else:
        tee.line(f"备份完成：{done} 个库 → {where}（保留 {keep if keep else '不限'} 份）")
    return rc


def step_web(args, tee: Tee) -> int:
    """全流程的最后一步：把看板起起来（阻塞，Ctrl-C 退出）。

    为什么要放在流程里：跑完六步之后人想看的正是"这一批结果"，而看板的
    默认页就是最新批次。省掉"再敲一条命令、再记一次端口"。
    """
    hr("⑥ 看板 ./run.py web（Ctrl-C 退出，退出后本次流程结束）")
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 5000)
    tee.line(f"打开 http://{host}:{port} 看这一批（{db_hint()}）")
    # 先把日志收掉：后面是长驻进程，它的访问日志写进本文件只会把有用的几行冲散。
    tee.close()
    say("")
    try:
        return cmd_web(argparse.Namespace(host=host, port=port, debug=False))
    except KeyboardInterrupt:
        say("")
        say("看板已关闭。")
        return 0


def db_hint() -> str:
    return DB_PATH.name


# ---------------------------------------------------------------------------
# 交互菜单
# ---------------------------------------------------------------------------

MENU = """
  1) 全流程：日历 → 选股 → 周期弹性 → 质量初筛 → 顶部判断 → 看板  ← 每周五盘后
  2) 只跑选股            stock_kline_screen
  3) 周期弹性            stock_cycle_elastic     （回车=批量，或填单只代码）
  4) 质量初筛            stock_quality_screen    （回车=批量，或填单只代码）
  5) 顶部判断            stock_judge_top         （单只/多只；回车=当天选股批量）
  0) 退出
"""


def interactive() -> int:
    days = load_calendar()
    span = f"{days[0]} .. {days[-1]}" if days else "（无日历）"
    say(f"股票研究工具箱 —— 交易日历：{span}")

    while True:
        say(MENU)
        try:
            choice = input("选择 [0-5]: ").strip()
        except (EOFError, KeyboardInterrupt):
            say("")
            return 130
        if choice in ("0", "q", ""):
            return 0
        if choice not in {"1", "2", "3", "4", "5"}:
            say("请输入 0-5。")
            continue

        day_hint = input(f"基准日（回车=自动，今天 {dt.date.today()}）: ").strip() or None

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        tee = Tee(LOG_DIR / f"run-{stamp}.log", verbose=False)
        try:
            if choice == "1":
                args = argparse.Namespace(
                    date=day_hint, no_calendar=False,
                    select_args=[], cycle_args=["--workers", "2"],
                    quality_args=["--workers", "2"], top_args=[],
                    top_workers=4, skip_cycle=False, skip_quality=False,
                    skip_top=False, keep_going=False, strict=False,
                    no_web=False, host="127.0.0.1", port=5000, verbose=False)
                rc = cmd_all(args, tee)
            elif choice == "2":
                args = argparse.Namespace(date=day_hint, no_calendar=False, extra=[])
                rc = cmd_single("select")(args, tee)
            elif choice in ("3", "4"):
                who = "周期弹性" if choice == "3" else "质量初筛"
                code = input(f"{who}——股票代码（回车=批量跑当天的选股名单）: ").strip()
                args = argparse.Namespace(date=day_hint, no_calendar=False,
                                          stock=code or None,
                                          extra=[] if code else ["--workers", "2"])
                rc = cmd_single("cycle" if choice == "3" else "quality")(args, tee)
            else:
                raw = input("股票代码（可多个，空格分隔；回车=用当天选股结果批量跑）: ").split()
                args = argparse.Namespace(date=day_hint, no_calendar=False,
                                          codes=raw, csv=None,
                                          from_select=not raw, workers=4, extra=[])
                rc = cmd_single("top")(args, tee)
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
  ./run.py all                            # 周五盘后全流程（日历→选股→周期→质量→顶部→看板）
  ./run.py menu                           # 交互菜单
  ./run.py select --date 2026-11-20       # 只跑选股，指定基准日
  ./run.py cycle   --stock 600519         # 周期弹性，单只
  ./run.py quality --stock 600519         # 质量初筛，单只
  ./run.py top     600519                 # 顶部判断
  ./run.py web                            # 只打开看板（读 results.db，只读）
  ./run.py backup                         # 备份数据库（VACUUM INTO + 保留 7 份）
  ./run.py all --no-web                   # 跑完不起看板（放进定时任务时用）

不带参数只打印本帮助，不跑任何程序（全流程要三十多分钟，免得误敲回车就开跑）。

下游（cycle/quality/top）认的是 results.db 里「基准日 = 那一天」的选股运行记录，
所以它们的 --date 必须和那天的选股一致；不给就按「今天（过了 15:05）」算。
四个工具都不再产出 CSV/HTML：结果只进 results.db，人看结果用 ./run.py web。""")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="把逐只失败播报也打到屏幕上（日志文件本来就是全量）")
    sub = p.add_subparsers(dest="cmd")

    def add_verbose(sp):
        sp.add_argument("-v", "--verbose", action="store_true",
                        help="把逐只失败播报也打出来（默认只留汇总，日志里是全量）")

    def add_date(sp):
        sp.add_argument("--date", default=None,
                        help="基准交易日 YYYY-MM-DD；today=自动（默认）")
        sp.add_argument("--no-calendar", action="store_true",
                        help="即使日历过期也不要自动更新")

    sp = sub.add_parser("all", aliases=["weekly"],
                        help="全流程：日历 → 选股 → 周期 → 质量 → 顶部 → 看板")
    add_date(sp)
    add_verbose(sp)
    sp.add_argument("--skip-cycle", action="store_true", help="跳过周期弹性")
    sp.add_argument("--skip-quality", action="store_true", help="跳过质量初筛")
    sp.add_argument("--skip-top", action="store_true", help="跳过顶部判断")
    sp.add_argument("--keep-going", action="store_true", help="某一步失败也继续往下跑")
    sp.add_argument("--strict", action="store_true",
                    help="任何失败都停（默认：该步一只都没成功才停，"
                         "个别票上游缺数据不拦整条流程）")
    sp.add_argument("--select-args", default="", help="透传给 stock_kline_screen 的参数")
    sp.add_argument("--cycle-args", default="--workers 2", help="透传给 cycle_report.py")
    sp.add_argument("--quality-args", default="--workers 2", help="透传给 stock_report.py")
    sp.add_argument("--top-args", default="", help="透传给 stock_judge_top 的参数")
    sp.add_argument("--top-workers", type=int, default=4, help="顶部判断并发数，默认 4")
    sp.add_argument("--no-web", action="store_true",
                    help="跑完不起看板（无头/自动化环境用）")
    sp.add_argument("--host", default="127.0.0.1", help="看板监听地址，默认只本机")
    sp.add_argument("--port", type=int, default=5000, help="看板端口，默认 5000")

    sp = sub.add_parser("select", help="只跑选股 stock_kline_screen（只能批量）")
    add_date(sp)
    add_verbose(sp)
    sp.add_argument("extra", nargs="*", default=[], help="透传给 stock_kline_screen 的参数")

    for name, helptext, flag in (
            ("cycle", "只跑周期弹性 stock_cycle_elastic", "--stock"),
            ("quality", "只跑质量初筛 stock_quality_screen", "--stock-code")):
        sp = sub.add_parser(name, help=helptext + "（批量或单只）")
        add_date(sp)
        add_verbose(sp)
        sp.add_argument("--stock", metavar="代码", default=None,
                        help=f"单只模式：只跑这一只（转成 {flag}），不需要当天的选股名单")
        sp.add_argument("extra", nargs="*", default=[], help="透传给该程序的参数")

    sp = sub.add_parser("top", help="顶部判断 stock_judge_top（单只/多只/名单批量）")
    add_date(sp)
    add_verbose(sp)
    sp.add_argument("codes", nargs="*", default=[],
                    help="股票代码，如 sh600519 600519 600519.SH（与 --csv 二选一）")
    sp.add_argument("--csv", metavar="CSV", default=None,
                    help="批量：从该 CSV 读代码列（代码/股票代码/证券代码/code）")
    sp.add_argument("--from-select", action="store_true",
                    help="批量：直接用结果库里当天的选股名单（基准日 = --date）")
    sp.add_argument("--workers", type=int, default=4, help="批量并发线程数，默认 4")
    sp.add_argument("extra", nargs="*", default=[], help="透传给 stock_judge_top 的参数")

    sp = sub.add_parser("backup", help="备份 shared.db / results.db 到 backup/（VACUUM INTO）")
    sp.add_argument("--dir", default=None,
                    help="备份目录，默认 backup/（可用绝对路径，如 ~/MyData/backups）")
    sp.add_argument("--keep", type=int, default=7,
                    help="每个库保留最近几份，默认 7（只删本命令生成的，手改的不动）")
    sp.add_argument("--only", choices=["shared", "results"], default=None,
                    help="只备份其中一个库")
    sp.add_argument("--list", dest="list_only", action="store_true",
                    help="只列出已有备份，不做备份")

    sp = sub.add_parser("web", help="打开看板：Flask + Chart.js，读 results.db")
    sp.add_argument("--port", type=int, default=5000, help="端口，默认 5000")
    sp.add_argument("--host", default="127.0.0.1", help="监听地址，默认只本机")
    sp.add_argument("--debug", action="store_true", help="Flask 调试模式（改模板自动重载）")

    sub.add_parser("menu", help="交互菜单（0-5 选择）")
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
    elif args.cmd in ("backup", "menu", "web"):
        # 这三个不接受透传参数：未知参数直接报错，别让 `--dry-run` 这种
        # 打错/臆想的开关被静默吞掉（人会以为它生效了）。
        if unknown:
            say(f"❌ {args.cmd} 不认识这些参数：{' '.join(unknown)}")
            return 2
    else:
        args.extra = list(getattr(args, "extra", []) or []) + list(unknown)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    tee = Tee(LOG_DIR / f"run-{stamp}.log",
              verbose=bool(getattr(args, "verbose", False)))
    try:
        if args.cmd == "menu":
            tee.close()
            return interactive()
        if args.cmd in ("all", "weekly"):
            args.select_args = split_passthrough(getattr(args, "select_args", ""))
            args.cycle_args = split_passthrough(getattr(args, "cycle_args", ""))
            args.quality_args = split_passthrough(getattr(args, "quality_args", ""))
            args.top_args = split_passthrough(getattr(args, "top_args", ""))
            return cmd_all(args, tee)
        if args.cmd == "web":
            tee.close()
            return cmd_web(args)
        if args.cmd == "backup":
            return cmd_backup(args, tee)
        handler = cmd_single(args.cmd)
        return handler(args, tee)
    except KeyboardInterrupt:
        say("\n已中断。")
        return 130
    finally:
        tee.close()


if __name__ == "__main__":
    sys.exit(main())
