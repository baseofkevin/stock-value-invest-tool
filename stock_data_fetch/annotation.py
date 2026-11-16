# -*- coding: utf-8 -*-
"""人工标注（域 `annotation`）：读写与 CLI。

## 为什么标注要在共享库里，而不是一个 yaml 文件

原先是 `stock_quality_screen/annotations/moat_annotations.yaml` 一张 850 行的表。
四个问题：

1. **跟着代码目录走。** 换工作区、换机器、或者哪天清理 `stock-quality-screen-tool`，
   113 条人工核实过的判断就没了。而代码可以重写，人工核实的事实重写不了；
2. **只有 quality 读得到。** cycle-elastic / judge-top 将来也需要"这家有没有垄断性"，
   它们没有理由去翻另一个工具的目录；
3. **改一条要动文本文件。** 改一个字段得小心缩进，改错一个 `-` 整份文件解析失败；
4. **和"每只股票的事实"这一属性不匹配。** 标注属于公司，不属于某次跑批。

## 为什么单独一张表，不塞进 `snapshot`

`snapshot` 的语义是「**从某个上游源取来的一份快照**」：它有 `src`（哪个源）、
`as_of`（算哪天的），而且可能被 GC 按访问时间回收。标注三样都不符合：
没有上游源、`as_of` 没有意义（只有 `verified` 核实日期）、**绝不能回收**。
硬塞进去会让 GC、源优先级、`empty_reason` 的判断统统变味。

## 与 `stock` 表不设外键

标注可以比 profile 先存在。您完全可能先把 113 只自选股标注一遍，之后才逐个取数；
加外键会让"先录标注"直接失败——而那恰好是最自然的用法。查的时候 JOIN 不到
就当"无标注"处理，不影响任何计算。

## 三个字段的语义，容易搞混

- `moat_type`：**枚举**（`config.MOAT_TYPES`），`"无标注"` 是合法值，
  含义是"查过，没发现垄断性"——这是**结论**，不是缺口；
- `verified`：YYYY-MM-DD，最后一次核实日期。超 `ANNOTATION_STALE_DAYS` 天
  报告显示「待复核」，但**不删标注**：三年前核实的"独家品种"今年可能不独家了，
  可它当年确实独家，抹掉就丢了判断依据；
- `sources`：JSON 数组存 URL。**必须能点开核实**——没有来源的标注等于猜测，
  而"垄断性"是会被证伪的事实判断，不是观点。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from .config import ANNOTATION_KIND_MOAT, ANNOTATION_STALE_DAYS, MOAT_TYPES, Config

MOAT_KIND = ANNOTATION_KIND_MOAT


# ---------------------------------------------------------------- 行 ↔ 字典

_EMPTY_MOAT: dict[str, Any] = {
    "annotated": False,
    "moat_type": "无标注",
    # 下面四个**必须给空值而不是省略**：`render.py` 用 `.get()` 读，
    # 省略不会崩，但原版（yaml 时代）的返回里这几个键一直都在。
    # 保持形状一致，将来若有人写成 `rec["evidence"]` 也不会 KeyError。
    "evidence": "",
    "controller": "",
    "sources": [],
    "verified": "",
    "stale": False,
}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _split_sources(raw: Any) -> list[str]:
    """`sources` 列 → list[str]。库里存 JSON 数组，坏数据当空列表。"""
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if str(s).strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return [str(s).strip() for s in parsed if str(s).strip()]
    return []


def _join_sources(sources: Any) -> str:
    """list[str] → 库里的 JSON 串。空列表存 `[]` 而不是 NULL：
    "查过但没有来源"和"压根没这一栏"是两件事，用 `[]` 能区分。"""
    items = _split_sources(sources)
    return json.dumps(items, ensure_ascii=False)


def _parse_date(raw: Any) -> dt.date | None:
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def normalize_moat_type(raw: Any) -> str:
    """把各种写法归到枚举上。认不出来就是「无标注」（**不是**报错）。

    yaml 里写错一个值不该让整份标注表废掉，也不该让人以为"这家有垄断性"。
    退化成「无标注」最安全：报告会显示"无标注"，而 `load_error` 那条路
    （见 `screen/score.py`）能看出是解析失败还是真没有。
    """
    text = str(raw or "").strip()
    return text if text in MOAT_TYPES else "无标注"


def row_to_moat(row: Any) -> dict[str, Any]:
    """`annotation` 一行 → `score.load_resource_moat()` 的返回形状。

    **字段名必须和历史版本一模一样**：`annotated` / `moat_type` / `evidence` /
    `controller` / `sources` / `verified` / `stale`。报告渲染代码（`render.py`）
    直接读这几个键，改名会让标注区整块消失而不报错。
    """
    if row is None:
        return dict(_EMPTY_MOAT)
    moat_type = normalize_moat_type(row["moat_type"] if "moat_type" in row.keys() else None)
    verified = str(row["verified"] or "") if "verified" in row.keys() else ""
    vdate = _parse_date(verified)
    stale = bool(vdate and (dt.date.today() - vdate).days > ANNOTATION_STALE_DAYS)
    out = {
        # 「待核实」不是结论：报告里不该出现垄断块，否则等于替没核过的票背书。
        "annotated": moat_type not in ("无标注", "待核实"),
        "moat_type": moat_type,
        "evidence": str(row["evidence"] or "") if "evidence" in row.keys() else "",
        "controller": str(row["controller"] or "") if "controller" in row.keys() else "",
        "sources": _split_sources(row["sources"] if "sources" in row.keys() else None),
        "verified": verified,
        "stale": stale,
    }
    note = str(row["note"] or "") if "note" in row.keys() else ""
    if note:
        out["note"] = note
    return out


# ---------------------------------------------------------------- 读

def get(conn, symbol: str, kind: str = MOAT_KIND) -> dict[str, Any]:
    """读一只票的一条标注；没有就返回「无标注」。**绝不抛错**。

    这是 `score.load_resource_moat()` 唯一依赖的入口。不抛错的理由：
    标注缺失是正常状态（全市场 5000 多只，人工只标了 113 只），
    不该因为"这只没标"就让报告生成失败。
    """
    row = conn.execute(
        "SELECT symbol,kind,moat_type,evidence,controller,sources,verified,note,updated_at "
        "FROM annotation WHERE symbol=? AND kind=?", (symbol, kind)).fetchone()
    return row_to_moat(row) if kind == MOAT_KIND else row_to_moat(None)


def load_all(conn, kind: str = MOAT_KIND) -> dict[str, dict[str, Any]]:
    """全部标注，`{symbol: {...}}`。给 CLI 和导出用。"""
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
            "SELECT symbol,kind,moat_type,evidence,controller,sources,verified,note,updated_at "
            "FROM annotation WHERE kind=? ORDER BY symbol", (kind,)):
        out[row["symbol"]] = row_to_moat(row)
    return out


def count(conn, kind: str | None = None) -> int:
    if kind:
        return conn.execute("SELECT COUNT(*) FROM annotation WHERE kind=?",
                            (kind,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM annotation").fetchone()[0]


# ---------------------------------------------------------------- 写

def put(conn, symbol: str, *, moat_type: Any = None, evidence: Any = None,
        controller: Any = None, sources: Any = None, verified: Any = None,
        note: Any = None, kind: str = MOAT_KIND, replace: bool = False) -> dict[str, Any]:
    """写入/更新一条标注。

    `replace=False`（默认）时**只覆盖给了值的字段**：
    `set sh600519 --moat-type 无标注` 不该把已有的 evidence/sources 一起清空——
    那是一次误操作就可能毁掉人工核实成果的地方。要整条重写就传 `replace=True`。

    返回写入后的完整记录，方便 CLI 直接打印给用户看"到底存成什么样了"。
    """
    old = conn.execute(
        "SELECT moat_type,evidence,controller,sources,verified,note "
        "FROM annotation WHERE symbol=? AND kind=?", (symbol, kind)).fetchone()

    def pick(new, key: str):
        if replace and old is not None:
            return new if new is not None else None
        if new is not None:
            return new
        return (old[key] if old is not None else None)

    mt = pick(moat_type, "moat_type")
    mt = normalize_moat_type(mt)
    ev = pick(evidence, "evidence")
    ctrl = pick(controller, "controller")
    src_raw = pick(sources, "sources")
    ver = pick(verified, "verified")
    nt = pick(note, "note")

    ver_text = ""
    vdate = _parse_date(ver)
    if vdate:
        ver_text = vdate.isoformat()
    elif ver:
        raise ValueError(f"verified 必须是 YYYY-MM-DD，收到 {ver!r}")

    conn.execute(
        "INSERT INTO annotation(symbol,kind,moat_type,evidence,controller,sources,"
        "verified,note,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,kind) DO UPDATE SET "
        "moat_type=excluded.moat_type, evidence=excluded.evidence, "
        "controller=excluded.controller, sources=excluded.sources, "
        "verified=excluded.verified, note=excluded.note, "
        "updated_at=excluded.updated_at",
        (symbol, kind, mt,
         None if ev is None else str(ev),
         None if ctrl is None else str(ctrl),
         _join_sources(src_raw) if src_raw is not None else None,
         ver_text or None,
         None if nt is None else str(nt),
         _now()))
    return get(conn, symbol, kind)


def delete(conn, symbol: str, kind: str = MOAT_KIND) -> bool:
    """删一条。返回是否真的删到了——CLI 要据此告诉用户"本来就没有"。"""
    cur = conn.execute("DELETE FROM annotation WHERE symbol=? AND kind=?", (symbol, kind))
    return cur.rowcount > 0


# ---------------------------------------------------------------- yaml 互转

def from_yaml_obj(raw: Any) -> list[dict[str, Any]]:
    """`moat_annotations.yaml` 解析出来的 dict → 待写入的列表。

    只做**形状转换**，不校验内容（`put()` 会校验）。键统一小写：
    yaml 里是 `sh601958`，用户手写也可能写 `SH601958`，而库里一律小写。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return out
    for key, entry in raw.items():
        symbol = str(key or "").strip().lower()
        if not symbol or not isinstance(entry, dict):
            continue
        out.append({
            "symbol": symbol,
            "moat_type": entry.get("moat_type"),
            "evidence": entry.get("evidence"),
            "controller": entry.get("controller"),
            "sources": entry.get("sources"),
            "verified": entry.get("verified"),
            "note": entry.get("note"),
        })
    return out


