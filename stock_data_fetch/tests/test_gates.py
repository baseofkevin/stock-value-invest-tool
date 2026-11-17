"""闸门测试（离线，不联网，不打上游）。

跑法：

    cd /home/kevin/MyData/stock-value-invest-tool/stock_data_fetch && \
    /home/kevin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
        -m stock_data_fetch.tests.test_gates

覆盖 CONTRACT §15 的用例 8，以及 §4 六道闸的边界。
"""

from __future__ import annotations

import datetime as dt
import os
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent        # stock-value-invest-tool/（包的父目录）
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import config, db                       # noqa: E402
from stock_data_fetch.calendar import TradingCalendar, target_date  # noqa: E402
from stock_data_fetch.errors import HubNotReady, MarketOpenError, SchemaMismatch  # noqa: E402
from stock_data_fetch.hub import preflight                    # noqa: E402
from stock_data_fetch.need import want                        # noqa: E402

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


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def expect_raise(name: str, exc, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc as e:
        check(name, True, "")
        print(f"      → {type(e).__name__}: {e}")
        return
    except Exception as e:                                  # noqa: BLE001
        check(name, False, f"抛的是 {type(e).__name__}: {e}")
        return
    check(name, False, "没有抛异常")


def tmp_cfg(tmp: Path, calendar: Path | None = None,
            name: str = "shared.db") -> config.Config:
    c = config.Config()
    c.db_path = tmp / name
    c.state_dir = tmp / "state"
    c.schema_path = config.DEFAULT_SCHEMA
    c.calendar_path = calendar or config.DEFAULT_CALENDAR
    return c


_V1_DDL = """
PRAGMA user_version = 1;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kline(
  symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL, PRIMARY KEY(symbol, date)
) WITHOUT ROWID;
"""


def _has_table(path: Path, name: str) -> bool:
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                         (name,)).fetchone() is not None
    finally:
        c.close()


