"""主流程端到端测试（**离线**：`FakeHub` 顶替共享库，一次网络都不打）。

跑法：

    cd /home/kevin/MyData/stock_value_invest_tool
    python3 -m stock_kline_screen.tests.test_pipeline

`test_rules` / `test_turnover` / `test_turnaround` 各自测一层；这个文件测的是
**它们串起来之后**的那条链：粗筛 → 排除集 → K 线 → 裁决 → 次新复检 → 换手 → 标注 → 写盘。

三类断言，每类都对应一次真实故障：

1. **计数自洽**：`粗筛池 == 入选 + 剔除`，且每只股票有且只有一个去向。这条不成立时，
   名单会悄悄少几只而报告看起来完全正常（原版遇到过一次）。
2. **历史不足 ≠ 取数失败**：次新股只有 36 根 K 线时，`ensure_many` 抛
   `InsufficientDepth`。若把它当致命错误，整跑中止——2026-11-14 真跑就是被
   `sh601091`（上市两个月）打断的。它必须走"剔除"，且剔除原因要能看出来是**数据深度**。
3. **基准日有成交却取不到 K 线 → 必须中止**：那不能按停牌放过，否则名单里少一只
   当周最该入选的票，而报告一句都不提。
"""


from __future__ import annotations

# 结果库指到临时目录：本文件被直接跑（不经 tests 包）时也要生效。
try:
    from . import _env  # noqa: F401  （作为包导入）
except ImportError:  # pragma: no cover - 直接跑文件
    import _env  # noqa: F401

import argparse
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

from stock_data_fetch.api import Batch, Range                 # noqa: E402
from stock_data_fetch.calendar import TradingCalendar         # noqa: E402
from stock_data_fetch.errors import InsufficientDepth, PartialData  # noqa: E402
from stock_kline_screen import pipeline as P                  # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def section(title: str) -> None:
    print(f"\n{title}")


def mkdtemp() -> Path:
    # 从测试文件位置推导项目根，不写死 `~/MyData/<项目名>`：
    # 项目 2026-11-15 改过名，写死的路径会让临时目录建到不存在的地方。
    # `__file__` = <项目根>/stock_kline_screen/tests/test_pipeline.py
    root = Path(__file__).resolve().parent.parent.parent / ".testtmp"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="kline-e2e-", dir=root))


# ---------------------------------------------------------------- 造市场

