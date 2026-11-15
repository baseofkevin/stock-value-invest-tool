#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业质量初筛 CLI。

单股票：
    python3 stock_report.py --stock-code 600519
    python3 stock_report.py --stock-code 600519 --bundle tests/fixtures/mini_bundle.json

批量（读取 CSV 的代码列，多线程生成报告；输入 CSV 只读，不写任何 CSV）：
    python3 stock_report.py --csv ../stock-kline-screen-tool/csv_output/选股_2026-11-06.csv

输出目录不开放命令行参数，统一由 `_out_dir_for()` 决定，四个工具都落在
`stock-value-invest-tool/output/<工具名>/` 下（见 output/README.md）：

    批量  output/quality-screen/<批次日期>/   （批次日期取自 CSV 文件名或「启动日」列）
    单股  output/quality-screen/              （没有批次概念，直接落根下）

原因：以前 `--out-dir` 默认值是源码目录里的 `html-report/`，又允许随意覆盖，
于是同一个工具的报告散在源码目录、`html-report2/`、`/tmp/初筛` 好几处，
「哪份是最新的」只能靠翻 mtime 猜。取消这个参数后产物位置是可预测的。

批量产物只有两处：单股 HTML 与同目录的导航页 `<输入 CSV 主名>.html`。
**输入 CSV 本身不会被改动**，指向只读表也安全；批量不产出 CSV，也不产出 Excel：
评分、链接与二级行业等扩展列只体现在导航页里。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from screen.batch import (  # noqa: E402
    EASTMONEY_COLUMN,
    LINK_COLUMN,
    SCORE_COLUMN,
    SKIP_SCORE_TEXT,
    CsvError,
    ReportResult,
    eastmoney_link_cell,
    ensure_result_columns,
    read_csv_table,
    report_link_cell,
)
from screen.compute import build_report_data  # noqa: E402
from screen.fetch import (DataError, close_hub, fetch_all, load_bundle,  # noqa: E402
                          normalize_code, set_quiet)
from screen.index_html import write_index_html  # noqa: E402
from screen.render import render_html  # noqa: E402
from screen.score import compute_scores  # noqa: E402
from screen.summary import SummaryError, extract_score_summary  # noqa: E402
from screen.validate import ScreenError, SkipStock, validate  # noqa: E402


def _safe_name(value: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(value)).strip(" .")
    return s or "企业"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# 批次日期长这样：2026-11-13 / 2026-11-13T09:30 / 2026/11/13。
# CSV 文件名（选股_2026-11-13.csv）和「启动日」列都是这个形状。
_DATE_RE = re.compile(r"(\d{4})[-/.](\d{2})[-/.](\d{2})")


def _date_from_name(path: str | Path) -> str | None:
    """从文件名里抠出 YYYY-MM-DD；抠不到返回 None。

    取**最后一个**匹配：文件名可能是 `选股_2026-11-13_备份-2026-11-14.csv`，
    最后那个才是想表达的日期（也是重命名时最可能改的）。
    """
    hits = _DATE_RE.findall(Path(path).stem)
    if not hits:
        return None
    y, m, d = hits[-1]
    return f"{y}-{m}-{d}"


def _date_from_rows(rows: list[dict[str, str]], column: str = "启动日") -> str | None:
    """从「启动日」列取批次日期：取出现最多的那个非空值。

    用众数而不是第一行：CSV 可能混进别的交易日（拼接两次选股结果），
    第一行恰好是异类时整批报告会落到错的日期目录里。
    """
    counts: dict[str, int] = {}
    for row in rows:
        m = _DATE_RE.search(str(row.get(column) or ""))
        if not m:
            continue
        key = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    # 并列时取日期最大的，保证同样的输入稳定得到同一个目录
    return max(counts, key=lambda k: (counts[k], k))


