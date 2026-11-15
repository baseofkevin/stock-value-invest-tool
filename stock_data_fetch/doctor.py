"""`doctor` —— 只读体检。回答一个问题：**这个库现在能不能放心用？**

    python -m stock_data_fetch.doctor            # 人读
    python -m stock_data_fetch.doctor --json     # 机读（给四个工具的入口脚本用）
    python -m stock_data_fetch.doctor --quiet    # 只打问题，不打概览

三条硬约束
----------

1. **只读**。连接用 `mode=ro`，一条写语句都没有：体检工具顺手改了库，就没法拿它
   去验"是不是写坏了"。库不存在直接报错，**不建库**（`preflight()` 才建）。
2. **离线**。一次上游都不打。要跨源对账请用 `probe`——启动路径上一旦依赖网络，
   上游抖一下四个工具就都起不来（同 `hub.preflight` 的理由）。
3. **不抛异常**。所有检查都包在 try 里，坏一处只掉一项，剩下的照跑——
   体检工具自己崩掉是最没用的结果。

检查什么（按"能不能用"排序）
----------------------------

| 级别 | 含义 | 退出码 |
|---|---|---|
| `ERROR` | 现在就不可用（库打不开、版本不符、PRAGMA 没落实、coverage 与数据不一致） | 1 |
| `WARN` | 能用但有窟窿（覆盖不全、partial、阻断洞、深度不足、日历太短、源已降级） | 0 |
| `INFO` | 概览与留痕，不需要动作 | 0 |

最值钱的是**不变式 3**（数据与 coverage 同事务）：`coverage.n` 说 300 行而表里只有
299 行、`coverage.end_date` 说 2026-11-13 而 `MAX(date)` 是 2026-11-12、表里有数据
却没有任何覆盖记录——这三种状态都会让 `hit()` 撒谎，而且是**静默**撒谎：
工具拿到半份数据、报告照出、名单悄悄变了。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import coverage as C
from . import db
from .calendar import TradingCalendar
from .config import Config, SCHEMA_VERSION
from .errors import HubNotReady
from .need import MIN_VALUATION_WEEKS

KLINE_MIN_BARS = 250
"""kline 的使用门槛。kline-screen 要 71、judge-top 要 250，取最严的报。"""

FINANCE_MIN_PERIODS = 40
"""财报门槛。cycle 要 40 期 TTM，这是四个工具里最严的。"""

CALENDAR_NEEDED_YEARS = 10
"""日历至少该有多长。低于它，任何"要 5 年以上"的需求都会被静默截断（`build_calendar` 里同理）。"""

WAL_WARN_MB = 64
"""`-wal` 超过这个体积告警：说明长期没有 checkpoint，读会被拖慢。"""

LEVEL_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}


# ---------------------------------------------------------------- 结果模型

@dataclass
class Finding:
    """一条体检结论。`examples` 只放能直接定位问题的那些行（最多 5 条）。"""

    level: str
    code: str
    title: str
    detail: str = ""
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "title": self.title,
                "detail": self.detail, "examples": self.examples}


class Report:
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.facts: dict = {}

    def add(self, level: str, code: str, title: str, detail: str = "",
            examples: list[str] | None = None) -> None:
        assert level in LEVEL_ORDER, level
        self.findings.append(Finding(level, code, title, detail,
                                     (examples or [])[:5]))

    def error(self, code: str, title: str, detail: str = "",
              examples: list[str] | None = None) -> None:
        self.add("ERROR", code, title, detail, examples)

    def warn(self, code: str, title: str, detail: str = "",
             examples: list[str] | None = None) -> None:
        self.add("WARN", code, title, detail, examples)

    def info(self, code: str, title: str, detail: str = "",
             examples: list[str] | None = None) -> None:
        self.add("INFO", code, title, detail, examples)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "ERROR"]

    @property
    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "WARN"]

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (LEVEL_ORDER[f.level], f.code))


# ---------------------------------------------------------------- 各项检查

def check_db(cfg: Config, rep: Report) -> sqlite3.Connection | None:
    """闸 2/3/4 的只读复核。返回连接（只读）或 None。"""
    path = Path(cfg.db_path)
    if not path.exists():
        rep.error("db.missing", f"库不存在：{path}",
                  "先跑一次 `preflight()`（四个工具启动即建）或 `--create` 手动建库")
        return None
    size_mb = path.stat().st_size / 1024 / 1024
    try:
        conn = db.connect(path, readonly=True)
    except sqlite3.Error as e:
        rep.error("db.open", f"库打不开：{e}", str(path))
        return None

    try:
        ver = db.schema_version(conn)
        if ver != SCHEMA_VERSION:
            if db.can_upgrade(ver, SCHEMA_VERSION):
                # 只读体检**不迁移**。这里只如实说"下次启动会自己补齐"——
                # 把它报成 ERROR 会让人以为库坏了，而它其实只是版本旧了点。
                # 用 `can_upgrade()` 而不是查 `AUTO_UPGRADE` 里的单对：v1 到 v3
                # 这一对没登记过，但经 v2 逐跳是通的，判成 ERROR 就是误报。
                rep.warn("db.version",
                         f"库版本 {ver}，代码要求 {SCHEMA_VERSION}（可自动升级）",
                         "下次 preflight 会自动迁移，不动既有数据")
            else:
                rep.error("db.version",
                          f"库版本 {ver}，代码要求 {SCHEMA_VERSION}",
                          "版本不符一律拒绝启动（数据可再生，备份后删库重建）")
        problems = db.pragma_problems(conn)
        if problems:
            rep.error("db.pragma", "PRAGMA 没落实", "；".join(problems))
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        rep.facts.update({"db_path": str(path), "db_mb": round(size_mb, 2),
                          "schema_version": ver, "page_size": page,
                          "free_pages": free})
        wal = path.with_name(path.name + "-wal")
        if wal.exists():
            mb = wal.stat().st_size / 1024 / 1024
            rep.facts["wal_mb"] = round(mb, 2)
            if mb > WAL_WARN_MB:
                rep.warn("db.wal", f"-wal 已 {mb:.0f}MB",
                         "长期没 checkpoint，读会被拖慢；空闲时 `PRAGMA wal_checkpoint(TRUNCATE)`")

        counts = db.table_counts(conn)
        rep.facts["tables"] = counts
        if sum(counts.values()) == 0:
            rep.info("db.empty", "库是空的（只有表结构）", "首次运行会按需填充，属正常")
        # 覆盖记录数 vs 数据表行数：一眼看出"有没有真东西"
        rep.facts["coverage_rows"] = counts.get("coverage", 0)
    except sqlite3.Error as e:
        rep.error("db.inspect", f"读库元信息失败：{e}")
    return conn


def check_calendar(cfg: Config, rep: Report) -> TradingCalendar | None:
    """闸 5 的复核 + 跨度是否够。**跨度不够不拦启动，但会让长历史需求静默截断。**"""
    path = Path(cfg.calendar_path)
    try:
        cal = TradingCalendar.load(path)
    except HubNotReady as e:
        rep.error("cal.load", f"日历不可用：{e}", str(path))
        return None

    span_days = (dt.date.fromisoformat(cal.days[-1])
                 - dt.date.fromisoformat(cal.days[0])).days
    rep.facts.update({"calendar_path": str(path), "calendar_days": len(cal.days),
                      "calendar_span": f"{cal.days[0]}..{cal.days[-1]}",
                      "calendar_years": round(span_days / 365, 1)})
    problems = cal.sanity(dt.date.today().isoformat(), cfg.calendar_max_lag_days)
    if problems:
        rep.error("cal.sanity", "日历自检不过", "；".join(problems))
    if span_days < CALENDAR_NEEDED_YEARS * 365:
        rep.warn("cal.short", f"日历只有 {span_days / 365:.1f} 年",
                 f"低于 {CALENDAR_NEEDED_YEARS} 年会让长历史需求被静默截断；"
                 "跑 `python -m stock_data_fetch.build_calendar` 生成长的")
    return cal


def domain_overview(conn: sqlite3.Connection, rep: Report) -> list[dict]:
    """按域汇总覆盖记录。这是"库里有什么"的唯一可信来源。"""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage'"
                        ).fetchone():
        rep.error("cov.missing", "coverage 表不存在", "这个库不是本项目的库？")
        return []
    rows = conn.execute(
        "SELECT domain, COUNT(*) symbols, SUM(n) rows_, SUM(holes) holes, "
        "       SUM(partial) partial, MIN(src) src, MIN(start_date) min_start, "
        "       MAX(end_date) max_end, COUNT(DISTINCT fp) fps "
        "FROM coverage GROUP BY domain ORDER BY domain").fetchall()
    out = [dict(r) for r in rows]
    if out:
        rep.facts["domains"] = out
        for d in out:
            if d["partial"]:
                rep.warn("cov.partial", f"{d['domain']} 有 {d['partial']} 条 partial=1",
                         "上一次取数被限频截断，这些股票的覆盖不可信，重跑即可")
            if d["holes"]:
                rep.warn("cov.holes", f"{d['domain']} 有 {d['holes']} 个洞",
                         "洞不阻塞命中，但要出现在报告里；`permanent` 的永远补不上")
    return out


def check_invariants(conn: sqlite3.Connection, rep: Report) -> None:
    """**不变式 3**：coverage 说的和表里实际有的必须一致。

    这是 doctor 最该做的事。三条都要查，因为三种错法症状不同：

    - `n` 不符 → `hit()` 按 `n >= min_points` 判，会放过一份缺条数的数据；
    - `end_date` 不符 → `hit()` 认为"已覆盖到今天"，于是永不重取，末端永远缺；
    - 有数据无覆盖 → 每次运行都重取一遍（白烧配额），且日志里没有任何异常；
    - 有覆盖无数据 → 直接骗过 `hit()`，工具拿到空表还以为是"这只票没数据"。
    """
    for domain, (table, dcol, _cadence) in C.DOMAIN_SPEC.items():
        if not _has_table(conn, table):
            continue
        rows = conn.execute(
            f"SELECT c.symbol, c.fp, c.n, c.end_date, c.start_date, "
            f"       x.real_n, x.real_end "
            f"FROM coverage c LEFT JOIN (SELECT symbol, COUNT(*) real_n, MAX({dcol}) real_end "
            f"                          FROM {table} GROUP BY symbol) x USING(symbol) "
            f"WHERE c.domain = ?", (domain,)).fetchall()
        bad_n, bad_end, no_data = [], [], []
        for r in rows:
            real_n = r["real_n"] or 0
            if real_n == 0:
                no_data.append(f"{r['symbol']} fp={r['fp'] or '-'}")
                continue
            if int(r["n"]) != real_n:
                bad_n.append(f"{r['symbol']} 覆盖 n={r['n']} 实际 {real_n}")
            if r["end_date"] and r["real_end"] and r["end_date"] != r["real_end"]:
                bad_end.append(f"{r['symbol']} 覆盖 end={r['end_date']} 实际 {r['real_end']}")
        if no_data:
            rep.error(f"inv.{domain}.covered_but_empty",
                      f"{domain}：{len(no_data)} 只有覆盖记录但表里没数据",
                      "`hit()` 会直接命中，工具拿到空表还以为是「这只票没数据」",
                      no_data)
        if bad_n:
            rep.error(f"inv.{domain}.n_mismatch",
                      f"{domain}：{len(bad_n)} 条的 n 与表内行数不符",
                      "点数门槛会按错的那个数字判，缺条数的数据被当成完整",
                      bad_n)
        if bad_end:
            rep.error(f"inv.{domain}.end_mismatch",
                      f"{domain}：{len(bad_end)} 条的 end_date 与表内最新日期不符",
                      "末端判据会认为「已覆盖到今天」，于是永不重取",
                      bad_end)

        # 反向：表里有数据但没覆盖记录 —— 每次运行都会重取，白烧配额
        orphans = [r[0] for r in conn.execute(
            f"SELECT DISTINCT t.symbol FROM {table} t "
            f"WHERE NOT EXISTS (SELECT 1 FROM coverage c "
            f"                  WHERE c.domain = ? AND c.symbol = t.symbol)", (domain,))]
        if orphans:
            rep.warn(f"inv.{domain}.orphan",
                     f"{domain}：{len(orphans)} 只有数据但没有覆盖记录",
                     "`hit()` 永远不命中 → 每次运行都重取一遍（不报错，只慢）",
                     orphans)

    # 点域：coverage.n 恒为 1，判"有覆盖就必须有一份数据"
    for domain, table in (("quote", "quote"), ("profile", "stock")):
        if domain not in C.POINT_DOMAINS or not _has_table(conn, table):
            continue
        missing = [r[0] for r in conn.execute(
            f"SELECT c.symbol FROM coverage c WHERE c.domain = ? AND NOT EXISTS "
            f"(SELECT 1 FROM {table} t WHERE t.symbol = c.symbol)", (domain,))]
        if missing:
            rep.error(f"inv.{domain}.covered_but_empty",
                      f"{domain}：{len(missing)} 只有覆盖记录但表里没数据",
                      "点域一股一份，有记录就必须有那一份", missing)


def check_depths(conn: sqlite3.Connection, rep: Report) -> None:
    """深度够不够。**只对库里已有的股票报**，不去猜 universe。"""
    def short(code: str, sql: str, label: str, threshold: int) -> None:
        if not _has_table(conn, sql.split("FROM ")[1].split()[0]):
            return
        rows = conn.execute(sql).fetchall()
        bad = [f"{r[0]} n={r[1]}" for r in rows if (r[1] or 0) < threshold]
        if bad:
            rep.warn(code, f"{len(bad)} 只 {label} 不足 {threshold}",
                     "四个工具的门槛按最严的算；这些票会在门槛处被跳过", bad)

    short("depth.kline", "SELECT symbol, COUNT(*) FROM kline GROUP BY symbol",
          "K 线", KLINE_MIN_BARS)
    short("depth.finance", "SELECT symbol, COUNT(*) FROM finance GROUP BY symbol",
          "财报期数", FINANCE_MIN_PERIODS)
    short("depth.valuation", "SELECT symbol, COUNT(*) FROM valuation GROUP BY symbol",
          "估值周数", MIN_VALUATION_WEEKS)


def check_watermark(conn: sqlite3.Connection, rep: Report) -> None:
    """水位线与覆盖末端应当一致。不一致说明某次写入只做了半边（不变式 3 被绕过）。"""
    if not _has_table(conn, "watermark"):
        return
    rows = conn.execute(
        "SELECT w.domain, w.last_ok_date, c.max_end FROM watermark w "
        "LEFT JOIN (SELECT domain, MAX(end_date) max_end FROM coverage GROUP BY domain) c "
        "USING(domain)").fetchall()
    if not rows:
        rep.info("wm.empty", "还没有水位线记录", "取过数之后才会有")
        return
    bad = [f"{r['domain']} 水位 {r['last_ok_date']} vs 覆盖末端 {r['max_end']}"
           for r in rows
           if r["last_ok_date"] and r["max_end"] and r["last_ok_date"] > r["max_end"]]
    if bad:
        rep.warn("wm.ahead", f"{len(bad)} 个域的水位线跑到覆盖前面",
                 "水位线是从日历推的，领先覆盖说明那一批没落库", bad)


def check_state(cfg: Config, conn: sqlite3.Connection, rep: Report) -> None:
    """留痕与节流状态。这些不是错误，但出过事必须能从库里看到。"""
    try:
        degraded = db.meta_get(conn, "source_degraded")
        if degraded:
            rep.warn("meta.degraded", "存在源降级留痕", degraded)
        intraday = db.meta_get(conn, "intraday_override_at")
        if intraday:
            rep.info("meta.intraday", f"最近一次盘中放行：{intraday}",
                     "盘中取到的当日 bar 可能是半成品")
        srcs = {r[0][len("domain_src:"):]: r[1] for r in conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'domain_src:%'")}
        if srcs:
            rep.facts["domain_src"] = srcs
    except sqlite3.Error:
        pass

    root = Path(cfg.state_dir) / "throttle"
    if not root.exists():
        return
    snap = {}
    for p in sorted(root.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rep.warn("state.throttle_bad", f"节流状态读不了：{p.name}",
                     "坏文件会让下次运行从空桶开始（只是多等，不出错）")
            continue
        left = max(0.0, float(d.get("cooldown_until") or 0) - dt.datetime.now().timestamp())
        snap[p.stem] = {"tokens": round(float(d.get("tokens") or 0), 0),
                        "cooldown_left_s": round(left, 0),
                        "cooldown_len_s": round(float(d.get("cooldown_len") or 0), 0)}
        if d.get("cooldown_len"):
            rep.warn("state.cooldown", f"{p.stem} 上次被限频过（冷却长度 "
                     f"{d.get('cooldown_len')}s）",
                     "冷却长度不清零说明最近一直没干净返回")
    if snap:
        rep.facts["throttle"] = snap


def check_space(cfg: Config, rep: Report) -> None:
    import shutil
    try:
        free = shutil.disk_usage(Path(cfg.db_path).parent).free / 1024 / 1024
    except OSError:                                   # pragma: no cover
        return
    rep.facts["free_mb"] = round(free, 0)
    if free < cfg.min_free_mb:
        rep.warn("space.low", f"剩余 {free:.0f}MB < {cfg.min_free_mb}MB",
                 "低于 MIN_FREE_MB 只告警不拦（清理是 GC 的职责）")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


# ---------------------------------------------------------------- 入口

def run(cfg: Config | None = None) -> Report:
    """跑完全部检查。**不抛异常**，坏一处只掉一项。"""
    cfg = cfg or Config.from_env()
    rep = Report()
    rep.facts["checked_at"] = dt.datetime.now().isoformat(timespec="seconds")
    conn = None
    try:
        conn = check_db(cfg, rep)
        check_calendar(cfg, rep)
        if conn is not None:
            for fn in (domain_overview, check_invariants, check_depths,
                       check_watermark):
                try:
                    fn(conn, rep)                     # type: ignore[operator]
                except sqlite3.Error as e:
                    rep.error("check.failed", f"{fn.__name__} 没跑完：{e}")
            try:
                check_state(cfg, conn, rep)
            except sqlite3.Error as e:
                rep.error("check.failed", f"check_state 没跑完：{e}")
        check_space(cfg, rep)
    finally:
        if conn is not None:
            conn.close()
    return rep


def render(rep: Report, *, as_json: bool = False, quiet: bool = False) -> str:
    if as_json:
        return json.dumps({"ok": not rep.errors, "facts": rep.facts,
                           "findings": [f.to_dict() for f in rep.sorted_findings()]},
                          ensure_ascii=False, indent=2)

    out: list[str] = []
    if not quiet:
        f = rep.facts
        out.append("=" * 66)
        out.append("共享数据层体检")
        out.append("=" * 66)
        out.append(f"库      {f.get('db_path', '?')}  "
                   f"{f.get('db_mb', '?')}MB  版本 {f.get('schema_version', '?')}")
        out.append(f"日历    {f.get('calendar_span', '?')}  {f.get('calendar_days', '?')} 天"
                   f"（{f.get('calendar_years', '?')} 年）")
        if f.get("free_mb") is not None:
            out.append(f"磁盘    剩余 {f['free_mb']:.0f}MB")
        tables = f.get("tables") or {}
        if tables:
            out.append("表      " + "  ".join(f"{k}={v}" for k, v in tables.items()
                                              if v or k in ("coverage", "stock")))
        doms = f.get("domains") or []
        if doms:
            out.append("")
            out.append(f"{'域':<10}{'股票':>5}{'行数':>7}{'洞':>4}{'部分':>5}  "
                       f"{'src':<10}{'区间'}")
            for d in doms:
                if d["min_start"] or d["max_end"]:
                    span = f"{d['min_start'] or '-'} .. {d['max_end'] or '-'}"
                else:
                    span = "（点域，无区间）"
                out.append(f"{d['domain']:<10}{d['symbols']:>5}{d['rows_'] or 0:>7}"
                           f"{d['holes'] or 0:>4}{d['partial'] or 0:>5}  "
                           f"{(d['src'] or '-'):<10}{span}")
        else:
            out.append("\n（还没有任何覆盖记录）")
        out.append("")

    buckets = {"ERROR": [], "WARN": [], "INFO": []}
    for f in rep.sorted_findings():
        buckets[f.level].append(f)
    icon = {"ERROR": "❌", "WARN": "⚠️ ", "INFO": "· "}
    for level in ("ERROR", "WARN", "INFO"):
        for f in buckets[level]:
            if level == "INFO" and quiet:
                continue
            out.append(f"{icon[level]} [{f.code}] {f.title}")
            if f.detail:
                out.append(f"      {f.detail}")
            for ex in f.examples:
                out.append(f"      - {ex}")

    n_err, n_warn = len(rep.errors), len(rep.warns)
    out.append("")
    if n_err:
        out.append(f"结论：❌ 有 {n_err} 个 ERROR、{n_warn} 个 WARN —— 现在不能放心用")
    elif n_warn:
        out.append(f"结论：⚠️  可用，但有 {n_warn} 个 WARN")
    else:
        out.append("结论：✅ 没发现问题")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="共享数据层只读体检（不联网、不写库）")
    ap.add_argument("--db", default="", help="默认按 STOCKHUB_DB / Config 的库")
    ap.add_argument("--calendar", default="", help="默认按 STOCKHUB_CALENDAR / Config 的日历")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")
    ap.add_argument("--quiet", action="store_true", help="只打问题，不打概览")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if args.db:
        cfg.db_path = Path(args.db).expanduser()
    if args.calendar:
        cfg.calendar_path = Path(args.calendar).expanduser()

    rep = run(cfg)
    print(render(rep, as_json=args.json, quiet=args.quiet))
    return 1 if rep.errors else 0


if __name__ == "__main__":                            # pragma: no cover
    raise SystemExit(main())
