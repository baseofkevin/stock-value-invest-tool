# -*- coding: utf-8 -*-
"""CSV 批量模式：从一份 CSV 里读股票代码，逐只出四因子观察报告。

口径与另外三个工具一致：
- 输入 CSV **只读**，不改写、不产出新 CSV；
- 代码列认 `代码` / `股票代码` / `证券代码` / `code`（大小写不敏感），没有表头时取第一列；
- 同一只股票出现多次只跑一次（保序）。

为什么要有批量：`stock_judge_top` 是买入之后盯顶部用的，平时单只；但持仓一多、
或者想在下周五选股结果上整体扫一遍"谁已经贴着 250 日高点了"，一只只敲太慢。

## 线程与 Hub

`stock_data_fetch` 的 SQLite 连接**不能跨线程**（`SQLite objects created in a thread
can only be used in that same thread`，实测共用会只有第一个线程成功）。
所以这里沿用 `stock_quality_screen/screen/fetch.py` 的做法：每个线程一个 Hub，
存在 `threading.local()` 里，跑完各自 `close()`。
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape

from stock_data_fetch.hub import preflight

from .cli import normalize_code
from .config import OUT_DIR
from .evaluate import evaluate
from .log import log
from .render.html import write_html_file

CODE_COLUMNS = ("代码", "股票代码", "证券代码", "code", "symbol", "股票编码", "证券编码")
NAME_COLUMNS = ("名称", "股票名称", "证券名称", "name", "简称")

_TLS = threading.local()


def _thread_hub(db: str | None):
    h = getattr(_TLS, "hub", None)
    if h is None:
        h = preflight(db_path=db) if db else preflight()
        _TLS.hub = h
    return h


def _close_thread_hub() -> None:
    h = getattr(_TLS, "hub", None)
    if h is not None:
        try:
            h.close()
        finally:
            _TLS.hub = None


# ---------------------------------------------------------------- 读 CSV

def read_targets(path: str) -> tuple[list[dict], list[str]]:
    """读出 [(code, name)]，保序去重。返回 (targets, warnings)。"""
    warnings: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return [], ["CSV 是空的"]

    header = [c.strip().lower() for c in rows[0]]
    code_idx = next((i for i, c in enumerate(header) if c in
                     [x.lower() for x in CODE_COLUMNS]), None)
    name_idx = next((i for i, c in enumerate(header) if c in
                     [x.lower() for x in NAME_COLUMNS]), None)
    if code_idx is None:
        # 没有表头：整个文件都当数据，取第一列
        code_idx, name_idx, body = 0, (1 if len(rows[0]) > 1 else None), rows
    else:
        body = rows[1:]

    out, seen = [], set()
    for lineno, row in enumerate(body, start=1 if code_idx == 0 and not header else 2):
        if not row or not row[code_idx].strip():
            continue
        raw = row[code_idx].strip()
        try:
            code = normalize_code(raw)
        except ValueError as exc:
            warnings.append(f"第 {lineno} 行跳过：{exc}")
            continue
        if code in seen:
            warnings.append(f"第 {lineno} 行 {raw} 与前面的重复，跳过")
            continue
        seen.add(code)
        name = row[name_idx].strip() if (name_idx is not None and name_idx < len(row)) else ""
        out.append({"code": code, "name": name})
    return out, warnings


# ---------------------------------------------------------------- 跑一只

def _one(target: dict, end: str, refresh: bool, db: str | None) -> dict:
    """跑一只股票；异常不外抛，转成结果里的 error。"""
    code, name = target["code"], target["name"]
    try:
        hub = _thread_hub(db)
        r = evaluate(hub, code, end, refresh)
    except Exception as exc:                      # noqa: BLE001 - 单只失败不拖垮批量
        return {"code": code, "name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        path = write_html_file(r)
    except Exception as exc:                      # noqa: BLE001
        path, err = "", f"HTML 写入失败：{exc}"
    else:
        err = ""

    views = [(v.get("title") or "", v.get("state") or "") for v in (r.get("views") or [])]
    return {
        "code": code,
        "name": r.get("name") or name,
        "ok": not err,
        "error": err,
        "date": r.get("date"),
        "close": r.get("close"),
        "pos250": r.get("pos250"),
        "days_since_high": r.get("days_since_high"),
        "phase": r.get("phase"),
        "views": views,
        "html": path,
    }


def run(path: str, end: str, *, refresh: bool = False, workers: int = 4,
        db: str | None = None, quiet: bool = False) -> int:
    """批量入口。返回进程退出码：0 全成 / 1 有失败 / 2 输入有问题。"""
    targets, warnings = read_targets(path)
    for w in warnings:
        print(f"⚠️  {w}")
    if not targets:
        print(f"错误：{path} 里没有可用的股票代码")
        return 2

    total = len(targets)
    print(f"批量顶部观察：{total} 只，基准日 {end}，并发 {workers}，来自 {path}")
    log(f"[批量] {total} 只，基准日 {end}")

    results: list[dict] = []
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_one, t, end, refresh, db): t for t in targets}
            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    r = fut.result()
                except Exception as exc:          # noqa: BLE001 - 兜底，不让线程异常吞掉进度
                    r = {"code": t["code"], "name": t["name"], "ok": False,
                         "error": f"{type(exc).__name__}: {exc}"}
                results.append(r)
                done += 1
                if r["ok"]:
                    if not quiet:
                        print(f"[{done:2d}/{total}] {_line(r)}")
                else:
                    print(f"[{done:2d}/{total}] {r['code']} {r.get('name') or ''} "
                          f"失败：{r['error']}")
    finally:
        _close_thread_hub()

    # 按输入顺序排回来（as_completed 是完成顺序）
    order = {t["code"]: i for i, t in enumerate(targets)}
    results.sort(key=lambda r: order.get(r["code"], 1 << 30))

    failed = [r for r in results if not r["ok"]]
    if not quiet:
        _print_summary(results, failed)
    index = _write_index(results, end)
    if index:
        print(f"\n索引页：{index}")
    print(f"批量完成：成功 {len(results) - len(failed)} 只，失败 {len(failed)} 只，共 {total} 只")
    return 0 if not failed else 1


def _w(text: str) -> int:
    """显示宽度：中日韩字符占 2 列，用它对齐表格。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def _pad(text, width: int, align: str = "left") -> str:
    text = str(text)
    gap = max(0, width - _w(text))
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def _line(r: dict) -> str:
    pos = r.get("pos250")
    pos_txt = f"{pos * 100:+.1f}%" if isinstance(pos, (int, float)) else "—"
    states = "｜".join(f"{t} {s}" for t, s in r.get("views") or [])
    close = r.get("close")
    close_txt = f"{close:.2f}" if isinstance(close, (int, float)) else "—"
    return (f"{r['code']} {_pad(r.get('name') or '', 8)} 收盘 {_pad(close_txt, 8, 'right')}  "
            f"距250日高点 {_pad(pos_txt, 8, 'right')}  {states}")