def _out_dir_for(csv_path: str | Path | None = None,
                 rows: list[dict[str, str]] | None = None) -> Path:
    """本次运行该往哪个目录写报告。

    - 批量（给了 csv_path）：`output/quality-screen/<批次日期>/`，
      日期先看 CSV 文件名，再看「启动日」列，都没有才退化成当天。
    - 单股（csv_path=None）：`output/quality-screen/`，没有批次可言，不加日期层。

    没有「用户显式指定」这一路：`--out-dir` 参数已删除，见模块文档。
    """
    base = Path(os.path.expanduser(config.DEFAULT_OUT_DIR)).resolve()
    if csv_path is None:
        return base
    batch_date = _date_from_name(csv_path) or _date_from_rows(rows or [])
    if not batch_date:
        batch_date = f"{datetime.now():%Y-%m-%d}"
    return base / batch_date


def _find_existing_report(out_dir: Path, code: str) -> Path | None:
    """在输出目录里找该股票最新的一份完整报告（增量续跑的唯一依据）。"""
    if not out_dir.is_dir():
        return None
    marker = f"-{code}-"
    matches = [
        p for p in out_dir.glob("*-企业质量初筛-*.html")
        if marker in p.name and p.is_file()
    ]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="企业质量初筛：质量 + 估值 + 机构预期，输出 HTML；支持 CSV 批量"
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--stock-code", help="股票代码，如 600519 / sh600519 / 600519.SH")
    source.add_argument(
        "--csv", "--csv-input", dest="csv_input", metavar="CSV",
        help="批量模式：读取该 CSV 的代码列（代码 / 股票代码 / 证券代码 / code），逐只生成报告。"
             "输入 CSV 只读，不会被改动，也不会产出新的 CSV 或 Excel；评分、链接与"
             "二级行业等扩展列只体现在导航页（输出目录下的 <输入 CSV 主名>.html）里。"
             "--csv-input 是这个参数的旧名，行为完全一致。",
    )
    p.add_argument("--name", default="", help="覆盖报告文件名中的公司名（仅单股票模式）")
    p.add_argument("--bundle", default="", help="离线采集包路径（旧 raw JSON），不发起网络请求")
    p.add_argument("--timeout", type=float, default=90.0, help="单次取数超时秒数，默认 90")
    p.add_argument("--workers", type=int, default=4, help="批量模式并发线程数，默认 4")
    p.add_argument("--retries", type=int, default=2,
                   help="取数失败后的重试次数（限频 / 非 JSON / 超时），默认 2")
    p.add_argument("--retry-wait", type=float, default=60.0,
                   help="重试前等待秒数，默认 60")
    p.add_argument("--force", action="store_true",
                   help="批量模式忽略已有报告，全部重新生成")
    p.add_argument("--quiet", action="store_true", help="只输出错误")
    return p


def _generate_report(
    stock_code: str,
    *,
    out_dir: Path,
    bundle: str,
    timeout: float,
    name_override: str = "",
    with_code: bool = False,
    stamp: str | None = None,
    need_summary: bool = False,
    retries: int = 0,
    retry_wait: float = 60.0,
) -> ReportResult:
    """抓数、计算、渲染并落盘一只股票；供单股票和批量线程共用。"""
    if bundle:
        raw = load_bundle(bundle)
    else:
        try:
            raw = fetch_all(stock_code, timeout=timeout, retries=retries,
                            retry_wait=retry_wait)
        finally:
            # 取数完**无论如何**都关掉本线程的 Hub：SQLite 连接是线程私有的，
            # 批量模式下每个 worker 会各开一个。不关的话要到进程退出才释放，
            # 39 只 × 4 线程会攒下几十个句柄（WAL 文件跟着涨）。
            # 失败时更要关——否则那只票的异常会连带漏掉一个连接。
            # 下面的计算与渲染完全不碰数据库，所以这里关是安全的。
            close_hub()
    data = build_report_data(raw["calls"], raw["code"])
    data["data_gaps"] = raw.get("errors") or []
    data = compute_scores(data)
    validate(data)
    html_text = render_html(data)

    name = name_override.strip() or data["meta"].get("name") or data["meta"]["code"]
    code = data["meta"]["code"]
    report_stamp = stamp or f"{datetime.now():%Y%m%d-%H%M%S}"
    base = _safe_name(name)
    if with_code:
        base = f"{base}-{code}"
    filename = f"{base}-企业质量初筛-{report_stamp}.html"
    target = out_dir / filename

    summary = None
    summary_error = ""
    if need_summary:
        try:
            summary = extract_score_summary(html_text)
        except SummaryError as exc:
            summary_error = str(exc)

    _write_atomic(target, html_text)
    return ReportResult(
        code=code,
        name=name,
        target=target,
        summary=summary,
        summary_error=summary_error,
        data=data,
        html_text=html_text,
        gaps=raw.get("errors") or [],
    )


