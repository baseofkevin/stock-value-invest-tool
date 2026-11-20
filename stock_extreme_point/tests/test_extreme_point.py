"""`stock_extreme_point` 的离线测试：抓取解析 / 口径 / 两个库的落库 / CLI。

    cd /home/kevin/MyData/stock-value-invest-tool && \
    /home/kevin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
        -m stock_extreme_point.tests.test_extreme_point

重点测四类"错了也不会报错"的东西：

1. **`SECUCODE` → `symbol`**：上游给 `600519.SH`，库里要 `sh600519`；沪深的
   `000xxx` 在 `SECURITY_CODE` 里会撞车，所以只能用带市场前缀的那个字段。
2. **整天整份替换**：同一天第二次只回来 2 只时，上次的第 3 只必须消失，
   否则"这天全市场 PE 中位数"被一只当天不在的票悄悄拉偏。
3. **价格与摘要同事务**：分开写会出现"价格换了一批、摘要还是上一批"——
   页面上卡片和表格对不上，却没有任何报错。这里断言摘要行和属于它的那批价格一致。
4. **两个库各有什么表**：`shared.db` 只有 `price`（摘要属于"结论"不属于原始层），
   `results.db` 有 `price` + `price_stat`。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

from stock_extreme_point import calendar as C, cli, schema, source, stat, store  # noqa: E402

PASS = 0
FAIL: list[str] = []

D1 = "2026-11-16"      # 周一
D2 = "2026-11-13"      # 周五


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


# ---------------------------------------------------------------- 假上游

def fake_payload(rows: list[dict], *, pages: int = 1, success: bool = True,
                 code: str = "0") -> str:
    return json.dumps({"success": success, "code": code, "message": "",
                       "result": {"data": rows, "pages": pages, "count": len(rows)}},
                      ensure_ascii=False)


def raw_row(secucode: str, name: str, close: float, pct: float,
            pe: float | None, pb: float | None, ps: float | None,
            mv: float, cmv: float, day: str = D1) -> dict:
    return {"SECUCODE": secucode, "TRADE_DATE": f"{day} 00:00:00",
            "SECURITY_NAME_ABBR": name, "CLOSE_PRICE": close,
            "CHANGE_RATE": pct, "PE_TTM": pe, "PB_MRQ": pb, "PS_TTM": ps,
            "TOTAL_MARKET_CAP": mv, "NOTLIMITED_MARKETCAP_A": cmv}


def fake_rows(day: str = D1) -> list[dict]:
    """上游原样返回的 4 行：2 主板 + 1 创业板 + 1 B 股（B 股应被剔掉）。"""
    return [
        raw_row("600519.SH", "贵州茅台", 1453.9, 1.30, 22.12, 6.70, 10.42,
                1.82e12, 1.82e12, day),
        raw_row("000001.SZ", "平安银行", 11.20, -0.89, 5.39, 0.52, 1.71,
                2.17e11, 2.17e11, day),
        raw_row("300750.SZ", "宁德时代", 182.50, 3.20, 41.00, 4.80, 5.10,
                8.03e11, 7.10e11, day),
        raw_row("900901.SH", "云赛B股", 0.83, 0.00, 12.0, 0.9, 1.0,
                1.0e9, 1.0e9, day),
    ]


def make_fetcher(rows: list[dict], *, pages: int = 1):
    """返回一个可注入 `source.fetch_day(get=...)` 的假 reader。"""
    calls: list[str] = []

    def reader(url: str, *, referer: str = "") -> str:
        calls.append(url)
        return fake_payload(rows, pages=pages)

    reader.calls = calls          # type: ignore[attr-defined]
    return reader


# ---------------------------------------------------------------- 临时目录

def tmp_root() -> Path:
    """临时目录放在仓库内（这台机器 `/tmp` 只有 10MB 的 tmpfs）。"""
    base = PROJECT / ".testtmp"
    base.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="priceshow-", dir=base))


# ---------------------------------------------------------------- 1 抓取解析

def test_source() -> None:
    print("\n1 抓取解析（东财按日横截面）")
    rows = source.fetch_day(D1, get=make_fetcher(fake_rows()))
    check("4 行响应 → 3 行入库（B 股被剔掉）", len(rows) == 3, f"got {len(rows)}")
    syms = sorted(r["symbol"] for r in rows)
    check("SECUCODE → symbol 带市场前缀", syms == ["sh600519", "sz000001", "sz300750"],
          str(syms))
    b = next(r for r in rows if r["symbol"] == "sh600519")
    check("名称/收盘/涨跌幅映射", b["name"] == "贵州茅台" and b["close"] == 1453.9
          and b["pct_chg"] == 1.30)
    check("date 钉成请求日", all(r["date"] == D1 for r in rows))
    check("PS 有值（本表存在的理由）", b["ps_ttm"] == 10.42)
    check("src 留痕", all(r["src"] == "eastmoney" for r in rows))

    # 分页：pages=3 时要翻 3 次
    f = make_fetcher(fake_rows(), pages=3)
    source.fetch_day(D1, get=f)
    check("pages=3 → 翻 3 页", len(f.calls) == 3, f"calls={len(f.calls)}")
    check("第 2 页带了 pageNumber=2", "pageNumber=2" in f.calls[1], f.calls[1][:120])
    check("filter 里钉死日期", "TRADE_DATE%3D%272026-11-16%27" in f.calls[0]
          or "TRADE_DATE='2026-11-16'" in f.calls[0].replace("%27", "'"),
          f.calls[0][:200])

    # 空：9201
    empty = json.dumps({"success": False, "code": "9201", "message": "返回数据为空"})
    check("9201 → 空列表（不是异常）",
          source.fetch_day(D1, get=lambda url, referer="": empty) == [])

    # 接口报错（非 9201）要抛
    bad = json.dumps({"success": False, "code": "9501", "message": "columns 不对"})
    try:
        source.fetch_day(D1, get=lambda url, referer="": bad)
        check("非 9201 的 success:false 抛 SourceError", False, "没抛")
    except source.SourceError:
        check("非 9201 的 success:false 抛 SourceError", True)

    # 混进别的日期必须拦
    mix = fake_rows(D1) + [raw_row("600000.SH", "浦发银行", 8.0, 1.0, 5.0, 0.5, 1.0,
                                  1e11, 1e11, D2)]
    try:
        source.fetch_day(D1, get=make_fetcher(mix))
        check("混进别的交易日 → 抛", False, "没抛")
    except source.SourceError as e:
        check("混进别的交易日 → 抛", "别的交易日" in str(e), str(e)[:80])

    # 不是 JSON
    try:
        source.fetch_day(D1, get=lambda url, referer="": "<html>502</html>")
        check("非 JSON → 抛", False, "没抛")
    except source.SourceError:
        check("非 JSON → 抛", True)

    # 缺列 / 脏值：数值缺失给 None，不填 0
    dirty = [{"SECUCODE": "600519.SH", "TRADE_DATE": f"{D1} 00:00:00",
              "SECURITY_NAME_ABBR": "贵州茅台", "CLOSE_PRICE": 1453.9,
              "CHANGE_RATE": None, "PE_TTM": "-", "PB_MRQ": "", "PS_TTM": None,
              "TOTAL_MARKET_CAP": 1.8e12, "NOTLIMITED_MARKETCAP_A": None}]
    got = source.fetch_day(D1, get=make_fetcher(dirty))
    check("缺失的估值给 None（不填 0）",
          got[0]["pe_ttm"] is None and got[0]["pb_mrq"] is None
          and got[0]["pct_chg"] is None and got[0]["circ_mv"] is None)

    check("无法识别的 SECUCODE 被丢弃",
          source._symbol_of("ABCDEF") is None and source._symbol_of("600519.HK") is None)
    check("北交所要保留", source._symbol_of("920992.BJ") == "bj920992")

    # 单只：代码归一化 + SECUCODE 过滤 + 只返回目标那只
    check("代码归一化：纯数字/带市场/带后缀",
          source.normalize_symbol("002096") == "sz002096"
          and source.normalize_symbol("sz002096") == "sz002096"
          and source.normalize_symbol("002096.SZ") == "sz002096"
          and source.normalize_symbol("600519.SH") == "sh600519"
          and source.normalize_symbol("920992.BJ") == "bj920992")
    check("代码归一化：认不出返回空",
          source.normalize_symbol("ABCDEF") == ""
          and source.normalize_symbol("12345") == "")
    single_calls: list[str] = []

    def single_reader(url: str, *, referer: str = "") -> str:
        single_calls.append(url)
        return fake_payload([raw_row("002096.SZ", "某只票", 12.34, 1.23,
                                     15.0, 1.8, 2.0, 1e10, 8e9)])

    one = source.fetch_symbol(D1, "002096", get=single_reader)
    check("单只：返回目标股票", one is not None and one["symbol"] == "sz002096"
          and one["close"] == 12.34, str(one))
    check("单只：URL 带 SECUCODE 过滤",
          "SECUCODE" in single_calls[0]
          and "002096.SZ" in single_calls[0].replace("%27", "'"),
          single_calls[0][:200])
    check("单只：只请求一页", len(single_calls) == 1, str(len(single_calls)))
    check("单只：上游空 → None",
          source.fetch_symbol(D1, "002096",
                              get=lambda url, referer="": json.dumps(
                                  {"success": False, "code": "9201",
                                   "message": "返回数据为空"})) is None)
    try:
        source.fetch_symbol(D1, "ABCDEF")
        check("单只：代码认不出 → SourceError", False, "没抛")
    except source.SourceError:
        check("单只：代码认不出 → SourceError", True)

    # 沪深300：单独接口、单独解析
    kline = json.dumps({"rc": 0, "data": {
        "name": "沪深300",
        "klines": [f"{D1},3984.77,3979.80,3997.40,3959.93,185439196,"
                   "430554181504.80,0.93,-0.91,-36.49,0.55"]}})
    calls: list[str] = []

    def idx_reader(url: str, *, referer: str = "") -> str:
        calls.append(url)
        return kline

    idx = source.fetch_index(D1, get=idx_reader)
    check("沪深300：收盘/涨跌幅映射",
          idx and idx["close"] == 3979.80 and idx["pct_chg"] == -0.91,
          str(idx))
    check("沪深300：symbol/name 固定", idx and idx["symbol"] == "sh000300"
          and idx["name"] == "沪深300", str(idx))
    check("沪深300：URL 钉死 secid 与当天",
          "secid=1.000300" in calls[0] and "beg=20261116" in calls[0]
          and "end=20261116" in calls[0], calls[0][:160])
    check("沪深300：K 线日期不是请求日 → None",
          source.fetch_index(D2, get=idx_reader) is None)
    empty_idx = json.dumps({"rc": 0, "data": None})
    check("沪深300：非交易日 data:null → None",
          source.fetch_index(D1, get=lambda url, referer="": empty_idx) is None)
    try:
        source.fetch_index(D1, get=lambda url, referer="": '{"rc": 102}')
        check("沪深300：rc 非 0 抛 SourceError", False, "没抛")
    except source.SourceError:
        check("沪深300：rc 非 0 抛 SourceError", True)


# ---------------------------------------------------------------- 2 口径

def test_stat() -> None:
    print("\n2 冷热摘要口径（stat.py）")
    check("主板涨停阈值 9.8", stat.limit_pct("sh600519") == 9.8)
    check("创业板 19.8", stat.limit_pct("sz300750") == 19.8)
    check("科创板 19.8", stat.limit_pct("sh688981") == 19.8)
    check("北交所 29.8", stat.limit_pct("bj920992") == 29.8)
    check("ST 主板 4.8", stat.limit_pct("sh600001", "*ST华幸") == 4.8)
    check("ST 创业板仍是 4.8（ST 优先于板块）",
          stat.limit_pct("sz300001", "ST某某") == 4.8)
    check("北交所先于 ST（那里没有 5% 限制）",
          stat.limit_pct("bj920992", "*ST某某") == 29.8)

    rows = [
        {"date": D1, "symbol": "sh600001", "name": "涨停A", "pct_chg": 9.95,
         "pe_ttm": 10.0, "pb_mrq": 1.0, "ps_ttm": 1.0, "total_mv": 100.0},
        {"date": D1, "symbol": "sz000002", "name": "跌停B", "pct_chg": -10.0,
         "pe_ttm": -5.0, "pb_mrq": 2.0, "ps_ttm": 2.0, "total_mv": 200.0},
        {"date": D1, "symbol": "sz300003", "name": "平C", "pct_chg": 0.0,
         "pe_ttm": 30.0, "pb_mrq": None, "ps_ttm": 3.0, "total_mv": 300.0},
        {"date": D1, "symbol": "bj920001", "name": "停D", "pct_chg": None,
         "pe_ttm": None, "pb_mrq": None, "ps_ttm": None, "total_mv": None},
    ]
    s = stat.summarize(rows, src="eastmoney", updated_at="2026-11-17T10:00:00")
    check("涨跌平家数", (s["n_up"], s["n_down"], s["n_flat"]) == (1, 1, 1),
          str((s["n_up"], s["n_down"], s["n_flat"])))
    check("涨停/跌停各 1", (s["n_limit_up"], s["n_limit_down"]) == (1, 1))
    check("停牌不计入涨跌家数但计入 n", s["n"] == 4)
    check("PE 中位数只取正值（-5 被丢）", s["pe_median"] == 20.0, str(s["pe_median"]))
    check("PE 只数 = 2", s["n_pe"] == 2, str(s["n_pe"]))
    check("PB 只统计正值（两个）", s["pb_median"] == 1.5 and s["n_pb"] == 2,
          f"{s['pb_median']} / {s['n_pb']}")
    check("总市值合计跳过 None", s["mv_sum"] == 600.0, str(s["mv_sum"]))

    try:
        stat.summarize([{"date": D1, "symbol": "sh600000"},
                        {"date": D2, "symbol": "sh600001"}])
        check("多天混算 → 抛 ValueError", False, "没抛")
    except ValueError:
        check("多天混算 → 抛 ValueError", True)


# ---------------------------------------------------------------- 3 落库

def test_store(root: Path) -> None:
    print("\n3 落库（shared 只有 price；results 有 price + price_stat）")
    shared_p = root / "shared.db"
    results_p = root / "results.db"

    sh = store.connect(shared_p)
    store.ensure(sh, "shared")
    rs = store.connect(results_p)
    store.ensure(rs, "results")

    def tables(conn):
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    check("shared.db 只有 price（没有 price_stat）",
          "price" in tables(sh) and "price_stat" not in tables(sh),
          str(sorted(tables(sh))))
    check("results.db 有 price + price_stat",
          {"price", "price_stat"} <= tables(rs), str(sorted(tables(rs))))
    check("两个库都有 index_price",
          {"index_price"} <= tables(sh) and {"index_price"} <= tables(rs),
          f"shared={sorted(tables(sh))} results={sorted(tables(rs))}")

    rows = source.fetch_day(D1, get=make_fetcher(fake_rows()))
    n = store.write_day(sh, D1, rows)
    check("写原始库 3 行", n == 3 and store.count(sh, D1) == 3, f"n={n}")
    idx = {"date": D1, "symbol": "sh000300", "name": "沪深300",
           "close": 3979.80, "pct_chg": -0.91, "src": "eastmoney"}
    check("写沪深300 一行", store.write_index(sh, D1, idx) == 1
          and (store.index_row(sh, D1) or {}).get("close") == 3979.80)
    check("写指数不认错日期", store.write_index(sh, D2, idx) == 0)

    # 整天替换：第二次只回 2 只，第 3 只必须消失
    fewer = [r for r in rows if r["symbol"] != "sz300750"]
    store.write_day(sh, D1, fewer)
    check("整天整份替换（第 3 只消失）", store.count(sh, D1) == 2,
          f"count={store.count(sh, D1)}")

    # 复制到结果库 + 摘要同事务
    store.write_day(rs, D1, rows, with_stat=True)
    check("结果库 price 3 行", store.count(rs, D1) == 3)
    s = rs.execute("SELECT * FROM price_stat WHERE date=?", (D1,)).fetchone()
    check("price_stat 有这一天的行", s is not None)
    check("摘要的家数与价格一致（同事务）", s and s["n"] == 3, f"n={s['n'] if s else None}")
    check("摘要 PE 中位 = 22.12（三个正值的中位，不是均值）",
          abs((s["pe_median"] or 0) - 22.12) < 1e-9,
          str(s["pe_median"] if s else None))

    # 另一天
    rows2 = source.fetch_day(D2, get=make_fetcher(fake_rows(D2)))
    for r in rows2:
        r["pct_chg"] = -1.0
    store.write_day(sh, D2, rows2)
    check("第二天独立入库", store.count(sh, D2) == 3 and store.count(sh, D1) == 2)
    check("days() 新→旧", store.days(sh) == [D1, D2], str(store.days(sh)))
    check("last_day / last_day(before)", store.last_day(sh) == D1
          and store.last_day(sh, before=D1) == D2,
          f"{store.last_day(sh)} / {store.last_day(sh, before=D1)}")

    # sync：结果库缺 D2，补上（含摘要）
    done = store.sync(sh, rs)
    check("sync 补上缺的那天 + 覆盖已有的", set(done) == {D1, D2}, str(done))
    check("结果库两天都在", store.count(rs, D1) == 2 and store.count(rs, D2) == 3)
    st2 = rs.execute("SELECT n FROM price_stat WHERE date=?", (D2,)).fetchone()
    check("同步时顺带算摘要", st2 is not None and st2[0] == 3, str(st2[0] if st2 else None))
    check("同步时带上沪深300", (store.index_row(rs, D1) or {}).get("close") == 3979.80,
          str(store.index_row(rs, D1)))
    check("同步后结果库与原始库行数一致",
          store.count(rs, D1) == store.count(sh, D1))

    # restat：改口径 / 老库没摘要 → 不联网重算
    rs.execute("DELETE FROM price_stat")
    rs.commit()
    n = store.restat(rs)
    check("restat 重算 2 天", n == 2, f"n={n}")
    check("restat 后两天都有摘要",
          rs.execute("SELECT COUNT(*) FROM price_stat").fetchone()[0] == 2)
    n1 = store.restat(rs, D1)
    check("restat 指定单天", n1 == 1, f"n={n1}")

    # prune
    sh.execute("INSERT OR REPLACE INTO price(date,symbol,updated_at) "
               "VALUES('2026-01-05','sh600000','x')")
    sh.commit()
    rs.execute("INSERT OR REPLACE INTO price(date,symbol,updated_at) "
               "VALUES('2026-01-05','sh600000','x')")
    rs.commit()
    store.write_index(sh, "2026-01-05", {"date": "2026-01-05",
                                        "symbol": "sh000300",
                                        "name": "沪深300", "close": 1.0,
                                        "pct_chg": 0.0})
    cut = store.prune(sh, 30, today="2026-11-17")
    check("prune 删掉 30 天前的行", cut == 1 and store.count(sh, "2026-01-05") == 0,
          f"cut={cut}")
    check("prune 连旧指数一起删", store.index_row(sh, "2026-01-05") is None)
    store.prune(rs, 30, today="2026-11-17")
    check("prune 结果库同步删", store.count(rs, "2026-01-05") == 0)
    check("prune keep<=0 不动", store.prune(sh, 0) == 0)

    sh.close()
    rs.close()


# ---------------------------------------------------------------- 4 CLI

def test_cli(root: Path) -> None:
    print("\n4 CLI 端到端（离线）")
    shared_p = root / "cli-shared.db"
    results_p = root / "cli-results.db"
    base = ["--shared-db", str(shared_p), "--results-db", str(results_p),
            "--calendar", str(root / "nope.json")]

    calls = {"n": 0}
    real_fetch = source.fetch_day
    real_index = source.fetch_index
    index_calls = {"n": 0}

    def fake_fetch(day, **kw):
        calls["n"] += 1
        return real_fetch(day, get=make_fetcher(fake_rows(day)))

    def fake_index(day, **kw):
        index_calls["n"] += 1
        return {"date": day, "symbol": "sh000300", "name": "沪深300",
                "close": 3979.80, "pct_chg": -0.91, "src": "test"}

    source.fetch_day = fake_fetch                     # type: ignore[assignment]
    source.fetch_index = fake_index                   # type: ignore[assignment]
    try:
        rc = cli.main(base + ["--date", D1])
        check("首次运行退出码 0", rc == 0, f"rc={rc}")
        sh = sqlite3.connect(shared_p)
        rs = sqlite3.connect(results_p)
        check("原始库有 3 行", sh.execute(
            "SELECT COUNT(*) FROM price").fetchone()[0] == 3)
        check("结果库有 3 行 + 1 行摘要",
              rs.execute("SELECT COUNT(*) FROM price").fetchone()[0] == 3
              and rs.execute("SELECT COUNT(*) FROM price_stat").fetchone()[0] == 1)
        check("首次运行写入沪深300（原始库 + 结果库）",
              sh.execute("SELECT close FROM index_price").fetchone()[0] == 3979.80
              and rs.execute("SELECT close FROM index_price").fetchone()[0] == 3979.80)
        sh.close()
        rs.close()

        # 第二次：库里已有 → 不联网（fetch/index 次数都不增加）
        before = calls["n"]
        before_idx = index_calls["n"]
        rc = cli.main(base + ["--date", D1])
        check("已有数据时跳过取数（不联网）", rc == 0 and calls["n"] == before
              and index_calls["n"] == before_idx,
              f"rc={rc} calls={calls['n'] - before} idx={index_calls['n'] - before_idx}")

        # --force 强制重取（价格与指数都重取）
        rc = cli.main(base + ["--date", D1, "--force"])
        check("--force 真的重取", rc == 0 and calls["n"] == before + 1
              and index_calls["n"] == before_idx + 1,
              f"rc={rc} calls={calls['n'] - before} idx={index_calls['n'] - before_idx}")

        # 上游空 → 退出码 1
        source.fetch_day = lambda day, **kw: []        # type: ignore[assignment]
        rc = cli.main(base + ["--date", D2, "--force"])
        check("上游没数据 → 退出码 1", rc == 1, f"rc={rc}")

        # 日期格式错 → 退出码 2
        rc = cli.main(base + ["--date", "2026/11/16"])
        check("日期格式错 → 退出码 2", rc == 2, f"rc={rc}")

        # --sync 把原始库已有的日子补到结果库
        source.fetch_day = fake_fetch                  # type: ignore[assignment]
        rs = sqlite3.connect(results_p)
        rs.execute("DELETE FROM price_stat")
        rs.commit()
        rs.close()
        rc = cli.main(base + ["--sync"])
        check("--sync 退出码 0", rc == 0, f"rc={rc}")
        rs = sqlite3.connect(results_p)
        check("--sync 补回摘要", rs.execute(
            "SELECT COUNT(*) FROM price_stat").fetchone()[0] == 1)
        check("--sync 补回沪深300", rs.execute(
            "SELECT COUNT(*) FROM index_price").fetchone()[0] == 1)
        rs.close()

        # --restat
        rc = cli.main(base + ["--restat"])
        check("--restat 退出码 0", rc == 0, f"rc={rc}")

        # --code：只 upsert 这一只，不整天替换
        import io
        from contextlib import redirect_stdout
        real_symbol = source.fetch_symbol
        source.fetch_symbol = lambda day, code, **kw: {  # type: ignore[assignment]
            "date": D1, "symbol": "sz002096", "name": "某只票",
            "close": 12.34, "pct_chg": 1.23, "pe_ttm": 15.0, "pb_mrq": 1.8,
            "ps_ttm": 2.0, "total_mv": 1e10, "circ_mv": 8e9,
            "src": "test"}
        try:
            sh = sqlite3.connect(shared_p)
            rs = sqlite3.connect(results_p)
            before_rows = (sh.execute("SELECT COUNT(*) FROM price").fetchone()[0],
                           rs.execute("SELECT COUNT(*) FROM price").fetchone()[0])
            sh.close()
            rs.close()
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(base + ["--code", "002096", "--date", D1])
            out = buf.getvalue()
            check("--code 退出码 0", rc == 0, f"rc={rc}")
            check("--code 打印名称/收盘/写入说明",
                  "某只票" in out and "12.34" in out and "price 1 行" in out, out[:240])
            sh = sqlite3.connect(shared_p)
            rs = sqlite3.connect(results_p)
            after_rows = (sh.execute("SELECT COUNT(*) FROM price").fetchone()[0],
                          rs.execute("SELECT COUNT(*) FROM price").fetchone()[0])
            stat_n = rs.execute("SELECT n FROM price_stat WHERE date=?",
                                (D1,)).fetchone()[0]
            sh.close()
            rs.close()
            check("--code 只多写一只（不清掉当天其它行）",
                  after_rows == (before_rows[0] + 1, before_rows[1] + 1),
                  f"{before_rows} → {after_rows}")
            check("--code 重算当天摘要（3 只 + 新增 1 只）", stat_n == 4,
                  f"n={stat_n}")
        finally:
            source.fetch_symbol = real_symbol          # type: ignore[assignment]
        with redirect_stdout(io.StringIO()):
            rc = cli.main(base + ["--code", "ABCDEF", "--date", D1])
        check("--code 非法代码 → 退出码 2", rc == 2, f"rc={rc}")
        with redirect_stdout(io.StringIO()):
            rc = cli.main(base + ["--code", "002096", "--date", D1, "--force"])
        check("--code + --force → 退出码 2", rc == 2, f"rc={rc}")
    finally:
        source.fetch_day = real_fetch                  # type: ignore[assignment]
        source.fetch_index = real_index                # type: ignore[assignment]

    # 交易日历：定"今天"，以及非交易日直接拦
    cal = C.Calendar(["2026-11-12", "2026-11-13", "2026-11-16"], end="2026-11-16")
    check("周六 → 上一个交易日（周五）",
          C.target(cal, dt.datetime(2026, 11, 14, 10, 0)) == D2,
          C.target(cal, dt.datetime(2026, 11, 14, 10, 0)))
    check("交易日但没到 15:05 → 上一个交易日",
          C.target(cal, dt.datetime(2026, 11, 16, 10, 11)) == D2,
          C.target(cal, dt.datetime(2026, 11, 16, 10, 11)))
    check("交易日且过了 15:05 → 今天",
          C.target(cal, dt.datetime(2026, 11, 16, 16, 0)) == D1)
    check("日历读不到 → None（不抛）", C.Calendar.load(root / "nope.json") is None)
    check("非交易日 → 退出码 2",
          cli.main(base + ["--date", "2026-11-14",
                            "--calendar", str(cal_file(root))]) == 2)


def cal_file(root: Path) -> Path:
    p = root / "cal.json"
    p.write_text(json.dumps({"span": "2026-11-12", "end": "2026-11-16",
                             "days": ["2026-11-12", "2026-11-13", "2026-11-16"]}),
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------- 5 schema

def test_schema() -> None:
    print("\n5 建表脚本")
    check("shared 的 DDL 有三条（price + index_price + 一个索引）",
          len(schema.SHARED_DDL) == 3)
    check("results 的 DDL 有四条（price + index_price + 索引 + price_stat）",
          len(schema.RESULTS_DDL) == 4)
    check("shared 不建 price_stat",
          not any("price_stat" in s for s in schema.SHARED_DDL))
    check("两个库的 price 列顺序一致（同一段 DDL）",
          schema.PRICE_DDL in schema.SHARED_DDL and schema.PRICE_DDL in schema.RESULTS_DDL)
    check("两个库的 index_price 定义一致",
          schema.INDEX_TABLE_DDL in schema.SHARED_DDL
          and schema.INDEX_TABLE_DDL in schema.RESULTS_DDL)
    check("索引名不重名（ATTACH 在一起也不冲突）",
          "ix_price_symbol" in schema.SHARED_DDL[2]
          and "ix_r_price_symbol" in schema.RESULTS_DDL[2])
    check("price 主键是 (date, symbol)", "PRIMARY KEY (date, symbol)" in schema.PRICE_DDL)


def main() -> int:
    root = tmp_root()
    print("=" * 60)
    print("stock_extreme_point 离线测试")
    print("=" * 60)
    try:
        test_source()
        test_stat()
        test_store(root)
        test_cli(root)
        test_schema()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\n" + "=" * 60)
    if FAIL:
        print(f"\033[31m通过 {PASS} 项，失败 {len(FAIL)} 项：{'、'.join(FAIL)}\033[0m")
        return 1
    print(f"\033[32m通过 {PASS} 项，失败 0 项\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
