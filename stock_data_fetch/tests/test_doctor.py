"""doctor 测试（离线，只读，不联网）。

跑法：

    cd /home/kevin/MyData/stock-value-invest-tool
    python3 -m stock_data_fetch.tests.test_doctor

覆盖两类事：

1. **正常库要判"没问题"**——否则体检工具会变成噪音源，谁都不看；
2. **每种坏法都要被抓到**——尤其不变式 3 的四种裂法（n 不符 / end 不符 /
   有覆盖没数据 / 有数据没覆盖）。这四种在真实库里都是**静默**的：
   工具照跑、报告照出，只是数据悄悄少了一截。
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

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent        # stock-value-invest-tool/（包的父目录）
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import config, db, doctor                # noqa: E402
from stock_data_fetch.calendar import TradingCalendar         # noqa: E402
from stock_data_fetch.config import DEFAULT_CALENDAR, DEFAULT_SCHEMA, Config  # noqa: E402

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
    """不能用 `/tmp`：本机 `/tmp` 是 10MB tmpfs，建几个 SQLite 就撞墙。"""
    root = Path(os.environ.get("STOCKHUB_TMP")
                or HERE.parent.parent.parent / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def _exit_code(argv: list[str]) -> int:
    """跑 CLI 但吞掉输出——体检报告会污染测试的可读性。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return doctor.main(argv)


def mkcfg(tmp: Path, *, calendar: Path | None = None) -> Config:
    c = Config()
    c.db_path = tmp / "shared.db"
    c.state_dir = tmp / "state"
    c.schema_path = DEFAULT_SCHEMA
    c.calendar_path = calendar or DEFAULT_CALENDAR
    c.min_free_mb = 1                      # 测试机器磁盘可能很紧，别让空间告警干扰断言
    return c


def codes(rep: doctor.Report) -> set[str]:
    return {f.code for f in rep.findings}


def seed(conn) -> None:
    """造一个"健康"的库：一只票的 kline + finance + quote + profile，覆盖记录都对得上。"""
    cal = TradingCalendar.load(DEFAULT_CALENDAR)
    days = [d for d in cal.days if d >= "2026-01-05"][:5]
    with db.tx(conn):
        for d in days:
            conn.execute("INSERT INTO kline(symbol,date,open,high,low,close,volume,amount,src) "
                         "VALUES('sh600519',?,10,11,9,10.5,100,1000,'eastmoney')", (d,))
        conn.execute("INSERT INTO coverage(domain,symbol,fp,start_date,end_date,n,holes,"
                     "hole_detail,partial,src,updated_at) "
                     "VALUES('kline','sh600519','fq=raw',?,?,?,0,NULL,0,'eastmoney',?)",
                     (days[0], days[-1], len(days), dt.datetime.now().isoformat()))
        conn.execute("INSERT INTO finance(symbol,period,OperatingRevenue,src,updated_at) "
                     "VALUES('sh600519','2026-09-30',1.0,'tencent',?)",
                     (dt.datetime.now().isoformat(),))
        conn.execute("INSERT INTO coverage(domain,symbol,fp,start_date,end_date,n,holes,"
                     "hole_detail,partial,src,updated_at) "
                     "VALUES('finance','sh600519','type=all;fields=all','2026-09-30',"
                     "'2026-09-30',1,0,NULL,0,'tencent',?)",
                     (dt.datetime.now().isoformat(),))
        conn.execute("INSERT INTO quote(symbol,trade_date,price,src,updated_at) "
                     "VALUES('sh600519','2026-11-13',1435.0,'tencent',?)",
                     (dt.datetime.now().isoformat(),))
        conn.execute("INSERT INTO coverage(domain,symbol,fp,start_date,end_date,n,holes,"
                     "hole_detail,partial,src,updated_at) VALUES('quote','sh600519','',"
                     "'2026-11-13','2026-11-13',1,0,NULL,0,'tencent',?)",
                     (dt.datetime.now().isoformat(),))
        conn.execute("INSERT INTO stock(symbol,name,exch,setcode,is_financial,updated_at) "
                     "VALUES('sh600519','贵州茅台','SH',1,0,?)",
                     (dt.datetime.now().isoformat(),))
        conn.execute("INSERT INTO coverage(domain,symbol,fp,start_date,end_date,n,holes,"
                     "hole_detail,partial,src,updated_at) VALUES('profile','sh600519','',"
                     "NULL,NULL,1,0,NULL,0,'tencent',?)", (dt.datetime.now().isoformat(),))


