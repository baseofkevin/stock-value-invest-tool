"""第 3 步测试：throttle / sources / router / api.ensure(kline)。**默认离线**。

跑法：

    cd /home/kevin/MyData/stock_value_invest_tool/stock_data_fetch && \
    /home/kevin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
        -m stock_data_fetch.tests.test_fetch

    STOCKHUB_LIVE=1 …  # 额外跑"用例 1"：真打一次东财，验证 300 根落库

覆盖 `CONTRACT` §15 的用例 2（二次调用上游计数 0）、用例 7（两进程只打一次上游）、
用例 k（限频只重取失败组、先冷却、失败者 partial=1 而成功者 end 推进），
以及 §7 节流器的退避序列。

**没有一个断言依赖真实时间**：节流器注入假时钟，`sleep` 只是把假时钟往前拨。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import multiprocessing as mp
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent        # stock_value_invest_tool/（包的父目录）
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import api, coverage as C, sources                  # noqa: E402
from stock_data_fetch import router as R
from stock_data_fetch.calendar import TradingCalendar                     # noqa: E402
from stock_data_fetch.config import DEFAULT_CALENDAR, DEFAULT_SCHEMA, Config  # noqa: E402
from stock_data_fetch.errors import (EmptyResult, PartialData,            # noqa: E402
                              SourceUnavailable, ThrottleTimeout)
from stock_data_fetch.hub import preflight                                # noqa: E402
from stock_data_fetch.need import Need, want                              # noqa: E402
from stock_data_fetch.sources import eastmoney as em, tdx                 # noqa: E402
from stock_data_fetch.sources.base import BatchError, Got, Quota, SourceSpec  # noqa: E402
from stock_data_fetch.throttle import Bucket, Throttle                    # noqa: E402

def mkdtemp(prefix: str) -> Path:
    """测试临时目录。

    **不能用 `/tmp`**：本机 `/tmp` 是 10MB 的 tmpfs，建几个 SQLite（含 WAL）就会撞墙，
    表现为进程被 SIGKILL、连报错都没有（`EXIT=137`）。放到磁盘上，
    可用 `STOCKHUB_TMP` 改位置。
    """
    root = Path(os.environ.get("STOCKHUB_TMP")
                or HERE.parent.parent.parent / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))

PASS = 0
FAIL: list[str] = []
TRACE: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def expect_raise(name: str, exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        check(name, True)
        print(f"      → {type(e).__name__}: {e}")
        return e
    except Exception as e:                                    # noqa: BLE001
        check(name, False, f"抛的是 {type(e).__name__}: {e}")
        return None
    check(name, False, "没有抛异常")
    return None


def section(title: str) -> None:
    print(f"\n{title}")


# ================================================================ 假时钟

class FakeClock:
    """假时钟 + 假 sleep：睡多久就把时间往前拨多久，测试瞬间跑完。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.waits.append(round(s, 3))
        self.t += s

    @property
    def slept(self) -> float:
        return round(sum(self.waits), 3)


def tcfg(tmp: Path, **kw) -> Config:
    c = Config()
    c.db_path = tmp / "shared.db"
    c.state_dir = tmp / "state"
    c.schema_path = DEFAULT_SCHEMA
    c.calendar_path = DEFAULT_CALENDAR
    c.rate_rows_per_min = {"t": 60.0}          # 1 行/秒，方便算
    c.min_interval_ms = {"t": 0.0, "fast": 300.0}
    c.burst_rows = 5.0
    c.cooldown_base = 30.0
    c.cooldown_max = 300.0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ================================================================ 1. 节流器