def main() -> int:
    tmp = mkdtemp("stockhub-test-")
    print(f"临时目录：{tmp}\n")

    # ---------------- 闸 2 / 3 / 4：建库与版本 ----------------
    print("闸 2/3/4 建库、幂等、版本")
    c = tmp_cfg(tmp)
    hub = preflight(cfg=c, now=dt.datetime(2026, 11, 14, 10, 0))   # 周六，不拦
    try:
        counts = db.table_counts(hub.conn)
        # 钉死表数是有意的：每加一张表都必须来这里显式改一次，逼人回答
        # "它凭什么占一块磁盘"（v1 = 11 张，v2 加 `scan` = 12 张，
        # v5 加 `annotation` = 13 张）。
        # annotation 的理由：人工标注是"公司的事实"，不是"某次取数的快照"，
        # 不能塞进 snapshot（那张表会被 GC 按访问时间回收）。
        check("建库后有 13 张表", len(counts) == 13, str(sorted(counts)))
        check("annotation 表在", "annotation" in counts)
        check("user_version == SCHEMA_VERSION",
              db.schema_version(hub.conn) == config.SCHEMA_VERSION)
        check("scan 表在", "scan" in counts)
        check("journal_mode == wal",
              hub.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
        check("busy_timeout 已落实",
              int(hub.conn.execute("PRAGMA busy_timeout").fetchone()[0]) == db.BUSY_TIMEOUT_MS)
        created = db.meta_get(hub.conn, "created_at")
        check("meta.created_at 已写", bool(created))
    finally:
        hub.close()

    hub2 = preflight(cfg=c, now=dt.datetime(2026, 11, 14, 10, 0))
    try:
        check("第二次 preflight 不重建（created_at 不变）",
              db.meta_get(hub2.conn, "created_at") == created)
        check("第二次表数仍是 13", len(db.table_counts(hub2.conn)) == 13)
    finally:
        hub2.close()

    # 用例 8：版本不符 → 拒绝启动，且不动任何数据
    # 用 `SCHEMA_VERSION + 1` 而不是写死 2：这条测的是"未来的库版本不认"，
    # 写死数字的话每升一版都要回来改，而改漏了它就从"测拒绝"变成"测放行"。
    conn = sqlite3.connect(c.db_path, isolation_level=None)
    conn.execute("INSERT INTO meta(key,value) VALUES('probe','1')")
    before = {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
              for n in ("meta", "kline", "finance")}
    conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION + 1}")
    conn.close()
    expect_raise("版本不符抛 SchemaMismatch", SchemaMismatch, preflight,
                 cfg=c, now=dt.datetime(2026, 11, 14, 10, 0))
    conn = sqlite3.connect(c.db_path)
    after = {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
             for n in ("meta", "kline", "finance")}
    conn.close()
    check("版本不符时库内容零改动", before == after, f"{before} vs {after}")
    conn = sqlite3.connect(c.db_path, isolation_level=None)   # 复位，供后续用例
    conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION}")
    conn.close()

    # 用例 8b：v1 老库（没有 scan 表）→ 自动补表，且**既有数据一条都不动**
    old = tmp_cfg(tmp, name="old.db")
    conn = sqlite3.connect(old.db_path, isolation_level=None)
    conn.executescript(_V1_DDL)
    conn.execute("INSERT INTO meta(key,value) VALUES('precious','2026-11-01')")
    conn.execute("INSERT INTO kline(symbol,date,close) VALUES('sh600519','2026-11-13',1435.26)")
    conn.close()
    check("造出来的老库确实没有 scan", _has_table(old.db_path, "scan") is False)
    hub3 = preflight(cfg=old, now=dt.datetime(2026, 11, 14, 10, 0))
    try:
        check("v1 → v2 自动补了 scan 表", _has_table(old.db_path, "scan") is True)
        check("升级后 user_version 已是当前版本",
              db.schema_version(hub3.conn) == config.SCHEMA_VERSION)
        check("升级没动 meta 里的既有键",
              db.meta_get(hub3.conn, "precious") == "2026-11-01")
        check("升级没动 kline 既有行",
              hub3.conn.execute("SELECT COUNT(*) FROM kline").fetchone()[0] == 1)
        check("升级留了痕", bool(db.meta_get(hub3.conn, "schema_upgraded_at")))
    finally:
        hub3.close()

    # ---------------- 闸 1：盘中 ----------------
    #
    # 2026-11-14 改语义：盘中**不再拒绝启动**，只标记 `intraday=True`（= 不许联网）。
    # 理由：库里已有的数据盘中照样能用，把整个工具在 09:15–15:05 关掉是过度拦截；
    # 真正要拦的是"联网取半成品 bar"，那件事发生在 `api._pull()` 里。
    print("\n闸 1 盘中（只拦联网，不拦启动）")
    for hhmm, why in (((10, 0), "10:00"), ((9, 20), "09:20 集合竞价"), ((15, 4), "15:04")):
        h = preflight(cfg=c, now=dt.datetime(2026, 11, 13, *hhmm))
        check(f"交易日 {why} 可以启动", h.conn is not None)
        check(f"交易日 {why} 标记 intraday（联网会被拦）", h.intraday is True, str(h.intraday))
        h.close()
    h = preflight(cfg=c, now=dt.datetime(2026, 11, 13, 10, 0))
    expect_raise("盘中 check_online → MarketOpenError", MarketOpenError,
                 h.check_online, "kline", "sh600519")
    # 2026-11-17：盘中允许补**历史**（end < 今天），但当日数据仍拦。
    check("盘中补 11-12 的 scan 放行",
          h.check_online("scan", "*", want.scan("2026-11-12")) is None)
    check("盘中补 11-12 的 K 线放行",
          h.check_online("kline", "sh600519",
                         want.kline(h.cal, "2026-11-12", bars=5, min_bars=1)) is None)
    check("盘中取 profile（静态）放行",
          h.check_online("profile", "sh600519", want.profile()) is None)
    expect_raise("盘中要当日 scan → 仍拦", MarketOpenError,
                 h.check_online, "scan", "*", want.scan("2026-11-13"))
    expect_raise("盘中要当日 K 线 → 仍拦", MarketOpenError,
                 h.check_online, "kline", "sh600519",
                 want.kline(h.cal, "2026-11-13", bars=5, min_bars=1))
    expect_raise("quote 没有历史版本，盘中始终拦", MarketOpenError,
                 h.check_online, "quote", "sh600519", want.quote("2026-11-12"))
    h.close()
    h = preflight(cfg=c, now=dt.datetime(2026, 11, 13, 15, 6))
    check("15:06 放行且 target=当日", h.target == "2026-11-13", h.target)
    check("15:06 不标记 intraday", h.intraday is False, str(h.intraday))
    h.close()
    h = preflight(cfg=c, now=dt.datetime(2026, 11, 13, 10, 0), allow_intraday=True)
    check("allow_intraday 放行联网", h.intraday is False, str(h.intraday))
    check("盘中放行写 meta 留痕", db.meta_get(h.conn, "intraday_override_at") is not None)
    check("盘中 10:00 的 target 退到上一交易日", h.target == "2026-11-12", h.target)
    h.close()

    # ---------------- 闸 1 + target：周末 ----------------
    print("\n非交易日与 target_date")
    h = preflight(cfg=c, now=dt.datetime(2026, 11, 14, 10, 0))     # 周六
    check("周六不拦", True)
    check("周六的 target = 上周五", h.target == "2026-11-13", h.target)
    h.close()
    h = preflight(cfg=c, now=dt.datetime(2026, 11, 15, 22, 0))     # 周日
    check("周日的 target = 上周五", h.target == "2026-11-13", h.target)
    h.close()

    # ---------------- 闸 5：日历自检 ----------------
    print("\n闸 5 日历")
    bad_cal = tmp / "cal_expired.json"
    days = ["2022-11-14", "2022-11-15", "2022-11-16"]
    bad_cal.write_text(json.dumps({"end": "2022-11-16", "span": "2022-11-14",
                                   "days": days}), encoding="utf-8")
    c_bad = tmp_cfg(tmp, calendar=bad_cal)
    c_bad.db_path = c.db_path
    expect_raise("日历过期抛 HubNotReady", HubNotReady, preflight,
                 cfg=c_bad, now=dt.datetime(2026, 11, 14, 10, 0))
    expect_raise("日历文件缺失抛 HubNotReady", HubNotReady, preflight,
                 cfg=tmp_cfg(tmp, calendar=tmp / "nope.json"),
                 now=dt.datetime(2026, 11, 14, 10, 0))

    cal = TradingCalendar.load(config.DEFAULT_CALENDAR)
    check("日历无断档/过期", cal.sanity("2026-11-14") == [], str(cal.sanity("2026-11-14")))
    check("日历覆盖 300 根起点",
          cal.start_for_bars("2026-11-13", 300) is not None)
    check("between 计数一致",
          len(cal.between("2026-01-01", "2026-11-13")) == cal.count("2026-01-01", "2026-11-13"))

    # ---------------- 长日历：生成器校验 + 路径解析 ----------------
    print("\n长日历（build_calendar）")
    from stock_data_fetch import build_calendar as BC          # noqa: E402

    def synth(years: int, end: str = "2026-11-13") -> list[str]:
        """合成一份"像样"的日历：工作日 - 每年 10 个法定假日。只用于校验逻辑，不是真日历。"""
        out = []
        d = dt.date.fromisoformat(end) - dt.timedelta(days=365 * years + 10)
        stop = dt.date.fromisoformat(end)
        holiday = {f"{y}{m}" for y in range(d.year, stop.year + 1)
                   for m in ("-01-01", "-10-01", "-10-02", "-10-03", "-05-01",
                             "-01-02", "-01-03", "-04-04", "-04-05", "-06-22")}
        while d <= stop:
            if d.weekday() < 5 and d.isoformat() not in holiday:
                out.append(d.isoformat())
            d += dt.timedelta(days=1)
        return out

    good = synth(12)
    check("合格日历校验通过", BC.check(good, today="2026-11-13") == [],
          str(BC.check(good, today="2026-11-13")))
    check("12 年日历首日早于 10 年前（够估值与分红两条需求）",
          good[0] < "2016-11-15", good[0])
    check("末年不满一年不算问题（只查完整年份）",
          not any("2026 年" in p for p in BC.check(good, today="2026-11-13")))

    holes = [d for d in good if not ("2024-03-04" <= d <= "2024-03-20")]
    check("断档 16 天 → 报错", any("断了" in p for p in BC.check(holes, today="2026-11-13")),
          str(BC.check(holes, today="2026-11-13"))[:120])
    few = [d for d in good if not d.startswith("2023")]           # 整年缺失
    check("缺一整年 → 年度计数报错", any("2023 年" in p for p in BC.check(few, today="2026-11-13")),
          str(BC.check(few, today="2026-11-13"))[:120])
    check("日历没跟上（差 40 天）→ 报错",
          any("没跟上" in p for p in BC.check(good, today="2026-12-23")),
          str(BC.check(good, today="2026-12-23"))[:120])

    # resolve_calendar：有了长日历就用它，没有就退回 4 年那份
    st = tmp / "state_long"
    st.mkdir(parents=True, exist_ok=True)
    check("state 里没有日历时退回 DEFAULT_CALENDAR",
          config.resolve_calendar(st) == config.DEFAULT_CALENDAR)
    (st / config.CALENDAR_NAME).write_text(json.dumps({"days": good[:5]}), encoding="utf-8")
    check("state 里有日历时优先用它",
          config.resolve_calendar(st) == st / config.CALENDAR_NAME,
          str(config.resolve_calendar(st)))
    os.environ["STOCKHUB_STATE"] = str(st)
    try:
        c_env = config.Config.from_env()
        check("from_env 会挑长日历", c_env.calendar_path == st / config.CALENDAR_NAME,
              str(c_env.calendar_path))
        os.environ["STOCKHUB_CALENDAR"] = str(config.DEFAULT_CALENDAR)
        c_env2 = config.Config.from_env()
        check("显式 STOCKHUB_CALENDAR 优先于自动发现",
              c_env2.calendar_path == config.DEFAULT_CALENDAR, str(c_env2.calendar_path))
    finally:
        os.environ.pop("STOCKHUB_STATE", None)
        os.environ.pop("STOCKHUB_CALENDAR", None)
    check("裸 Config() 仍是确定的 DEFAULT_CALENDAR（测试可复现）",
          config.Config().calendar_path == config.DEFAULT_CALENDAR)

    # ---------------- 深交所日历（权威源）----------------
    print("\n深交所月度日历")
    check("按月切片：跨年也对",
          BC._months("2015-11-14", "2016-02-01") == ["2015-11", "2015-12", "2016-01", "2016-02"],
          str(BC._months("2015-11-14", "2016-02-01")))
    check("同月只切一次", BC._months("2026-11-01", "2026-11-13") == ["2026-11"])

    asked: list[str] = []

    def fake_szse(url: str, *, referer: str = "") -> dict:
        ym = url.split("month=")[1][:7]
        asked.append(ym)
        assert referer, "深交所接口缺 Referer 会 403"
        days = {"2026-10": [("2026-10-09", "1"), ("2026-10-12", "1"), ("2026-10-01", "0")],
                "2026-11": [("2026-11-12", "1"), ("2026-11-13", "1"), ("2026-11-14", "0")]}
        return {"data": [{"jyrq": d, "jybz": f} for d, f in days.get(ym, [])]}

    with patch.object(BC, "_http_json", fake_szse), patch.object(BC.time, "sleep", lambda s: None):
        got = BC._from_szse("2026-10-01", "2026-11-13")
    check("按 jybz 取交易日（0 的不算）", got == ["2026-10-09", "2026-10-12",
                                            "2026-11-12", "2026-11-13"], str(got))
    check("按月各问一次", asked == ["2026-10", "2026-11"], str(asked))

    with patch.object(BC, "_http_json", fake_szse), patch.object(BC.time, "sleep", lambda s: None):
        win = BC._from_szse("2026-10-01", "2026-10-31")
    check("窗口外的日子被过滤掉（只回 10 月）",
          win == ["2026-10-09", "2026-10-12"], str(win))

    with patch.object(BC, "_http_json", lambda *a, **k: {"data": []}), \
         patch.object(BC.time, "sleep", lambda s: None):
        try:
            BC._from_szse("2026-11-01", "2026-11-13")
            raised = False
        except RuntimeError as e:
            raised = "返回空" in str(e)
    check("某个月返回空 → 抛错（不当成休市）", raised)

    check("szse 进了源表", "szse" in BC._FETCHERS)

    # ---------------- 闸 6：空间只告警 ----------------
    print("\n闸 6 空间")
    h = preflight(cfg=c, now=dt.datetime(2026, 11, 14, 10, 0), free_mb=10)
    check("空间不足只告警不抛", h is not None)
    h.close()

    # ---------------- 一致性约束 ----------------
    print("\n配置一致性")
    check("CLOSE_GRACE 与 MARKET_CLOSE 必须同值",
          config.CLOSE_GRACE == config.MARKET_CLOSE,
          f"{config.CLOSE_GRACE} vs {config.MARKET_CLOSE}")

    # ---------------- 事务 ----------------
    print("\n事务")
    conn = db.connect(c.db_path)
    n0 = db.table_counts(conn)["meta"]
    try:
        with db.tx(conn):
            conn.execute("INSERT INTO meta(key,value) VALUES('t_rollback','1')")
            raise RuntimeError("故意失败")
    except RuntimeError:
        pass
    check("tx 异常回滚", db.table_counts(conn)["meta"] == n0)
    with db.tx(conn):
        conn.execute("INSERT INTO meta(key,value) VALUES('t_commit','1')")
    check("tx 正常提交", db.table_counts(conn)["meta"] == n0 + 1)
    conn.close()

    # ---------------- 收尾 ----------------
    print(f"\n{'=' * 50}")
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  失败：{f}")
    print(f"临时目录（可删）：{tmp}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
