"""批量编排：读名单 → 逐只算 → 写 results.db。

2026-11-15 起**不再产出任何文件**（HTML 报告、索引页都移除了，人看结果走
`./run.py web`）。所以这个模块只剩三件事：名单从哪来、逐只怎么算、
结论写哪里。

三条硬约束（沿用旧版）：
  * **输入只读**：无论名单来自 `--csv` 还是结果库，一个字节都不改。
  * **失败不传染**：一只票取数失败只影响它自己，整批照跑。
  * **续跑不重复算**：同一基准日里已经算过的票直接复用库里的结论，
    除非 `--force`。原来靠扫报告文件判断，现在问结果库。
"""

import csv
import datetime
import json
import os
import threading

import config
from cycle import build as build_mod
from cycle import codes as codes_mod
from cycle import decide, fetch, metrics

#: 代码列的可能列名（小写比较）
CODE_COLUMNS = ("代码", "股票代码", "证券代码", "code", "symbol", "secuCode")
#: 名称列的可能列名
NAME_COLUMNS = ("名称", "股票名称", "证券简称", "name", "secuAbbr")


# ---------------------------------------------------------------------------
# 名单来源
# ---------------------------------------------------------------------------


def read_csv(path):
    """读外部 CSV，返回 [(code_raw, name_raw)]。**只读，不写回。**

    列名匹配不区分大小写，且允许前后空格。找不到代码列时抛 ValueError。
    BOM 必须用 utf-8-sig 吃掉，否则第一列名会带 \ufeff 而匹配失败。

    典型用途：拿自己持仓的一份表跑周期弹性——选股名单走 `--from-select`，
    不需要用户先去凑一份 CSV。
    """
    if not os.path.exists(path):
        raise ValueError(f"CSV 文件不存在：{path}")

    out = []
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

        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue
            code_raw = raw[code_idx].strip() if code_idx < len(raw) else ""
            name_raw = raw[name_idx].strip() if (name_idx is not None and name_idx < len(raw)) else ""
            out.append((code_raw, name_raw))
    return out


def _find_column(header, candidates):
    lowered = [h.strip().lower() for h in header]
    for cand in candidates:
        key = cand.lower()
        if key in lowered:
            return lowered.index(key)
    return None


def read_results(target_date):
    """从结果库读选股名单：[(code, name)]。读不到就是空列表。"""
    try:
        from stock_data_fetch import results as results_db
        rows = results_db.symbols_for("select", target_date)
    except Exception:                            # noqa: BLE001
        return []
    return [(r["symbol"], r.get("name") or "") for r in rows if r.get("symbol")]


# ---------------------------------------------------------------------------
# 单只处理
# ---------------------------------------------------------------------------


class ItemResult:
    """一只股票的批量结果。"""

    __slots__ = ("code", "name", "ok", "decision", "stock", "error",
                 "reused", "duplicate")

    def __init__(self, code, name=""):
        self.code = code
        self.name = name
        self.ok = False
        self.decision = None
        self.stock = None
        self.error = ""
        self.reused = False
        self.duplicate = False


def process_one(code, name_hint="", force=False):
    """取数 → 构建 → 指标 → 判定。返回 (ok, decision, stock, error)。

    **不抛错**（除 Ctrl-C）：失败信息走 error，整批继续。
    """
    try:
        raw = fetch.fetch_one(code)
    except fetch.DataError as exc:
        return False, None, None, str(exc)

    stock = build_mod.build(raw)
    if not stock["quarters"]:
        return False, None, None, "无有效季度数据"
    if name_hint and not stock.get("name"):
        stock["name"] = name_hint

    m = metrics.compute_all(stock["quarters"], config)
    decision = decide.decide(stock["quarters"], m, stock["industry"], config)
    # 最近报告期挂进 decision：结果库要用它当基准日（各股未必同一期）
    decision.setdefault("meta", {})["latest"] = str(
        (stock["quarters"][-1] or {}).get("date") or "")[:10]
    return True, decision, stock, ""


# ---------------------------------------------------------------------------
# 批量主流程
# ---------------------------------------------------------------------------


