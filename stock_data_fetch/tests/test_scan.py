"""第 7 步测试：`scan` 域（按日全市场横截面）。

跑法：

    cd /home/kevin/MyData/stock-value-invest-tool && \
    /home/kevin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
        -m stock_data_fetch.tests.test_scan

**全部离线**：`westock.call` 整个换掉，断言里没有一个真实日期/真实网络依赖。

这一节测的东西比它看起来重要：`scan` 是本层**唯一按日分片**的域，
而"覆盖记录分不清是哪一天"正是本层最贵的一类 bug（`snapshot` 三个子域踩过一次，
症状是"写完 consensus 之后 mainop 永远命中却查出 0 行"，全程没有一句报错）。
所以这里的重点是"第二天必须重新取"而不是"第一天取到了"。
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

os.environ.setdefault("STOCKHUB_WESTOCK_TOKEN", "test-token-not-real")

from stock_data_fetch import api, coverage as C                 # noqa: E402
from stock_data_fetch.config import (DEFAULT_CALENDAR, DEFAULT_SCHEMA,   # noqa: E402
                                     SCAN_LIMIT, SCAN_POINTS, Config)
from stock_data_fetch.errors import EmptyResult, SourceUnavailable       # noqa: E402
from stock_data_fetch.hub import preflight                      # noqa: E402
from stock_data_fetch.need import want                          # noqa: E402
from stock_data_fetch.sources import westock as W               # noqa: E402

PASS = 0
FAIL: list[str] = []

D1 = "2026-11-13"      # 周五
D2 = "2026-11-12"      # 周四
NOW = dt.datetime(2026, 11, 14, 16, 0)


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


@contextmanager
def patch(obj, name: str, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def mkdtemp(prefix: str) -> Path:
    root = Path(os.environ.get("STOCKHUB_TMP")
                or HERE.parent.parent.parent / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def make_hub(tmp: Path):
    cfg = Config()
    cfg.db_path = tmp / "shared.db"
    cfg.state_dir = tmp / "state"
    cfg.schema_path = DEFAULT_SCHEMA
    cfg.calendar_path = DEFAULT_CALENDAR
    cfg.rate_rows_per_min = {"westock": 15000.0, "eastmoney": 15000.0, "tdx": 0.0}
    cfg.min_interval_ms = {"westock": 0.0, "eastmoney": 0.0, "tdx": 0.0}
    cfg.burst_rows = 200000.0
    cfg.log_level = "ERROR"
    return preflight(cfg=cfg, now=NOW, free_mb=10_000)


# ================================================================ 假上游

def stock(sym: str, chg: float = 1.0, close: float = 10.0, name: str | None = None) -> dict:
    """一条 `tool_filter` 的 stocks 元素，字段名照抄上游。

    只有四个字段——`SCAN_EXPR` 只有 `ClosePrice > 0` 一条条件，上游就只回
    `ChangePCT` / `ClosePrice` / `code` / `name`。市值与净流入**故意不请求**：
    多一个条件就撞服务端 2 秒超时（见 `config.SCAN_EXPR` 的实测表）。
    """
    return {"code": sym, "name": name or sym, "ChangePCT": f"{chg:.2f}",
            "ClosePrice": f"{close:.2f}"}


def scan_payload(stocks: list[dict], total: int | None = None) -> dict:
    return {"ok": True,
            "data": {"totalStocks": len(stocks) if total is None else total,
                     "stockAmountInUniverse": "全市场",
                     "stocks": stocks}}


class FakeUpstream:
    """把 `westock.call` 换掉。`by_day` 让每天回不同的池子。"""

    def __init__(self, by_day: dict[str, dict] | None = None,
                 default: dict | None = None) -> None:
        self.by_day = by_day or {}
        self.default = default if default is not None else scan_payload([])
        self.n = 0
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool: str, args: dict, **kw):
        self.n += 1
        self.calls.append((tool, dict(args)))
        if tool != "tool_filter":
            return {}
        return self.by_day.get(args.get("date"), self.default)

    def dates(self) -> list[str]:
        return [a.get("date") for t, a in self.calls if t == "tool_filter"]


# ================================================================ 1. 指纹

def test_need() -> None:
    section("1. need：fp 必须钉死日期（这是本节的核心）")

    n1 = want.scan(D1)
    n2 = want.scan(D2)
    check("domain 是 scan", n1.domain == "scan")
    check("fp = date=<那天>", n1.fp == f"date={D1}", n1.fp)
    check("两天的 fp 不同", n1.fp != n2.fp, f"{n1.fp} vs {n2.fp}")
    check("end/start 都是那天", n1.end == D1 and n1.start == D1, f"{n1.start}..{n1.end}")
    check("points 是预估行数（估配额用）", n1.points == SCAN_POINTS, n1.points)
    check("min_points = 1（有一行就算取到）", n1.min_points == 1, n1.min_points)

    # fp 不同 → 覆盖记录是两条。`hit` 才不会把"取过周五"当成"周四也有"。
    check("周五的 fp 不等于周五以外的任何一天",
          n1.fp != want.scan("2026-11-11").fp)


# ================================================================ 2. 适配器

def test_adapter() -> None:
    section("2. 适配器：字段映射 / 截断 / 报错 / 整列空")

    up = FakeUpstream({D1: scan_payload([
        stock("sh600519", 1.77, 1435.26, "贵州茅台"),
        stock("sz000858", -0.56, 118.0, "五粮液"),
    ])})
    with patch(W, "call", up):
        got = W._scan(["*"], want.scan(D1))

    check("一次就够（codes 不用）", up.n == 1, str(up.n))
    check("rows 的键是 '*'", list(got.rows) == [W.SCAN_ROW_KEY], str(list(got.rows)))
    rows = got.rows[W.SCAN_ROW_KEY]
    check("两只票两行", len(rows) == 2, str(len(rows)))
    r = rows[0]
    check("date 就是请求那天", r["date"] == D1, r["date"])
    check("code → symbol", r["symbol"] == "sh600519", r["symbol"])
    check("name 跟着带回来（省一次 profile）", r["name"] == "贵州茅台", r["name"])
    check("ClosePrice → close", r["close"] == 1435.26, r["close"])
    check("ChangePCT → pct_chg（百分数）", r["pct_chg"] == 1.77, r["pct_chg"])
    check("下跌的票也存（-0.56 不被丢掉）", rows[1]["pct_chg"] == -0.56,
          rows[1]["pct_chg"])

    # ---- 表达式与 limit ----
    tool, args = up.calls[0]
    check("调的是 tool_filter", tool == "tool_filter", tool)
    check("limit 用 SCAN_LIMIT", args.get("limit") == SCAN_LIMIT, str(args.get("limit")))
    check("market = hs", args.get("market") == "hs")
    check("date 是请求那天", args.get("date") == D1)
    check("按涨幅降序（截断时先丢最弱的）",
          (args.get("orderby"), args.get("order")) == ("ChangePCT", "desc"),
          str((args.get("orderby"), args.get("order"))))
    # 表达式必须**只有一条条件**：多一条就撞服务端 2 秒超时（见 config 的实测表）。
    check("表达式只引用 ClosePrice 与 ChangePCT",
          "ClosePrice > 0" in str(args.get("expression")), str(args.get("expression")))
    for must_not in ("MainNetFlow", "NegotiableMV", "TotalMV", "TurnoverRate"):
        check(f"表达式不引用 {must_not}（多一条就超时）",
              must_not not in str(args.get("expression")),
              str(args.get("expression")))

    # ---- 截断：上游总数大于回来条数 → 必须抛，不许静默少 2000 只 ----
    up2 = FakeUpstream({D1: scan_payload([stock("sh600519")], total=5031)})
    with patch(W, "call", up2):
        e = expect_raise("截断 → SourceUnavailable", SourceUnavailable,
                         W._scan, ["*"], want.scan(D1))
    check("截断的报错里带上游总数", e is not None and "5031" in str(e), str(e))

    # ---- ok: false ----
    up3 = FakeUpstream({D1: {"ok": False, "message": "date 不是交易日"}})
    with patch(W, "call", up3):
        expect_raise("ok=false → SourceUnavailable", SourceUnavailable,
                     W._scan, ["*"], want.scan(D1))

    # ---- call 返回 None（令牌失效/限频/服务端 2 秒超时）----
    with patch(W, "call", lambda *a, **k: None):
        expect_raise("call 返回 None → SourceUnavailable", SourceUnavailable,
                     W._scan, ["*"], want.scan(D1))

    # ---- 空池：不是错误，给 empty_reason ----
    up4 = FakeUpstream({D1: scan_payload([])})
    with patch(W, "call", up4):
        got4 = W._scan(["*"], want.scan(D1))
    check("空池 → empty_reason=no_data，不抛", got4.empty_reason == "no_data",
          str(got4.empty_reason))
    check("空池 → rows 为空", got4.rows == {})

    # ---- 整列空：表达式不再引用某列 → 全部 None，必须抛 ----
    blank = [dict(stock("sh600519"), ClosePrice="--"),
             dict(stock("sz000858"), ClosePrice="")]
    with patch(W, "call", FakeUpstream({D1: scan_payload(blank)})):
        e = expect_raise("close 整列为空 → SourceUnavailable", SourceUnavailable,
                         W._scan, ["*"], want.scan(D1))
    check("整列空的报错点名了那一列", e is not None and "close" in str(e), str(e))


# ================================================================ 3. 落库与覆盖

def test_store(tmp: Path) -> None:
    section("3. 落库与覆盖：第二天必须重新取")

    hub = make_hub(tmp / "store")
    try:
        up = FakeUpstream({
            D1: scan_payload([stock("sh600519", 1.77), stock("sz000858", -0.56)]),
            D2: scan_payload([stock("sh600519", 0.42), stock("sz300750", 3.10),
                              stock("bj430047", 5.00)]),
        })
        with patch(W, "call", up):
            rows1 = api.ensure_scan(hub, D1)
            check("第一次取回 2 行", len(rows1) == 2, str(len(rows1)))
            check("打完一次上游", up.n == 1, str(up.n))

            cov = C.read(hub.conn, "scan", api.SCAN_SYMBOL, want.scan(D1).fp)
            check("覆盖记录的 symbol 是 '*'", cov.symbol == api.SCAN_SYMBOL, str(cov.symbol))
            check("覆盖记录的 fp 带日期", cov.fp == f"date={D1}", cov.fp)
            check("覆盖记录的 start=end=那天", cov.start == D1 and cov.end == D1,
                  f"{cov.start}..{cov.end}")
            check("覆盖记录的 n 是真实行数（不是写死 1）", cov.n == 2, str(cov.n))
            check("覆盖记录没有洞", cov.holes == 0 and cov.partial == 0)

            # 同一天再要一次 → 命中，不再联网
            rows_again = api.ensure_scan(hub, D1)
            check("同一天第二次不再打上游", up.n == 1, str(up.n))
            check("同一天第二次拿到的还是那 2 行", len(rows_again) == 2)

            # ---- 关键用例：往**前**一天要，不能因为 cov.end 更大就命中 ----
            rows2 = api.ensure_scan(hub, D2)
            check("换一天必须重新打上游", up.n == 2, str(up.n))
            check("第二天取回 3 行", len(rows2) == 3, str(len(rows2)))
            cov2 = C.read(hub.conn, "scan", api.SCAN_SYMBOL, want.scan(D2).fp)
            check("两天各有一条覆盖记录", cov2 is not None and cov2.fp == f"date={D2}",
                  str(cov2.fp if cov2 else None))
            check("第二天那条的 n = 3", cov2.n == 3, str(cov2.n))

            # 库里的行按日分开，读哪天的就只有哪天的
            check("库里共 5 行（2+3）",
                  hub.conn.execute("SELECT COUNT(*) FROM scan").fetchone()[0] == 5)
            d1 = hub.conn.execute("SELECT COUNT(*) FROM scan WHERE date=?", (D1,)).fetchone()[0]
            check("D1 那天 2 行", d1 == 2, str(d1))

            r1 = api.read(hub, "scan", api.SCAN_SYMBOL, want.scan(D1))
            check("read(D1) 只回 D1 的行", {r["date"] for r in r1.rows} == {D1},
                  str({r["date"] for r in r1.rows}))
            r2 = api.read(hub, "scan", api.SCAN_SYMBOL, want.scan(D2))
            check("read(D2) 只回 D2 的行", {r["date"] for r in r2.rows} == {D2})

            # ---- 整天替换：上游这次只回 1 只，库里不能留着上次的 ----
            up.by_day[D1] = scan_payload([stock("sh600519", 9.9)])
            with patch(W, "call", up):
                rows3 = api.ensure_scan(hub, D1, force=True)
            check("force 重取后只剩 1 行", len(rows3) == 1, str(len(rows3)))
            left = hub.conn.execute("SELECT COUNT(*) FROM scan WHERE date=?", (D1,)).fetchone()[0]
            check("库里的 D1 也只剩 1 行（整天替换，不是逐行 upsert）", left == 1, str(left))
            check("D2 的行没被牵连",
                  hub.conn.execute("SELECT COUNT(*) FROM scan WHERE date=?", (D2,)).fetchone()[0] == 3)
            cov3 = C.read(hub.conn, "scan", api.SCAN_SYMBOL, want.scan(D1).fp)
            check("覆盖的 n 跟着变成 1", cov3.n == 1, str(cov3.n))

            # ---- 多天 ----
            up.n = 0
            up.by_day = {D1: scan_payload([stock("sh600519")]),
                         D2: scan_payload([stock("sz000858")]),
                         "2026-11-11": scan_payload([stock("sz300750")])}
            with patch(W, "call", up):
                many = hub.scan_many([D1, D2, "2026-11-11"], force=True)
            check("scan_many 三天都有", sorted(many) == sorted([D1, D2, "2026-11-11"]),
                  str(sorted(many)))
            check("scan_many 每天一行", all(len(v) == 1 for v in many.values()),
                  str({k: len(v) for k, v in many.items()}))
            check("scan_many 打了三次上游", up.n == 3, str(up.n))
    finally:
        hub.close()


# ================================================================ 4. 只读与失败

def test_readonly_and_failure(tmp: Path) -> None:
    section("4. 只读入口 / 取不到就抛（不许产出空名单）")

    hub = make_hub(tmp / "ro")
    try:
        up = FakeUpstream({})
        with patch(W, "call", up):
            cached = hub.scan_cached(D1)
            check("库空时 scan_cached 返回 []", cached == [], str(cached))
            check("scan_cached 一次上游都没打", up.n == 0, str(up.n))

            def boom(tool, args, **kw):
                raise SourceUnavailable("上游 503")

            with patch(W, "call", boom):
                expect_raise("首次取数上游报错 → 抛（不是返回空名单）",
                             SourceUnavailable, api.ensure_scan, hub, D1)
            check("抛了之后库里仍然没有 scan 行",
                  hub.conn.execute("SELECT COUNT(*) FROM scan").fetchone()[0] == 0)

            # 首次返回空池 → EmptyResult（"这一天一只票都没有"必是代码/日期有问题）
            with patch(W, "call", up):
                up.by_day[D1] = scan_payload([])
                expect_raise("首次取数上游给空池 → EmptyResult", EmptyResult,
                             api.ensure_scan, hub, D1)

            # ---- 盘中：要联网就拦 ----
            hub.intraday = True
            up.by_day[D1] = scan_payload([stock("sh600519")])
            from stock_data_fetch.errors import MarketOpenError
            with patch(W, "call", up):
                expect_raise("盘中要联网 → MarketOpenError", MarketOpenError,
                             api.ensure_scan, hub, D1)
            check("盘中那次一次上游都没打", up.n == 1, str(up.n))
            hub.intraday = False
            with patch(W, "call", up):
                api.ensure_scan(hub, D1)
            check("非盘中再要就取到了",
                  hub.conn.execute("SELECT COUNT(*) FROM scan").fetchone()[0] == 1)
    finally:
        hub.close()


# ================================================================ 5. 保留期

def test_prune(tmp: Path) -> None:
    section("5. 保留期清理：只删过期的，覆盖记录跟着删")

    hub = make_hub(tmp / "prune")
    try:
        from stock_data_fetch import db
        days = [f"2026-{m:02d}-{d:02d}" for m, d in
                ((8, 1), (9, 1), (10, 1), (11, 1), (11, 13))]
        up = FakeUpstream({d: scan_payload([stock("sh600519")]) for d in days})
        with patch(W, "call", up):
            for d in days:
                api.ensure_scan(hub, d)
        check("清前 5 天都在",
              hub.conn.execute("SELECT COUNT(DISTINCT date) FROM scan").fetchone()[0] == 5)

        # 120 天保留期（meta 里的默认值）：锚在 hub.target（2026-11-13），
        # cutoff = 07-16 → 5 天全在窗口内，一行都不删
        check("默认 120 天内一行都不删", hub.prune_scan() == 0)

        # 60 天：cutoff = 09-14 → 8-01 / 9-01 出窗
        n = hub.prune_scan(keep_days=60)
        left = [r[0] for r in hub.conn.execute(
            "SELECT DISTINCT date FROM scan ORDER BY date")]
        check("删了 2 天（120 天窗口外）", n == 2, str(n))
        check("留下的都是窗口内的", left == ["2026-10-01", "2026-11-01", "2026-11-13"],
              str(left))
        covs = [r[0] for r in hub.conn.execute(
            "SELECT fp FROM coverage WHERE domain='scan' ORDER BY fp")]
        check("被删那天的覆盖记录也没了（否则下次 hit 谎报命中）",
              all("08-01" not in c and "09-01" not in c for c in covs), str(covs))
        check("再清一次是幂等的（0 行）", hub.prune_scan() == 0)
        check("keep_days=0 表示不清理", hub.prune_scan(keep_days=0) == 0)
    finally:
        hub.close()


def test_meta(tmp: Path) -> None:
    section("5. schema 侧：scan 表存在、保留期写进 meta")

    hub = make_hub(tmp / "meta")
    try:
        from stock_data_fetch import db
        check("scan 表在", "scan" in db.table_counts(hub.conn))
        check("scan_retention_days 写进了 meta",
              db.meta_get(hub.conn, "scan_retention_days") == "120",
              str(db.meta_get(hub.conn, "scan_retention_days")))
        check("schema_version = 2", db.meta_get(hub.conn, "schema_version") == "2")
        idx = [r[0] for r in hub.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='scan'")]
        check("有 ix_scan_symbol", "ix_scan_symbol" in idx, str(idx))
    finally:
        hub.close()


# ================================================================ main

def main() -> int:
    tmp = mkdtemp("stockhub-scan-")
    print(f"临时目录：{tmp}\n")
    test_need()
    test_adapter()
    test_store(tmp)
    test_readonly_and_failure(tmp)
    test_prune(tmp)
    test_meta(tmp)
    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    print(f"临时目录（可删）：{tmp}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
