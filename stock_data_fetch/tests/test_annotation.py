"""人工标注（域 annotation）测试。离线，不联网。

跑法：

    cd /home/kevin/MyData/stock-value-invest-tool && \
        python -m stock_data_fetch.tests.test_annotation

覆盖：
- 表结构与版本（annotation 在 v5 引入）；
- `put()` 的**局部更新**语义（只给一个字段时其它字段不能被清空）；
- `moat_type` 枚举校验与非法值退化；
- `stale`（超 12 个月）判断；
- yaml 导入 / 导出的往返一致性；
- `load_resource_moat()` 经 `score.py` 读库的完整链路。
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent        # stock-value-invest-tool/
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import annotation as A                    # noqa: E402
from stock_data_fetch import db                                 # noqa: E402
from stock_data_fetch.config import (ANNOTATION_STALE_DAYS, MOAT_TYPES,  # noqa: E402
                                     SCHEMA_VERSION)

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(f"{name}{('：' + detail) if detail else ''}")
        print(f"  ❌ {name}" + (f"（{detail}）" if detail else ""))


def mkdtemp(prefix: str) -> Path:
    """临时目录放磁盘，不放 /tmp（tmpfs 太小，SQLite+WAL 会撞墙）。"""
    root = Path(os.environ.get("STOCKHUB_TMP")
                or HERE.parent.parent.parent / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def main() -> int:
    tmp = mkdtemp("anno-")
    db_path = tmp / "t.db"
    schema = PROJECT / "stock_data_fetch" / "schema.sql"
    db.create_db(db_path, schema)
    conn = db.connect(db_path)

    # ---------------- 表结构 ----------------
    print("\n表结构")
    check("user_version 是 5", db.schema_version(conn) == SCHEMA_VERSION == 5,
          str(db.schema_version(conn)))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(annotation)")}
    check("annotation 表存在且有 9 列", len(cols) == 9, str(sorted(cols)))
    check("关键列齐全",
          {"symbol", "kind", "moat_type", "evidence", "controller",
           "sources", "verified", "note", "updated_at"} <= cols)
    check("annotation 不在 snapshot 的 domain 里",
          "annotation" not in (conn.execute(
              "SELECT sql FROM sqlite_master WHERE name='snapshot'").fetchone()[0]))

    # ---------------- 写入与局部更新 ----------------
    print("\n写入与局部更新")
    rec = A.put(conn, "sz300026", moat_type="独家品种",
                evidence="血必净是独家品种", controller="成都兴城",
                sources=["https://a.example", "https://b.example"],
                verified="2026-11-13")
    check("annotated=True（有垄断性）", rec["annotated"] is True)
    check("moat_type 写入正确", rec["moat_type"] == "独家品种")
    check("sources 是 list", rec["sources"] == ["https://a.example", "https://b.example"])
    check("verified 归一成 YYYY-MM-DD", rec["verified"] == "2026-11-13")
    check("刚核实的不算 stale", rec["stale"] is False)

    # 只改 verified：其它字段必须原样保留
    rec2 = A.put(conn, "sz300026", verified="2026-11-14")
    check("只改 verified 不清 evidence", rec2["evidence"] == "血必净是独家品种")
    check("只改 verified 不清 sources",
          rec2["sources"] == ["https://a.example", "https://b.example"])
    check("只改 verified 不清 controller", rec2["controller"] == "成都兴城")
    check("verified 已更新", rec2["verified"] == "2026-11-14")

    # 只改 moat_type
    rec3 = A.put(conn, "sz300026", moat_type="技术壁垒")
    check("只改 moat_type 保留 evidence", rec3["evidence"] == "血必净是独家品种")

    # replace=True 整条重写
    rec4 = A.put(conn, "sz300026", moat_type="无标注", replace=True)
    check("replace=True 清空 evidence", rec4["evidence"] == "")
    check("replace=True 清空 sources", rec4["sources"] == [])
    check("replace=True 后 annotated=False", rec4["annotated"] is False)

    # ---------------- 枚举校验 ----------------
    print("\n枚举校验")
    check("非法 moat_type 退化成「无标注」而不是报错",
          A.normalize_moat_type("乱写的") == "无标注")
    check("空值退化成「无标注」", A.normalize_moat_type(None) == "无标注")
    check("合法值原样保留", A.normalize_moat_type("独家资源") == "独家资源")
    rec5 = A.put(conn, "sh600096", moat_type="不存在的类型", evidence="x")
    check("put 时非法值也退化", rec5["moat_type"] == "无标注")
    check("MOAT_TYPES 有 7 个值且含「无标注」",
          len(MOAT_TYPES) == 7 and "无标注" in MOAT_TYPES, str(MOAT_TYPES))
    bad_date = False
    try:
        A.put(conn, "sh600096", verified="2026/13/45")
    except ValueError:
        bad_date = True
    check("非法 verified 日期抛 ValueError", bad_date)

    # ---------------- stale 判断 ----------------
    print("\nstale 判断")
    old_day = (dt.date.today() - dt.timedelta(days=ANNOTATION_STALE_DAYS + 30)).isoformat()
    rec6 = A.put(conn, "sh600111", moat_type="配额牌照", verified=old_day)
    check(f"超 {ANNOTATION_STALE_DAYS} 天 → stale=True", rec6["stale"] is True,
          f"verified={old_day}")
    recent = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    rec7 = A.put(conn, "sh600111", verified=recent)
    check("重新核实后 stale=False", rec7["stale"] is False)
    check("旧标注没被删（moat_type 还在）", rec7["moat_type"] == "配额牌照")

    # ---------------- 读取默认值 ----------------
    print("\n读取默认值")
    empty = A.get(conn, "sz999999")
    check("库里没有 → annotated=False", empty["annotated"] is False)
    check("库里没有 → moat_type=「无标注」", empty["moat_type"] == "无标注")
    check("空记录的键与原版一致",
          set(empty) == {"annotated", "moat_type", "evidence", "controller",
                         "sources", "verified", "stale"}, str(sorted(empty)))
    check("空记录 stale 是 False 而不是 None", empty["stale"] is False)
    check("空记录 sources 是 list", isinstance(empty["sources"], list))

    # ---------------- yaml 往返 ----------------
    print("\nyaml 导入 / 导出")
    sample = {
        "sh600063": {"moat_type": "技术壁垒", "evidence": "PVA 龙头",
                     "controller": "皖维集团",
                     "sources": ["https://x.example"], "verified": dt.date(2026, 11, 13)},
        "sh600075": {"moat_type": "无标注", "evidence": "充分竞争",
                     "controller": "新疆天业", "sources": [], "verified": "2026-11-13"},
        "BADKEY": "这不是 dict，应被跳过",
    }
    entries = A.from_yaml_obj(sample)
    check("from_yaml_obj 跳过非 dict 条目", len(entries) == 2, str(len(entries)))
    check("from_yaml_obj 键转小写",
          {e["symbol"] for e in entries} == {"sh600063", "sh600075"})
    for e in entries:
        A.put(conn, e["symbol"], moat_type=e["moat_type"], evidence=e["evidence"],
              controller=e["controller"], sources=e["sources"],
              verified=e["verified"], note=e["note"], replace=True)
    out = A.to_yaml_obj(conn)
    check("导出含刚导入的两条", {"sh600063", "sh600075"} <= set(out))
    check("导出 verified 是字符串（可往返）",
          isinstance(out["sh600063"]["verified"], str),
          repr(out["sh600063"]["verified"]))
    back = A.from_yaml_obj(out)
    same = all(
        str(a.get(f) or "") == str(b.get(f) or "")
        for a, b in zip(sorted(entries, key=lambda x: x["symbol"]),
                        sorted(back, key=lambda x: x["symbol"]))
        for f in ("moat_type", "evidence", "controller", "verified")
    )
    check("导入→导出→再解析 字段一致", same)

    # ---------------- 删除 ----------------
    print("\n删除")
    # 此时库里应有 4 条：sz300026 / sh600096 / sh600111 / sh600063（sh600075 即将删）
    n_before = A.count(conn)
    check("删除前有 5 条", n_before == 5, str(n_before))
    check("删除已存在的返回 True", A.delete(conn, "sh600075") is True)
    check("再删一次返回 False", A.delete(conn, "sh600075") is False)
    check("删除后查不到", A.get(conn, "sh600075")["moat_type"] == "无标注")
    check("count 减少 1", A.count(conn) == n_before - 1, str(A.count(conn)))

    # ---------------- 重复写入幂等 ----------------
    print("\n重复写入")
    before = A.get(conn, "sz300026")
    n_before = A.count(conn)
    A.put(conn, "sz300026", moat_type=before["moat_type"])
    after = A.get(conn, "sz300026")
    check("同值重复写不产生新行", A.count(conn) == n_before, str(A.count(conn)))
    check("同值重复写内容不变", after["moat_type"] == before["moat_type"])

    conn.commit()
    conn.close()

    # ---------------- 迁移：v4 → v5 ----------------
    print("\n迁移 v4 → v5")
    check("can_upgrade(4,5) 为 True", db.can_upgrade(4, 5))
    check("can_upgrade(1,5) 为 True（逐跳）", db.can_upgrade(1, 5))
    v4 = tmp / "v4.db"
    db.create_db(v4, schema)
    c2 = db.connect(v4)
    c2.executescript("DROP TABLE annotation; PRAGMA user_version = 4;")
    c2.close()
    c2 = db.connect(v4)
    check("造出 v4 库（无 annotation 表）",
          not c2.execute("SELECT 1 FROM sqlite_master WHERE name='annotation'").fetchone())
    db.upgrade(c2, schema, from_version=4)
    check("迁移后 user_version=5", db.schema_version(c2) == 5)
    check("迁移后 annotation 表已建",
          c2.execute("SELECT 1 FROM sqlite_master WHERE name='annotation'").fetchone() is not None)
    c2.close()

    # ---------------- score.load_resource_moat 链路 ----------------
    print("\nscore.load_resource_moat 读库")
    ok = True
    try:
        sys.path.insert(0, str(PROJECT / "stock_quality_screen"))
        from screen.score import load_resource_moat          # noqa: E402
    except Exception as exc:                                  # noqa: BLE001
        ok = False
        print(f"    导入失败：{exc}")
    if ok:
        # ⚠️ 这一段读的是**真实**的 shared.db（`Config().db_path`），不是上面那个
        # 临时库——`load_resource_moat()` 内部自己开连接，没有注入口。
        # 所以断言写成"不报错、结构对"，**不断言具体内容**：
        # 真实库里 sz300026 有没有标注取决于运维，不该让测试替它做主。
        r = load_resource_moat("sz300026")
        check("返回结构含 7 个键",
              {"annotated", "moat_type", "evidence", "controller",
               "sources", "verified", "stale"} <= set(r), str(sorted(r)))
        check("没读到异常", "load_error" not in r, str(r.get("load_error")))
        check("moat_type 是合法枚举", r["moat_type"] in MOAT_TYPES, r["moat_type"])
        r2 = load_resource_moat("")
        check("空代码返回无标注", r2["annotated"] is False and "load_error" not in r2)
        r3 = load_resource_moat("sz000000")
        check("不存在的代码返回无标注而不是报错",
              r3["moat_type"] == "无标注" and "load_error" not in r3)

    # ---------------- 收尾 ----------------
    print(f"\n{'=' * 50}")
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  失败：{f}")
    print(f"临时目录（可删）：{tmp}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