def test_throttle(tmp: Path) -> None:
    section("throttle：令牌桶（按行数）")
    c = tcfg(tmp)
    clk = FakeClock()
    th = Throttle(c, clock=clk, sleep=clk.sleep)

    check("首跑桶是满的（不需要先等一个桶）", th.snapshot("t")["tokens"] == 5.0)
    check("拿满一桶不等待", th.take("t", 5) == 0.0)
    check("拿完之后桶空", th.snapshot("t")["tokens"] == 0.0, str(th.snapshot("t")))

    waited = th.take("t", 3)
    check("要 3 行只剩 0 → 等 3 秒", abs(waited - 3.0) < 0.01, f"waited={waited}")
    check("真的睡了 3 秒（不是忙等）", clk.slept == 3.0, str(clk.waits))

    # 桶容量上限：放 100 秒也只补到 burst，不会攒出天量
    clk.t += 100
    check("补充不超过 burst", th.snapshot("t")["tokens"] == 5.0, str(th.snapshot("t")))

    section("throttle：最小间隔")
    c2 = tcfg(tmp / "mi", min_interval_ms={"fast": 300.0}, rate_rows_per_min={"fast": 0.0})
    clk2 = FakeClock()
    th2 = Throttle(c2, clock=clk2, sleep=clk2.sleep)
    th2.take("fast", 1)
    w = th2.take("fast", 1)
    check("第二个请求等 0.3s", abs(w - 0.3) < 0.001, f"waited={w}")

    section("throttle：冷却闸（退避序列）")
    c3 = tcfg(tmp / "cd")
    clk3 = FakeClock()
    th3 = Throttle(c3, clock=clk3, sleep=clk3.sleep)
    seq = [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    got = [th3.penalize("t") for _ in seq]
    check("冷却 30→60→120→240→300 封顶", got == seq, str(got))

    clk3.t += 1
    waited = th3.take("t", 1)
    check("冷却期内 take 会等到冷却结束", waited > 0 and waited <= 300.0, f"waited={waited}")

    section("throttle：干净返回的衰减")
    c4 = tcfg(tmp / "rw")
    clk4 = FakeClock()
    th4 = Throttle(c4, clock=clk4, sleep=clk4.sleep)
    th4.penalize("t")
    check("一次干净返回 → 减半", th4.reward("t") == 15.0)
    check("再来一次 → 7.5", th4.reward("t") == 7.5)
    check("连续 3 次干净 → 清零", th4.reward("t") == 0.0)
    check("清零后再限频，从 30 重新开始", th4.penalize("t") == 30.0)

    section("throttle：预算与状态持久化")
    c5 = tcfg(tmp / "bd", throttle_budget=2.0)
    clk5 = FakeClock()
    th5 = Throttle(c5, clock=clk5, sleep=clk5.sleep)
    th5.take("t", 5)                                  # 先把桶清空
    expect_raise("等超预算抛 ThrottleTimeout", ThrottleTimeout, th5.take, "t", 5)

    c5b = tcfg(tmp / "bd2", throttle_budget=5.0)
    clk5b = FakeClock()
    th5b = Throttle(c5b, clock=clk5b, sleep=clk5b.sleep)
    th5b.take("t", 5)
    try:
        w = th5b.take("t", 2, budget=5.0)              # 要 2s，够
        check("预算内不会抛（等了 2 秒）", abs(w - 2.0) < 0.01, f"waited={w}")
    except ThrottleTimeout as e:
        check("预算内不会抛", False, str(e))

    c6 = tcfg(tmp / "share")
    th_a = Throttle(c6, clock=FakeClock(), sleep=lambda s: None)
    th_a.take("t", 5)
    th_b = Throttle(c6, clock=FakeClock(), sleep=lambda s: None)
    check("两个实例看到同一份桶（走文件，不是进程内存）",
          th_b.snapshot("t")["tokens"] == 0.0, str(th_b.snapshot("t")))

    section("throttle：单次申请 > 桶容量（曾经会死等）")
    c7 = tcfg(tmp / "big", rate_rows_per_min={"t": 60.0}, burst_rows=5.0)
    clk7 = FakeClock()
    th7 = Throttle(c7, clock=clk7, sleep=clk7.sleep)
    try:
        w = th7.take("t", 300, budget=10.0)          # 300 行 ≫ 桶容量 5
        check("按桶容量计费，立即放行而不是等满 300 行", w == 0.0, f"waited={w}")
    except ThrottleTimeout as e:
        check("按桶容量计费，立即放行而不是等满 300 行", False, str(e))
    check("没有把时间睡掉", clk7.slept == 0.0, str(clk7.waits))

    st = Bucket.load(tmp / "share" / "state" / "throttle" / "t.json")
    check("桶状态确实落了 JSON", st.tokens == 0.0, str(st))


# ================================================================ 2. base

def test_got() -> None:
    section("Got：合并与失败清单")
    a = Got(rows={"s1": [{"date": "2026-11-12", "close": 1.0},
                         {"date": "2026-11-13", "close": 2.0}]},
            errors=[BatchError(["s2"]), BatchError(["s3"])])
    b = Got(rows={"s1": [{"date": "2026-11-13", "close": 9.0}], "s2": [{"date": "2026-11-13"}]})

    a.extend(b)
    check("同日期覆盖而不是追加", [r["close"] for r in a.rows["s1"]] == [1.0, 9.0],
          str(a.rows["s1"]))
    check("新 symbol 直接并入", a.rows["s2"][0]["date"] == "2026-11-13")
    check("总数按行算", a.total == 3, str(a.total))
    check("有 errors 就算 partial", a.partial is True)

    a.drop_error_symbols(["s3"])
    check("重试成功的 symbol 从失败清单摘掉",
          a.failed_symbols == ["s3"], str(a.failed_symbols))
    check("无 errors 时 partial 为假", Got(rows={"s": [{}]}).partial is False)


# ================================================================ 3. 适配器

def test_adapters() -> None:
    section("eastmoney：secid 映射与解析")
    check("沪 → 1.", em.secid("sh600519") == "1.600519", em.secid("sh600519"))
    check("深 → 0.", em.secid("sz000858") == "0.000858", em.secid("sz000858"))
    check("北 → 0.", em.secid("bj920002") == "0.920002", em.secid("bj920002"))
    check("裸代码按首位猜市场", em.secid("600519") == "1.600519", em.secid("600519"))

    real = {"data": {"name": "贵州茅台", "klines": [
        "2026-11-12,1395.13,1410.27,1411.00,1388.11,32373,4536814592.00",
        "2026-11-13,1404.00,1435.26,1437.69,1394.00,41780,5954834070.00"]}}
    rows, why = _with_body(real, "sh600519")
    check("解析出 2 行", len(rows) == 2 and why is None, f"{len(rows)} {why}")
    check("日期是 YYYY-MM-DD", rows[-1]["date"] == "2026-11-13", rows[-1]["date"])
    check("收盘取对（fqt=0 的 raw）", abs(rows[-1]["close"] - 1435.26) < 1e-9, str(rows[-1]))
    check("成交量 手 → 股（×100）", abs(rows[-1]["volume"] - 4178000.0) < 1e-6,
          str(rows[-1]["volume"]))
    check("成交额已是元，不再换算", abs(rows[-1]["amount"] - 5954834070.0) < 1e-3)

    rows, why = _with_body({"data": {"name": "诺思兰德(已切换)", "klines": []}}, "bj430047")
    check("代码已迁移 → code_moved（不是「停牌」）", why == "code_moved", str(why))
    rows, why = _with_body({"data": {"name": "停牌股", "klines": []}}, "sz000001")
    check("真没数据 → no_data", why == "no_data", str(why))
    rows, why = _with_body({"data": None}, "sz000002")
    check("data 为 null → no_data", why == "no_data", str(why))

    section("tdx：代码映射与行解析")
    check("sh600519 → (600519, 1)", tdx.split_symbol("sh600519") == ("600519", 1))
    check("sz000858 → (000858, 0)", tdx.split_symbol("sz000858") == ("000858", 0))
    check("bj920002 → (920002, 2)", tdx.split_symbol("bj920002") == ("920002", 2))

    sample = {"Rows": [{"Data": "20261113", "Open": "1404.000000", "High": "1437.689941",
                        "Low": "1394.000000", "Close": "1435.260010",
                        "Amount": 5954833920, "Volume": 41779.84,
                        "RawVolume": 4177984, "Unit": 100}]}
    r = tdx._rows(sample)[0]
    check("tdx 日期转成 YYYY-MM-DD", r["date"] == "2026-11-13", r["date"])
    check("tdx 成交量用 RawVolume（股），不是 Volume（手）",
          r["volume"] == 4177984.0, str(r["volume"]))
    check("tdx 收盘取对（上游给 6 位小数，容忍 1e-4 浮点噪声）",
          abs(r["close"] - 1435.26) < 1e-4, str(r["close"]))

    no_raw = {"Rows": [dict(sample["Rows"][0], RawVolume=None)]}
    check("没有 RawVolume 时用 Volume×Unit 兜底",
          abs(tdx._rows(no_raw)[0]["volume"] - 4177984.0) < 0.01,
          str(tdx._rows(no_raw)[0]["volume"]))

    ok, why = tdx.available()
    check("tdx 无令牌 → 明确不可用并给原因", ok is False and "令牌" in why, why)


def _with_body(payload: dict, symbol: str) -> tuple[list[dict], str | None]:
    """把 `em._get` 换成固定响应，不联网。"""
    old = em._get
    em._get = lambda url: json.dumps(payload)          # noqa: ARG005
    try:
        return em._fetch_one(symbol, Need("kline", "fq=raw", "2026-11-13", "2026-10-01", 30, 5))
    finally:
        em._get = old


# ================================================================ 4. 路由

@contextmanager
def fake_world(mods: dict[str, object], specs: list[SourceSpec]):
    """临时把注册表换成假源，退出时恢复。"""
    old_sources = sources.SOURCES
    old_mods = dict(sources.MODULES)
    sources.SOURCES = list(specs)
    sources.MODULES = {**old_mods, **mods}
    try:
        yield
    finally:
        sources.SOURCES = old_sources
        sources.MODULES = old_mods


class FakeSrc:
    """假适配器。`rows_for(sym)` 决定返回什么；调用清单留在 `calls` 里供断言。"""

    def __init__(self, rows_for, *, err=None, sleep=0.0, log_file=None, empty=None):
        self.rows_for = rows_for
        self.err = err or (lambda codes: [])
        self.sleep = sleep
        self.log_file = log_file
        self.empty = empty
        self.calls: list[list[str]] = []

    def fetch(self, domain, codes, need):
        self.calls.append(list(codes))
        if self.log_file:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"{os.getpid()} {','.join(codes)}\n")
        if self.sleep:
            time.sleep(self.sleep)
        got = Got()
        for s in codes:
            rows = self.rows_for(s)
            if rows:
                got.rows[s] = rows
        errs = self.err(list(codes))
        if errs:
            got.errors = errs
        if not got.rows and self.empty:
            got.empty_reason = self.empty
        return got


