"""judge_top 命令行入口。

迁移说明（2026-11-15）：入口换成共享库 hub——`preflight()` 过六道闸
（库不存在自动建、版本不符拒绝启动、盘中只标记不拦），`--refresh`
透传为 hub 的 `force`（清重试标记后重取），`--db` 新增透传库路径。
命令行接口与原版保持一致。
"""
import argparse
import datetime as dt
import json
import sys

from stock_data_fetch.errors import StockfetchError as HubError
from stock_data_fetch.hub import preflight

from .evaluate import evaluate
from .log import configure, log
from .render.text import print_report

PREFIXES = ("sh", "sz", "bj")
# 只在 --json 时剔除的字段：行情快照体积大、且未做清洗
JSON_DROP = ("quote",)


def normalize_code(raw):
    """兼容 sh600519 / 600519 / 600519.SH / 920xxx 等写法。

    无法识别时抛 ValueError（由 main() 转成退出码 2），
    避免空代码一路传到取数层才报一个看不懂的错。
    """
    code = (raw or "").strip().lower().replace(" ", "")
    if not code:
        raise ValueError("股票代码不能为空（示例：sh600519 / 600519 / 600519.SH）")

    if "." in code:
        left, right = code.split(".", 1)
        if right in PREFIXES:
            # 600519.SH → sh600519；sz000001.SZ → sz000001（left 已带前缀就不再叠加）
            code = left if left.startswith(PREFIXES) else right + left
        else:
            # 600519.XSHG / sh600519.ss 这类：只认左边
            code = left

    if code.startswith(PREFIXES):
        if len(code) <= 2 or not code[2:].isdigit():
            raise ValueError(f"无法识别的股票代码：{raw}（示例：sh600519 / 600519 / 600519.SH）")
        return code

    if not (len(code) == 6 and code.isdigit()):
        raise ValueError(f"无法识别的股票代码：{raw}（A 股为 6 位数字，示例：600519）")

    # 920xxx 是北交所新代码段，必须排在「9 → sh」之前。
    if code.startswith("920"):
        return "bj" + code
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    if code.startswith(("0", "1", "2", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):     # 43/83/87/88 均为北交所
        return "bj" + code
    raise ValueError(f"无法判断交易所：{raw}（示例：sh600519 / bj920002）")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="单只股票四因子独立观察（不合并总分，不筛选；共享数据层版）")
    ap.add_argument("code", nargs="?", default=None,
                    help="股票代码：sh600519 / 600519 / 600519.SH；用 --csv 时省略")
    ap.add_argument("--from-select", metavar="日期", nargs="?", const="",
                    default=None,
                    help="批量模式：名单用结果库里这一天的选股结果（省略日期=最近一次）")
    ap.add_argument("--csv", metavar="CSV",
                    help="批量模式：从该 CSV 的代码列逐只出报告（代码 / 股票代码 / 证券代码 / code）")
    ap.add_argument("--workers", type=int, default=4,
                    help="批量模式并发线程数，默认 4（每线程一个库连接）")
    ap.add_argument("--date", help="基准交易日，默认取「今天（过了 15:05）或上一个交易日」")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取")
    ap.add_argument("--json", action="store_true",
                    help="stdout 只输出 JSON（--json 的 JSON 不受 --quiet 影响）")
    ap.add_argument("--json-full", action="store_true",
                    help="JSON 里同时带行情快照 quote（默认剔除）")
    ap.add_argument("--verbose", action="store_true",
                    help="打印执行过程与完整命令行报告（默认只写 results.db）")
    ap.add_argument("--quiet", action="store_true",
                    help="不输出日志与命令行报告，只写 results.db")
    ap.add_argument("--db", help="共享库路径（默认 STOCKHUB_DB 或项目内 shared.db）")
    a = ap.parse_args(argv)

    configure(quiet=a.quiet or not a.verbose,
              stream=sys.stderr if a.json else sys.stdout)

    if a.csv and a.code:
        sys.stderr.write("参数错误：--csv 与股票代码不能同时给\n")
        return 2
    if a.from_select is not None and (a.code or a.csv):
        sys.stderr.write("参数错误：--from-select 不能和股票代码 / --csv 一起用\n")
        return 2
    if not a.csv and not a.code and a.from_select is None:
        sys.stderr.write("参数错误：给一个股票代码，或用 --from-select / --csv 跑批量\n")
        return 2
    if (a.csv or a.from_select is not None) and a.json:
        sys.stderr.write("参数错误：--json 只支持单只模式\n")
        return 2

    try:
        hub = preflight(db_path=a.db) if a.db else preflight()
    except HubError as e:
        sys.stderr.write(f"错误（数据层）：{e}\n")
        return 3

    # 基准日默认取 hub 的目标交易日（今天过了 15:05 才是今天，否则上一个交易日）。
    # 原来默认 `dt.date.today()`：周末或盘中跑会得到「末端不足（库 11-13 / 需 11-15）」
    # 然后报「当前无 K 线（可能停牌）」——把「今天不是交易日」说成了「停牌」。
    end = a.date or hub.target

    if a.from_select is not None:
        from .batch import from_select
        return from_select(a.from_select or None, end, refresh=a.refresh,
                           workers=a.workers, db=a.db, quiet=a.quiet,
                           progress=a.verbose)

    if a.csv:
        from .batch import run as run_batch
        return run_batch(a.csv, end, refresh=a.refresh, workers=a.workers,
                         db=a.db, quiet=a.quiet, progress=a.verbose)

    try:
        code = normalize_code(a.code)
    except ValueError as e:
        sys.stderr.write(f"参数错误：{e}\n")
        return 2

    log(f"[开始] {code}，基准日 {end}")

    try:
        with hub:
            r = evaluate(hub, code, end, a.refresh)
    except BrokenPipeError:
        raise
    except (HubError, RuntimeError) as e:
        # 数据类错误不打 traceback：用户要的是那句原因，不是调用栈。
        sys.stderr.write(f"判断失败：{e}\n")
        return 1

    # --quiet 优先：不打印日志、不打印报告；但 --json 的 JSON 是结果本身，仍然输出。
    if a.json:
        out = r if a.json_full else {k: v for k, v in r.items() if k not in JSON_DROP}
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        sys.stdout.flush()
    elif a.verbose and not a.quiet:
        print_report(r)
        sys.stdout.flush()

    # 单只模式也落结果库：批量是常态，但单只盯盘同样是"跑出来的一次结论"
    try:
        from stock_data_fetch import results as results_db
        results_db.save("top", [{**r, "code": code, "name": r.get("name"),
                                 "ok": True}], end, argv="stock_judge_top",
                        kind="single")
    except Exception:                            # noqa: BLE001 - 落库失败绝不影响出报告
        pass

    if not a.quiet and not a.json:
        print(f"[完成] {code} 的结果已写入 results.db（用 ./run.py web 查看）")
        sys.stdout.flush()
    return 0