def days(n: int = 120) -> list[str]:
    """连续 n 个"交易日"（跳过周末即可，日历只要求有序且是字符串日期）。"""
    import datetime as dt
    out, d = [], dt.date(2026, 5, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def bars_for(cal: list[str], *, spike: bool = True) -> list[dict]:
    """造一只"最后 5 天向上突破 MA20"的票。

    前面全部平在 10.00（MA20 = MA60 = 10.00），然后连涨 5 天到 10.50。
    `find_start` 的回扫停在 `ci = len-5`：`close(ci-1)=10.00` 不大于
    `MA20(ci-1)=10.00`，正好是规则要求的"真上穿"；`age = 4 = N`，⑦ 不剔除。
    **长度必须与 cal 一致**——造短了 `find_start` 会拿不到 S 那天的 bar。
    """
    n = len(cal)
    closes = [10.0] * (n - 5) + [10.1, 10.2, 10.3, 10.4, 10.5]
    vols = [1_000_000.0] * (n - 5) + ([5_000_000.0] if spike else [1_000_000.0]) \
        + [1_000_000.0] * 4
    rows = []
    for d, c, v in zip(cal, closes, vols):
        rows.append({"date": d, "open": c, "high": c * 1.01, "low": c * 0.99,
                     "close": c, "volume": v, "amount": 0.0})
    return rows


class FakeHub:
    """只实现 pipeline 用到的那几个入口。"""

    def __init__(self, *, cal: TradingCalendar, kline: dict[str, list[dict]],
                 scan: dict[str, list[dict]], fail: dict[str, Exception] | None = None,
                 profiles: dict[str, dict] | None = None,
                 quotes: dict[str, dict] | None = None,
                 finance_fail: bool = True) -> None:
        self.cal = cal
        self.target = cal.days[-1]
        self.cfg = SimpleNamespace(state_dir=mkdtemp() / "state")
        self._kline = kline
        self._scan = scan
        self._fail = fail or {}
        self._profiles = profiles or {}
        self._quotes = quotes or {}
        self._finance_fail = finance_fail
        self.calls: list[tuple[str, int]] = []

    # 横截面
    def scan_many(self, ds):
        return {d: self._scan.get(d, []) for d in ds}

    def scan_cached(self, d):
        return self._scan.get(d, [])

    def prune_scan(self):
        return None

    def prune_kline(self):
        return None

    def ensure_many(self, domain, symbols, need, **_):
        self.calls.append((domain, len(symbols)))
        b = Batch()
        for s in symbols:
            if s in self._fail:
                b.failed[s] = self._fail[s]
                continue
            rows = self._rows(domain, s)
            if rows is None:
                b.failed[s] = PartialData(f"{domain} 假失败")
            else:
                b.ok[s] = Range(domain=domain, symbol=s, rows=rows,
                                n=len(rows), start=rows[0].get("date") if rows else None,
                                end=rows[-1].get("date") if rows else None)
        return b

    def read(self, domain, s, need):
        """只读：`ensure_bulk` 之后 pipeline 靠它把已命中的那份读回来。"""
        rows = self._rows(domain, s) or []
        return Range(domain=domain, symbol=s, rows=rows, n=len(rows),
                     start=rows[0].get("date") if rows else None,
                     end=rows[-1].get("date") if rows else None)

    def ensure_bulk(self, domain, symbols, need, **_):
        """真实链路里它是"组级批量预取"；测试里直接退化为逐只 ensure_many——
        行为等价（成功进 ok、其余进 failed），只是没有批量收益。"""
        return self.ensure_many(domain, symbols, need)

    def _rows(self, domain, s):
        if domain == "kline":
            return self._kline.get(s)
        if domain == "profile":
            p = self._profiles.get(s)
            return [p] if p else None
        if domain == "quote":
            q = self._quotes.get(s)
            return [q] if q else None
        if domain == "finance":
            return None if self._finance_fail else []
        return None


def make_market(tmp: Path, *, drop: str | None = None, deep: str | None = None):
    """5 天窗口（S=最后一天），4 只票：好票 / ST / 历史不足 / 基准日无成交。"""
    cal = days()
    S = cal[-1]
    need = cal[-5:]
    good, st_stock, newish, illiquid = "sh600001", "sh600002", "sh600003", "sh600004"

    kline = {good: bars_for(cal), st_stock: bars_for(cal), newish: bars_for(cal)[-36:]}
    scan = {}
    for d in need:
        rows = []
        for sym, name in ((good, "好票"), (st_stock, "*ST 烂票"), (newish, "次新")):
            chg = 3.0 if d == cal[-5] else (0.0 if d != S else 1.0)
            rows.append({"date": d, "symbol": sym, "name": name,
                         "close": 10.0, "pct_chg": chg})
        scan[d] = rows

    fail = {}
    if deep:
        fail[deep] = InsufficientDepth("点数不足（库 36 / 需 71）", domain="kline", symbol=deep)
    if drop:
        fail[drop] = PartialData("限频截断", domain="kline", symbol=drop)

    hub = FakeHub(cal=TradingCalendar(cal, cal[0], cal[-1]), kline=kline, scan=scan,
                  fail=fail,
                  profiles={good: {"symbol": good, "list_date": "2010-01-01",
                                   "industry": "白酒", "is_financial": 0},
                            st_stock: {"symbol": st_stock, "list_date": "2010-01-01",
                                       "industry": "化工", "is_financial": 0}},
                  quotes={good: {"symbol": good, "trade_date": S, "circ_mv": 1e10,
                                 "total_mv": 2e10, "price": 10.5},
                          st_stock: {"symbol": st_stock, "trade_date": S, "circ_mv": 1e10,
                                     "total_mv": 2e10, "price": 10.5}})
    return hub, S, {"good": good, "st": st_stock, "new": newish, "illiquid": illiquid}


def args(n: int = 4) -> argparse.Namespace:
    return argparse.Namespace(
        date=None, n=n, k_vol=1.5, chg_lo=0.5, chg_hi=20.5, turn_pct=0.0,
        turn_min=5, turn_max=30, ma60_gap=0.0, db=None)


def run_quiet(hub, a, st=None):
    """跑一次，捕获输出。`st` 传一个 dict 就把内部状态回填进去。

    原来这些断言都是读 `output/kline-screen/result_<S>.json` 验的；2026-11-15
    按要求删掉了那份 JSON（当时的产物只剩 CSV，现在连 CSV 也没了，只留
    results.db），改为直接拿 `run(..., into=st)` 回填的内存状态——比读盘更直接，
    也不会出现"盘上的和跑出来的不一致"。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = P.run(a, hub, into=st)
    return rc, buf.getvalue()


# ---------------------------------------------------------------- 用例

def test_happy_path() -> None:
    section("正常链路：一只票走完全部 9 步并落库")
    hub, S, codes = make_market(mkdtemp())
    st = {}
    rc, out = run_quiet(hub, args(), st)
    check("退出码 0", rc == 0, str(rc))
    # 2026-11-15 起不再产出 CSV/HTML：名单只进 results.db（web 看板读它），
    # 所以这里改成"output 目录一个文件都不该有"。
    outdir = Path(str(hub.cfg.state_dir.parent)) / "output" / "kline-screen"
    check("output 目录里没有产物文件（CSV/HTML 已移除）",
          not outdir.exists() or not list(outdir.iterdir()),
          str(sorted(p.name for p in outdir.iterdir())) if outdir.exists() else "不存在")

    final = st["final"]
    check("粗筛池 = 入选 + 剔除（计数自洽）",
          st["pool_n"] == len(final) + sum(st["rej_raw"].values()),
          f'{st["pool_n"]} vs {len(final)}+{sum(st["rej_raw"].values())}')
    check("每只股票都有去向（剔除代码 ∪ 名单 = 粗筛池）",
          set(st["rej_code"]) | {x["code"] for x in final} == set(st["pool"]),
          f'{len(set(st["rej_code"]) | {x["code"] for x in final})} vs {len(st["pool"])}')
    check("落库行数 = 名单只数", st.get("n_csv") == len(final), str(len(final)))
    check("好票入选了（规则链真的能选出票）",
          codes["good"] in {x["code"] for x in final},
          str([x["code"] for x in final]))
    check("金融/行业标注落地", final and final[0]["sw1"] != "未分类",
          str(final[:1]))
    check("财务取数失败时毛利率列留空、不影响选股",
          final and final[0]["turn_p1"] == "",
          str(final[:1]) if final else "无名单")
    check("横截面只查了 5 天（不重复扫）", all(c[0] != "scan" for c in hub.calls), str(hub.calls))


def test_st_excluded() -> None:
    section("排除集：*ST 按名称剔除，且不影响别的票")
    hub, _, codes = make_market(mkdtemp())
    st = {}
    rc, out = run_quiet(hub, args(), st)
    st_rej = [k for k in st["rej_raw"] if "ST" in k]
    check("*ST 被剔除且原因写明 ST", bool(st_rej), str(st["rej_raw"]))
    check("剔除代码里 ST 票的去向是 judge 阶段",
          st["rej_code"].get(codes["st"], ["", ""])[0] in ("JD", "FS"),
          str(st["rej_code"].get(codes["st"])))


def test_depth_not_fatal() -> None:
    section("历史不足（次新）→ 剔除，不中止整跑")
    hub, S, codes = make_market(mkdtemp(), deep="sh600003")
    st = {}
    rc, out = run_quiet(hub, args(), st)
    key = [k for k in st["rej_raw"] if "历史不足" in k]
    check("没有抛异常（rc=0）", rc == 0, str(rc))
    check("剔除原因写的是「K线历史不足」而不是「取数失败」", bool(key),
          str(st["rej_raw"]))
    check("这只票的去向是 FS 阶段",
          st["rej_code"].get(codes["new"], ["", ""])[0] == "FS",
          str(st["rej_code"].get(codes["new"])))
    check("计数仍然自洽", st["pool_n"] == len(st["final"]) + sum(st["rej_raw"].values()),
          str(st["pool_n"]))
    check("终端里明确说了「历史不足」", "历史不足" in out, out[-300:])


def test_hard_fail_aborts() -> None:
    section("基准日有成交但取不到 K 线 → 中止（不许当成停牌）")
    hub, S, codes = make_market(mkdtemp(), drop="sh600001")
    try:
        run_quiet(hub, args())
        check("应抛 RuntimeError", False)
    except RuntimeError as e:
        check("抛 RuntimeError 且点名代码", codes["good"] in str(e), str(e))


def test_window_too_short() -> None:
    section("参数与窗口边界")
    hub, _, _ = make_market(mkdtemp())
    try:
        run_quiet(hub, args(n=200))
        check("窗口长于可用历史 → 报错", False)
    except P.CliError as e:
        check("窗口长于可用历史 → CliError", "交易日不足" in str(e), str(e))

    bad = args()
    bad.chg_lo, bad.chg_hi = 9.0, 1.0
    try:
        run_quiet(hub, bad)
        check("涨幅区间反了 → 报错", False)
    except P.CliError as e:
        check("涨幅区间反了 → CliError", "chg-lo" in str(e), str(e))


# ---------------------------------------------------------------- 换手分位口径

def make_wide_market(tmp: Path, pool_n: int, *, good_hot: bool = False):
    """`pool_n` 只票全部通过涨幅粗筛，其中只有 1~2 只是「好票」结构（能入选）。

    其余的票最后一根收盘 9.0 < MA20，`find_start` 判「未站上 MA20」被剔。
    它们的 K 线 + 流通市值都在，只是入选不了 —— 这正是换手分位该有的分母成员。

    换手值由成交量决定：第 i 只的日均量 = (i+1) × 1e6，换手严格单调递增。
    `good_hot=True` 时把好票的量抬到 3e7（= 第 29 只的水平），它就成了名单里
    最热的那只，硬过滤下必被 TN 剔除；同时补一只「冷好票」保证名单不为空，
    否则 `--turn-min` 会把档位放宽到 60% 把谁都不剔（自适应逻辑本身是对的）。
    """
    cal = days()
    S, need = cal[-1], cal[-5:]
    good = "sh600001"
    good_bars = bars_for(cal)
    # 放量根压到 1.6e6：仍 ≥1.5× 前 5 日均量（1e6）过 ②，20 日均值只多 0.002e6。
    good_bars[-5]["volume"] = 1_600_000.0
    if good_hot:
        for b in good_bars:
            b["volume"] = 30_000_000.0
        good_bars[-5]["volume"] = 48_000_000.0
    others = [f"sh61{i:04d}" for i in range(pool_n - 1)]
    cold_friend = "sh600099" if good_hot else None
    kline, scan, quotes = {good: good_bars}, {}, {}

    def flat(sym: str, vol: float) -> list[dict]:
        """前面平在 10、最后一根跌到 9 —— 持仓够 71 根，但 S 日跌破 MA20。"""
        closes = [10.0] * (len(cal) - 1) + [9.0]
        return [{"date": d, "open": c, "high": c, "low": c, "close": c,
                 "volume": vol, "amount": 0.0} for d, c in zip(cal, closes)]

    if cold_friend:
        kline[cold_friend] = bars_for(cal)          # 同样结构，量的基准 1e6（很冷）
        quotes[cold_friend] = {"symbol": cold_friend, "trade_date": S, "circ_mv": 1e10,
                               "total_mv": 2e10, "price": 10.5}

    for d in need:
        rows = [{"date": d, "symbol": good, "name": "好票", "close": 10.0,
                 "pct_chg": 3.0 if d == cal[-5] else (1.0 if d == S else 0.0)}]
        if cold_friend:
            rows.append({"date": d, "symbol": cold_friend, "name": "冷好票",
                         "close": 10.0,
                         "pct_chg": 1.5 if d == cal[-5] else (1.0 if d == S else 0.0)})
        for i, sym in enumerate(others):
            rows.append({"date": d, "symbol": sym, "name": f"平票{i}",
                         "close": 9.0, "pct_chg": 2.0})
        scan[d] = rows

    quotes[good] = {"symbol": good, "trade_date": S, "circ_mv": 1e10,
                    "total_mv": 2e10, "price": 10.5}
    for i, sym in enumerate(others):
        kline[sym] = flat(sym, (i + 1) * 1_000_000.0)
        quotes[sym] = {"symbol": sym, "trade_date": S, "circ_mv": 1e10,
                       "total_mv": 2e10, "price": 9.0}

    prof = {good: {"symbol": good, "list_date": "2010-01-01",
                   "industry": "白酒", "is_financial": 0}}
    if cold_friend:
        prof[cold_friend] = {"symbol": cold_friend, "list_date": "2010-01-01",
                             "industry": "白酒", "is_financial": 0}
    hub = FakeHub(cal=TradingCalendar(cal, cal[0], cal[-1]), kline=kline, scan=scan,
                  profiles=prof, quotes=quotes)
    return hub, S, good


def test_turnover_denominator_is_pool() -> None:
    section("换手分位：分母是整个粗筛池，不是入选名单")
    hub, S, good = make_wide_market(mkdtemp(), pool_n=25)
    st = {}
    rc, out = run_quiet(hub, args(), st)
    check("跑通", rc == 0, str(rc))

    n = st["turn_meta"]["池内样本"]
    check("池内样本 = 25（粗筛池全体），不是入选数 1", n == 25,
          f"池内样本={n}，入选={len(st['final'])}")
    qcalls = [c for c in hub.calls if c[0] == "quote"]
    check("quote 是对整个池取的，不只是入选股",
          any(n_ >= 20 for _, n_ in qcalls), str(qcalls))

    row = st["final"][0] if st["final"] else {}
    # 好票 20 日均量 ≈ 1.04e6（含一根 1.6e6），换手 0.104%；池内第 0 只 0.10% 比它更低，
    # 所以分位 = 1/25 = 4.0。关键是它**不再是 0 或空**：分母里真有别的票在比。
    rank = row.get("turn20_rank")
    check("好票分位是个小值（池内只有 1 只比它更冷）", rank is not None and rank <= 5.0,
          f"rank={rank}，turn20={row.get('turn20')}")
    check("分位星级 = ***（Q1）", rank is not None and rank < 20, str(rank))


def test_hard_filter_can_fire() -> None:
    section("池内样本 ≥20 时 --turn-pct 真的能触发硬过滤")
    # good_hot：好票换手 0.3%，池内 25 只中排最热，--turn-pct 0.02 下必被 TN。
    # 另有一只「冷好票」（换手 0.1%）保证名单不为空，从而不触发 --turn-min 的放宽。
    hub, S, good = make_wide_market(mkdtemp(), pool_n=25, good_hot=True)
    a = args()
    a.turn_pct = 0.02
    st = {}
    rc, out = run_quiet(hub, a, st)
    check("跑通", rc == 0, str(rc))

    meta = st["turn_meta"]
    check("换手过滤标记为启用", meta.get("启用") is True, str(meta))
    # 2% 档只留 0~1 只、少于 --turn-min=5，规则按设计放宽到 60%。
    # 这也是修复前的盲区所在：`池内样本 < 20 → 直接退化为只标注`，
    # 硬过滤连「放宽」这一步都走不到。这里是「放宽了但仍然剔了人」——
    # 只有分母真是整个池，60% 这条线才会落在好票身上。
    check("自适应逻辑照常工作（2% 档不足 → 放宽到 60%）",
          "放宽" in (meta.get("自适应") or ""), str(meta))
    tn = {k: v for k, v in st["rej_code"].items() if v[0] == "TN"}
    check("有人被 TN 剔除（不再「永不触发」）", bool(tn), str(tn))
    check("被 TN 剔的正是最热的那只好票", good in tn, str(list(tn)[:5]))
    check("计数仍自洽", st["pool_n"] == len(st["final"]) + sum(st["rej_raw"].values()),
          str(st["pool_n"]))


def main() -> int:
    test_happy_path()
    test_st_excluded()
    test_depth_not_fatal()
    test_hard_fail_aborts()
    test_window_too_short()
    test_turnover_denominator_is_pool()
    test_hard_filter_can_fire()

    print("\n" + "=" * 60)
    if FAIL:
        print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
        for name in FAIL:
            print(f"  - {name}")
        return 1
    print(f"通过 {PASS} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