def spec(name: str, prio: int = 0, up: str | None = None) -> SourceSpec:
    return SourceSpec(name, up or name, "lib", ("kline",), priority=prio, batch=99,
                      quota=Quota(rows_per_min=0.0))


def test_router(tmp: Path, cal: TradingCalendar) -> None:
    section("router：跳过冷却中的源")
    c = tcfg(tmp / "rt", rate_rows_per_min={"a": 0.0, "b": 0.0})
    clk = FakeClock()
    th = Throttle(c, clock=clk, sleep=clk.sleep)
    rows = [{"date": d, "open": 1, "high": 1, "low": 1, "close": 1,
             "volume": 1, "amount": 1} for d in cal.days[-5:]]
    a, b = FakeSrc(lambda s: rows), FakeSrc(lambda s: rows)
    with fake_world({"a": a, "b": b}, [spec("a", 0), spec("b", 1)]):
        th.penalize("a")
        rtr = R.Router(cfg=c, throttle=th)
        order = [s.name for s in rtr.order("kline")]
        check("冷却中的主源被跳过", order == ["b"], str(order))
        got, used = rtr.fetch("kline", ["sh600519"],
                              Need("kline", "fq=raw", cal.days[-1], cal.days[-5], 5, 1))
        check("实际用了备源", used.name == "b", used.name)
        check("主源一次都没被调用", a.calls == [], str(a.calls))

    section("router：连续失败才降级")
    c = tcfg(tmp / "rt2", rate_rows_per_min={"a": 0.0, "b": 0.0}, degrade_after=3)
    clk = FakeClock()
    th = Throttle(c, clock=clk, sleep=clk.sleep)
    boom = FakeSrc(lambda s: (_ for _ in ()).throw(SourceUnavailable("401 无令牌")))
    ok = FakeSrc(lambda s: rows)
    with fake_world({"a": boom, "b": ok}, [spec("a", 0), spec("b", 1)]):
        rtr = R.Router(cfg=c, throttle=th)
        need = Need("kline", "fq=raw", cal.days[-1], cal.days[-5], 5, 1)
        for i in range(2):
            got, used = rtr.fetch("kline", ["sh600519"], need)
            check(f"第 {i + 1} 次仍先试主源（失败 {i + 1}/3）", used.name == "b")
        got, used = rtr.fetch("kline", ["sh600519"], need)
        n_boom = len(boom.calls)
        got, used = rtr.fetch("kline", ["sh600519"], need)
        check("3 次之后主源被判死，不再尝试", len(boom.calls) == n_boom, str(boom.calls))

    section("router：限频不降级（规则 4）")
    c = tcfg(tmp / "rt3", rate_rows_per_min={"a": 0.0, "b": 0.0})
    clk = FakeClock()
    th = Throttle(c, clock=clk, sleep=clk.sleep)
    bad = FakeSrc(lambda s: [], err=lambda codes: [BatchError(list(codes))])
    good = FakeSrc(lambda s: rows)
    with fake_world({"a": bad, "b": good}, [spec("a", 0), spec("b", 1)]):
        rtr = R.Router(cfg=c, throttle=th)
        got, used = rtr.fetch("kline", ["sh600519"],
                              Need("kline", "fq=raw", cal.days[-1], cal.days[-5], 5, 1),
                              rounds=2)
        check("有组级失败时**不**换源", used.name == "a", used.name)
        check("备源一次没动", good.calls == [], str(good.calls))
        check("主源被重试了（rounds=2 → 共 2 次）", len(bad.calls) == 2, str(bad.calls))
        check("重试前触发过冷却", th.cooling("a") > 0 or clk.waits != [], str(clk.waits))


