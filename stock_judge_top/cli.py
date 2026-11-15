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
from .render.html import write_html_file
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
    ap.add_argument("code", help="股票代码：sh600519 / 600519 / 600519.SH")
    ap.add_argument("--date", help="基准交易日，默认今天（YYYY-MM-DD）")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取")
    ap.add_argument("--json", action="store_true",
                    help="stdout 只输出 JSON（--json 的 JSON 不受 --quiet 影响）")
    ap.add_argument("--json-full", action="store_true",
                    help="JSON 里同时带行情快照 quote（默认剔除）")
    ap.add_argument("--verbose", action="store_true",
                    help="打印执行过程和完整命令行报告（默认只生成文件）")
    ap.add_argument("--quiet", action="store_true",
                    help="不输出日志与命令行报告，只写结果文件")
    ap.add_argument("--no-html", "--no-md", dest="no_html", action="store_true",
                    help="不生成 HTML 结果文件（--no-md 为兼容旧用法）")
    ap.add_argument("--db", help="共享库路径（默认 STOCKHUB_DB 或项目内 shared.db）")
    a = ap.parse_args(argv)

    configure(quiet=a.quiet or not a.verbose,
              stream=sys.stderr if a.json else sys.stdout)

    try:
        code = normalize_code(a.code)
    except ValueError as e:
        sys.stderr.write(f"参数错误：{e}\n")
        return 2

    try:
        hub = preflight(db_path=a.db) if a.db else preflight()
    except HubError as e:
        sys.stderr.write(f"错误（数据层）：{e}\n")
        return 3

    end = a.date or dt.date.today().isoformat()
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

    if not a.no_html:
        try:
            html_path = write_html_file(r)
            if not a.quiet and not a.json:
                print(f"[输出] HTML：{html_path}")
                sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"[输出] HTML 写入失败：{e}\n")
    return 0