def _print_summary(results: list[dict], failed: list[dict]) -> None:
    ok = [r for r in results if r["ok"]]
    if not ok:
        return
    widths = (9, 10, 10, 13, 11)
    head = (_pad("代码", widths[0]) + _pad("名称", widths[1])
            + _pad("收盘", widths[2], "right") + _pad("距250日高点", widths[3], "right")
            + _pad("距高点天数", widths[4], "right") + "   四因子")
    rule = "-" * (_w(head) + 8)
    print("\n" + rule)
    print(head)
    print(rule)
    for r in sorted(ok, key=lambda x: (x.get("pos250") is None, -(x.get("pos250") or 0))):
        pos = r.get("pos250")
        pos_txt = f"{pos * 100:+.1f}%" if isinstance(pos, (int, float)) else "—"
        close = r.get("close")
        close_txt = f"{close:.2f}" if isinstance(close, (int, float)) else "—"
        days = r.get("days_since_high")
        states = "｜".join(f"{t} {s}" for t, s in (r.get("views") or []))
        print(_pad(r["code"], widths[0]) + _pad(r.get("name") or "", widths[1])
              + _pad(close_txt, widths[2], "right") + _pad(pos_txt, widths[3], "right")
              + _pad(days if days is not None else "—", widths[4], "right")
              + "   " + states)
    print(rule)
    print("按「距 250 日高点」从近到远排序：越靠上，价格离一年高点越近。")
    print("程序只摆 4 个因子的独立结果，不合并总分、不替你判断顶部。")
    if failed:
        print(f"\n失败 {len(failed)} 只：" + "、".join(f"{r['code']}" for r in failed))


