"""probe 测试（**离线**：上游全部打假，一个网络包都不发）。

跑法：

    cd /home/kevin/MyData/stock_value_invest_tool
    python3 -m stock_data_fetch.tests.test_probe

为什么必须离线：probe 天生联网，而"联网才能跑"的测试没法进 `tests`——它会在
东财封禁时红、在别人机器上红、在没有 westock 令牌时红。假上游只验证**判定逻辑**：
什么算一致、什么算不一致、什么该跳过。真实口径的对账是冒烟时手工跑的。

覆盖四类事：

1. **一致就该绿**——探针乱报比不报更糟，四个工具的启动信心全靠它；
2. **每种不一致都要被抓到**——尤其分红"同日多方案漏记"（历史上真发生过）；
3. **前置条件不满足时是 SKIP 不是失败**——上游被封、库里没这只票，都不该让退出码变红；
4. **探针自己崩了要降级成 MISMATCH**——崩掉等于没探，静默通过最危险。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent        # stock_value_invest_tool/（包的父目录）
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import db, probe                         # noqa: E402
from stock_data_fetch.calendar import TradingCalendar         # noqa: E402
from stock_data_fetch.config import DEFAULT_SCHEMA, Config    # noqa: E402
from stock_data_fetch.errors import SourceUnavailable         # noqa: E402
from stock_data_fetch.sources import eastmoney as em          # noqa: E402
from stock_data_fetch.sources import westock as W             # noqa: E402

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


def mkdtemp(prefix: str) -> Path:
    root = Path(os.environ.get("STOCKHUB_TMP")
                or HERE.parent.parent.parent / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def codes(results: list[probe.Result]) -> list[str]:
    return [r.status for r in results]


def status(results: list[probe.Result]) -> str:
    """单条探针一次只该有一个结论时，取那个结论。"""
    return results[0].status if len(results) == 1 else ",".join(codes(results))


def mkcfg(tmp: Path, cal: Path) -> Config:
    c = Config()
    c.db_path = tmp / "shared.db"
    c.state_dir = tmp / "state"
    c.schema_path = DEFAULT_SCHEMA
    c.calendar_path = cal
    return c


def write_cal(tmp: Path) -> TradingCalendar:
    """写一份 400 个交易日的假日历，**末日贴近今天**。

    末日必须贴近今天：`probe_suspension` 按"近 60 天"筛洞，日历跑到两年前的话，
    所有洞都会被判成"都在更早"，测出来的是筛子而不是判定逻辑。
    """
    days: list[str] = []
    d = dt.date.today() - dt.timedelta(days=580)
    while d <= dt.date.today():
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    days = days[-400:]
    p = tmp / "trading_calendar.json"
    p.write_text(json.dumps({"days": days, "span": "probe-test", "end": days[-1],
                             "source": "test"}, ensure_ascii=False), encoding="utf-8")
    return TradingCalendar(days=days, span="probe-test", end=days[-1], source="test")


def mkdb(tmp: Path, cal: TradingCalendar, *, symbol: str = "sh600000",
         bars: int = 60, price: float = 10.0, dividends: list[tuple] | None = None,
         valuations: list[tuple] | None = None, holes: list[dict] | None = None) -> Path:
    """造一个只有一只票的最小库。价格线性微涨，方便算相对差。"""
    dbp = tmp / "shared.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)
    days = cal.days[-bars:]
    with db.tx(conn):
        conn.executemany(
            "INSERT INTO kline(symbol,date,open,high,low,close,volume,amount,src) "
            "VALUES(?,?,?,?,?,?,100,1000,'eastmoney')",
            [(symbol, d, price, price, price, price + i * 0.01)
             for i, d in enumerate(days)])
        for ex, cash, bonus, transfer, text in (dividends or []):
            conn.execute(
                "INSERT INTO dividend(symbol,ex_date,cash_per_share,bonus_per_share,"
                "transfer_per_share,rights_per_share,rights_price,plan_text,src,updated_at) "
                "VALUES(?,?,?,?,?,0,NULL,?,'tencent','2026-11-14T00:00:00')",
                (symbol, ex, cash, bonus, transfer, text))
        for day, pe, pb in (valuations or []):
            conn.execute("INSERT INTO valuation(symbol,trade_date,pe_ttm,pb_mrq,ps_ttm,"
                         "total_mv,circ_mv,src) VALUES(?,?,?,?,NULL,NULL,NULL,'eastmoney')",
                         (symbol, day, pe, pb))
        conn.execute(
            "INSERT INTO coverage(domain,symbol,fp,start_date,end_date,n,holes,hole_detail,"
            "partial,src,updated_at) VALUES('kline',?,'',?,?,?,?,?,0,'eastmoney',"
            "'2026-11-14T00:00:00')",
            (symbol, days[0], days[-1], len(days), len(holes or []),
             json.dumps(holes, ensure_ascii=False) if holes else None))
    conn.close()
    return dbp


# ================================================================ 1. 日历

def test_calendar(cal: TradingCalendar) -> None:
    section("1. calendar：本地 vs 上游交易日历")

    monday = cal.days[-1]
    good = {"days": [{"date": d, "isTrading": True} for d in cal.days[-90:]]}
    with patch.object(W, "call", lambda *a, **k: good):
        r = probe.probe_calendar(cal, days=90)
    check("完全一致 → OK", status(r) == "OK", str(r))

    # 上游少一天 = 本地多算一天
    short = {"days": [{"date": d, "isTrading": True} for d in cal.days[-90:-1]]}
    with patch.object(W, "call", lambda *a, **k: short):
        r = probe.probe_calendar(cal, days=90)
    check("上游少一天 → MISMATCH", status(r) == "MISMATCH", str(r))
    check("文案点明「本地多出」并给出日期", "本地多出" in r[0].title and monday in r[0].rows[0],
          f"{r[0].title} / {r[0].rows}")

    # 上游多一天 = 本地缺一天（那天永远补不上）
    more = {"days": [{"date": d, "isTrading": True} for d in cal.days[-90:]]
            + [{"date": "1999-01-04", "isTrading": True}]}
    with patch.object(W, "call", lambda *a, **k: more):
        r = probe.probe_calendar(cal, days=90)
    check("上游的日期在窗口外不算差异", status(r) == "OK", str(r))

    # isTrading=False 的不算交易日（上游把标志位和筛参数混用时必须挡住）
    flagged = {"days": [{"date": d, "isTrading": False} for d in cal.days[-90:]]}
    with patch.object(W, "call", lambda *a, **k: flagged):
        r = probe.probe_calendar(cal, days=90)
    check("isTrading=False 一律不当交易日", status(r) == "SKIP", str(r))

    with patch.object(W, "call", lambda *a, **k: {"days": []}):
        r = probe.probe_calendar(cal, days=90)
    check("上游返回空 → SKIP（不是失败）", status(r) == "SKIP", str(r))

    # 空日历：`TradingCalendar` 构造时就拒绝空列表，这里用桩对象模拟"文件里没日子"
    empty = type("EmptyCal", (), {"days": []})()
    check("本地日历是空的 → SKIP", status(probe.probe_calendar(empty)) == "SKIP")

    # 参数必须是 year：传 start/end 会被上游忽略，然后拿回整年
    seen: list[dict] = []

    def spy(tool, args, **kw):
        seen.append(args)
        return {"days": [{"date": d, "isTrading": True} for d in cal.days[-90:]]}

    with patch.object(W, "call", spy):
        probe.probe_calendar(cal, days=90)
    check("按年问上游（参数是 year，不是 start/end）",
          all("year" in a and "start" not in a for a in seen), str(seen[:2]))


# ================================================================ 2. 前复权

def _em_rows(prices: dict[str, float]) -> list[dict]:
    return [{"date": d, "close": c} for d, c in sorted(prices.items())]


def test_qfq(cal: TradingCalendar, tmp: Path) -> None:
    section("2. qfq：本地前复权 vs 上游")

    tmp1 = mkdtemp("stockhub-probe-qfq-")
    dbp = mkdb(tmp1, cal, price=10.0)

    # 上游给的正好等于库里的 raw（这票没除过权，qfq == raw）
    conn = db.connect(dbp, readonly=True)
    raw = {r["date"]: float(r["close"]) for r in conn.execute("SELECT * FROM kline")}
    conn.close()

    with patch.object(em, "_fetch_one", lambda s, n, **k: (_em_rows(raw), None)):
        r = probe.probe_qfq(db.connect(dbp, readonly=True), cal, "sh600000")
    check("两边一致 → OK", status(r) == "OK", str(r))
    check("文案写明口径与对账源", "等比" in r[0].title and "东财" in r[0].detail,
          f"{r[0].title} / {r[0].detail}")

    # 上游整体高 1% → 两种口径都对不上 → 这是数据错
    off = {d: c * 1.01 for d, c in raw.items()}
    with patch.object(em, "_fetch_one", lambda s, n, **k: (_em_rows(off), None)):
        r = probe.probe_qfq(db.connect(dbp, readonly=True), cal, "sh600000")
    check("差 1% → MISMATCH", status(r) == "MISMATCH", str(r))
    check("MISMATCH 里提示先去查 dividend 表", "dividend" in r[0].detail, r[0].detail)
    check("MISMATCH 同时报出两种口径的差", "等比差" in r[0].title and "等差差" in r[0].title,
          r[0].title)
    check("最多列 5 个差异点", len(r[0].rows) <= 5, str(len(r[0].rows)))

    # 上游用的是**等差**口径（派现直接减）→ 该判 OK 并说明是口径差
    tmp_div = mkdtemp("stockhub-probe-qfq-sub-")
    ex = cal.days[-20]
    dbp_div = mkdb(tmp_div, cal, price=10.0,
                   dividends=[(ex, 1.0, 0.0, 0.0, "10派10.000元")])
    conn = db.connect(dbp_div, readonly=True)
    raw2 = {r["date"]: float(r["close"]) for r in conn.execute("SELECT * FROM kline")}
    conn.close()
    sub = {d: (c - 1.0 if d < ex else c) for d, c in raw2.items()}   # 手算的等差序列
    with patch.object(em, "_fetch_one", lambda s, n, **k: (_em_rows(sub), None)):
        r = probe.probe_qfq(db.connect(dbp_div, readonly=True), cal, "sh600000",
                            n=60, max_bars=600)
    check("上游是等差口径 → OK 而不是报错", status(r) == "OK", str(r))
    check("文案点明「上游是等差口径」", "等差" in r[0].title, r[0].title)
    check("解释差值来源（派现 × (1 − 当日价/前收盘)）", "派现" in r[0].detail, r[0].detail)

    # 上游两条腿都挂 → SKIP
    with patch.object(em, "_fetch_one",
                      lambda *a, **k: (_ for _ in ()).throw(SourceUnavailable("x"))), \
         patch.object(W, "call",
                      lambda *a, **k: (_ for _ in ()).throw(SourceUnavailable("y"))):
        r = probe.probe_qfq(db.connect(dbp, readonly=True), cal, "sh600000")
    check("两条腿都挂 → SKIP（不是失败）", status(r) == "SKIP", str(r))

    # 东财挂、westock 顶上时，文案里要写清换的是哪条腿
    with patch.object(em, "_fetch_one",
                      lambda *a, **k: (_ for _ in ()).throw(
                          SourceUnavailable("东财连接中断", transient=True))), \
         patch.object(W, "call", lambda *a, **k: {"nodes": [
             {"date": d, "last": v} for d, v in sorted(raw.items())]}):
        r = probe.probe_qfq(db.connect(dbp, readonly=True), cal, "sh600000")
    check("东财挂了走 westock → OK 且注明退路",
          status(r) == "OK" and "westock" in r[0].detail, f"{r[0].title} / {r[0].detail}")

    # 库里 K 线太少
    tmp2 = mkdtemp("stockhub-probe-qfq2-")
    dbp2 = mkdb(tmp2, cal, bars=3)
    r = probe.probe_qfq(db.connect(dbp2, readonly=True), cal, "sh600000")
    check("K 线不足 5 根 → SKIP", status(r) == "SKIP", str(r))


def test_qfq_window(cal: TradingCalendar) -> None:
    section("2b. qfq 窗口必须跨过除权日（否则这条探针是瞎的）")

    tmp = mkdtemp("stockhub-probe-win-")
    # 除权日在第 30 根 bar 上；不拉窗口的话最近 20 根根本不跨事件
    ex = cal.days[-30]
    dbp = mkdb(tmp, cal, bars=60, dividends=[(ex, 0.5, 0.0, 0.0, "10派5.000元")])
    conn = db.connect(dbp, readonly=True)

    seen: list[dict] = []

    def spy_em(symbol, need, *, fqt="0"):
        seen.append({"fqt": fqt, "start": need.start, "end": need.end})
        return [], "no_data"

    with patch.object(em, "_fetch_one", spy_em), \
         patch.object(W, "call", lambda *a, **k: {"nodes": []}):
        probe.probe_qfq(conn, cal, "sh600000", n=20)
    check("自动把起点拉到除权日之前", seen and seen[0]["start"] < ex,
          f"{seen[0] if seen else None} vs ex={ex}")
    check("对账用的是 fqt=1（前复权）", seen and seen[0]["fqt"] == "1", str(seen[:1]))
    conn.close()

    # 没有分红记录时不拉窗口（不白白多要 K 线）
    tmp2 = mkdtemp("stockhub-probe-win2-")
    dbp2 = mkdb(tmp2, cal, bars=60)
    conn2 = db.connect(dbp2, readonly=True)
    seen.clear()
    with patch.object(em, "_fetch_one", spy_em), \
         patch.object(W, "call", lambda *a, **k: {"nodes": []}):
        probe.probe_qfq(conn2, cal, "sh600000", n=20)
    check("没有除权日就只取 n 根", seen and seen[0]["start"] >= cal.days[-20],
          str(seen[:1]))
    conn2.close()


# ================================================================ 3. 分红

def test_dividend(cal: TradingCalendar) -> None:
    section("3. dividend：同日多方案必须相加")

    tmp = mkdtemp("stockhub-probe-div-")
    ex, ex2 = "2026-09-10", "2026-06-10"
    dbp = mkdb(tmp, cal, dividends=[(ex, 1.2, 0.0, 0.0, "10派10.000元 + 10派2.000元"),
                                    (ex2, 0.5, 0.0, 0.0, "10派5.000元")])
    conn = db.connect(dbp, readonly=True)

    plans = [{"exDiviDate": ex, "cashDiviRMB": 10.0, "dividendPlan": "10派10.000元"},
             {"exDiviDate": ex, "cashDiviRMB": 2.0, "dividendPlan": "10派2.000元"},
             {"exDiviDate": ex2, "cashDiviRMB": 5.0, "dividendPlan": "10派5.000元"}]
    with patch.object(W, "call", lambda *a, **k: {"plans": plans}):
        r = probe.probe_dividend(conn, "sh600000")
    check("聚合后一致 → OK", status(r) == "OK", str(r))

    # 库里只记了前一条（历史 bug 的形态）
    tmp2 = mkdtemp("stockhub-probe-div2-")
    dbp2 = mkdb(tmp2, cal, dividends=[(ex, 1.0, 0.0, 0.0, "10派10.000元")])
    conn2 = db.connect(dbp2, readonly=True)
    with patch.object(W, "call", lambda *a, **k: {"plans": plans}):
        r = probe.probe_dividend(conn2, "sh600000")
    check("漏记一条 → MISMATCH", status(r) == "MISMATCH", str(r))
    check("指出了「同日多方案没合并」这个原因", "多方案" in r[0].detail, r[0].detail)
    check("差异里写明库里 1.0 vs 上游 1.2",
          "1.0000" in r[0].rows[0] and "1.2000" in r[0].rows[0], str(r[0].rows))

    # 上游重复下发同一条 → 不许翻倍
    dup = [{"exDiviDate": ex, "cashDiviRMB": 10.0, "dividendPlan": "10派10.000元"},
           {"exDiviDate": ex, "cashDiviRMB": 10.0, "dividendPlan": "10派10.000元"}]
    tmp3 = mkdtemp("stockhub-probe-div3-")
    dbp3 = mkdb(tmp3, cal, dividends=[(ex, 1.0, 0.0, 0.0, "10派10.000元")])
    conn3 = db.connect(dbp3, readonly=True)
    with patch.object(W, "call", lambda *a, **k: {"plans": dup}):
        r = probe.probe_dividend(conn3, "sh600000")
    check("上游重复下发不翻倍 → OK", status(r) == "OK", str(r))

    # 库里更全（上游只回 N 年）→ OK，但要说明差异来源
    tmp4 = mkdtemp("stockhub-probe-div4-")
    dbp4 = mkdb(tmp4, cal, dividends=[(ex, 1.0, 0.0, 0.0, "a"), ("2014-06-27", 0.4, 0.0, 0.0, "b")])
    conn4 = db.connect(dbp4, readonly=True)
    with patch.object(W, "call", lambda *a, **k: {"plans": [
            {"exDiviDate": ex, "cashDiviRMB": 10.0, "dividendPlan": "a"}]}):
        r = probe.probe_dividend(conn4, "sh600000")
    check("库里更全 → OK 但注明多了哪些除权日",
          status(r) == "OK" and "多" in r[0].title and "2014-06-27" in r[0].rows,
          f"{r[0].title} / {r[0].rows}")

    # 无数据两条路
    tmp5 = mkdtemp("stockhub-probe-div5-")
    dbp5 = mkdb(tmp5, cal)
    conn5 = db.connect(dbp5, readonly=True)
    check("库里没分红 → SKIP", status(probe.probe_dividend(conn5, "sh600000")) == "SKIP")
    with patch.object(W, "call", lambda *a, **k: {"plans": []}):
        r = probe.probe_dividend(conn4, "sh600000")
    check("上游没给分红 → SKIP", status(r) == "SKIP", str(r))


# ================================================================ 4. 估值

def test_valuation(cal: TradingCalendar) -> None:
    section("4. valuation：周频 vs 东财原始行")

    tmp = mkdtemp("stockhub-probe-val-")
    d1, d2 = cal.days[-5], cal.days[-2]
    dbp = mkdb(tmp, cal, valuations=[(d1, 20.0, 3.0), (d2, 21.0, 3.1)])
    conn = db.connect(dbp, readonly=True)

    good = [{"TRADE_DATE": d1, "PE_TTM": 20.0, "PB_MRQ": 3.0},
            {"TRADE_DATE": d2, "PE_TTM": 21.0, "PB_MRQ": 3.1}]
    with patch.object(em, "_datacenter", lambda *a, **k: good):
        r = probe.probe_valuation(conn, "sh600000")
    check("一致 → OK", status(r) == "OK", str(r))

    bad_rows = [{"TRADE_DATE": d1, "PE_TTM": 25.0, "PB_MRQ": 3.0},
                {"TRADE_DATE": d2, "PE_TTM": 21.0, "PB_MRQ": 3.1}]
    with patch.object(em, "_datacenter", lambda *a, **k: bad_rows):
        r = probe.probe_valuation(conn, "sh600000")
    check("PE 不符 → MISMATCH", status(r) == "MISMATCH", str(r))
    check("差异里点名哪个字段、哪天", "PE" in r[0].rows[0] and d1 in r[0].rows[0], str(r[0].rows))

    with patch.object(em, "_datacenter",
                      lambda *a, **k: (_ for _ in ()).throw(SourceUnavailable("封了"))):
        r = probe.probe_valuation(conn, "sh600000")
    check("东财封禁 → SKIP（唯一估值源，封了就无从对账）", status(r) == "SKIP", str(r))

    with patch.object(em, "_datacenter", lambda *a, **k: []):
        r = probe.probe_valuation(conn, "sh600000")
    check("上游返回空 → SKIP", status(r) == "SKIP", str(r))

    tmp2 = mkdtemp("stockhub-probe-val2-")
    dbp2 = mkdb(tmp2, cal)
    check("库里没估值 → SKIP",
          status(probe.probe_valuation(db.connect(dbp2, readonly=True), "sh600000")) == "SKIP")


# ================================================================ 5. 停牌判定

def test_suspension(cal: TradingCalendar) -> None:
    section("5. suspension：洞是停牌还是我们缺数据")

    gap = {"from": cal.days[-10], "to": cal.days[-8], "n": 3, "retry": 1,
           "next_try_at": None, "cls": "retry", "reason": "no_data"}
    tmp = mkdtemp("stockhub-probe-sus-")
    dbp = mkdb(tmp, cal, holes=[gap])
    conn = db.connect(dbp, readonly=True)

    # 上游同区间也没有 bar → 停牌
    others = [{"date": d, "last": 10.0} for d in cal.days[-60:]
              if not (gap["from"] <= d <= gap["to"])]
    with patch.object(W, "call", lambda *a, **k: {"nodes": others}):
        r = probe.probe_suspension(conn, cal, "sh600000")
    check("上游也没有 → 判停牌（OK）", status(r) == "OK", str(r))
    check("结论提示可以 settle 成永久洞", "settle" in r[0].detail, r[0].detail)

    # 上游有 bar → 是我们缺数据
    full = [{"date": d, "last": 10.0} for d in cal.days[-60:]]
    with patch.object(W, "call", lambda *a, **k: {"nodes": full}):
        r = probe.probe_suspension(conn, cal, "sh600000")
    check("上游有 bar → MISMATCH（我们缺数据）", status(r) == "MISMATCH", str(r))
    check("指出重跑该票即可补上", "重跑" in r[0].detail, r[0].detail)

    tmp2 = mkdtemp("stockhub-probe-sus2-")
    dbp2 = mkdb(tmp2, cal)
    r = probe.probe_suspension(db.connect(dbp2, readonly=True), cal, "sh600000")
    check("没有洞 → SKIP（无洞即无需判）", status(r) == "SKIP", str(r))

    # 老洞不算（只判近 60 天）
    old = dict(gap, **{"from": "2020-01-02", "to": "2020-01-06"})
    tmp3 = mkdtemp("stockhub-probe-sus3-")
    dbp3 = mkdb(tmp3, cal, holes=[old])
    r = probe.probe_suspension(db.connect(dbp3, readonly=True), cal, "sh600000")
    check("很老的洞不重打上游 → SKIP", status(r) == "SKIP", str(r))

    # hole_detail 是坏 JSON
    tmp4 = mkdtemp("stockhub-probe-sus4-")
    dbp4 = mkdb(tmp4, cal, holes=[gap])
    c = db.connect(dbp4)
    with db.tx(c):
        c.execute("UPDATE coverage SET hole_detail='{oops' WHERE symbol='sh600000'")
    c.close()
    r = probe.probe_suspension(db.connect(dbp4, readonly=True), cal, "sh600000")
    check("hole_detail 坏 JSON → SKIP 并指向 doctor", "doctor" in r[0].detail, r[0].detail)


# ================================================================ 6. run / 入口

def test_run(cal: TradingCalendar, calpath: Path) -> None:
    section("6. run：挑票、降级、退出码")

    tmp = mkdtemp("stockhub-probe-run-")
    # 两只票：A 有 K 线和估值，B 只有估值——按探针各自挑票时两只都该被点到
    dbp = mkdb(tmp, cal, symbol="sh600000", valuations=[(cal.days[-3], 20.0, 3.0)])
    conn = db.connect(dbp)
    with db.tx(conn):
        conn.execute(
            "INSERT INTO valuation(symbol,trade_date,pe_ttm,pb_mrq,ps_ttm,total_mv,circ_mv,src)"
            " VALUES('sz000001',?,15.0,1.2,NULL,NULL,NULL,'eastmoney')", (cal.days[-3],))
    conn.close()

    cfg = mkcfg(tmp, calpath)
    picked: list[str] = []

    def spy_qfq(conn, cal, symbol, **kw):
        picked.append(symbol)
        return [probe.ok("qfq", f"{symbol} fake")]

    def spy_val(conn, symbol, **kw):
        picked.append(symbol)
        return [probe.ok("valuation", f"{symbol} fake")]

    with patch.object(probe, "probe_qfq", spy_qfq), \
         patch.object(probe, "probe_valuation", spy_val), \
         patch.object(probe, "probe_calendar", lambda *a, **k: [probe.ok("calendar", "x")]):
        probe.run({"qfq": True, "valuation": True, "symbols": []}, cfg=cfg)
    check("按探针各自挑票：两只都被点到", "sh600000" in picked and "sz000001" in picked,
          str(picked))

    picked.clear()
    with patch.object(probe, "probe_qfq", spy_qfq), \
         patch.object(probe, "probe_valuation", spy_val):
        probe.run({"qfq": True, "valuation": True, "symbols": ["sz000001"]}, cfg=cfg)
    check("显式给 --symbols 时照单全跑（不问有没有数据）",
          sorted(picked) == ["sz000001", "sz000001"], str(picked))

    # 某条探针一只票都挑不出来 → 明确说原因，不要"0 通过 0 不一致 0 跳过"这种空账
    tmp3 = mkdtemp("stockhub-probe-run3-")
    mkdb(tmp3, cal)                        # 只有 K 线，没有分红
    r = probe.run({"dividend": True}, cfg=mkcfg(tmp3, calpath))
    check("没有该域数据 → SKIP 并说明原因",
          len(r) == 1 and r[0].status == "SKIP" and "没有任何可查的票" in r[0].detail, str(r))

    # 探针自己抛异常 → 降级成 MISMATCH，不影响别的探针
    def boom(*a, **k):
        raise RuntimeError("假崩")

    with patch.object(probe, "probe_dividend", boom), \
         patch.object(probe, "probe_valuation", spy_val):
        r = probe.run({"dividend": True, "valuation": True, "symbols": ["sh600000"]}, cfg=cfg)
    check("探针崩了 → MISMATCH 而不是静默", any(x.status == "MISMATCH" for x in r), str(r))
    check("崩一个不影响另一个", any(x.status == "OK" for x in r), str(r))
    check("崩了的文案带异常类型",
          any("RuntimeError" in x.title for x in r), str([x.title for x in r]))

    # 库打不开 / 日历加载不了
    bad = mkcfg(tmp, calpath)
    bad.db_path = tmp / "nope.db"
    r = probe.run({"qfq": True}, cfg=bad)
    check("库不存在 → SKIP 而不是炸", all(x.status != "MISMATCH" for x in r), str(r))
    check("不存在也不许顺手建库", not (tmp / "nope.db").exists())

    bad2 = mkcfg(tmp, tmp / "no-cal.json")
    r = probe.run({"calendar": True}, cfg=bad2)
    check("日历加载不了 → SKIP", all(x.status != "MISMATCH" for x in r), str(r))

    # 渲染与退出码
    rs = [probe.ok("qfq", "a"), probe.bad("dividend", "b"), probe.skip("valuation", "c")]
    text = probe.render(rs)
    check("render 三种状态都有图标", "✅" in text and "❌" in text and "⏭" in text, text[:60])
    check("render 结尾给结论与计数", "1 通过 / 1 不一致 / 1 跳过" in text, text[-80:])

    with patch.object(probe, "run", lambda *a, **k: [probe.ok("qfq", "a")]), \
         contextlib.redirect_stdout(io.StringIO()):
        check("全 OK → 退出码 0", probe.main(["--qfq", "--db", str(dbp)]) == 0)
    with patch.object(probe, "run", lambda *a, **k: [probe.bad("qfq", "a")]), \
         contextlib.redirect_stdout(io.StringIO()):
        check("有 MISMATCH → 退出码 1", probe.main(["--qfq", "--db", str(dbp)]) == 1)
    with patch.object(probe, "run", lambda *a, **k: [probe.skip("qfq", "a")]), \
         contextlib.redirect_stdout(io.StringIO()):
        check("只有 SKIP → 退出码 0（跳过不算失败）",
              probe.main(["--qfq", "--db", str(dbp)]) == 0)

    buf = io.StringIO()
    with patch.object(probe, "run", lambda *a, **k: rs), contextlib.redirect_stdout(buf):
        probe.main(["--json", "--db", str(dbp)])
    payload = json.loads(buf.getvalue())
    check("--json 输出可解析且字段完整",
          isinstance(payload, list) and {"probe", "status", "title", "rows"} <= set(payload[0]))


def main() -> int:
    t0 = mkdtemp("stockhub-probe-")
    cal = write_cal(t0)
    calpath = t0 / "trading_calendar.json"
    test_calendar(cal)
    test_qfq(cal, t0)
    test_qfq_window(cal)
    test_dividend(cal)
    test_valuation(cal)
    test_suspension(cal)
    test_run(cal, calpath)

    print("\n" + "=" * 60)
    if FAIL:
        print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
        for name in FAIL:
            print(f"  - {name}")
        return 1
    print(f"通过 {PASS} 项，失败 0 项")
    print(f"临时目录（可删）：{t0.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