# ================================================================ 5. api.ensure

def kline_rows(cal: TradingCalendar, n: int = 300) -> list[dict]:
    return [{"date": d, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
             "volume": 1000.0, "amount": 10500.0} for d in cal.days[-n:]]


def make_hub(tmp: Path, sub: str):
    # cooldown 调到毫秒级：这里测的是"有没有冷却"，不是冷却多久（退避序列在 throttle 那节单独测）
    c = tcfg(tmp / sub, rate_rows_per_min={"fake": 0.0},
             cooldown_base=0.01, cooldown_max=0.02)
    hub = preflight(cfg=c, now=dt.datetime(2026, 11, 14, 10, 0))
    return hub


def test_ensure(tmp: Path, cal: TradingCalendar) -> None:
    rows = kline_rows(cal)
    need = want.kline(cal, cal.days[-1], bars=300, min_bars=250)

    section("api.ensure：空库取 300 根（用例 1 的离线版）")
    hub = make_hub(tmp, "e1")
    fake = FakeSrc(lambda s: rows)
    try:
        with fake_world({"fake": fake}, [spec("fake", 0)]):
            r = hub.ensure("kline", "sh600519", need)
            check("拿到 300 根", r.n == 300, str(r.n))
            check("fetched=True（这次真打了上游）", r.fetched is True)
            check("起止与 need 对齐", r.end == need.end, f"{r.end} vs {need.end}")
            cov = C.read(hub.conn, "kline", "sh600519", need.fp)
            check("覆盖记录 n=300、无洞", cov.n == 300 and cov.holes == 0, str(cov.detail))
            check("覆盖记的是上游而不是适配器名", cov.src == "fake", str(cov.src))
            check("watermark 已推进",
                  hub.conn.execute("SELECT last_ok_date FROM watermark WHERE domain='kline'")
                  .fetchone()[0] == cal.days[-1])

            section("api.ensure：二次调用不打上游（用例 2）")
            n_before = len(fake.calls)
            r2 = hub.ensure("kline", "sh600519", need)
            check("上游调用计数 0", len(fake.calls) == n_before, str(fake.calls))
            check("仍然返回 300 根", r2.n == 300)
            check("fetched=False", r2.fetched is False)

            section("api.ensure：列式形态与 read 只读")
            s = r2.series()
            check("series 是列式且长度一致",
                  s["date"][0] == rows[0]["date"] and len(s["close"]) == 300, str(len(s)))
            r3 = api.read(hub, "kline", "sh600519", need)
            check("read 不打上游", len(fake.calls) == n_before)
            check("read 返回行式", r3.rows[0]["date"] == rows[0]["date"])
    finally:
        hub.close()

    section("api.ensure：末段延伸只补新的一天")
    hub = make_hub(tmp, "e2")
    part = FakeSrc(lambda s: rows[:-1])           # 先给 299 根
    try:
        with fake_world({"fake": part}, [spec("fake", 0)]):
            hub.ensure("kline", "sh600519",
                       want.kline(cal, cal.days[-2], bars=299, min_bars=200))
            before = len(part.calls)
            part.rows_for = lambda s: rows                  # 补最后一天
            hub.ensure("kline", "sh600519", need)
            check("只补末端一段（多打一次上游）", len(part.calls) == before + 1)
            check("补完 300 根",
                  C.read(hub.conn, "kline", "sh600519", need.fp).n == 300)
    finally:
        hub.close()

    section("api.ensure：换源不覆盖已有 bar（SOURCES §4.1）")
    hub = make_hub(tmp, "e3")
    try:
        with fake_world({"fake": FakeSrc(lambda s: rows)}, [spec("fake", 0, "upA")]):
            hub.ensure("kline", "sh600519", need)
        other = [{**r, "close": 99.0} for r in rows]
        with fake_world({"fake": FakeSrc(lambda s: other)},
                        [spec("fake", 0, "upB")]):
            hub.conn.execute("DELETE FROM coverage")       # 逼它重取
            hub.ensure("kline", "sh600519", need, force=True)
        closes = [r[0] for r in hub.conn.execute(
            "SELECT close FROM kline WHERE symbol='sh600519' ORDER BY date").fetchall()]
        check("另一个上游的 bar 没有覆盖已有 300 根",
              set(closes) == {10.5}, str(set(closes)))
    finally:
        hub.close()

    section("api.ensure：空表要报错，不静默写 0 行")
    hub = make_hub(tmp, "e4")
    try:
        with fake_world({"fake": FakeSrc(lambda s: [], empty="no_data")}, [spec("fake", 0)]):
            expect_raise("空表抛 EmptyResult", EmptyResult,
                         hub.ensure, "kline", "sh600519", need)
        with fake_world({"fake": FakeSrc(lambda s: [], empty="code_moved")}, [spec("fake", 0)]):
            e = expect_raise("代码迁移单独报错", EmptyResult,
                             hub.ensure, "kline", "sh600519", need)
            check("文案点明「已切换」", e is not None and "迁移" in str(e), str(e))
    finally:
        hub.close()


