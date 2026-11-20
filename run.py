#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每周研究流程的统一入口。

    python3 run.py all             # 全流程：日历 → 选股 → 周期 → 质量 → 顶部 → A股大势（不起看板）
    python3 run.py menu            # 交互菜单
    python3 run.py -h              # 帮助
    python3 run.py select          # 只跑选股（stock_kline_screen）
    python3 run.py cycle           # 周期弹性批量（读结果库里当天的选股名单）
    python3 run.py cycle --stock 600519     # 周期弹性单只
    python3 run.py quality         # 质量初筛批量（读结果库里当天的选股名单）
    python3 run.py quality --stock 600519   # 质量初筛单只
    python3 run.py top sh600519    # 顶部判断（stock_judge_top，单只，随时可跑）
    python3 run.py top --from-select            # 顶部判断批量：读结果库的名单
    python3 run.py price --date 2026-11-17  # 冷热极点快照（价 + PE + PB + PS + 沪深300）
    python3 run.py price --code 002096 --date 2026-11-03  # 单只补数（只改这一只）
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

退出码：0 成功 / 1 某步失败 / 2 用法错误 / 3 选股 0 只 / 4 日期定不下来 / 130 用户中断。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
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

EXIT_BAD_DATE = 4
"""目标交易日定不下来：`--date` 不在日历 / 没有日历 / 日历早于今天。

原来这类错走 `raise SystemExit("错误：…")`，而带字符串的 `SystemExit`
解释器只退 **1**——和"某步有票失败"撞在同一个码上。于是 `&&` 链、CI、
`./run.py all || retry` 都没法区分"日期敲错了，改参数重跑"和"上游缺数据，
过一会儿再跑也许就好"。（2026-11-17 拆出来：1 只表示数据层面有票失败。）"""