def _score_line(data: dict) -> str:
    """CLI 摘要里的 Q/V 那一行。

    金融股的 Q/V 是 None（not_applicable），直接 `f"{x:.1f}"` 会抛
    TypeError，所以这里显式区分“不适用”和“算不出来（—）”。
    两栏写法必须一致：Q 也不能一律打成「不适用」。
    """
    q_block = data.get("quality") or {}
    qs = q_block.get("score")
    if isinstance(qs, (int, float)):
        q_text = f"{qs:.1f} / 100"
    else:
        q_text = "不适用" if q_block.get("not_applicable") else "—"
    v_block = data.get("valuation_score") or {}
    vs = v_block.get("score")
    if isinstance(vs, (int, float)):
        v_text = f"{vs:.1f} / 100"
    else:
        v_text = "不适用" if v_block.get("not_applicable") else "—"
    return f"  质量分 Q {q_text}   估值分 V {v_text}"


def _run_single(args: argparse.Namespace) -> int:
    out_dir = _out_dir_for()
    try:
        result = _generate_report(
            args.stock_code,
            out_dir=out_dir,
            bundle=args.bundle,
            timeout=args.timeout,
            name_override=args.name,
            retries=args.retries,
            retry_wait=args.retry_wait,
        )
    except SkipStock as exc:
        # 不是错误：不满足处理条件（上市不足 3 年），不生成报告，退出码 0
        print(f"跳过  {args.stock_code}：{exc}")
        return 0
    except (DataError, ScreenError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"未预期错误：{exc}", file=sys.stderr)
        return 3

    data = result.data or {}
    target = result.target
    if not args.quiet:
        d = data["decision"]
        dq = data.get("data_quality") or {}
        # 分母用「适用项」：金融股有 5 项口径不适用，算进分母会出现 6/14 这种假数字
        applicable = (dq.get("risk_total") or 0) - (dq.get("risk_na") or 0)
        print(f"已生成  {target}")
        print(_score_line(data))
        print(f"  数据    风险检查 {dq.get('risk_known', '—')}/{applicable or '—'}；"
              f"机构覆盖 {dq.get('institution_coverage', '—')} 家")
        print(f"  结论    {d['label']}（{d['reason']}）")
        print(f"  文件    {target.stat().st_size / 1024:.1f} KB")
        if result.gaps:
            print("  数据缺口：" + "；".join(
                f"{x.get('tool')}: {x.get('error')}" for x in result.gaps[:4]
            ))
    return 0


def _apply_result(table, indices: list[int], result: ReportResult) -> None:
    for index in indices:
        row = table.rows[index]
        if result.skipped:
            row[SCORE_COLUMN] = SKIP_SCORE_TEXT
            row[LINK_COLUMN] = ""
        elif result.error:
            row[SCORE_COLUMN] = "生成失败"
            row[LINK_COLUMN] = ""
        elif result.summary is None:
            row[SCORE_COLUMN] = "评分提取失败"
            row[LINK_COLUMN] = report_link_cell(result.target) if result.target else ""
        else:
            row[SCORE_COLUMN] = result.summary.conclusion
            row[LINK_COLUMN] = report_link_cell(result.target) if result.target else ""