# ================================================================ 6. 用例 k / 7

def test_partial(tmp: Path, cal: TradingCalendar) -> None:
    section("api.ensure：组级重试只重取失败的那几只（用例 k）")
    hub = make_hub(tmp, "k1")
    rows = kline_rows(cal)
    need = want.kline(cal, cal.days[-1], bars=300, min_bars=250)
    bad = "sh600519"
    fak = FakeSrc(lambda s: [], err=lambda codes: [BatchError(list(codes))])
    try:
        with fake_world({"fake": fak}, [spec("fake", 0)]):
            expect_raise("3 轮仍失败 → PartialData", PartialData,
                         hub.ensure, "kline", bad, need)
        check("请求清单始终只有那一只（不重取整批）",
              all(c == [bad] for c in fak.calls), str(fak.calls))
        check("一共 3 轮", len(fak.calls) == 3, str(len(fak.calls)))
        cov = C.read(hub.conn, "kline", bad, need.fp)
        check("失败者标记 partial=1", cov is not None and cov.partial == 1, str(cov))
        check("partial 时**不推进** end_date", cov.end is None, str(cov.end))

        rtr2 = R.Router(cfg=hub.cfg, throttle=hub.throttle)
        check("限频没有导致降级（源没被判死）",
              ("kline", "fake") not in rtr2.dead and rtr2.strikes.get(("kline", "fake"), 0) == 0,
              f"dead={rtr2.dead} strikes={rtr2.strikes}")
    finally:
        hub.close()

    section("api.ensure：一半成功一半失败时的粒度")
    hub = make_hub(tmp, "k2")
    only_bad = "sz000858"
    def mixed_err(codes):
        hit = [c for c in codes if c == only_bad]
        return [BatchError(hit)] if hit else []

    mixed = FakeSrc(lambda s: rows if s != only_bad else [], err=mixed_err)
    try:
        with fake_world({"fake": mixed}, [spec("fake", 0)]):
            batch = hub.ensure_many("kline", ["sh600519", only_bad], need)
        check("成功的进 ok", list(batch.ok) == ["sh600519"], str(list(batch.ok)))
        check("失败的进 failed", list(batch.failed) == [only_bad], str(list(batch.failed)))
        check("Batch 不替调用方抛（要显式 raise_if_any）", isinstance(
            batch.failed[only_bad], PartialData), type(batch.failed[only_bad]).__name__)
        expect_raise("raise_if_any 才抛", PartialData, batch.raise_if_any)
        cov_ok = C.read(hub.conn, "kline", "sh600519", need.fp)
        cov_bad = C.read(hub.conn, "kline", only_bad, need.fp)
        check("成功者 end_date 已推进", cov_ok.end == cal.days[-1], str(cov_ok.end))
        check("成功者没有 partial", cov_ok.partial == 0)
        check("失败者 partial=1", cov_bad.partial == 1, str(cov_bad.partial))
    finally:
        hub.close()