def to_yaml_obj(conn, kind: str = MOAT_KIND) -> dict[str, dict[str, Any]]:
    """库 → yaml 形状（`export` 用）。

    `verified` 转成 ISO 字符串而不是 date 对象：yaml.safe_dump 能把 date 写成
    `2026-11-13`，但读回来是 `datetime.date`，再写又变——来一遍导出/导入
    不保证字节一致。统一成字符串，往返稳定。
    """
    out: dict[str, dict[str, Any]] = {}
    for symbol, rec in load_all(conn, kind).items():
        entry: dict[str, Any] = {"moat_type": rec["moat_type"]}
        for key in ("evidence", "controller"):
            if rec.get(key):
                entry[key] = rec[key]
        if rec.get("sources"):
            entry["sources"] = list(rec["sources"])
        if rec.get("verified"):
            entry["verified"] = rec["verified"]
        if rec.get("note"):
            entry["note"] = rec["note"]
        out[symbol] = entry
    return out


# ---------------------------------------------------------------- CLI

_USAGE = """\
用法：python -m stock_data_fetch.annotation <命令> [参数]

命令
  show   <代码>                      查看一只股票的标注
  list   [--kind moat] [--only-annotated]   列出全部标注
  set    <代码> [选项]               写入/更新一条标注
  delete <代码>                      删除一条标注
  import <yaml 路径>                 从 yaml 批量导入（已有则合并）
  export [--out 路径]                导出成 yaml（不给 --out 就打到屏幕）

set 的选项（给哪个改哪个，不给的保持原值）
  --moat-type <类型>     枚举：{types}
  --evidence <文本>      一句话依据
  --controller <文本>    控股股东 / 实际控制人
  --source <URL>         核实来源，可重复给多次
  --verified <日期>      YYYY-MM-DD，不填则默认今天
  --note <文本>          自由备注
  --replace              整条重写（没给的字段会被清空）
  --db <路径>            指定数据库，默认用 STOCKHUB_DB 或 shared.db
""".format(types=" / ".join(MOAT_TYPES))


