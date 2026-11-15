"""CSV 批量编排（README.md「CLI 参考」）。

产物落点：**默认全部落在 config.DEFAULT_OUT_DIR（项目内 html-report/），扁平结构**
    html-report/
    ├── 池.html                                    ← 索引页（CSV 主名 + .html）
    ├── 贵州茅台-600519-周期弹性初筛-20261112-144710.html
    └── ...

三条硬约束：
  * **输入 CSV 只读**，一个字节都不改（CLI 层用 md5 断言）。
  * **不产出任何汇总表/结果文件**。
  * 报告文件名的时间戳**整批共用一个**，便于按批次归档。
"""

import csv
import datetime
import os
import random
import re
import threading

import config
from cycle import build as build_mod
from cycle import codes, decide, fetch, metrics, render

#: 代码列的可能列名（小写比较）
CODE_COLUMNS = ("代码", "股票代码", "证券代码", "code", "symbol", "secuCode")
#: 名称列的可能列名
NAME_COLUMNS = ("名称", "股票名称", "证券简称", "name", "secuAbbr")

#: 报告文件名匹配：<公司名>-<代码>-周期弹性初筛-<时间戳>.html
_REPORT_RE = re.compile(r"-(?P<code>\d{6})-" + re.escape(config.REPORT_NAME_TAG) + r"-(?P<ts>\d{8}-\d{6})\.html$")

#: 文件名非法字符
_ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def safe_name(text, fallback="未命名"):
    """清洗成可用作文件名的字符串。

    Windows 不允许 `\\ / : * ? " < > |`，也忌讳首尾的点和空格。
    """
    if text is None:
        return fallback
    cleaned = _ILLEGAL.sub("_", str(text))
    cleaned = cleaned.strip().strip(".")
    return cleaned or fallback


def report_filename(name, code, timestamp):
    """`<公司名>-<代码>-周期弹性初筛-<时间戳>.html`"""
    return f"{safe_name(name, code)}-{code}-{config.REPORT_NAME_TAG}-{timestamp}.html"


def index_filename(csv_path):
    """`池.csv` → `池.html`；`我的股票池.csv` → `我的股票池.html`。"""
    base = os.path.basename(csv_path)
    stem, _ext = os.path.splitext(base)
    return safe_name(stem, "索引") + ".html"