def test_stop_and_transient(tmp: Path, cal: TradingCalendar) -> None:
    """停牌缺口要认账；连接中断要冷却重试而不是换源。"""
    all_rows = kline_rows(cal)
    need = want.kline(cal, cal.days[-1], bars=300, min_bars=250)

    section("api.ensure：停过一天牌的股票仍然可用（settle 成永久洞）")
    hub = make_hub(tmp, "s1")
    gap = cal.days[-120]
    holed = [r for r in all_rows if r["date"] != gap]
    src = FakeSrc(lambda s: holed)
    try:
        with fake_world({"fake": src}, [spec("fake", 0)]):
            r = hub.ensure("kline", "sh600525", need)
        cov = C.read(hub.conn, "kline", "sh600525", need.fp)
        check("不再抛错，正常返回", r.n == 299, str(r.n))
        check("缺口留了痕迹（不算「没有」）", cov.holes == 1, str(cov.detail))
        check("缺口被判 permanent", cov.detail[0].permanent is True, str(cov.detail[0]))
        check("permanent 洞不阻塞命中", cov.blocking == 0)
        check("Range 里能看到永久洞", [h.start for h in r.permanent_holes] == [gap],
              str(r.permanent_holes))
        check("整段 1 次 + 缺口段 2 次，没有走 6 次退避", len(src.calls) == 3, str(len(src.calls)))

        n_before = len(src.calls)
        hub.ensure("kline", "sh600525", need)
        check("第二次运行不再为它打上游", len(src.calls) == n_before, str(src.calls))
    finally:
        hub.close()

    section("router：连接中断按限频处理，冷却后重试同一源")
    c = tcfg(tmp / "tr", rate_rows_per_min={"x": 0.0, "y": 0.0},
             cooldown_base=0.01, cooldown_max=0.02)
    clk = FakeClock()
    th = Throttle(c, clock=clk, sleep=clk.sleep)
    calls = {"n": 0}

    class Flaky:
        def fetch(self, domain, codes, need_):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise SourceUnavailable("东财连接中断", transient=True)
            return Got(rows={s: all_rows for s in codes})

    other = FakeSrc(lambda s: all_rows)
    with fake_world({"x": Flaky(), "y": other}, [spec("x", 0), spec("y", 1)]):
        rtr = R.Router(cfg=c, throttle=th)
        got, used = rtr.fetch("kline", ["sh600519"],
                              Need("kline", "fq=raw", cal.days[-1], cal.days[-5], 5, 1))
        check("断连 2 次后第 3 次成功", got.total == 300 and calls["n"] == 3, str(calls))
        check("没有换源（限频不是「源坏了」）", used.name == "x", used.name)
        check("备源一次没动", other.calls == [], str(other.calls))
        bstate = json.loads((tmp / "tr" / "state" / "throttle" / "x.json").read_text())
        check("每次断连都触发了冷却（桶里留下冷却长度）",
              bstate["cooldown_len"] > 0, str(bstate))

    section("router：第一次瞬断立刻重发，不冷却（2026-11-14 实测改的）")
    # 冒烟实测：10 只 K 线跑了 336.5 秒，其中 240 秒在等冷却——上游掐一次连接
    # （成本 0.4s）就罚 30s，连掐翻倍。掐连接是偶发的（同批 8 只连续成功），
    # 不该触发指数退避。现在第一次瞬断直接重发，第二次起才冷却。
    c3 = tcfg(tmp / "tr3", rate_rows_per_min={"x": 0.0},
              cooldown_base=30.0, cooldown_max=300.0)
    clk3 = FakeClock()
    th3 = Throttle(c3, clock=clk3, sleep=clk3.sleep)
    seq = {"n": 0}

    class DropOnce:
        def fetch(self, domain, codes, need_):
            seq["n"] += 1
            if seq["n"] == 1:
                raise SourceUnavailable("东财连接中断", transient=True)
            return Got(rows={s: all_rows for s in codes})

    with fake_world({"x": DropOnce()}, [spec("x", 0)]):
        rtr = R.Router(cfg=c3, throttle=th3)
        got, used = rtr.fetch("kline", ["sh600519"],
                              Need("kline", "fq=raw", cal.days[-1], cal.days[-5], 5, 1))
        check("瞬断后立刻重发，第 2 次就成功", got.total == 300 and seq["n"] == 2, str(seq))
        check("这一次完全没有进入冷却", th3.cooling("x") == 0, str(th3.cooling("x")))
        check("一次冷却都没睡（基准 30s，睡过就不算立刻）",
              clk3.slept < 1.0, str(clk3.waits))

    section("router：断连三次且有备源 → 降级（2026-11-14 深夜改的）")
    # 旧规则是"断连三次就抛 ThrottleTimeout，绝不换源"。它被真实场景打穿了：
    # 东财 push2his 突发配额用完后整段拒连（持续 20 分钟以上），而腾讯那条同值的
    # K 线通道就闲在旁边。按旧规则，4300 只的候选池会变成"每 30 秒冷却到期就有一批
    # 股票失败"——工具拿到的是一份少了若干只却不报错的报告。
    c2 = tcfg(tmp / "tr2", rate_rows_per_min={"x": 0.0, "y": 0.0},
              cooldown_base=0.01, cooldown_max=0.02)
    clk2 = FakeClock()
    th2 = Throttle(c2, clock=clk2, sleep=clk2.sleep)

    class Dead:
        def __init__(self):
            self.n = 0

        def fetch(self, domain, codes, need_):
            self.n += 1
            raise SourceUnavailable("东财连接中断", transient=True)

    dead, backup = Dead(), FakeSrc(lambda s: all_rows)
    with fake_world({"x": dead, "y": backup}, [spec("x", 0), spec("y", 1)]):
        rtr = R.Router(cfg=c2, throttle=th2)
        got, used = rtr.fetch("kline", ["sh600519"],
                              Need("kline", "fq=raw", cal.days[-1], cal.days[-5], 5, 1))
        check("没有抛错，改由备源给出数据", got.total == 300, str(got.total))
        check("确实用了备源", used.name == "y", used.name)
        check("主源只试了 3 次就停手（不是无限重试）", dead.n == 3, str(dead.n))
        check("主源被标死，本进程内不再用", ("kline", "x") in rtr.dead, str(rtr.dead))

    section("router：断连三次且**没有**备源 → 仍抛 ThrottleTimeout")
    # 没有别的源可换时，"稍后重跑"才是正确动作；硬撑只会产出一份空报告。
    c4 = tcfg(tmp / "tr4", rate_rows_per_min={"x": 0.0},
              cooldown_base=0.01, cooldown_max=0.02)
    clk4 = FakeClock()
    th4 = Throttle(c4, clock=clk4, sleep=clk4.sleep)
    lonely = Dead()
    with fake_world({"x": lonely}, [spec("x", 0)]):
        rtr = R.Router(cfg=c4, throttle=th4)
        expect_raise("抛 ThrottleTimeout（码 6，稍后重跑）", ThrottleTimeout,
                     rtr.fetch, "kline", ["sh600519"],
                     Need("kline", "fq=raw", cal.days[-1], cal.days[-5], 5, 1))
        check("只试了 3 次就停手", lonely.n == 3, str(lonely.n))