def _run_batch(args: argparse.Namespace) -> int:
    if args.bundle:
        print("错误：批量模式（--csv）不支持 --bundle（每个股票需要实时取数）", file=sys.stderr)
        return 2
    if args.name:
        print("错误：批量模式（--csv）不能使用 --name（每只股票使用自身公司名）", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("错误：--workers 必须大于等于 1", file=sys.stderr)
        return 2

    try:
        table = read_csv_table(args.csv_input)
        ensure_result_columns(table)
    except CsvError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    row_codes: dict[int, str] = {}
    row_errors: dict[int, str] = {}
    unique_codes: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(table.rows):
        raw_code = (row.get(table.code_column) or "").strip()
        if not raw_code:
            row_errors[index] = "缺少股票代码"
            continue
        try:
            code = normalize_code(raw_code)
        except ValueError as exc:
            row_errors[index] = str(exc)
            continue
        row_codes[index] = code
        table.rows[index][EASTMONEY_COLUMN] = eastmoney_link_cell(code)
        if code not in seen:
            seen.add(code)
            unique_codes.append(code)

    code_rows: dict[str, list[int]] = {}
    for index, code in row_codes.items():
        code_rows.setdefault(code, []).append(index)

    # 输出目录按批次分：先读表，才能从「启动日」列兜底取日期
    out_dir = _out_dir_for(args.csv_input, table.rows)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"

    # 先扫描已有报告：中断后重跑时不重新抓数，只补缺失的股票。
    pending_codes: list[str] = []
    reused_rows = 0
    reused_codes = 0
    # code → ScoreSummary：结果库要落每只票的 Q/V/结论（复用和现生成的都算）
    summaries: dict[str, object] = {}
    data_by_code: dict[str, dict] = {}
    for code in unique_codes:
        indices = code_rows[code]
        # 续跑只看输出目录里已有的报告（带代码的文件名），不依赖任何 CSV
        existing = None if args.force else _find_existing_report(out_dir, code)
        summary = None
        if existing is not None:
            try:
                summary = extract_score_summary(existing.read_text(encoding="utf-8"))
            except (OSError, SummaryError) as exc:
                if not args.quiet:
                    print(f"已有报告不可用，重新生成 {existing.name}：{exc}", file=sys.stderr)
                existing = None
        if existing is not None and summary is not None:
            summaries[code] = summary
            for index in indices:
                table.rows[index][SCORE_COLUMN] = summary.conclusion
                table.rows[index][LINK_COLUMN] = report_link_cell(existing)
            reused_codes += 1
            reused_rows += len(indices)
        else:
            pending_codes.append(code)
            for index in indices:
                table.rows[index][SCORE_COLUMN] = ""
                table.rows[index][LINK_COLUMN] = ""

    for index, error in row_errors.items():
        table.rows[index][SCORE_COLUMN] = "代码无效" if "应为" in error else error
        table.rows[index][LINK_COLUMN] = ""
        table.rows[index][EASTMONEY_COLUMN] = ""

    if not args.quiet:
        if args.force:
            print("--force：忽略已有报告，全部重新生成")
        if reused_codes:
            print(f"已有报告 {reused_codes} 只（{reused_rows} 行），跳过；"
                  f"待生成 {len(pending_codes)} 只")
        else:
            print(f"待生成 {len(pending_codes)} 只股票")

    done_count = 0
    if pending_codes:
        pool = ThreadPoolExecutor(max_workers=args.workers)
        futures = {
            pool.submit(
                _generate_report,
                code,
                out_dir=out_dir,
                bundle="",
                timeout=args.timeout,
                with_code=True,
                stamp=stamp,
                need_summary=True,
                retries=args.retries,
                retry_wait=args.retry_wait,
            ): code
            for code in pending_codes
        }
        total = len(futures)
        try:
            for done, future in enumerate(as_completed(futures), 1):
                code = futures[future]
                try:
                    result = future.result()
                except SkipStock as exc:
                    # 不填 error：跳过不是失败，error 只留给真错误
                    result = ReportResult(code=code, skipped=True)
                    if not args.quiet:
                        print(f"[{done:>3}/{total}] {code}  跳过：{exc}", file=sys.stderr)
                except Exception as exc:  # noqa: BLE001
                    result = ReportResult(code=code, error=str(exc))
                    print(f"[{done:>3}/{total}] {code} 生成失败：{exc}", file=sys.stderr)
                if result.summary is not None:
                    summaries[code] = result.summary
                if getattr(result, "data", None):
                    data_by_code[code] = result.data
                _apply_result(table, code_rows[code], result)
                done_count += 1
                if not args.quiet:
                    if result.summary is not None:
                        print(f"[{done:>3}/{total}] {result.name} {code}  "
                              f"{result.summary.conclusion}")
                    elif not result.error:
                        print(f"[{done:>3}/{total}] {result.name} {code}  "
                              f"报告已生成，评分提取失败")
                if result.summary_error:
                    print(f"[{done:>3}/{total}] {code} 评分提取失败：{result.summary_error}",
                          file=sys.stderr)
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            try:
                write_index_html(table, out_dir)
            except Exception:
                pass
            print("\n已中断：本次已完成的报告已保存，导航页按已完成部分生成；"
                  "重新运行会跳过已有报告。", file=sys.stderr)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(130)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    ok = 0
    failed = 0
    skipped = 0
    for index, row in enumerate(table.rows):
        if index in row_errors:
            failed += 1
        elif row.get(SCORE_COLUMN) == SKIP_SCORE_TEXT:
            skipped += 1                      # 硬门槛跳过：不是失败，不影响退出码
        elif row.get(SCORE_COLUMN) in ("", "生成失败", "评分提取失败"):
            failed += 1
        else:
            ok += 1

    _save_results(table, summaries, data_by_code, out_dir)

    nav_path: Path | None = None
    try:
        nav_path = write_index_html(table, out_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"警告：导航页生成失败（HTML 报告不受影响）：{exc}", file=sys.stderr)

    if not args.quiet:
        print(f"\n批量完成：成功 {ok} 行（复用 {reused_rows} 行），失败 {failed} 行，"
              f"共 {len(table.rows)} 行；股票 {len(unique_codes)} 只")
        if skipped:
            print(f"  跳过 {skipped} 行（{SKIP_SCORE_TEXT}）")
        if pending_codes:
            print(f"  本次生成 {done_count}/{len(pending_codes)} 只")
        print(f"  输入 CSV {table.path}（只读，未改动）")
        print(f"  报告目录 {out_dir}")
        if nav_path is not None:
            print(f"  导航页   {nav_path}")
    return 0 if failed == 0 else 1


def _num_from_text(text: str) -> float | None:
    """从 "62.3" / "62.3（良好）" 里取第一个数；取不到就是 None。"""
    match = re.search(r"-?\d+(?:\.\d+)?", str(text or ""))
    return float(match.group(0)) if match else None


def _grade_from_text(text: str) -> str:
    """Q/V 文本里的档位（"62.3（良好）" → "良好"），没有就空串。"""
    match = re.search(r"[（(]([^）)]+)[）)]", str(text or ""))
    return match.group(1) if match else ""


_Q_NOTE_RE = re.compile(
    r'data-metric="q"[^>]*>[^<]*</div>\s*<div class="note">([^<]*)</div>', re.S)
_V_NOTE_RE = re.compile(
    r'data-metric="v">[^<]*</div>\s*<div class="note">([^<]*)</div>', re.S)


def _html_extra(code: str, out_dir: Path) -> dict:
    """从已有报告里补 Q/V 档位与风险已知数（续跑复用路径，没有内存数据可用）。

    报告里 Q 那格下面写着「良好 · 风险检查 9/14 已知」，V 那格下面只写档位。
    解析它比再跑一遍计算便宜得多，也是"续跑不重算"这个设计的代价。
    """
    import html as _html
    try:
        path = _find_existing_report(out_dir, code)
        if path is None:
            return {}
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict = {}
    m = _Q_NOTE_RE.search(text)
    if m:
        note = _html.unescape(m.group(1))
        out["q_grade"] = note.split("·")[0].strip()
        hit = re.search(r"风险检查\s*(\d+)\s*/\s*(\d+)", note)
        if hit:
            out["risks_known"] = int(hit.group(1))
            out["risks_applicable"] = int(hit.group(2))
    for key in ("risks_hit", "risks_watch"):
        hit = re.search(rf'data-{key.replace("_", "-")}="(\d+)"', text)
        if hit:
            out[key] = int(hit.group(1))
    m = _V_NOTE_RE.search(text)
    if m:
        out["v_grade"] = _html.unescape(m.group(1)).strip()
    return out


def _save_results(table, summaries, data_by_code, out_dir: Path) -> None:
    """把本批的 Q/V/结论写进 results.db（派生数据，失败只 warn）。

    基准日取输出目录名里的日期（`_out_dir_for` 由启动日推出来的），
    取不到就用今天——质量初筛没有"基准日"概念，这里只求能对上是哪一批。
    """
    try:
        from stock_data_fetch import results
    except Exception:                            # noqa: BLE001
        return
    names = {}
    for index, row in enumerate(table.rows):
        code = table.rows[index].get("代码") or ""
        if code:
            names.setdefault(code, row.get("名称") or "")
    rows = []
    for code, summary in summaries.items():
        payload = _payload(data_by_code.get(code))
        if not payload:
            payload = _html_extra(code, out_dir)
        rows.append({
            "symbol": code, "name": names.get(code),
            "q": payload.get("q_score") if payload.get("q_score") is not None
            else _num_from_text(summary.quality),
            "q_grade": payload.get("q_grade") or _grade_from_text(summary.quality),
            "v": payload.get("v_score") if payload.get("v_score") is not None
            else _num_from_text(summary.valuation),
            "v_grade": payload.get("v_grade") or _grade_from_text(summary.valuation),
            "conclusion": summary.conclusion,
            "reason": payload.get("reason") or "",
            "risks_hit": payload.get("risks_hit"),
            "risks_watch": payload.get("risks_watch"),
            "risks_known": payload.get("risks_known"),
            "payload": {**payload, "quality_text": summary.quality,
                        "valuation_text": summary.valuation},
        })
    if not rows:
        return
    date = next((p.name for p in [out_dir] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)), "")
    results.save("quality", rows, date or datetime.now().date().isoformat(),
                 argv="stock_quality_screen")


def _payload(data: dict | None) -> dict:
    """从内存里的报告数据取几个能直接查的字段（复用的报告没有 data，返回空）。"""
    if not data:
        return {}
    dec = data.get("decision") or {}
    dq = data.get("data_quality") or {}
    checks = ((data.get("risks") or {}).get("checks") or [])
    q = data.get("quality") or {}
    v = data.get("valuation_score") or {}
    return {
        "reason": dec.get("reason") or "",
        "q_grade": q.get("grade") or "",
        "v_grade": v.get("grade") or "",
        "q_score": q.get("score"),
        "v_score": v.get("score"),
        "risks_known": dq.get("risk_known"),
        "risks_hit": sum(1 for c in checks if c.get("status") == "hit"),
        "risks_watch": sum(1 for c in checks if c.get("status") == "watch"),
        "fatal": list(data.get("fatal") or []),
    }


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
        if args.csv_input:
            return _run_batch(args)
        return _run_single(args)
    except KeyboardInterrupt:
        print("\n已中断。批量模式下重新运行同一条命令即可从已有报告继续。",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