def batch_timestamp(now=None):
    """整批共用的时间戳。"""
    return (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")


def write_atomic(path, text):
    """先写临时文件再 rename，避免中断留下半个文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# CSV 读取
# ---------------------------------------------------------------------------


class CsvRow:
    """CSV 的一行（可能失败）。"""

    __slots__ = ("line", "code_raw", "name_raw", "code", "error", "duplicate")

    def __init__(self, line, code_raw, name_raw):
        self.line = line
        self.code_raw = code_raw or ""
        self.name_raw = name_raw or ""
        self.code = ""
        self.error = ""
        self.duplicate = False


def read_csv(path):
    """读 CSV，返回 [CsvRow]。**只读，不写回。**

    列名匹配不区分大小写，且允许前后空格。找不到代码列时抛 ValueError。
    BOM 必须用 utf-8-sig 吃掉，否则第一列名会带 \ufeff 而匹配失败。
    """
    if not os.path.exists(path):
        raise ValueError(f"CSV 文件不存在：{path}")

    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)

        header = None
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue              # 跳过空行
            header = [c.strip() for c in raw]
            break
        if header is None:
            return []

        code_idx = _find_column(header, CODE_COLUMNS)
        if code_idx is None:
            raise ValueError(
                f"CSV 里找不到代码列。可用列名：{'、'.join(CODE_COLUMNS[:4])}；"
                f"实际表头：{'、'.join(header)}"
            )
        name_idx = _find_column(header, NAME_COLUMNS)

        line_no = 1
        for raw in reader:
            line_no += 1
            if not any(cell.strip() for cell in raw):
                continue
            code_raw = raw[code_idx].strip() if code_idx < len(raw) else ""
            name_raw = raw[name_idx].strip() if (name_idx is not None and name_idx < len(raw)) else ""
            rows.append(CsvRow(line_no, code_raw, name_raw))
    return rows


def _find_column(header, candidates):
    lowered = [h.strip().lower() for h in header]
    for cand in candidates:
        key = cand.lower()
        if key in lowered:
            return lowered.index(key)
    return None


# ---------------------------------------------------------------------------
# 已有报告复用
# ---------------------------------------------------------------------------


def scan_existing(out_dir):
    """扫描已有报告，返回 {code: (timestamp, path)}，同代码取最新。"""
    found = {}
    if not os.path.isdir(out_dir):
        return found
    for fname in os.listdir(out_dir):
        m = _REPORT_RE.search(fname)
        if not m:
            continue
        code = m.group("code")
        ts = m.group("ts")
        prev = found.get(code)
        if prev is None or ts > prev[0]:
            found[code] = (ts, os.path.join(out_dir, fname))
    return found


# ---------------------------------------------------------------------------
# 单只处理
# ---------------------------------------------------------------------------


class ItemResult:
    """一只股票的批量结果。"""

    __slots__ = ("row", "ok", "path", "decision", "stock", "error", "reused")

    def __init__(self, row):
        self.row = row
        self.ok = False
        self.path = ""
        self.decision = None
        self.stock = None
        self.error = ""
        self.reused = False


def process_one(code, name_hint="", out_dir=None, timestamp=None, force=False):
    """处理一只股票：取数 → 计算 → 判定 → 渲染 → 落盘。

    返回 (ok, path, decision, error)。**不抛错**（除 Ctrl-C），失败信息走 error。
    """
    out_dir = out_dir or config.DEFAULT_OUT_DIR
    timestamp = timestamp or batch_timestamp()
    ensure_dir(out_dir)

    if not force:
        existing = scan_existing(out_dir).get(code)
        if existing:
            path = existing[1]
            decision = parse_report_decision(path)
            return True, path, decision, ""

    try:
        raw = fetch.fetch_one(code)
    except fetch.DataError as exc:
        return False, "", None, str(exc)

    stock = build_mod.build(raw)
    if not stock["quarters"]:
        return False, "", None, "无有效季度数据"

    m = metrics.compute_all(stock["quarters"], config)
    decision = decide.decide(stock["quarters"], m, stock["industry"], config)
    # 最近报告期挂进 decision：结果库要用它当基准日（各股未必同一期）
    decision.setdefault("meta", {})["latest"] = str(
        (stock["quarters"][-1] or {}).get("date") or "")[:10]
    charts = metrics.series_for_chart(stock["quarters"], config)
    html = render.render(stock, decision, m, charts)

    display_name = stock.get("name") or name_hint or code
    path = os.path.join(out_dir, report_filename(display_name, code, timestamp))
    write_atomic(path, html)
    return True, path, decision, ""


# ---------------------------------------------------------------------------
# 从报告里解析 data-*（续跑复用 + 索引页回填）
# ---------------------------------------------------------------------------

_ATTR_RE = {
    "code": re.compile(r'data-code="([^"]*)"'),
    "name": re.compile(r'data-name="([^"]*)"'),
    "quarters": re.compile(r'data-quarters="([^"]*)"'),
    "gm_std": re.compile(r'data-gm-std="([^"]*)"'),
    "cycle": re.compile(r'data-cycle="([^"]*)"'),
    "elastic": re.compile(r'data-elastic="([^"]*)"'),
    "elastic_ratio": re.compile(r'data-elastic-ratio="([^"]*)"'),
    "position": re.compile(r'data-position="([^"]*)"'),
    "position_pct": re.compile(r'data-position-pct="([^"]*)"'),
    "financial": re.compile(r'data-financial="([^"]*)"'),
    "gm_cv": re.compile(r'data-gm-cv="([^"]*)"'),
    "cycle_channel": re.compile(r'data-cycle-channel="([^"]*)"'),
    "capacity_direction": re.compile(r'data-capacity-direction="([^"]*)"'),
    "elastic_reason": re.compile(r'data-elastic-reason="([^"]*)"'),
    "latest": re.compile(r'data-latest="([^"]*)"'),
}

_HTML_UNESCAPE = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                  ("&quot;", '"'), ("&#39;", "'"))


def parse_report_decision(path):
    """从已有报告 HTML 里把 9 个 data-* 读回来，拼成 decision 结构。

    缺属性时对应项为 None / "na"，**不抛错** —— 索引页宁可显示 `—`。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            head = fh.read(8192)
    except OSError:
        return None

    got = {}
    for key, pattern in _ATTR_RE.items():
        m = pattern.search(head)
        got[key] = _unescape(m.group(1)) if m else ""

    if not got.get("code") and not got.get("name"):
        return None

    def as_float(key):
        val = got.get(key) or ""
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    cycle_label = got.get("cycle") or decide.NA
    cycle_num = {"1": decide.CYCLE_YES, "0": decide.CYCLE_NO}.get(cycle_label, decide.NA)

    return {
        "cycle": {"label": cycle_num, "gm_std": as_float("gm_std"),
                  "gm_cv": as_float("gm_cv"),
                  # 老报告（双通道之前）没有 data-cycle-channel，按 None 处理：
                  # 那时标签只可能来自绝对通道，渲染层不会误标"相对波动"。
                  "channel": got.get("cycle_channel") or None},
        "elastic": {"label": got.get("elastic") or decide.NA,
                    "ratio": as_float("elastic_ratio"),
                    # 老报告没有 data-elastic-reason，按 None 处理：
                    # 渲染层只会退回"营收增速不足"那句通用文案，不会误报原因。
                    "reason": got.get("elastic_reason") or None},
        "position": {"label": got.get("position") or decide.NA,
                     "pct": as_float("position_pct"),
                     "capacity_direction": got.get("capacity_direction") or None},
        # 老报告没有 data-financial（"9 个锚点"时代），按 0 处理 ——
        # 最坏结果只是索引页把"不适用"显示成"—"，不会误判成别的标签。
        "flags": {"financial": got.get("financial") == "1",
                  "insufficient_history": False},
        # latest 是最近报告期（结果库拿它当基准日）；老报告没这个属性时为空串，
        # 由调用方退回今天——不能瞎猜一期，宁可承认不知道。
        "meta": {"quarters": int(as_float("quarters") or 0),
                 "latest": got.get("latest") or ""},
    }


def _unescape(text):
    for raw, safe in _HTML_UNESCAPE:
        text = text.replace(raw, safe)
    return text


# ---------------------------------------------------------------------------
# 批量主流程
# ---------------------------------------------------------------------------


def run_batch(csv_path, out_dir=None, workers=4, force=False, progress=None,
              build_index=True):
    """跑完一个 CSV。返回 (results, index_path)。

    `progress(done, total, code, ok, message)` 可选回调，用于打进度。
    失败行不影响其他股票；Ctrl-C 会向上抛，已完成的部分保留。
    """
    out_dir = out_dir or config.DEFAULT_OUT_DIR
    ensure_dir(out_dir)
    timestamp = batch_timestamp()

    rows = read_csv(csv_path)
    results = _new_results(rows)

    # 去重：同代码只处理第一次出现，其余标"重复"
    seen = {}
    to_process = []
    for item in results:
        if item.row.error:
            continue
        code = item.row.code
        if code in seen:
            item.row.duplicate = True
            item.error = "重复"
            continue
        seen[code] = item
        to_process.append(item)

    total = len(to_process)
    done_counter = {"n": 0}
    lock = threading.Lock()

    def work(item):
        try:
            ok, path, decision, error = process_one(
                item.row.code,
                name_hint=item.row.name_raw,
                out_dir=out_dir,
                timestamp=timestamp,
                force=force,
            )
            item.ok = ok
            item.path = path
            item.decision = decision
            item.error = error
        except KeyboardInterrupt:
            raise
        except Exception as exc:                      # 单只的意外不能拖垮整批
            item.ok = False
            item.error = f"未预期异常：{exc}"
        finally:
            with lock:
                done_counter["n"] += 1
                done = done_counter["n"]
            if progress:
                progress(done, total, item.row.code, item.ok, item.error)

    if workers <= 1:
        for item in to_process:
            work(item)
    else:
        _run_threaded(to_process, work, workers)

    _save_results(results)

    index_path = ""
    if build_index:
        from cycle import index_html
        index_path = index_html.build_index(
            rows=results,
            csv_path=csv_path,
            out_dir=out_dir,
        )

    return results, index_path


def _save_results(results) -> None:
    """把批量结果写进 results.db（派生数据，失败只 warn）。

    `decision` 是 decide() 的三档结构；字段名对齐 results.cycle_result。
    失败的股票不入库——结果库只存"算出来的结论"，失败原因看 stderr。
    """
    try:
        from stock_data_fetch import results as results_db
    except Exception:                            # noqa: BLE001
        return
    rows = []
    for item in results:
        d = item.decision or {}
        if not item.ok or not d:
            continue
        cyc = d.get("cycle") or {}
        ela = d.get("elastic") or {}
        pos = d.get("position") or {}
        rows.append({
            "symbol": item.row.code, "name": item.stock.get("name") if item.stock else item.row.name_raw,
            "is_cycle": cyc.get("label") == 1,      # decide.CYCLE_YES
            "gm_std": cyc.get("gm_std"), "gm_cv": cyc.get("gm_cv"),
            "gm_median": cyc.get("gm_median"),
            "elasticity": ela.get("ratio"), "elastic_label": ela.get("label"),
            "position_pct": pos.get("percent"), "position_label": pos.get("label"),
            "payload": d,
        })
    if not rows:
        return
    results_db.save("cycle", rows, _latest_quarter(results) or _today(),
                    argv="stock_cycle_elastic")


def _latest_quarter(results) -> str:
    """结果里最新的报告期（批量里各股拉到的季度未必同一期）。"""
    best = ""
    for item in results:
        d = str(((item.decision or {}).get("meta") or {}).get("latest") or "")
        if d > best:
            best = d
    return best


def _today() -> str:
    import datetime as dt
    return dt.date.today().isoformat()


def _new_results(rows):
    """把 CsvRow 转成 ItemResult，并就地做代码校验。"""
    out = []
    for row in rows:
        item = ItemResult(row)
        item.error = row.error
        if not row.error:
            if not row.code_raw:
                item.error = "代码为空"
            else:
                try:
                    row.code = codes.parse(row.code_raw)
                except ValueError:
                    item.error = f"代码无效：{row.code_raw}"
        out.append(item)
    return out


def _run_threaded(items, work, workers):
    """简单的线程池：队列 + N 个 worker。

    不用 concurrent.futures 是为了更早暴露 KeyboardInterrupt
    （futures 会等所有任务收尾，Ctrl-C 要等好几秒）。
    """
    queue = list(items)
    queue.reverse()
    q_lock = threading.Lock()
    errors = []

    def worker():
        while True:
            with q_lock:
                if not queue:
                    return
                item = queue.pop()
            try:
                work(item)
            except KeyboardInterrupt:
                with q_lock:
                    queue.clear()
                errors.append(KeyboardInterrupt())
                return
            except Exception as exc:                  # 兜底，work 内部已经处理过
                errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=False)
               for _ in range(max(1, min(workers, len(items))))]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        raise
    for exc in errors:
        if isinstance(exc, KeyboardInterrupt):
            raise exc


# ---------------------------------------------------------------------------
# --stats 校准（README.md「参数全表」）
# ---------------------------------------------------------------------------


def run_stats(sample_size=500, seed=None, progress=None):
    """随机抽样跑一遍，只出统计。不产出任何报告。

    数据来源：本地 data/stock_names.csv（--refresh-names 生成）。
    没有该文件时抛 ValueError，提示先生成。
    """
    from cycle import stats as stats_mod
    return stats_mod.run(
        sample_size=sample_size, seed=seed,
        progress=progress,
    )
