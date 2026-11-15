#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业质量初筛 CLI。

单股票：
    python3 stock_report.py --stock-code 600519
    python3 stock_report.py --stock-code 600519 --bundle tests/fixtures/mini_bundle.json

批量：
    python3 stock_report.py --from-select 2026-11-13   # 读结果库里当天的选股名单
    python3 stock_report.py --csv 我的持仓.csv          # 读自己的表（只读）

**结果只写 results.db**（2026-11-15 起）：不再产出单股 HTML、不再写导航页、
不再回写 CSV 的「综合评分 / HTML报告链接」列。看结果用 `./run.py web`，
同一批结论在库里能和选股 / 周期 / 顶部对起来。

这个改动顺带解掉了旧设计的一个死结：原来"续跑"靠扫输出目录里已有的 HTML、
再用正则从 HTML 里把 Q/V 抠回来（`extract_score_summary`）。结论本来就是算出来
的 dict，却先渲染成 HTML、再解析回文本、还要处理解析失败——现在续跑只问结果库
有没有这只票的记录，读到的是数据结构本身。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from screen.batch import CsvError, read_csv_table  # noqa: E402
from screen.compute import build_report_data  # noqa: E402
from screen.fetch import (DataError, close_hub, fetch_all, load_bundle,  # noqa: E402
                          normalize_code, set_quiet)
from screen.score import compute_scores  # noqa: E402
from screen.validate import ScreenError, SkipStock, validate  # noqa: E402

SKIP_SCORE_TEXT = f"跳过：上市不足 {config.MIN_ANNUAL_YEARS} 年"


# ---------------------------------------------------------------------------
# 一只股票：取数 → 计算 → 校验
# ---------------------------------------------------------------------------


class ReportResult:
    """一只股票的结果（不再有 target / html_text）。"""

    __slots__ = ("code", "name", "data", "skipped", "error", "gaps", "payload")

    def __init__(self, code: str = "", name: str = "", data: dict | None = None,
                 skipped: bool = False, error: str = "", gaps=None,
                 payload: dict | None = None):
        self.code = code
        self.name = name
        self.data = data
        self.skipped = skipped
        self.error = error
        self.gaps = gaps or []
        # 续跑复用的行没有 data，但库里的 payload 要原样带过去：
        # 否则新一次 run 只写"这次真算的"几只，看板读最新 run 就少了一半票。
        self.payload = payload or {}


def _generate(stock_code: str, *, bundle: str = "", timeout: float = 90.0,
              name_override: str = "", retries: int = 0,
              retry_wait: float = 60.0) -> ReportResult:
    """抓数、计算、校验一只股票；单股票与批量线程共用。"""
    if bundle:
        raw = load_bundle(bundle)
    else:
        try:
            raw = fetch_all(stock_code, timeout=timeout, retries=retries,
                            retry_wait=retry_wait)
        finally:
            # 取数完**无论如何**都关掉本线程的 Hub：SQLite 连接是线程私有的，
            # 批量模式下每个 worker 各开一个。不关要到进程退出才释放，
            # 几十只 × 4 线程会攒下几十个句柄（WAL 文件跟着涨）。
            close_hub()
    data = build_report_data(raw["calls"], raw["code"])
    data["data_gaps"] = raw.get("errors") or []
    data = compute_scores(data)
    validate(data)

    name = (name_override.strip() or data["meta"].get("name")
            or data["meta"]["code"])
    return ReportResult(code=data["meta"]["code"], name=name, data=data,
                        gaps=raw.get("errors") or [])


def _payload(data: dict | None) -> dict:
    """从内存里的报告数据取几个能直接查的字段。"""
    if not data:
        return {}
    dec = data.get("decision") or {}
    dq = data.get("data_quality") or {}
    checks = ((data.get("risks") or {}).get("checks") or [])
    q = data.get("quality") or {}
    v = data.get("valuation_score") or {}
    return {
        "reason": dec.get("reason") or "",
        "label": dec.get("label") or "",
        "q_grade": q.get("grade") or "",
        "v_grade": v.get("grade") or "",
        "q_score": q.get("score"),
        "v_score": v.get("score"),
        "risks_known": dq.get("risk_known"),
        "risks_applicable": ((dq.get("risk_total") or 0)
                             - (dq.get("risk_na") or 0)) or None,
        "risks_hit": sum(1 for c in checks if c.get("status") == "hit"),
        "risks_watch": sum(1 for c in checks if c.get("status") == "watch"),
        "fatal": list(data.get("fatal") or []),
    }


def _score_line(data: dict) -> str:
    """CLI 摘要里的 Q/V 那一行。

    金融股的 Q/V 是 None（not_applicable），直接 f-string 会抛 TypeError，
    所以显式区分「不适用」和「算不出来（—）」。两栏写法必须一致。
    """
    def cell(block):
        score = block.get("score")
        if isinstance(score, (int, float)):
            return f"{score:.1f} / 100"
        return "不适用" if block.get("not_applicable") else "—"

    return (f"  质量分 Q {cell(data.get('quality') or {})}"
            f"   估值分 V {cell(data.get('valuation_score') or {})}")