def test_healthy(tmp: Path) -> None:
    section("1. 健康库：没有 ERROR，退出码 0")
    cfg = mkcfg(tmp)
    db.create_db(cfg.db_path, cfg.schema_path)
    conn = db.connect(cfg.db_path)
    seed(conn)
    conn.close()

    rep = doctor.run(cfg)
    check("没有 ERROR", rep.errors == [], str([f.code for f in rep.errors]))
    check("识别出四个域", {d["domain"] for d in rep.facts.get("domains", [])}
          == {"kline", "finance", "quote", "profile"},
          str(rep.facts.get("domains")))
    check("表行数被统计到", rep.facts.get("tables", {}).get("kline") == 5,
          str(rep.facts.get("tables")))
    check("正常库里没有不变式告警",
          not any(c.startswith("inv.") for c in codes(rep)), str(sorted(codes(rep))))
    # 4 年日历必然触发 cal.short（它就是本机现状），这是 WARN 不是 ERROR
    check("短日历报 WARN 而不是 ERROR", "cal.short" in codes(rep) and not rep.errors,
          str(sorted(codes(rep))))
    check("退出码 0", _exit_code(["--db", str(cfg.db_path),
                                 "--calendar", str(cfg.calendar_path)]) == 0)


def test_invariants(tmp: Path) -> None:
    section("2. 不变式 3 的四种裂法都要被抓到")
    cfg = mkcfg(tmp / "inv")
    db.create_db(cfg.db_path, cfg.schema_path)
    conn = db.connect(cfg.db_path)
    seed(conn)
    with db.tx(conn):
        conn.execute("UPDATE coverage SET n = 300 WHERE domain='kline'")
    rep = doctor.run(cfg)
    check("n 与表内行数不符 → ERROR", "inv.kline.n_mismatch" in codes(rep),
          str(sorted(codes(rep))))
    check("错误详情指出差多少", any("覆盖 n=300 实际 5" in e
                              for f in rep.errors for e in f.examples),
          str([f.examples for f in rep.errors]))

    with db.tx(conn):
        conn.execute("UPDATE coverage SET n = 5, end_date = '2026-11-13' WHERE domain='kline'")
    rep = doctor.run(cfg)
    check("end_date 与表内最新日期不符 → ERROR", "inv.kline.end_mismatch" in codes(rep),
          str(sorted(codes(rep))))

    with db.tx(conn):                       # 覆盖说 end=最新，但把数据删光
        conn.execute("DELETE FROM kline")
    rep = doctor.run(cfg)
    check("有覆盖记录但表里没数据 → ERROR",
          "inv.kline.covered_but_empty" in codes(rep), str(sorted(codes(rep))))

    with db.tx(conn):                       # 反过来：有数据没覆盖
        conn.execute("DELETE FROM coverage WHERE domain='kline'")
        conn.execute("INSERT INTO kline(symbol,date,close,src) "
                     "VALUES('sz000651','2026-11-13',40.0,'eastmoney')")
    rep = doctor.run(cfg)
    check("有数据没覆盖 → WARN（每次都会重取，白烧配额）",
          "inv.kline.orphan" in codes(rep), str(sorted(codes(rep))))
    check("孤儿数据算 WARN 不算 ERROR",
          any(f.level == "WARN" and f.code == "inv.kline.orphan" for f in rep.findings))

    with db.tx(conn):                       # 点域：有覆盖没那一份
        conn.execute("DELETE FROM stock WHERE symbol='sh600519'")
    rep = doctor.run(cfg)
    check("点域有覆盖没数据 → ERROR", "inv.profile.covered_but_empty" in codes(rep),
          str(sorted(codes(rep))))
    conn.close()


def test_depth_and_partial(tmp: Path) -> None:
    section("3. 深度不足 / partial / 洞 / 水位线")
    cfg = mkcfg(tmp / "depth")
    db.create_db(cfg.db_path, cfg.schema_path)
    conn = db.connect(cfg.db_path)
    seed(conn)
    with db.tx(conn):
        conn.execute("UPDATE coverage SET partial=1 WHERE domain='finance'")
        conn.execute("UPDATE coverage SET holes=2 WHERE domain='kline'")
        conn.execute("INSERT OR REPLACE INTO watermark(domain,scope,last_ok_date,updated_at) "
                     "VALUES('kline','','2026-12-31',?)", (dt.datetime.now().isoformat(),))
    rep = doctor.run(cfg)
    c = codes(rep)
    check("partial=1 → WARN", "cov.partial" in c, str(sorted(c)))
    check("有洞 → WARN", "cov.holes" in c, str(sorted(c)))
    check("水位线跑到覆盖前面 → WARN", "wm.ahead" in c, str(sorted(c)))
    check("K 线 5 根 < 250 → WARN", "depth.kline" in c, str(sorted(c)))
    check("财报 1 期 < 40 → WARN", "depth.finance" in c, str(sorted(c)))
    check("这些都是 WARN，没有 ERROR", rep.errors == [], str([f.code for f in rep.errors]))
    conn.close()


