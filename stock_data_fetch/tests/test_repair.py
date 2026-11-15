"""repair 测试（**离线**：上游打假）。

跑法：

    cd /home/kevin/MyData/stock-value-invest-tool
    python3 -m stock_data_fetch.tests.test_repair

repair 是唯一一个**会改写历史行**的维护工具，所以测试要盯死两件事：

1. **只修"能证明是漏记"的行**——认不出签名就不碰（换源差异、上游真改数，都不属于）；
2. **改了必有痕**——`meta` 里要有旧的、新的、原始政策原文。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import db, repair                        # noqa: E402
from stock_data_fetch.config import DEFAULT_SCHEMA, Config    # noqa: E402
from stock_data_fetch.errors import SourceUnavailable         # noqa: E402
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


def plan(ex: str, cash10: float, text: str) -> dict:
    """一条上游分红方案。`cash10` 是"每 10 股派 X 元"的 X。"""
    return {"exDiviDate": ex, "cashDiviRMB": cash10, "dividendPlan": text}


def mkdb(rows: list[tuple]) -> Path:
    tmp = mkdtemp("stockhub-repair-")
    dbp = tmp / "shared.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)
    with db.tx(conn):
        for sym, ex, cash, bonus, transfer, text in rows:
            conn.execute(
                "INSERT INTO dividend(symbol,ex_date,cash_per_share,bonus_per_share,"
                "transfer_per_share,rights_per_share,rights_price,plan_text,src,updated_at) "
                "VALUES(?,?,?,?,?,0,NULL,?,'tencent','2026-11-14T00:00:00')",
                (sym, ex, cash, bonus, transfer, text))
    conn.close()
    return dbp


def mksum(*plans: dict, symbol: str = "sh601318") -> list[dict]:
    """上游原始 plan 列表 → 聚合后的行（供断言用）。"""
    return W._dividend_rows(symbol, list(plans))


# ================================================================ 1. 判据

def test_find_fixes() -> None:
    section("1. find_fixes：只认「库里的值恰好等于上游其中一条」")

    ex = "2018-06-07"
    plans = [plan(ex, 10.0, "10派10.000元"), plan(ex, 2.0, "10派2.000元")]

    # 库里只记了一条 → 该修
    dbp = mkdb([("sh601318", ex, 1.0, 0.0, 0.0, "10派10.000元")])
    conn = db.connect(dbp, readonly=True)
    fixes = repair.find_fixes(conn, "sh601318", plans)
    conn.close()
    check("漏记一条 → 命中 1 行", len(fixes) == 1, str(fixes))
    check("旧值 = 1.0、新值 = 1.2", fixes and fixes[0].old[0] == 1.0 and fixes[0].new[0] == 1.2,
          str(fixes[0].old) if fixes else "")
    check("留住了上游两条政策原文",
          fixes and fixes[0].plans == ["10派10.000元", "10派2.000元"], str(fixes[0].plans))
    check("plan_text 拼成「A + B」", fixes and fixes[0].text == "10派10.000元 + 10派2.000元",
          fixes[0].text if fixes else "")

    # 库里已经是合计 → 不该修（幂等）
    dbp2 = mkdb([("sh601318", ex, 1.2, 0.0, 0.0, "10派10.000元 + 10派2.000元")])
    conn = db.connect(dbp2, readonly=True)
    check("已经是合计值 → 无待修", repair.find_fixes(conn, "sh601318", plans) == [])
    conn.close()

    # 上游只有一条方案 → 没有"漏记"这回事
    dbp3 = mkdb([("sh601318", ex, 1.0, 0.0, 0.0, "10派10.000元")])
    conn = db.connect(dbp3, readonly=True)
    check("上游只有一条方案 → 不动",
          repair.find_fixes(conn, "sh601318", [plan(ex, 10.0, "10派10.000元")]) == [])
    conn.close()

    # 库里的值对不上任何一条方案 → 认不出签名，不碰（这是"上游真改数"的地盘）
    dbp4 = mkdb([("sh601318", ex, 1.15, 0.0, 0.0, "10派11.500元")])
    conn = db.connect(dbp4, readonly=True)
    check("库里的值不等于任何单条方案 → 不动",
          repair.find_fixes(conn, "sh601318", plans) == [])
    conn.close()

    # 合计不比库里大 → 没什么可补（同日第二条是"10派0.000元"这种空方案）
    dbp5 = mkdb([("sh601318", ex, 1.0, 0.0, 0.0, "10派10.000元")])
    conn = db.connect(dbp5, readonly=True)
    same = [plan(ex, 10.0, "10派10.000元"), plan(ex, 0.0, "10派0.000元")]
    check("合计不比库里大 → 不动", repair.find_fixes(conn, "sh601318", same) == [])
    conn.close()

    # 库里没有这一行 → 归 ensure 管，repair 不越界
    dbp6 = mkdb([("sh601318", "2020-01-02", 0.5, 0.0, 0.0, "10派5.000元")])
    conn = db.connect(dbp6, readonly=True)
    check("库里压根没这行 → 不动（补行是 ensure 的活）",
          repair.find_fixes(conn, "sh601318", plans) == [])
    conn.close()

    # 送转也照样相加
    ex2 = "2024-04-30"
    dbp7 = mkdb([("sz300750", ex2, 2.011, 0.0, 0.0, "10派20.110元")])
    conn = db.connect(dbp7, readonly=True)
    f = repair.find_fixes(conn, "sz300750",
                          [plan(ex2, 20.11, "10派20.110元"), plan(ex2, 30.17, "10派30.170元")])
    check("送转/派现混合也相加（宁德 5.028）", f and abs(f[0].new[0] - 5.028) < 1e-9,
          str(f[0].new) if f else "未命中")
    conn.close()


# ================================================================ 2. 落库与留痕

def test_apply() -> None:
    section("2. apply：写回 + 留痕 + 幂等")

    ex = "2018-06-07"
    plans = [plan(ex, 10.0, "10派10.000元"), plan(ex, 2.0, "10派2.000元")]
    dbp = mkdb([("sh601318", ex, 1.0, 0.0, 0.0, "10派10.000元")])

    conn = db.connect(dbp)
    with patch.object(W, "call", lambda *a, **k: {"plans": plans}):
        fixes = repair.repair_symbol(conn, "sh601318", apply=True)
    check("repair_symbol 返回修了什么", len(fixes) == 1, str(fixes))

    row = conn.execute("SELECT cash_per_share,plan_text,updated_at FROM dividend "
                       "WHERE symbol='sh601318' AND ex_date=?", (ex,)).fetchone()
    check("库里的值改成 1.2", row["cash_per_share"] == 1.2, str(row["cash_per_share"]))
    check("plan_text 也更新成两条", row["plan_text"] == "10派10.000元 + 10派2.000元",
          row["plan_text"])
    check("updated_at 被刷新（复权因子的缓存签名靠它）",
          row["updated_at"] != "2026-11-14T00:00:00", row["updated_at"])

    audit = db.meta_get(conn, f"dividend_repair/sh601318/{ex}")
    check("meta 里留了痕", audit is not None, str(audit))
    payload = json.loads(audit or "{}")
    check("留痕含旧值/新值/原文/时间",
          payload.get("old") == [1.0, 0.0, 0.0, 0.0]
          and payload.get("new") == [1.2, 0.0, 0.0, 0.0]
          and payload.get("plans") == ["10派10.000元", "10派2.000元"]
          and bool(payload.get("at")), str(payload))

    with patch.object(W, "call", lambda *a, **k: {"plans": plans}):
        again = repair.repair_symbol(conn, "sh601318", apply=True)
    check("再跑一次 → 无待修（幂等）", again == [], str(again))
    n = conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'dividend_repair/%'").fetchone()[0]
    check("留痕不重复写", n == 1, str(n))
    conn.close()


def test_dry_run() -> None:
    section("3. 默认 dry-run：只看不写")

    ex = "2018-06-07"
    plans = [plan(ex, 10.0, "10派10.000元"), plan(ex, 2.0, "10派2.000元")]
    dbp = mkdb([("sh601318", ex, 1.0, 0.0, 0.0, "10派10.000元")])

    conn = db.connect(dbp)
    with patch.object(W, "call", lambda *a, **k: {"plans": plans}):
        fixes = repair.repair_symbol(conn, "sh601318", apply=False)
    check("dry-run 也报告要修什么", len(fixes) == 1, str(fixes))
    row = conn.execute("SELECT cash_per_share FROM dividend WHERE symbol='sh601318'").fetchone()
    check("dry-run 一个字节都没写", row["cash_per_share"] == 1.0, str(row["cash_per_share"]))
    check("dry-run 不写留痕",
          db.meta_get(conn, f"dividend_repair/sh601318/{ex}") is None)
    conn.close()

    # run() 在 dry-run 时用只读连接 —— 想写也写不了
    cfg = Config()
    cfg.db_path = dbp
    cfg.schema_path = DEFAULT_SCHEMA
    with patch.object(W, "call", lambda *a, **k: {"plans": plans}):
        fixes = repair.run(cfg=cfg)
    check("run(dry-run) 能报告", len(fixes) == 1, str(fixes))
    conn = db.connect(dbp, readonly=True)
    check("run(dry-run) 没改库",
          conn.execute("SELECT cash_per_share FROM dividend").fetchone()[0] == 1.0)
    conn.close()


def test_robust() -> None:
    section("4. 上游异常 / 空表")

    dbp = mkdb([("sh601318", "2018-06-07", 1.0, 0.0, 0.0, "10派10.000元")])
    conn = db.connect(dbp)
    with patch.object(W, "call",
                      lambda *a, **k: (_ for _ in ()).throw(SourceUnavailable("封了"))):
        check("上游挂了 → 返回空、不抛", repair.repair_symbol(conn, "sh601318", apply=True) == [])
    with patch.object(W, "call", lambda *a, **k: {"plans": []}):
        check("上游返回空方案 → 返回空", repair.repair_symbol(conn, "sh601318") == [])
    with patch.object(W, "call", lambda *a, **k: None):
        check("上游返回 None → 返回空", repair.repair_symbol(conn, "sh601318") == [])
    row = conn.execute("SELECT cash_per_share FROM dividend").fetchone()
    check("上游挂了的这几次都没改库", row["cash_per_share"] == 1.0)
    conn.close()

    empty = mkdb([])
    cfg = Config()
    cfg.db_path = empty
    cfg.schema_path = DEFAULT_SCHEMA
    check("空表 → 什么都不做", repair.run(cfg=cfg) == [])


# ================================================================ 5. CLI

def test_cli() -> None:
    section("5. CLI 与渲染")

    ex = "2018-06-07"
    plans = [plan(ex, 10.0, "10派10.000元"), plan(ex, 2.0, "10派2.000元")]
    dbp = mkdb([("sh601318", ex, 1.0, 0.0, 0.0, "10派10.000元")])

    f = repair.Fix("sh601318", ex, (1.0, 0.0, 0.0, 0.0), (1.2, 0.0, 0.0, 0.0),
                   ["10派10.000元", "10派2.000元"])
    text = repair.render([f], applied=False)
    check("dry-run 文案强调「没写库」", "没写库" in text and "--apply" in text, text[:60])
    check("行里带旧值→新值", "1.0000 → 1.2000" in text, text[:120])
    check("无待修时给结论", "没有需要修的行" in repair.render([], applied=True))

    buf = io.StringIO()
    with patch.object(W, "call", lambda *a, **k: {"plans": plans}), \
         contextlib.redirect_stdout(buf):
        rc = repair.main(["dividend", "--symbols", "sh601318", "--db", str(dbp)])
    out = buf.getvalue()
    check("CLI 默认 dry-run 退出码 0", rc == 0, str(rc))
    check("CLI 提示还差 --apply", "--apply" in out, out[-80:])
    conn = db.connect(dbp, readonly=True)
    check("CLI dry-run 没写库",
          conn.execute("SELECT cash_per_share FROM dividend").fetchone()[0] == 1.0)
    conn.close()

    buf2 = io.StringIO()
    with patch.object(W, "call", lambda *a, **k: {"plans": plans}), \
         contextlib.redirect_stdout(buf2):
        rc = repair.main(["dividend", "--symbols", "sh601318", "--db", str(dbp), "--apply"])
    check("CLI --apply 退出码 0", rc == 0, str(rc))
    conn = db.connect(dbp, readonly=True)
    check("CLI --apply 真的写了",
          conn.execute("SELECT cash_per_share FROM dividend").fetchone()[0] == 1.2)
    conn.close()
    check("--apply 文案说「已修」", "已修" in buf2.getvalue(), buf2.getvalue()[:60])


def main() -> int:
    test_find_fixes()
    test_apply()
    test_dry_run()
    test_robust()
    test_cli()

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