class BadDate(Exception):
    """目标交易日定不下来。**别用 `SystemExit(字符串)`**：那个只能退 1。"""

    code = EXIT_BAD_DATE

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

    定不下来时抛 `BadDate`（不是 `SystemExit`）：调用方据此退 4，
    与"某步有票失败"（退 1）区分开。
    """
    if explicit and explicit not in ("today", "latest"):
        day = explicit
        if days and day not in set(days):
            raise BadDate(
                f"错误：{day} 不在交易日历里（日历 {days[0]} .. {days[-1]}）。\n"
                f"      周五 15:10 之后先跑：python3 -m stock_data_fetch.build_calendar")
        return day

    if not days:
        raise BadDate("错误：没有交易日历（state/trading_calendar.json）。\n"
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
        raise BadDate(f"错误：交易日历早于 {today}，无法确定目标交易日。")

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


#: 逐只失败的播报行。屏幕上现在只剩一条进度条，这些行**全部只进日志**；
#: `--verbose` 时原样打到屏幕上（排查时用）。
_NOISE = (
    re.compile(r"^kline \S+ 失败："),        # stock_data_fetch.api 逐只 K 线失败
    re.compile(r"^\s*↻ "),                   # 「上游返回空，重试 (1/3)」
    re.compile(r"^\[FAIL\]"),
)

#: 进度行的形状：`[ 3/38] 宝钢股份 sh600019  观察`、`[1/38] ok 600019`、
#: 选股的 `[2/6] 横截面粗筛（5 天）...`。**认 `[n/m]` 这个开头就够了**：
#: 不管逐只还是分阶段，都是"第 n 步 / 共 m 步"，正好喂进度条。
_PROGRESS_ANY = re.compile(r"^\s*\[\s*(\d+)\s*/\s*(\d+)\s*\]")


def _is_noise(text: str) -> bool:
    return any(p.match(text) for p in _NOISE)


class Tee:
    """同时写日志文件和（很少的）屏幕输出。

    ## 屏幕默认只有一条进度条

    工具打的 `[3/38] …` 被解析成 `done/total` 画成进度条；其余输出——逐只失败、
    `↻ 重试`、报表、聚合汇总、命令行——**全部只进日志**。跑完 `run.py` 会打印
    日志路径，出了问题看那份文件（它就是过去的屏幕内容，一行不少）。

    为什么不在屏幕上留"关键信息"：2026-11-15 的两次反馈都是"显示太多了"。
    进度条回答的是"还要多久"，结论回答的是"这次跑成没有"，用看板看更清楚。

    ## 进度条只在真终端上画

    重定向到文件/管道时 `\r` 只会变成一坨乱码。那种情况下每步改成打一行
    「标题 … 结果」，日志照样完整。
    """

    BAR = 24                       # 进度条宽度（字符）

    def __init__(self, path: Path, verbose: bool = False,
                 progress: bool | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fh = path.open("w", encoding="utf-8")
        self.verbose = verbose
        # 显式传 progress 的只有测试（要能验进度条，不必真有 tty）；
        # 正常情况：不是 --verbose 且 stdout 连着真终端才画条。
        self.progress = (bool(progress) if progress is not None
                         else (not verbose and sys.stdout.isatty()))
        self.echo = False       # 单只模式：这一步的输出整段透传（不画条、不筛）
        self.muted = 0          # 只进了日志、没上屏幕的行数（--verbose 恒为 0）
        self._blank = False     # 上一条是不是空行（collapse 连续空行）
        self._prefix = ""       # 进度条前面那段标题
        self._seen = False      # 这一步有没有出现过进度行
        self._done = 0
        self._total = 0
        self._last = 0.0        # 上次重绘时刻（节流用）
        self._t0 = 0.0          # 这一步是什么时候开始的（算耗时用）
        self._stop: threading.Event | None = None
        self._lock = threading.Lock()   # 心跳线程和主线程都要写 stdout

    # ------------------------------------------------------------ 写行
    def line(self, text: str) -> None:
        """一行子进程输出：**永远进日志**，屏幕上至多变成进度条。"""
        self.fh.write(text + "\n")
        self.fh.flush()

        if self.verbose or self.echo:
            if not text and self._blank:
                return
            self._blank = not text
            say(text)
            return

        m = _PROGRESS_ANY.match(text)
        if m:
            if self.progress:
                self._draw(int(m.group(1)), int(m.group(2)))
        elif text:
            self.muted += 1          # 失败播报、报表、汇总——日志里有

    def alert(self, text: str) -> None:
        """流程级的坏消息（名单是空的、日历更新失败…）：**必须上屏幕**。

        这类行意味着整条流程要停下，人得当场看见，不能让人盯着一根停住的
        进度条猜。逐只失败不走这里——那种进日志。
        """
        self.fh.write(text + "\n")
        self.fh.flush()
        self._close_bar_line()
        say(text)

    # ------------------------------------------------------------ 进度条
    def start_bar(self, prefix: str) -> None:
        """每步开头：先打出标题和转圈，再起一个心跳线程。

        为什么要心跳：选股那一步有 15 秒一个进度行都不打（在逐条裁决 4300
        只），只靠子进程输出推着的进度条会一动不动，看着像卡死。心跳每 0.4s
        刷新一次"已经跑了多久"，顺便让转圈动起来。
        """
        self._prefix = prefix
        self._seen = False
        self._done = self._total = 0
        self._t0 = time.monotonic()
        if not self.progress:
            return
        self._stop = threading.Event()
        with self._lock:
            sys.stdout.write("\r" + self._line_text() + "\033[K")
            sys.stdout.flush()
        threading.Thread(target=self._tick, args=(self._stop,), daemon=True).start()

    _SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def _tick(self, stop: threading.Event) -> None:
        """心跳：只要子进程还没结束，就一直刷新耗时（顺带转圈）。"""
        i = 0
        while not stop.wait(0.4):
            i += 1
            with self._lock:
                if self._stop is not stop:
                    return                    # 已经换步了，别把新的一行盖掉
                text = self._line_text(spin=i)
                sys.stdout.write("\r" + text + "\033[K")
                sys.stdout.flush()

    def _head(self, spin: int = 0) -> str:
        """标题那一截（不带你已经跑了多久）。"""
        if not self._seen:
            return f"{self._prefix}  {self._SPIN[spin % len(self._SPIN)]}"
        width = self.BAR
        frac = 0.0 if not self._total else max(0.0, min(1.0, self._done / self._total))
        filled = int(round(frac * width))
        bar = "█" * filled + "░" * (width - filled)
        return f"{self._prefix}  [{bar}] {self._done}/{self._total}"

    def _line_text(self, spin: int = 0) -> str:
        """当前该显示的那一整行：标题 + 耗时。"""
        return f"{self._head(spin)}  {time.monotonic() - self._t0:5.1f}s"

    def _draw(self, done: int, total: int) -> None:
        """重画进度条。节流到 50ms 一次：`
` 写太密反而拖慢子进程。"""
        now = time.monotonic()
        self._done, self._total = done, total
        self._seen = True
        if done < total and now - self._last < 0.05:
            return
        self._last = now
        sys.stdout.write("\r" + self._bar_text(done, total) + "\033[K")
        sys.stdout.flush()

    def _bar_text(self, done: int, total: int, tail: str = "") -> str:
        width = self.BAR
        frac = 0.0 if not total else max(0.0, min(1.0, done / total))
        filled = int(round(frac * width))
        bar = "█" * filled + "░" * (width - filled)
        text = f"{self._prefix}  [{bar}] {done}/{total}"
        return f"{text}  {tail}" if tail else text

    def finish_bar(self, tail: str) -> None:
        """一步结束：把进度条定格成一行，后面跟上"成功 35 只 · 1.2s"。"""
        if self.progress:
            if self._seen and self._total:
                sys.stdout.write("\r" + self._bar_text(self._done, self._total, tail)
                                 + "\033[K\n")
            else:
                sys.stdout.write(f"\r{self._prefix}  …  {tail}\033[K\n")
            sys.stdout.flush()
        else:
            # 非终端（重定向/定时任务）：不画条，每步一行，别让输出变成空的
            say(f"{self._prefix}  …  {tail}")
        self._seen = False

    def _close_bar_line(self) -> None:
        """屏幕上还挂着一条没换行的进度时，先收尾再说话。"""
        if self.progress:
            sys.stdout.write("\n")
            sys.stdout.flush()

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


def _pump(tee: Tee, proc: subprocess.Popen) -> None:
    """把子进程输出**按行**喂给 Tee，`\r` 也算换行。

    cycle 用 `\r[{done}/{total}]` 原地刷新进度；只按 `\n` 切行的话，整段
    进度会憋在缓冲区里等到进程结束——进度条就成了一条直线。
    """
    out = proc.stdout
    assert out is not None
    buf = b""
    while True:
        chunk = out.read(1024)
        if not chunk:
            break
        buf += chunk
        while True:
            idx = -1
            for sep in (b"\n", b"\r"):
                i = buf.find(sep)
                if i >= 0 and (idx < 0 or i < idx):
                    idx = i
            if idx < 0:
                break
            raw, buf = buf[:idx], buf[idx + 1:]
            tee.line(raw.decode("utf-8", "replace"))
    if buf:
        tee.line(buf.decode("utf-8", "replace"))


def run_step(tee: Tee, title: str, argv: list[str], cwd: Path, *,
             short: str | None = None, tool: str | None = None,
             day: str | None = None, echo: bool = False) -> tuple[int, float]:
    """跑一步，画一条进度条。返回 (退出码, 秒)。

    `tool`/`day` 给了就去结果库读这一步写成多少只（成功/失败），定格在进度条
    后面——这样人一眼能看出"这步跑成没有"，不用去翻日志。

    `echo=True` 是**单只模式专用**：那一次运行的全部输出就是报告本身（人跑
    `run.py top 600519` 是为了看这只票），所以整段透传、不画条、不筛行。
    批量模式才用进度条——批量模式的报告在库里、在看板上。
    """
    # 日志里留一份可复读的抬头（屏幕上看不到）
    tee.line("")
    tee.line("=" * 68)
    tee.line(title)
    tee.line("=" * 68)
    tee.line(f"$ {' '.join(argv)}    (cwd={cwd.relative_to(ROOT) if cwd != ROOT else '.'})")
    tee.line("")

    tee.echo = echo
    if echo:
        say("")
        say(short or title)
    else:
        tee.start_bar(short or title)
    t0 = time.time()
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=0)
    try:
        _pump(tee, proc)
        code = proc.wait()
    except BaseException:
        # Ctrl-C 只杀到 wrapper 的话，子进程会被 init 收养、继续取数写库，
        # 下次再跑就像"怎么又慢又有旧数据"。先礼后兵，确保它一起停。
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait()
        raise
    spent = time.time() - t0
    LAST_SPENT["s"] = spent

    tail = [f"{spent:.1f}s"]
    if tool and day and code in (0, 1):
        n_ok, n_fail = step_stats(tool, day)
        if n_ok >= 0:
            tail.insert(0, f"{n_ok} 只")
        if n_fail > 0:
            tail.insert(1, f"失败 {n_fail} 只")
    mark = "✅" if code == 0 else ("⚠️" if code == 1 else "❌")
    if code not in (0, 1):
        tail.insert(0, f"退出码 {code}")
    if echo:
        say("")                       # 报告后面空一行，别和下一步粘在一起
    else:
        tee.finish_bar(f"{mark} " + " · ".join(tail))
    tee.echo = False
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
    # 屏幕上这一行已经由进度条收尾（"⚠️ 35 只 · 失败 3 只"）说清了，
    # 这里只把"为什么可以继续"记进日志，免得又铺三行解释。
    tee.line(f"（{title} 有 {n_fail} 只失败，但已写入 {n_ok} 只 → 继续往下跑；"
             "失败的多数是上游缺数据，看板里留空）")
    return True


def step_select(tee: Tee, day: str, extra: list[str]) -> int:
    argv = py_module("stock_kline_screen") + ["--date", day] + extra
    # 全流程的终局是看板：名单、剔除明细在「选股」页更清楚，终端只留一句。
    # 想看终端报表就别走 all，单跑 `./run.py select`；或者所有步骤都加 --verbose。
    if not tee.verbose and "--quiet" not in argv:
        argv.append("--quiet")
    code, _ = run_step(tee, f"① 选股 stock_kline_screen（{day}）", argv, ROOT,
                       short=f"① 选股（{day}）", tool="select", day=day)
    if code != 0:
        return code
    n = select_count(day)
    if n <= 0:
        tee.alert(f"⚠️  {day} 没有股票通过初筛（结果库里 0 行），流程到此为止。")
        return 3
    return 0


def step_cycle(tee: Tee, day: str, extra: list[str], stock: str | None = None,
               csv_path: Path | None = None, tool_args: list[str] | None = None) -> int:
    """周期弹性。`stock` 单只、`csv_path` 走 CSV，都不依赖当天名单。"""
    tool_args = tool_args or []
    if stock:
        argv = [sys.executable, "cycle_report.py", "--stock", stock] + tool_args + extra
        code, _ = run_step(tee, f"② 周期弹性 stock_cycle_elastic（{stock} 单只）",
                           argv, ROOT / "stock_cycle_elastic",
                           short=f"② 周期弹性（{stock}）", echo=True)
        return code
    if csv_path:
        argv = ([sys.executable, "cycle_report.py", "--csv", str(csv_path)]
                + tool_args + extra)
        code, _ = run_step(tee, f"② 周期弹性 stock_cycle_elastic（CSV {csv_path.name}）",
                           argv, ROOT / "stock_cycle_elastic",
                           short=f"② 周期弹性（CSV {csv_path.name}）")
        return code
    n = select_count(day)
    if n <= 0:
        tee.alert(f"❌ 结果库里没有 {day} 的选股名单；"
                  f"先跑：python3 run.py select --date {day}")
        return 2
    argv = [sys.executable, "cycle_report.py", "--from-select", day] + tool_args + extra
    code, _ = run_step(tee, f"② 周期弹性 stock_cycle_elastic（{day}）",
                       argv, ROOT / "stock_cycle_elastic",
                       short=f"② 周期弹性（{day}）", tool="cycle", day=day)
    return code


def step_quality(tee: Tee, day: str, extra: list[str], stock: str | None = None,
                 csv_path: Path | None = None, tool_args: list[str] | None = None) -> int:
    """质量初筛。`stock` 单只、`csv_path` 走 CSV，都不依赖当天名单。"""
    tool_args = tool_args or []
    if stock:
        argv = [sys.executable, "stock_report.py", "--stock-code", stock] + tool_args + extra
        code, _ = run_step(tee, f"③ 质量初筛 stock_quality_screen（{stock} 单只）",
                           argv, ROOT / "stock_quality_screen",
                           short=f"③ 质量初筛（{stock}）", echo=True)
        return code
    if csv_path:
        argv = ([sys.executable, "stock_report.py", "--csv", str(csv_path)]
                + tool_args + extra)
        code, _ = run_step(tee, f"③ 质量初筛 stock_quality_screen（CSV {csv_path.name}）",
                           argv, ROOT / "stock_quality_screen",
                           short=f"③ 质量初筛（CSV {csv_path.name}）")
        return code
    n = select_count(day)
    if n <= 0:
        tee.alert(f"❌ 结果库里没有 {day} 的选股名单；"
                  f"先跑：python3 run.py select --date {day}")
        return 2
    argv = [sys.executable, "stock_report.py", "--from-select", day] + tool_args + extra
    code, _ = run_step(tee, f"③ 质量初筛 stock_quality_screen（{day}）",
                       argv, ROOT / "stock_quality_screen",
                       short=f"③ 质量初筛（{day}）", tool="quality", day=day)
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
        if str(csv_path) == "results":
            n = select_count(day)
            if n <= 0:
                tee.alert(f"❌ 结果库里没有 {day} 的选股名单")
                return 2
            argv = py_module("stock_judge_top") + [
                "--from-select", day, "--date", day, "--workers", str(workers)] + extra
            label = f"批量 {n} 只 @ {day}"
        else:
            csv_path = Path(csv_path)
            if not csv_path.is_file():
                tee.alert(f"❌ CSV 不是文件：{csv_path}")
                return 2
            argv = py_module("stock_judge_top") + [
                "--csv", str(csv_path), "--date", day,
                "--workers", str(workers)] + extra
            label = f"CSV {csv_path.name} @ {day}"
        if tee.verbose:
            if "--verbose" not in argv and "-v" not in argv:
                argv.append("--verbose")
        elif "--quiet" not in argv:
            # 四因子表在「顶部」页看得更清楚；但逐只进度行要留着——
            # `run.py` 拿它画进度条（`--progress` 只开进度、不开报告表）。
            argv.extend(["--quiet", "--progress"])
        rc, _ = run_step(tee, f"④ 顶部判断 stock_judge_top（{label}）",
                         argv, ROOT, short=f"④ 顶部判断（{label}）",
                         tool="top", day=day)
        return rc

    if not codes:
        tee.alert("❌ 顶部判断需要至少一个股票代码（或用 --csv / --from-select 批量）。")
        return 2
    failed = []
    for code in codes:
        argv = py_module("stock_judge_top") + [code, "--date", day] + extra
        code_rc, _ = run_step(tee, f"④ 顶部判断 stock_judge_top（{code} @ {day}）",
                              argv, ROOT, short=f"④ 顶部判断（{code}）", echo=True)
        if code_rc not in (0,):
            failed.append(code)
    if failed:
        tee.alert(f"❌ 失败的代码：{'、'.join(failed)}")
        return 1
    return 0


def step_price(tee: Tee, day: str | None, extra: list[str],
               stock_code: str | None = None) -> int:
    """冷热极点：`price` + `price_stat` + `index_price`，一天一份。

    跑的是 `stock_extreme_point` 子进程——它是独立工具，自己抓东财、自己写两个库，
    入口这边只负责"把日期定下来、把输出收进日志、画进度条"。

    `day=None` 只出现在 `--restat` / `--sync`（纯本地动作，按库里已有的数据算或复制）：
    那时不该塞 `--date`，也不该去数"这天入库了几只"。

    `stock_code` 非空时是单只补数：子进程只 upsert 这一只、不整天替换；
    这步也不该报"这天入库了几只"。

    **不套 `tool=`/`day=`**：那两个参数是给四个工具数"成功几只"用的，本步的产出
    不在 `*_result` 表里。子进程自己也打了 `✅ 5579 只…`，但屏幕上除 `[n/m]`
    之外的行都会被 Tee 收进日志，所以入库只数在这里补一行（`_price_count`）。
    """
    argv = py_module("stock_extreme_point") + (
        ["--date", day] if day else []) + (
        ["--code", stock_code] if stock_code else []) + extra
    label = f"{day} · {stock_code}" if stock_code else (day or "按已有数据")
    rc, _ = run_step(tee, f"冷热极点 price（{label}）", argv, ROOT,
                     short=f"冷热极点（{label}）", echo=bool(stock_code))
    if rc == 0 and day and not stock_code:
        n = _price_count(day)
        if n > 0:
            # 子进程那行 "✅ 5579 只…" 只进了日志（屏幕上 `[n/m]` 之外的行都被收掉）。
            # 这一步的结果恰恰就是"入了多少只"，所以在这里补一行上屏幕。
            say(f"  {day} · {n} 只已入库（价 / PE / PB / PS + 沪深300，results.db）")
    return rc


def _price_count(day: str) -> int:
    """results.db 里 `price` 表这天有多少行；读不到就 0（只是页面提示，不该报错）。

    `STOCK_RESULTS_DB` 优先：与 `stock_extreme_point` / `stock_data_fetch.results`
    读同一个环境变量，拿副本调试时这行提示不会指向另一个库。
    """
    try:
        path = Path(os.environ.get("STOCK_RESULTS_DB") or DB_PATH).expanduser()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM price WHERE date=?", (day,)).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0
    except Exception:                        # noqa: BLE001 - 提示失败不该影响退出码
        return 0


def step_regime(tee: Tee, day: str, extra: list[str]) -> int:
    """A股大势：三路信号写 `results.db.market_regime`，一周一行。

    跑的是 `stock_market_trend` 子进程——它自己抓东财/中证/乐咕的四个接口、
    把原件写进 `shared.db`、把结论写进 `results.db`；入口这边只定日期、收日志。

    **失败不拦主流程**：它是背景板，四个工具的结果才是这一批的主体。
    估值或破净接口挂了，看板继续显示上一周的周记（并标"沿用上次"），
    比让整条流程停在最后一步强。

    也**不套 `tool=`/`day=`**：那两个参数是给四个工具数"成功几只"用的，
    本步的产出不在 `*_result` 表里。
    """
    argv = py_module("stock_market_trend") + ["--date", day] + extra
    rc, _ = run_step(tee, f"A股大势 stock_market_trend（{day}）", argv, ROOT,
                     short="A股大势")
    return rc


def step_calendar(tee: Tee) -> int:
    code, _ = run_step(tee, "⓪ 更新交易日历 build_calendar",
                       py_module("stock_data_fetch.build_calendar"), ROOT,
                       short="⓪ 更新交易日历")
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
            tee.alert("❌ 日历更新失败。没有日历，下游会回落到上周五并静默产出旧报告。")
            return "", rc
        days = load_calendar()
    elif stale and days and weekday:
        tee.line(f"⚠️  日历最后一天是 {days[-1]}，今天 {today}。")
        tee.line("    如果是交易日且已收盘，请先跑：python3 -m stock_data_fetch.build_calendar")
        tee.line("    （否则下游会回落到上一个交易日，产出一份旧报告）")

    try:
        return pick_date(day_wanted, days), 0
    except BadDate as exc:
        # 退 4 而不是 1：日期错"改参数就能解决"，与"某步有票失败"不是一回事。
        tee.line(str(exc))
        return "", EXIT_BAD_DATE


def summary(tee: Tee, day: str, steps: list[tuple[str, int, float]]) -> None:
    """流程收尾：屏幕上两行（成没成 + 日志在哪），`--verbose` 才打整张表。"""
    if not tee.verbose:
        bad = [(name, rc) for name, rc, _ in steps if rc != 0]
        bad_idx = [i for i, (_, rc, _) in enumerate(steps) if rc != 0]
        spent = sum(t for _, _, t in steps)
        if not bad:
            head = f"✅ 全部完成 · {len(steps)} 步 · {spent:.1f}s"
        elif all(rc in (1, 3) for _, rc in bad):
            # 1/3 是"有票失败但已写入一部分"：流程继续，别当成整步失败。
            # 但失败如果落在**最后一步**，说明流程根本没继续——此时说
            # "其余步骤完成"是假话（2026-11-20 选股取数失败就是这样：
            # 只跑了第 ① 步，汇总却说其余步骤都完成了）。
            stopped = bad_idx[-1] == len(steps) - 1
            names = "、".join(n for n, _ in bad)
            if stopped:
                if all(rc == 3 for _, rc in bad):
                    # 修完 cli 的退出码后，3 只剩"选股 0 只"这一种含义。
                    head = f"⚠️ {names} 0 只入选，流程停止 · {spent:.1f}s"
                else:
                    head = (f"⚠️ {names} 有失败，流程在此停止 · "
                            f"共完成 {len(steps)} 步 · {spent:.1f}s")
            else:
                head = f"⚠️ {names} 有失败，其余步骤完成 · {spent:.1f}s"
        else:
            head = "❌ {0} 失败，流程停止 · {1:.1f}s".format(
                "、".join(n for n, _ in bad), spent)
        say("")
        say(head)
        say(f"日志 {tee.path}")
        if bad:
            say("失败原因只写进日志（逐只失败、上游缺数据的明细都在里面）")
        return

    hr(f"汇总（{day}）")
    for name, rc, spent in steps:
        # 3 = 选股 0 只（交接处拦下），1 = 有失败但可能已部分写入
        mark = "✅" if rc == 0 else ("⚠️" if rc in (1, 3) else "❌")
        tee.line(f"  {mark} {name:<28s} {spent:6.1f}s   退出码 {rc}")
    tee.line("")
    tee.line(f"  结果库      {DB_PATH}（看板：./run.py web）")
    tee.line(f"  本次日志    {tee.path}")


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

    if getattr(args, "skip_regime", False):
        tee.line("（按 --skip-regime 跳过 A股大势）")
    else:
        rc = step_regime(tee, day, getattr(args, "regime_args", []) or [])
        steps.append(("⑤ A股大势", rc, LAST_SPENT["s"]))
        if rc != 0:
            # 不 return、不进 judge_step：A股大势算不成不影响这一批票的任何结论
            tee.line("  ⚠️ A股大势没算成；看板继续显示上一周的判断")

    summary(tee, day, steps)

    # 看板是长驻进程，不再是 all 的最后一步：流程跑完就结束，想看结果
    # 单独跑 ./run.py web。只有显式加 --web 才在流程结束后把看板起起来。
    if not getattr(args, "web", False):
        tee.line("")
        tee.line("（全流程不含看板；看结果用 ./run.py web）")
        return 0
    return step_web(args, tee)


def cmd_single(action: str):
    def run(args, tee: Tee) -> int:
        if action == "price" and (getattr(args, "restat", False)
                                  or getattr(args, "sync", False)):
            # 纯本地动作（重算摘要 / 原始库补到结果库）：不联网、不需要日历，
            # 所以不跑 ensure_calendar（那一步在日历过期时会去打上游）。
            if getattr(args, "code", None):
                tee.alert("❌ --code 只能单独查询，不能和 --restat / --sync 一起用。")
                return 2
            extra = ["--sync"] if getattr(args, "sync", False) else ["--restat"]
            return step_price(tee, args.date, extra)
        day, rc = ensure_calendar(tee, args.date, auto=not args.no_calendar)
        if not day:
            return rc or 1
        if action in ("cycle", "quality") and getattr(args, "csv", None):
            say("ℹ️  CSV 模式：股票名单全部来自该文件；"
                "上面的日期只作运行标记，不会改变名单。")
        if action == "select":
            return step_select(tee, day, args.extra)
        if action == "regime":
            return step_regime(tee, day, args.extra)
        if action == "price":
            extra = list(args.extra)
            stock_code = getattr(args, "code", None)
            if stock_code and (getattr(args, "restat", False)
                               or getattr(args, "sync", False)
                               or getattr(args, "force", False)
                               or int(getattr(args, "prune_keep", 0) or 0) > 0):
                tee.alert("❌ --code 只改这一只；不能和 --force / --prune-keep / "
                          "--restat / --sync 一起用。")
                return 2
            if getattr(args, "force", False):
                extra.append("--force")
            keep = int(getattr(args, "prune_keep", 0) or 0)
            if keep > 0:
                extra += ["--prune-keep", str(keep)]
            return step_price(tee, day, extra, stock_code=stock_code)
        if action in ("cycle", "quality"):
            stock = getattr(args, "stock", None)
            csv_path = None
            tool_args = []
            for attr, flag in (("workers", "--workers"), ("retries", "--retries"),
                               ("retry_wait", "--retry-wait"), ("timeout", "--timeout")):
                val = getattr(args, attr, None)
                if val is not None:
                    tool_args += [flag, str(val)]
            if getattr(args, "force", False):
                tool_args.append("--force")
            if getattr(args, "csv", None):
                csv_path = Path(args.csv).expanduser()
                if not csv_path.is_absolute():
                    csv_path = ROOT / csv_path
                if stock:
                    tee.alert("❌ --stock 和 --csv 二选一。")
                    return 2
                if not csv_path.is_file():
                    tee.alert(f"❌ CSV 不是文件：{csv_path}")
                    return 2
            if stock:
                rc = note_single_date(tee, day, args.date)
                if rc:
                    return rc
                return (step_cycle if action == "cycle" else step_quality)(
                    tee, day, args.extra, stock, tool_args=tool_args)
            return (step_cycle if action == "cycle" else step_quality)(
                tee, day, args.extra, csv_path=csv_path, tool_args=tool_args)
        if action == "top":
            if getattr(args, "refresh", False) and "--refresh" not in args.extra:
                args.extra.append("--refresh")
            csv_path = None
            if getattr(args, "from_select", False):
                csv_path = "results"          # 只是一个"走批量"的开关
            elif getattr(args, "csv", None):
                csv_path = Path(args.csv).expanduser()
                if not csv_path.is_absolute():
                    csv_path = ROOT / csv_path
                if not csv_path.is_file():
                    tee.alert(f"❌ CSV 不是文件：{csv_path}")
                    return 2
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
    app.run(host=args.host, port=args.port, debug=getattr(args, "debug", True))
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
    """全流程（`all --web`）的最后一步：把看板起起来（阻塞，Ctrl-C 退出）。

    默认不跑这一步——看板是长驻进程，放进全流程会挂住自动化。想跑完直接
    看结果时加 `--web`；平时看结果用单独的 `./run.py web`。
    """
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 5000)
    # 这一步不受"只画进度条"约束：URL 是唯一要人抄下来的东西。
    say("")
    say(f"⑥ 看板 http://{host}:{port}（Ctrl-C 退出，退出后本次流程结束）")
    tee.line(f"⑥ 看板 http://{host}:{port}（{db_hint()}）")
    # 先把日志收掉：后面是长驻进程，它的访问日志写进本文件只会把有用的几行冲散。
    tee.close()
    say("")
    try:
        return cmd_web(argparse.Namespace(host=host, port=port, debug=True))
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
  1) 全流程：日历 → 选股 → 周期弹性 → 质量初筛 → 顶部判断 → A股大势  ← 每周五盘后
  2) 只跑选股            stock_kline_screen
  3) 周期弹性            stock_cycle_elastic     （回车=批量，或填单只代码）
  4) 质量初筛            stock_quality_screen    （回车=批量，或填单只代码）
  5) 顶部判断            stock_judge_top         （单只/多只；回车=当天选股批量）
  6) 冷热极点            stock_extreme_point     （全市场 价/PE/PB/PS + 沪深300，收盘后跑）
  7) A股大势             stock_market_trend      （趋势 / 赔率 / 情绪；写 results.db，不参与选股）
  0) 退出
"""