def test_broken_db(tmp: Path) -> None:
    section("4. 库层面的坏法：不存在 / 版本不符")
    cfg = mkcfg(tmp / "nodb")
    rep = doctor.run(cfg)
    check("库不存在 → ERROR（且不建库）",
          "db.missing" in codes(rep) and not cfg.db_path.exists(), str(sorted(codes(rep))))
    check("库不存在时退出码 1",
          _exit_code(["--db", str(cfg.db_path), "--calendar", str(cfg.calendar_path)]) == 1)

    cfg2 = mkcfg(tmp / "badver")
    db.create_db(cfg2.db_path, cfg2.schema_path)
    conn = db.connect(cfg2.db_path)
    # 用"未来版本"而不是写死数字：写死的话每升一版都得回来改，
    # 改漏了这条就从"测拒绝"变成"测放行"，而它恰恰是防误删库的那一条。
    conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION + 1}")
    conn.close()
    rep = doctor.run(cfg2)
    check("版本不符 → ERROR", "db.version" in codes(rep), str(sorted(codes(rep))))

    # v1 老库（少 scan 表）走的是另一条路：**WARN**，不是 ERROR。
    # 它没坏，只是少一张新表，下次 preflight 会自己补。
    cfg_old = mkcfg(tmp / "oldver")
    db.create_db(cfg_old.db_path, cfg_old.schema_path)
    conn = db.connect(cfg_old.db_path, readonly=False)
    conn.execute("DROP TABLE scan")
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    rep = doctor.run(cfg_old)
    check("可自动升级的老库 → WARN 而不是 ERROR",
          "db.version" in codes(rep) and rep.errors == [],
          str([(f.code, f.level) for f in rep.findings]))

    cfg3 = mkcfg(tmp / "badcal", calendar=tmp / "nope.json")
    db.create_db(cfg3.db_path, cfg3.schema_path)
    rep = doctor.run(cfg3)
    check("日历缺失 → ERROR", "cal.load" in codes(rep), str(sorted(codes(rep))))


def test_render(tmp: Path) -> None:
    section("5. 输出：JSON 可被脚本消费，人读版本不炸")
    cfg = mkcfg(tmp / "render")
    db.create_db(cfg.db_path, cfg.schema_path)
    rep = doctor.run(cfg)

    text = doctor.render(rep)
    check("人读版本有结论行", any(line.startswith("结论：") for line in text.splitlines()),
          text[-80:])
    check("人读版本有库路径", "库      " in text)

    payload = json.loads(doctor.render(rep, as_json=True))
    check("JSON 顶层有 ok", payload["ok"] is True, str(payload.get("ok")))
    check("JSON 带 facts 与 findings",
          "facts" in payload and isinstance(payload["findings"], list))
    check("JSON 的 finding 结构完整",
          all({"level", "code", "title", "detail", "examples"} <= set(f)
              for f in payload["findings"]))
    check("不同级别按严重度排序",
          [f.level for f in rep.sorted_findings()]
          == sorted([f.level for f in rep.sorted_findings()],
                    key=lambda x: doctor.LEVEL_ORDER[x]))

    quiet = doctor.render(rep, quiet=True)
    check("quiet 不打概览但保留 WARN/ERROR",
          "共享数据层体检" not in quiet and ("⚠️" in quiet or "❌" in quiet or "结论" in quiet),
          quiet[:60])


def main() -> int:
    t1 = mkdtemp("stockhub-doc-ok-")
    test_healthy(t1)
    t2 = mkdtemp("stockhub-doc-inv-")
    test_invariants(t2)
    t3 = mkdtemp("stockhub-doc-depth-")
    test_depth_and_partial(t3)
    t4 = mkdtemp("stockhub-doc-bad-")
    test_broken_db(t4)
    t5 = mkdtemp("stockhub-doc-render-")
    test_render(t5)

    print("\n" + "=" * 60)
    if FAIL:
        print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
        for name in FAIL:
            print(f"  - {name}")
        return 1
    print(f"通过 {PASS} 项，失败 0 项")
    print(f"临时目录（可删）：{t1.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