# ---------------------------------------------------------------- 索引页

_INDEX_CSS = """
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
     margin:0;padding:24px;background:#f6f7f9;color:#1d2129}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#86909c;font-size:13px;margin-bottom:18px}
table{border-collapse:collapse;width:100%;background:#fff;
      box-shadow:0 1px 3px rgba(0,0,0,.06);border-radius:8px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;font-size:13px;border-bottom:1px solid #f0f1f3}
th{background:#fafbfc;font-weight:600;color:#4e5969;white-space:nowrap}
tr:hover td{background:#f7f8fa}
td.num{text-align:right;font-variant-numeric:tabular-nums}
a{color:#1664ff;text-decoration:none}
a:hover{text-decoration:underline}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;background:#eef4ff;
     color:#1664ff;font-size:12px;margin-right:6px}
.note{margin-top:14px;color:#86909c;font-size:12px;line-height:1.7}
"""


def _fmt_num(v, nd=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _write_index(results: list[dict], end: str) -> str:
    """把所有单只报告串成一个索引页；写不出来就算了，不影响结论。"""
    ok = [r for r in results if r["ok"]]
    if not ok:
        return ""
    ok.sort(key=lambda x: (x.get("pos250") is None, -(x.get("pos250") or 0)))
    rows = []
    for r in ok:
        pos = r.get("pos250")
        pos_txt = f"{pos * 100:+.1f}%" if isinstance(pos, (int, float)) else "—"
        link = os.path.basename(r.get("html") or "")
        cell = f'<a href="{escape(link)}">{escape(str(r.get("name") or r["code"]))}</a>'
        tags = "".join(f'<span class="tag">{escape(s)}</span>'
                       for _, s in (r.get("views") or []))
        rows.append(
            f"<tr><td>{escape(r['code'])}</td><td>{cell}</td>"
            f"<td class='num'>{_fmt_num(r.get('close'))}</td>"
            f"<td class='num'>{pos_txt}</td>"
            f"<td class='num'>{r.get('days_since_high') if r.get('days_since_high') is not None else '—'}</td>"
            f"<td>{tags}</td></tr>")
    failed = [r for r in results if not r["ok"]]
    fail_html = ""
    if failed:
        fail_html = ("<div class='note'>失败：" +
                     "、".join(f"{escape(r['code'])}（{escape(r['error'])}）" for r in failed) +
                     "</div>")
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>四因子观察 · 批量 | {escape(end)}</title>
<style>{_INDEX_CSS}</style></head><body>
<h1>四因子独立观察 · 批量</h1>
<div class="sub">基准日 {escape(end)} · 共 {len(ok)} 只 · 按「距 250 日高点」从近到远</div>
<table><thead><tr>
<th>代码</th><th>名称</th><th>收盘</th><th>距 250 日高点</th><th>距高点天数</th><th>四因子</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
<div class="note">4 个因子各自独立展示，不合并总分，程序不判断顶部——
顶部是「价格＋量能＋换手＋动能＋业绩」综合看出来的，工具只把料摆齐。<br>
距 250 日高点为 0% 表示就站在一年最高点上；负得越多离高点越远。</div>
{fail_html}
</body></html>
"""
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUT_DIR, f"四因子观察-批量_{end}_{stamp}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path