def interactive() -> int:
    days = load_calendar()
    span = f"{days[0]} .. {days[-1]}" if days else "（无日历）"
    say(f"股票研究工具箱 —— 交易日历：{span}")

    while True:
        say(MENU)
        try:
            choice = input("选择 [0-7]: ").strip()
        except (EOFError, KeyboardInterrupt):
            say("")
            return 130
        if choice in ("0", "q", ""):
            return 0
        if choice not in {"1", "2", "3", "4", "5", "6", "7"}:
            say("请输入 0-7。")
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
                    web=False, host="127.0.0.1", port=5000, verbose=False)
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
            elif choice == "6":
                args = argparse.Namespace(date=day_hint, no_calendar=False,
                                          code=None, force=False, prune_keep=0,
                                          extra=[])
                rc = cmd_single("price")(args, tee)
            elif choice == "7":
                args = argparse.Namespace(date=day_hint, no_calendar=False,
                                          extra=[])
                rc = cmd_single("regime")(args, tee)
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
        # 用 tee.path 而不是 tee.fh.name：close() 会把 fh 置成 None，
        # 而这一行在 finally 之后 —— 用 fh 会必崩（2026-11-20 修）。
        say(f"—— 退出码 {rc}；日志 {tee.path}")
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
        description="每周研究流程的统一入口。不带参数只打印帮助；"
                    "跑全流程用 all，想选着跑用 menu。",
        epilog="""示例：
  ./run.py all                            # 周五盘后全流程（日历→选股→周期→质量→顶部→A股大势）
  ./run.py menu                           # 交互菜单
  ./run.py select --date 2026-11-20       # 只跑选股，指定基准日
  ./run.py cycle   --stock 600519         # 周期弹性，单只
  ./run.py quality --stock 600519         # 质量初筛，单只
  ./run.py top     600519                 # 顶部判断
  ./run.py price --date 2026-11-17    # 冷热极点快照（价 + PE + PB + PS）
  ./run.py price --code 002096 --date 2026-11-03  # 只补这一只
  ./run.py web                            # 只打开看板（读 results.db，只读）
  ./run.py backup                         # 备份数据库（VACUUM INTO + 保留 7 份）
  ./run.py all --web                      # 全流程跑完顺便起看板（默认不起）

  # 自己给名单：--csv 直接读 CSV 的代码列，不依赖 results.db 里的选股记录
  ./run.py cycle   --csv ~/MyData/选股_2026-11-06.csv
  ./run.py quality --csv ~/MyData/选股_2026-11-06.csv --workers 2
  ./run.py quality --csv ~/MyData/选股_2026-11-06.csv --workers 2 --retries 0
  ./run.py top     --csv ~/MyData/选股_2026-11-06.csv

不带参数只打印本帮助，不跑任何程序（全流程要三十多分钟，免得误敲回车就开跑）。

cycle/quality/top 默认从 results.db 取「基准日 = --date 那一天」的选股名单，
所以 --date 必须和那天的选股一致；不给就按「今天（过了 15:05）」算。
用 --csv 时名单来自 CSV，不再依赖 results.db 的选股记录：
quality/cycle 的 --csv 与 --stock 二选一，top 的 --csv 与 --from-select 二选一。
四个工具都不再产出 CSV/HTML：结果只进 results.db，人看结果用 ./run.py web。""")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="不画进度条，把子进程的原始输出全部打到屏幕上"
                        "（默认只有进度条，明细都在日志里）")
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
                        help="全流程：日历 → 选股 → 周期 → 质量 → 顶部 → A股大势"
                             "（不起看板）")
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
    sp.add_argument("--skip-regime", action="store_true",
                    help="跳过 A股大势（趋势/赔率/情绪，它的结论不参与四步判断）")
    sp.add_argument("--regime-args", default="",
                    help="透传给 stock_market_trend 的参数（如 --recompute 用原件重算）")
    sp.add_argument("--web", action="store_true",
                    help="流程跑完后起看板（默认不起，跑完用 ./run.py web 看结果）")
    # 旧参数，保留只为不让已有脚本报错；现在默认就是不起看板。
    sp.add_argument("--no-web", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--host", default="127.0.0.1", help="看板监听地址，默认只本机")
    sp.add_argument("--port", type=int, default=5000, help="看板端口，默认 5000")

    sp = sub.add_parser("select", help="只跑选股 stock_kline_screen（只能批量）")
    add_date(sp)
    add_verbose(sp)
    sp.add_argument("extra", nargs="*", default=[], help="透传给 stock_kline_screen 的参数")

    sp = sub.add_parser("regime",
                        help="A股大势（stock_market_trend）：趋势 / 赔率 / 情绪（写 results.db）",
                        description="""沪深300 周线趋势 + ERP 赔率 + 破净/量能情绪。

三路各算各的，不合成总分。原件落 shared.db，结论落 results.db，
看板 /trend 与 /batches 顶部那块横幅读的就是它。`all` 的最后一步会顺带跑一次。

示例：
  ./run.py regime                     按今天（或最近交易日）算
  ./run.py regime --date 2026-11-20   给指定基准日算
  ./run.py regime --recompute         不联网，用 shared.db 的原件重算
""")
    add_date(sp)
    add_verbose(sp)
    # 刻意**不**声明 `extra` 位置参数：`nargs="*"` 会把 `--json x.json` 的
    # 值当成位置参数吞掉，和 `parse_known_args` 收上来的开关拼错顺序。
    # 本包所有开关都走 `unknown`（main() 里按原顺序接上 `args.extra`）。

    for name, helptext, flag in (
            ("cycle", "只跑周期弹性 stock_cycle_elastic", "--stock"),
            ("quality", "只跑质量初筛 stock_quality_screen", "--stock-code")):
        if name == "quality":
            examples = """示例：
  ./run.py quality --stock 600519
  ./run.py quality --csv ~/MyData/选股_2026-11-06.csv --workers 2
  ./run.py quality --csv ~/MyData/选股_2026-11-06.csv --workers 2 --retries 0
  ./run.py quality --csv ~/MyData/选股_2026-11-06.csv --force
"""
        else:
            examples = """示例：
  ./run.py cycle --stock 600519
  ./run.py cycle --csv ~/MyData/选股_2026-11-06.csv
  ./run.py cycle --csv ~/MyData/选股_2026-11-06.csv --workers 4
"""
        sp = sub.add_parser(name, help=helptext + "（批量或单只）",
                            formatter_class=argparse.RawDescriptionHelpFormatter,
                            epilog=examples)
        add_date(sp)
        add_verbose(sp)
        sp.add_argument("--stock", metavar="代码", default=None,
                        help=f"单只模式：只跑这一只（转成 {flag}），不需要当天的选股名单")
        sp.add_argument("--csv", metavar="CSV", default=None,
                        help="批量：从该 CSV 读代码列（与 --stock 二选一；"
                             "不依赖 results.db 的名单，--date 仅作运行标记）")
        sp.add_argument("--workers", type=int, default=None,
                        help="批量并发数（默认用工具自己的 4）")
        sp.add_argument("--force", action="store_true",
                        help="批量模式忽略库里已有结论，全部重算")
        if name == "quality":
            sp.add_argument("--retries", type=int, default=None,
                            help="取数失败重试次数（默认 2；0 = 失败即跳过）")
            sp.add_argument("--retry-wait", type=float, default=None,
                            help="重试等待秒数（默认 60）")
            sp.add_argument("--timeout", type=float, default=None,
                            help="单次取数超时秒数（默认 90）")
        sp.add_argument("extra", nargs="*", default=[], help="透传给该程序的参数")

    sp = sub.add_parser("top", help="顶部判断 stock_judge_top（单只/多只/名单批量）",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        epilog="""示例：
  ./run.py top 600519
  ./run.py top sh600519 sz000001
  ./run.py top --csv ~/MyData/持仓.csv
  ./run.py top --from-select --date 2026-11-13
""")
    add_date(sp)
    add_verbose(sp)
    sp.add_argument("codes", nargs="*", default=[],
                    help="股票代码，如 sh600519 600519 600519.SH（与 --csv 二选一）")
    sp.add_argument("--csv", metavar="CSV", default=None,
                    help="批量：从该 CSV 读代码列（代码/股票代码/证券代码/code；"
                         "不依赖 results.db 的名单）")
    sp.add_argument("--from-select", action="store_true",
                    help="批量：直接用结果库里当天的选股名单（基准日 = --date）")
    sp.add_argument("--workers", type=int, default=4, help="批量并发线程数，默认 4")
    sp.add_argument("--refresh", action="store_true",
                    help="忽略缓存重新取数，并把库里已有结果也重算")
    sp.add_argument("extra", nargs="*", default=[], help="透传给 stock_judge_top 的参数")

    sp = sub.add_parser("price",
                        help="冷热极点（stock_extreme_point）：全市场价格/估值快照"
                             "（写 shared.db + results.db）",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        epilog="""示例：
  ./run.py price                      # 今天（过了 15:05）或上一交易日
  ./run.py price --date 2026-11-17    # 指定交易日
  ./run.py price --date 2026-11-17 --force       # 忽略库里已有，重取整天
  ./run.py price --prune-keep 30      # 顺手删掉 30 天之前的 price 行
  ./run.py price --restat             # 只按结果库已有 price 重算每日摘要，不联网
  ./run.py price --sync               # 原始库 → 结果库，不联网
  ./run.py price --code 002096 --date 2026-11-03  # 只补这一只（保留当天其它行）

一天一份、一股一行，全市场约 5500 只（含北交所），约 6 个上游请求。
字段：收盘价 / 涨跌幅 / PE(TTM) / PB(MRQ) / PS(TTM) / 总市值 / 流通市值。
落两个库：shared.db 的 price 是原件；results.db 的 price 是看板 /extreme 读的复制品，
同一天还会写 price_stat（涨跌家数 / 涨跌停 / 估值中位数 / 总市值合计），
与 price 同事务写入。抓取与落库都在独立工具 `stock_extreme_point` 里。

⚠️ 这是**收盘快照**：上游当天收盘后才发布，盘中跑「今天」会报
「上游没有这一天的数据」。要么收盘后再跑，要么显式给上一个交易日。""")
    add_date(sp)
    sp.add_argument("--code", default=None,
                    help="只抓一只股票（002096 / sz002096 / 002096.SZ），"
                         "upsert 到 price 并重算 price_stat；不删当天其它行")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="把子进程的原始输出全部打到屏幕上（默认只有进度条，明细在日志里）")
    sp.add_argument("--force", action="store_true",
                    help="忽略库里已有的那一天，重新取一次（整天整份替换）")
    sp.add_argument("--prune-keep", type=int, default=0, metavar="天",
                    help="取完之后删掉这么多天之前的 price 行；0 = 不清理（默认）")
    sp.add_argument("--restat", action="store_true",
                    help="只按结果库已有 price 数据重算每日摘要（price_stat），不联网")
    sp.add_argument("--sync", action="store_true",
                    help="把 shared.db 已有的 price 复制/覆盖到 results.db（不联网）")
    sp.add_argument("extra", nargs="*", default=[], help="透传给 stock_extreme_point 的参数")

    sp = sub.add_parser("backup", help="备份 shared.db / results.db 到 backup/（VACUUM INTO）")
    sp.add_argument("--dir", default=None,
                    help="备份目录，默认 backup/（可用绝对路径，如 ~/MyData/backups）")
    sp.add_argument("--keep", type=int, default=7,
                    help="每个库保留最近几份，默认 7（只删本命令生成的，手改的不动）")
    sp.add_argument("--only", choices=["shared", "results"], default=None,
                    help="只备份其中一个库")
    sp.add_argument("--list", dest="list_only", action="store_true",
                    help="只列出已有备份，不做备份")

    sp = sub.add_parser("web",
                        help="打开看板：Flask + Chart.js，读 results.db"
                             "（默认进首页 /batches）")
    sp.add_argument("--port", type=int, default=5000, help="端口，默认 5000")
    sp.add_argument("--host", default="127.0.0.1", help="监听地址，默认只本机")
    sp.add_argument("--debug", action=argparse.BooleanOptionalAction, default=True,
                    help="Flask 调试模式（默认开，改模板/代码自动重载；--no-debug 关闭）")

    sub.add_parser("menu", help="交互菜单（0-7 选择）")
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
            args.regime_args = split_passthrough(getattr(args, "regime_args", ""))
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