def _child(cfg_dict: dict, symbol: str, out, log_file: str) -> None:
    """子进程：装同一个假源，跑一次 ensure，把结果塞进队列。"""
    c = Config(**cfg_dict)
    hub = preflight(cfg=c, now=dt.datetime(2026, 11, 14, 10, 0))
    cal = hub.cal
    rows = kline_rows(cal)
    fake = FakeSrc(lambda s: rows, sleep=0.25, log_file=log_file)
    with fake_world({"fake": fake}, [spec("fake", 0)]):
        try:
            r = hub.ensure("kline", symbol, want.kline(cal, cal.days[-1], 300, 250))
            out.put(("ok", r.n))
        except Exception as e:                                  # noqa: BLE001
            out.put(("err", f"{type(e).__name__}: {e}"))
    hub.close()


def test_concurrent(tmp: Path, cal: TradingCalendar) -> None:
    section("api.ensure：两进程同时取同一只（用例 7）")
    c = tcfg(tmp / "c1", rate_rows_per_min={"fake": 0.0})
    log_file = str(tmp / "c1" / "calls.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    Path(log_file).write_text("", encoding="utf-8")

    # 先建库，避免两个子进程同时建库互相踩
    preflight(cfg=c, now=dt.datetime(2026, 11, 14, 10, 0)).close()

    ctx = mp.get_context("fork")
    q = ctx.Queue()
    cfg_dict = dict(c.__dict__)
    ps = [ctx.Process(target=_child, args=(cfg_dict, "sh600519", q, log_file))
          for _ in range(2)]
    t0 = time.time()
    for p in ps:
        p.start()
    for p in ps:
        p.join(30)
    results = [q.get(timeout=5) for _ in ps]
    elapsed = time.time() - t0

    check("两个进程都没崩", all(r[0] == "ok" for r in results), str(results))
    check("两边都拿到 300 根", all(r[1] == 300 for r in results), str(results))
    calls = [ln for ln in Path(log_file).read_text().splitlines() if ln.strip()]
    check("上游只被打了 1 次（分片锁 + 双检）", len(calls) == 1, str(calls))
    check("确实是并发跑的（耗时 < 两次串行）", elapsed < 0.25 * 2 + 0.6, f"{elapsed:.2f}s")


# ================================================================ 7. 真联网

def test_live(cal: TradingCalendar) -> None:
    section("用例 1（真打东财）：空库取茅台 300 根")
    tmp = Path(tempfile.mkdtemp(prefix="stockhub-live-"))
    c = tcfg(tmp, rate_rows_per_min={"eastmoney": 15000.0},
             min_interval_ms={"eastmoney": 400.0}, burst_rows=12000.0)
    hub = preflight(cfg=c, now=dt.datetime(2026, 11, 14, 10, 0))
    need = want.kline(cal, cal.days[-1], bars=300, min_bars=250)
    try:
        r = hub.ensure("kline", "sh600519", need)
        check("真取到 300 根", r.n == 300, str(r.n))
        check("最后一天是 target", r.end == cal.days[-1], str(r.end))
        check("raw：茅台 2026-11-13 收盘 1435.26",
              abs(r.rows[-1]["close"] - 1435.26) < 1e-6, str(r.rows[-1]))
        check("成交量是股不是手", r.rows[-1]["volume"] > 1e6, str(r.rows[-1]["volume"]))
        check("src 记的是 upstream=eastmoney",
              C.read(hub.conn, "kline", "sh600519", need.fp).src == "eastmoney")
        t0 = time.time()
        r2 = hub.ensure("kline", "sh600519", need)
        check("二次调用走库（毫秒级）", time.time() - t0 < 0.3 and r2.fetched is False)
    finally:
        hub.close()


# ================================================================ main

def main() -> int:
    logging.disable(logging.WARNING)      # 测的是行为，不是日志噪音
    tmp = mkdtemp("stockhub-fetch-")
    print(f"临时目录：{tmp}")
    cal = TradingCalendar.load(DEFAULT_CALENDAR)

    test_throttle(tmp)
    test_got()
    test_adapters()
    test_router(tmp, cal)
    test_ensure(tmp, cal)
    test_partial(tmp, cal)
    test_stop_and_transient(tmp, cal)
    test_concurrent(tmp, cal)

    if os.environ.get("STOCKHUB_LIVE") in ("1", "true", "yes"):
        test_live(cal)
    else:
        print("\n（跳过用例 1：需要联网，设 STOCKHUB_LIVE=1 才跑）")

    print(f"\n{'=' * 50}")
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  失败：{f}")
    print(f"临时目录（可删）：{tmp}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