def run_batch(items, workers=4, force=False, progress=None, target_date=None):
    """跑一批名单。`items` 是 [(code, name)] 或 [ItemResult]。

    返回 [ItemResult]。`progress(done, total, code, ok, message)` 可选回调。
    失败只影响自己那一行；Ctrl-C 向上抛，已算出的结果已经写库、不会丢。
    """
    results = [_to_item(it) for it in items]

    # 去重：同代码只算第一次，其余标"重复"（输入里重复是笔误，不算失败）
    seen = {}
    todo = []
    for item in results:
        if item.code in seen:
            item.error = "重复"
            item.duplicate = True
            continue
        seen[item.code] = item
        todo.append(item)

    existing = {} if force else _existing(target_date)
    for item in results:
        hit = existing.get(item.code)
        if hit:
            item.ok = True
            item.reused = True
            item.decision = hit.get("payload") or {}
            item.name = hit.get("name") or item.name

    pending = [it for it in todo if not it.reused]
    total = len(pending)
    done_counter = {"n": 0}
    lock = threading.Lock()

    def work(item):
        try:
            ok, decision, stock, error = process_one(
                item.code, name_hint=item.name, force=force)
            item.ok, item.decision, item.stock, item.error = ok, decision, stock, error
            if ok and stock:
                item.name = stock.get("name") or item.name
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
                progress(done, total, item.code, item.ok, item.error)

    if workers <= 1:
        for item in pending:
            work(item)
    else:
        _run_threaded(pending, work, workers)

    _save_results(results, target_date)
    return results


def _to_item(item):
    """(code_raw, name) / ItemResult → ItemResult，顺带校验代码。

    校验在这里、不在 process_one：**名单里写错一个代码，不该让整批炸**，
    也不该发起一次注定失败的取数。错误留在这一行上，照常入库统计。
    """
    if isinstance(item, ItemResult):
        return item
    raw, name = (list(item) + ["", ""])[:2]
    result = ItemResult(raw or "", name or "")
    if not raw:
        result.error = "代码为空"
        return result
    try:
        result.code = codes_mod.parse(raw)
    except ValueError:
        result.error = f"代码无效：{raw}"
    return result


def _existing(target_date):
    """结果库里这个基准日已经算过的票：{symbol: row}。

    续跑复用不再需要"扫报告文件 + 从 HTML 里抠 data-*"——结论本来就
    以 JSON 存在库里，读回来就是原样的 decision 结构。
    """
    if not target_date:
        return {}
    try:
        from stock_data_fetch import results as results_db
        rows = results_db.latest_for("cycle", target_date)
    except Exception:                            # noqa: BLE001
        return {}
    out = {}
    for r in rows:
        # payload 列是 JSON 字符串，用之前先解回 dict（否则下游 .get() 会炸）
        try:
            r["payload"] = json.loads(r.get("payload") or "{}")
        except (TypeError, ValueError):
            r["payload"] = {}
        sym = r.get("symbol") or ""
        if not sym:
            continue
        # 两种写法都收：库里存 sh600519，名单里给 600519 的多（选股 CSV 就是纯 6 位）。
        out[sym] = r
        out[sym[-6:]] = r
    return out


def _save_results(results, target_date=None) -> None:
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
            "symbol": item.code,
            "name": item.name or (item.stock or {}).get("name"),
            "is_cycle": cyc.get("label") == 1,      # decide.CYCLE_YES
            "gm_std": cyc.get("gm_std"), "gm_cv": cyc.get("gm_cv"),
            "gm_median": cyc.get("gm_median"),
            "elasticity": ela.get("ratio"), "elastic_label": ela.get("label"),
            "position_pct": pos.get("pct"), "position_label": pos.get("label"),
            "payload": d,
        })
    if not rows:
        return
    results_db.save("cycle", rows, target_date or _latest_quarter(results) or _today(),
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
    return f"{datetime.datetime.now():%Y-%m-%d}"


def batch_timestamp(now=None):
    """整批共用一个时间戳（`YYYYMMDD-HHMMSS`）。"""
    now = now or datetime.datetime.now()
    return f"{now:%Y%m%d-%H%M%S}"


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