def _open_db(path: str | None):
    """开库。CLI 不走 `preflight()`：那会加载交易日历、探源可用性，
    改一条标注却要等两三秒，而且没有网络时会直接失败——改标注根本不需要联网。

    但**版本检查不能省**：在 v4 的库上写 `annotation` 表会直接报
    `no such table`，而那个报错信息不会告诉用户"该升级了"。这里主动检查，
    能自动升就升（`db.upgrade`），升不了就明确报错。
    """
    from . import db as _db
    from .config import SCHEMA_VERSION

    cfg = Config()
    db_path = Path(path).expanduser() if path else Path(cfg.db_path)
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}\n先跑一次任意工具建库，或用 --db 指定路径。")
    conn = _db.connect(db_path)

    ver = _db.schema_version(conn)
    if ver != SCHEMA_VERSION:
        if _db.can_upgrade(ver, SCHEMA_VERSION):
            _db.upgrade(conn, cfg.schema_path, from_version=ver)
            print(f"库版本 {ver} → {SCHEMA_VERSION}，已自动迁移", file=sys.stderr)
        else:
            _db.check_version(conn)      # 让它抛标准错误
    return conn, db_path


def _print_record(symbol: str, rec: dict[str, Any]) -> None:
    print(f"\n{symbol}")
    print(f"  标注类型  {rec.get('moat_type', '—')}"
          + ("（有垄断性）" if rec.get("annotated") else "（无垄断性结论）"))
    if rec.get("evidence"):
        print(f"  依据      {rec['evidence']}")
    if rec.get("controller"):
        print(f"  控股股东  {rec['controller']}")
    if rec.get("sources"):
        print("  来源")
        for i, s in enumerate(rec["sources"], 1):
            print(f"    [{i}] {s}")
    if rec.get("verified"):
        flag = "（已超一年，建议复核）" if rec.get("stale") else ""
        print(f"  核实日期  {rec['verified']}{flag}")
    if rec.get("note"):
        print(f"  备注      {rec['note']}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # `--db` 允许写在子命令前面或后面：`annotation --db x list` 和
    # `annotation list --db x` 都认。UNIX 惯例是前者（选项在前），
    # 但子命令式 CLI（git / kubectl）都是后者。两种都得支持，
    # 否则用户第一次照 `git` 的手感写就会撞上"不认识的命令：--db"。
    db_path = None
    while "--db" in args:
        i = args.index("--db")
        db_path = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]

    if not args or args[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0

    cmd, rest = args[0], args[1:]

    conn, path = _open_db(db_path)

    try:
        if cmd == "show":
            if not rest:
                raise SystemExit("show 需要一个股票代码")
            _print_record(rest[0], get(conn, rest[0]))
            return 0

        if cmd == "list":
            only = "--only-annotated" in rest
            records = load_all(conn)
            items = [(k, v) for k, v in records.items()
                     if not only or v["annotated"]]
            print(f"{path}：{len(items)} 条"
                  + (f"（共 {len(records)} 条，已滤掉无标注）" if only else ""))
            for symbol, rec in items:
                flag = "" if rec["annotated"] else "  ·"
                stale = "  ⚠待复核" if rec.get("stale") else ""
                print(f"  {symbol}  {rec['moat_type']:<6}{flag}{stale}")
            return 0

        if cmd == "set":
            if not rest:
                raise SystemExit("set 需要一个股票代码")
            symbol = rest[0]
            opts: dict[str, Any] = {}
            sources: list[str] = []
            replace = "--replace" in rest
            i = 1
            while i < len(rest):
                tok = rest[i]
                if tok == "--replace":
                    i += 1
                    continue
                if not tok.startswith("--"):
                    raise SystemExit(f"看不懂的参数：{tok}")
                val = rest[i + 1] if i + 1 < len(rest) else ""
                key = tok[2:]
                if key == "moat-type":
                    opts["moat_type"] = val
                elif key == "evidence":
                    opts["evidence"] = val
                elif key == "controller":
                    opts["controller"] = val
                elif key == "source":
                    sources.append(val)
                elif key == "verified":
                    opts["verified"] = val
                elif key == "note":
                    opts["note"] = val
                else:
                    raise SystemExit(f"不认识的选项：{tok}")
                i += 2
            if sources:
                opts["sources"] = sources
            if "moat_type" in opts and opts["moat_type"] not in MOAT_TYPES:
                raise SystemExit(
                    f"moat_type 必须是：{' / '.join(MOAT_TYPES)}\n收到：{opts['moat_type']}")
            # 没给 verified 就填今天：新录的标注默认"今天核实过"，
            # 否则它会因为 verified 为空而永远不显示「待复核」，也没人知道它多旧。
            if "verified" not in opts:
                opts["verified"] = dt.date.today().isoformat()
            rec = put(conn, symbol, replace=replace, **opts)
            conn.commit()
            print(f"已写入 {path}")
            _print_record(symbol, rec)
            return 0

        if cmd == "delete":
            if not rest:
                raise SystemExit("delete 需要一个股票代码")
            ok = delete(conn, rest[0])
            conn.commit()
            print(f"{'已删除' if ok else '本来就没有'}：{rest[0]}")
            return 0 if ok else 1

        if cmd == "import":
            if not rest:
                raise SystemExit("import 需要一个 yaml 路径")
            src = Path(rest[0]).expanduser()
            if not src.is_file():
                raise SystemExit(f"文件不存在：{src}")
            try:
                import yaml
            except ImportError:
                raise SystemExit("需要 pyyaml：pip install pyyaml") from None
            raw = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
            entries = from_yaml_obj(raw)
            added = updated = 0
            for e in entries:
                existed = conn.execute(
                    "SELECT 1 FROM annotation WHERE symbol=? AND kind=?",
                    (e["symbol"], MOAT_KIND)).fetchone() is not None
                put(conn, e["symbol"], kind=MOAT_KIND,
                    moat_type=e["moat_type"], evidence=e["evidence"],
                    controller=e["controller"], sources=e["sources"],
                    verified=e["verified"], note=e["note"], replace=True)
                if existed:
                    updated += 1
                else:
                    added += 1
            conn.commit()
            print(f"从 {src} 导入 {len(entries)} 条 → 新增 {added}、更新 {updated}")
            print(f"数据库 {path} 现有 {count(conn)} 条标注")
            return 0

        if cmd == "export":
            out_path = None
            if "--out" in rest:
                i = rest.index("--out")
                out_path = rest[i + 1] if i + 1 < len(rest) else None
            obj = to_yaml_obj(conn)
            try:
                import yaml
                text = yaml.safe_dump(obj, allow_unicode=True, sort_keys=True,
                                      default_flow_style=False, width=100)
            except ImportError:
                text = json.dumps(obj, ensure_ascii=False, indent=2)
            if out_path:
                p = Path(out_path).expanduser()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
                print(f"已导出 {len(obj)} 条 → {p}")
            else:
                print(text)
            return 0

        raise SystemExit(f"不认识的命令：{cmd}\n\n{_USAGE}")
    finally:
        conn.close()


if __name__ == "__main__":                      # pragma: no cover
    sys.exit(main())