# ---------------------------------------------------------------------------
# 名单来源
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})")


def _resolve_date(text: str | None) -> str | None:
    """`--from-select` 的日期：空 → None（用最近一次选股运行）。"""
    if not text:
        return None
    m = _DATE_RE.search(str(text))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _items_from_results(target_date: str | None) -> list[tuple[str, str]]:
    try:
        from stock_data_fetch import results as results_db
        rows = results_db.symbols_for("select", target_date)
    except Exception:                            # noqa: BLE001
        return []
    return [(r["symbol"], r.get("name") or "") for r in rows if r.get("symbol")]


def _items_from_csv(path: str) -> list[tuple[str, str]]:
    """外部 CSV：只认代码列（和名称列），其余列一概不碰。"""
    table = read_csv_table(path)
    out = []
    for row in table.rows:
        raw = (row.get(table.code_column) or "").strip()
        if raw:
            out.append((raw, (row.get("名称") or "").strip()))
    return out


def _existing(target_date: str | None) -> dict[str, dict]:
    """结果库里这个基准日已经算过的票：{symbol: row}（供续跑复用）。"""
    if not target_date:
        return {}
    try:
        from stock_data_fetch import results as results_db
        rows = results_db.latest_for("quality", target_date)
    except Exception:                            # noqa: BLE001
        return {}
    out = {}
    for r in rows:
        sym = r.get("symbol") or ""
        out[sym] = r
        out[sym[-6:]] = r
    return out


def _save_results(results: dict[str, ReportResult], target_date: str | None) -> None:
    """把这一批写进 results.db（失败只 warn，绝不影响报告本身）。"""
    try:
        from stock_data_fetch import results as results_db
    except Exception:                            # noqa: BLE001
        return
    rows = []
    for code, result in results.items():
        payload = _payload(result.data) or result.payload
        if not payload:
            continue
        rows.append({
            "symbol": code,
            "name": result.name,
            "q": payload.get("q_score"),
            "q_grade": payload.get("q_grade"),
            "v": payload.get("v_score"),
            "v_grade": payload.get("v_grade"),
            "conclusion": payload.get("label") or "",
            "reason": payload.get("reason") or "",
            "risks_hit": payload.get("risks_hit"),
            "risks_watch": payload.get("risks_watch"),
            "risks_known": payload.get("risks_known"),
            "payload": payload,
        })
    if not rows:
        return
    results_db.save("quality", rows,
                    target_date or datetime.now().date().isoformat(),
                    argv="stock_quality_screen")


# ---------------------------------------------------------------------------
# 单股票
# ---------------------------------------------------------------------------


def _run_single(args: argparse.Namespace) -> int:
    try:
        result = _generate(args.stock_code, bundle=args.bundle,
                           timeout=args.timeout, name_override=args.name,
                           retries=args.retries, retry_wait=args.retry_wait)
    except SkipStock as exc:
        # 不是错误：不满足处理条件（上市不足 3 年），不入库，退出码 0
        print(f"跳过  {args.stock_code}：{exc}")
        return 0
    except (DataError, ScreenError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"未预期错误：{exc}", file=sys.stderr)
        return 3

    _save_results({result.code: result}, None)

    if not args.quiet:
        data = result.data or {}
        d = data["decision"]
        dq = data.get("data_quality") or {}
        applicable = (dq.get("risk_total") or 0) - (dq.get("risk_na") or 0)
        print(f"已写入结果库  {result.code} {result.name}")
        print(_score_line(data))
        print(f"  数据    风险检查 {dq.get('risk_known', '—')}/{applicable or '—'}；"
              f"机构覆盖 {dq.get('institution_coverage', '—')} 家")
        print(f"  结论    {d['label']}（{d['reason']}）")
        print("  查看    ./run.py web")
        if result.gaps:
            print("  数据缺口：" + "；".join(
                f"{x.get('tool')}: {x.get('error')}" for x in result.gaps[:4]))
    return 0


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------


def _run_batch(args: argparse.Namespace) -> int:
    if args.bundle:
        print("错误：批量模式不支持 --bundle（每只股票都要实时取数）", file=sys.stderr)
        return 2
    if args.name:
        print("错误：批量模式不能用 --name（每只股票用自身公司名）", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("错误：--workers 必须大于等于 1", file=sys.stderr)
        return 2

    if args.from_select is not None:
        target_date = _resolve_date(args.from_select)
        items = _items_from_results(target_date)
        source = f"结果库 选股 {target_date or '最近一次'}"
    else:
        target_date = None
        try:
            items = _items_from_csv(args.csv_input)
        except CsvError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        source = os.path.basename(args.csv_input)
    if not items:
        print("错误：名单是空的，没有可算的股票", file=sys.stderr)
        return 2

    codes: list[str] = []
    names: dict[str, str] = {}
    row_count = 0
    invalid = 0
    for raw, name in items:
        row_count += 1
        try:
            code = normalize_code(raw)
        except ValueError as exc:
            print(f"  跳过 {raw}：{exc}", file=sys.stderr)
            invalid += 1
            continue
        names.setdefault(code, name)
        if code not in codes:
            codes.append(code)

    existing = {} if args.force else _existing(target_date)
    reused: dict[str, dict] = {}
    pending: list[str] = []
    for code in codes:
        hit = existing.get(code) or existing.get(code[-6:])
        if hit:
            reused[code] = hit
        else:
            pending.append(code)

    if not args.quiet:
        if args.force:
            print("--force：忽略库里已有结论，全部重算")
        tail = f"，库里已有 {len(reused)} 只" if reused else ""
        print(f"待算 {len(pending)} 只{tail}")

    results: dict[str, ReportResult] = {}
    for code, hit in reused.items():
        import json as _json
        try:
            payload = _json.loads(hit.get("payload") or "{}")
        except (TypeError, ValueError):
            payload = {}
        results[code] = ReportResult(code=code, payload=payload,
                                     name=hit.get("name") or names.get(code, ""))

    if pending:
        pool = ThreadPoolExecutor(max_workers=args.workers)
        futures = {pool.submit(
            _generate, code, timeout=args.timeout,
            name_override=names.get(code, ""),
            retries=args.retries, retry_wait=args.retry_wait): code
            for code in pending}
        total = len(futures)
        try:
            for done, future in enumerate(as_completed(futures), 1):
                code = futures[future]
                try:
                    result = future.result()
                except SkipStock as exc:
                    result = ReportResult(code=code, skipped=True)
                    if not args.quiet:
                        print(f"[{done:>3}/{total}] {code}  跳过：{exc}", file=sys.stderr)
                except Exception as exc:  # noqa: BLE001
                    result = ReportResult(code=code, error=str(exc))
                    print(f"[{done:>3}/{total}] {code} 生成失败：{exc}", file=sys.stderr)
                results[code] = result
                if not args.quiet and result.data:
                    label = (result.data.get("decision") or {}).get("label") or ""
                    print(f"[{done:>3}/{total}] {result.name} {code}  {label}")
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            # 已算完的先写库：不写的话这一批等于白跑
            _save_results(results, target_date)
            print("\n已中断：已完成的结果已写入 results.db；"
                  "重新运行会跳过库里已有的票。", file=sys.stderr)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(130)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    _save_results(results, target_date)

    ok = sum(1 for r in results.values() if r.data or r.payload)
    skipped = sum(1 for r in results.values() if r.skipped)
    failed = max(0, row_count - ok - skipped)
    if not args.quiet:
        print(f"\n批量完成：成功 {ok} 只（复用 {len(reused)} 只），跳过 {skipped} 只，"
              f"失败 {failed + invalid} 只，共 {row_count} 行")
        print(f"  名单    {source}")
        print("  结果    results.db（./run.py web 查看）")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="企业质量初筛：质量 + 估值 + 机构预期，结果写入 results.db"
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--stock-code", help="股票代码，如 600519 / sh600519 / 600519.SH")
    source.add_argument(
        "--csv", "--csv-input", dest="csv_input", metavar="CSV",
        help="批量模式：读该 CSV 的代码列（代码 / 股票代码 / 证券代码 / code）。"
             "输入只读，不会被改动，也不会产出新的 CSV。"
             "--csv-input 是这个参数的旧名，行为完全一致。",
    )
    source.add_argument("--from-select", metavar="日期", nargs="?", const="", default=None,
                        help="批量模式：直接用结果库里这一天的选股名单（省略日期=最近一次）")
    p.add_argument("--name", default="", help="覆盖公司名（仅单股票模式）")
    p.add_argument("--bundle", default="", help="离线采集包路径（旧 raw JSON），不发网络请求")
    p.add_argument("--timeout", type=float, default=90.0, help="单次取数超时秒数，默认 90")
    p.add_argument("--workers", type=int, default=4, help="批量并发线程数，默认 4")
    p.add_argument("--retries", type=int, default=2,
                   help="取数失败后的重试次数（限频 / 非 JSON / 超时），默认 2")
    p.add_argument("--retry-wait", type=float, default=60.0,
                   help="重试前等待秒数，默认 60")
    p.add_argument("--force", action="store_true",
                   help="批量模式忽略库里已有结论，全部重算")
    p.add_argument("--quiet", action="store_true", help="只输出错误")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.retries < 0:
        print("错误：--retries 不能小于 0", file=sys.stderr)
        return 2
    if args.retry_wait < 0:
        print("错误：--retry-wait 不能小于 0", file=sys.stderr)
        return 2
    # --quiet 连取数层的重试警告一起静音（失败仍会记进数据缺口）
    set_quiet(args.quiet)
    try:
        if args.csv_input or args.from_select is not None:
            return _run_batch(args)
        return _run_single(args)
    except KeyboardInterrupt:
        print("\n已中断。重新运行同一条命令即可从库里已有的结论继续。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
