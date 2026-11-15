"""生成长期交易日历：`python -m stock_data_fetch.build_calendar`。

**为什么需要这个模块**（2026-11-14 冒烟实测发现）

原来 `calendar.py` 直接从 kline-screen 的 `database/trading_calendar.json` 加载，
那份文件只有 **4 年（2022-11-14 起，970 天）**——对 kline-screen 的 300 根足够，
对别的东西不够。后果是静默的：

    want.valuation(years=10)  要 2016-11-15 起
    cal.on_or_after("2016-11-15")  →  2022-11-14   （二分查到插入点 0，不是 None）
    need.start 被夹成 4 年前 → 取回 205 周 → 门槛 480 → InsufficientDepth

错误信息写的是"库 205 / 需 480"，看起来是数据缺失，真因是**日历太短**。
日历是全局上限，任何要 5 年以上的域都会被它悄悄截断——`dividend` 的起点、
`start_for_bars` 的深度判定都受影响。所以这里把它一次修到位。

**来源选上证指数日线**：日历是"哪天开市"，指数每个交易日都有一根 bar，
比任何个股都可靠（个股会停牌、会退市、会迁移代码）。东财 `push2his` 无鉴权、
一次请求给全区间，且 `beg` 参数有效（2026-11-14 实测：2010 年起 4094 个交易日）。

产出格式与 kline-screen 那份**完全一致**（`{span, end, days[]}`），
多写 `source` / `generated_at` 两个字段给 `doctor` 审计用——旧解析器只读
`days`/`span`/`end`，多两个键不影响。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

from .config import CALENDAR_NAME, Config
from .need import Need
from .sources import eastmoney as em
from .sources import westock as W

log = logging.getLogger("stock_data_fetch.build_calendar")

INDEX = "sh000001"
"""上证指数。选它不选 399001/000300：沪市开市日就是全市场开市日，历史最长。"""

DEFAULT_YEARS = 12
"""默认 12 年。估值要 10 年、分红要 K 线起点再往前 1 年，12 年给两者都留了余量。"""

NEEDED_YEARS = 10
"""低于这个年数的日历**能生成但要告警**：估值的 430 周门槛需要 8.3 年，
东财的上限是 8.9 年，日历至少要 10 年才不会成为新的瓶颈。"""

SZSE_CAL_URL = "http://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
SZSE_REFERER = "http://www.szse.cn/aboutus/calendar/index.html"
SZSE_SLEEP = 0.25
"""深交所月度日历。按月一问，12 年 = 144 次请求，所以每次之间隔 0.25 秒。"""

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
HTTP_TIMEOUT = 15.0
"""交易所接口对 `UA` 和 `Referer` 都挑：缺一个就 403，用默认 UA 也是 403。"""

MIN_DAYS_PER_YEAR = 230
MAX_DAYS_PER_YEAR = 260
MAX_GAP_DAYS = 12
"""A 股最长休市是春节/国庆加调休，不会超过 12 天。超过就是缺数据，不是放假。"""


def fetch_days(years: int = DEFAULT_YEARS, *, today: str | None = None,
               tries: int = 3, source: str = "auto",
               sleep: float = 4.0) -> list[str]:
    """取交易日。`today` 只给测试注入用。

    **两条腿，westock 在前**（2026-11-14 定的，理由不是"哪家更稳"而是**语义**）：

    - `westock data_trade_calendar`：这是**日历本身**——直接回答"哪天开市"
      （`days[].isTrading`）。按年分页，实测 2015=244 天、2020=243 天，没有条数上限。
    - `eastmoney push2his`（`sh000001` 日线）：上证指数每天有一根 bar，是"那天开市"的
      **间接证据**，一次请求给全 12 年。缺点是对突发流量会**整段拒连**（§5.7）。

    原先只有东财一条腿（它一次能给 12 年），但 2026-11-14 生成时它正在封禁期，
    连续 30 分钟拒连。改用 westock 的日历接口之后反而更对：拿"指数有没有成交"去推
    "市场开没开市"，本来就绕了一层。**指数日线只能当备用证据。**
    """
    today = today or dt.date.today().isoformat()
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=365 * years + 10)).isoformat()
    order = ["westock", "szse", "eastmoney"] if source == "auto" else [source]
    errs: list[str] = []
    for name in order:
        fn = _FETCHERS.get(name)
        if fn is None:
            raise ValueError(f"未知日历源：{name}")
        for i in range(max(1, tries)):
            try:
                days = fn(start, today)
                if days:
                    return days
                errs.append(f"{name}: 返回空")
                break
            except Exception as e:                # noqa: BLE001 - 瞬断重试，最后原样报告
                errs.append(f"{name}: {e}")
                log.warning("取交易日失败（%s 第 %d/%d 次）：%s", name, i + 1, tries, e)
                if i + 1 < max(1, tries):
                    time.sleep(sleep * (i + 1))   # 线性退避：封禁期内连发只会更糟
    raise RuntimeError("所有日历源都失败：" + "；".join(errs[-4:]))


def _from_westock(start: str, end: str) -> list[str]:
    """按年取 `data_trade_calendar`。**主源**：日历问的是"哪天开市"，这是唯一直接答案。

    `trading_only=True` 已经筛过一遍，仍按 `isTrading` 再过滤一次——上游把"标志位"
    和"筛选参数"混用的情况不是没遇到过，少一层校验就等于信任一个没约定的行为。
    """
    out: set[str] = set()
    for year in range(int(start[:4]), int(end[:4]) + 1):
        d = W.call("data_trade_calendar",
                   {"year": year, "trading_only": True, "limit": 400})
        days = (W._unwrap(d) or {}).get("days") or []
        got = [x.get("date") for x in days
               if x.get("isTrading") and x.get("date") and start <= x["date"] <= end]
        if not got:
            raise RuntimeError(f"data_trade_calendar 对 {year} 年返回空")
        out.update(got)
    return sorted(out)


def _from_szse(start: str, end: str) -> list[str]:
    """**深交所月度交易日历**：一天一条，`jybz=1` 即交易日。**权威源**。

    为什么它是权威而不是"又一条腿"：交易所是休市安排的**制定者**，其余所有人（东财、
    腾讯、各家终端）都是转述。而且它给的是**逐日 0/1 标志**，不是"某指数有没有 bar"
    这种间接证据——`jybz` 直接就是答案。

    2026-11-14 用它对账过本地 12 年日历：抽查 11 个月（跨 2016/2020/2024/2025/2026），
    **10 个月逐日完全一致**，唯一差异是 2026-11——深交所已排到 11-30，我们的日历停在
    生成日 11-13。也就是说它**还能给出未来已排定的交易日**，这是别的源给不了的。

    代价是请求数：按月，12 年 = 144 次（westock 只要 12 次）。所以 `auto` 里它排第二，
    只在 westock 失败时上，或者显式 `--source szse` 时用。沪深两市交易日完全相同，
    深交所的日历对沪市同样有效。
    """
    out: set[str] = set()
    for ym in _months(start, end):
        url = (f"{SZSE_CAL_URL}?month={ym}&random=0.3")
        data = _http_json(url, referer=SZSE_REFERER)
        rows = data.get("data") or []
        if not rows:
            raise RuntimeError(f"深交所日历对 {ym} 返回空")
        out |= {x["jyrq"] for x in rows
                if str(x.get("jybz")) == "1" and x.get("jyrq") and start <= x["jyrq"] <= end}
        time.sleep(SZSE_SLEEP)          # 144 次请求，给交易所留点余地
    return sorted(out)


def _months(start: str, end: str) -> list[str]:
    """`2015-01-01`..`2026-11-13` → `["2015-01", ..., "2026-11"]`。"""
    y, m = int(start[:4]), int(start[5:7])
    out: list[str] = []
    while f"{y:04d}-{m:02d}" <= end[:7]:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _http_json(url: str, *, referer: str = "") -> dict:
    """交易所接口要 `UA` + `Referer`，缺一个就回 403/空。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _from_eastmoney(start: str, end: str) -> list[str]:
    """备用腿：上证指数日线的日期。间接证据，但一次请求给全区间。"""
    need = Need(domain="kline", fp="cal", end=end, start=start, points=0, min_points=0)
    rows, reason = em._fetch_one(INDEX, need)
    if not rows:
        raise RuntimeError(f"{INDEX} 返回空（{reason}）——上游接口可能改名了")
    return [r["date"] for r in rows]


_FETCHERS = {"westock": _from_westock, "szse": _from_szse, "eastmoney": _from_eastmoney}


def check(days: list[str], *, today: str | None = None) -> list[str]:
    """返回问题清单，空表示通过。**不通过就不许写文件**——一份坏日历比没有日历更糟，
    它会让"洞里没有数据"和"日历里没有这一天"永远分不清。

    逐年计数只查**完整年份**：首年从年中开始（比如 2014-11-14）、末年只到生成日
    （比如 2026-11-13 只有 210 个交易日），拿 [230,260] 去卡它们会误报——第一版
    就是这么写的，于是 `--years 12` 在任何时候都报"2026 年 210 个交易日，明显偏少"。
    """
    today = today or dt.date.today().isoformat()
    problems: list[str] = []
    if len(days) < 1000:
        problems.append(f"只有 {len(days)} 个交易日，明显偏少")

    last_year = days[-1][:4]
    first_year = days[0][:4]
    per_year: dict[str, int] = {}
    for d in days:
        per_year[d[:4]] = per_year.get(d[:4], 0) + 1
    # 按年份区间遍历而不是遍历 per_year 的键：整年缺失时那个键根本不存在，
    # 遍历键会漏掉"2023 年一天都没有"这种最严重的情况。
    for y in range(int(first_year), int(last_year) + 1):
        ys = str(y)
        n = per_year.get(ys, 0)
        partial_head = (ys == first_year and not days[0].endswith("-01-01"))
        partial_tail = (ys == last_year and days[-1] < f"{ys}-12-31")
        if partial_head or partial_tail:
            continue
        if n < MIN_DAYS_PER_YEAR or n > MAX_DAYS_PER_YEAR:
            problems.append(f"{ys} 年 {n} 个交易日，超出 [{MIN_DAYS_PER_YEAR},"
                            f"{MAX_DAYS_PER_YEAR}]")

    for a, b in zip(days, days[1:]):
        gap = (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
        if gap > MAX_GAP_DAYS:
            problems.append(f"{a} → {b} 断了 {gap} 天")
            break

    lag = (dt.date.fromisoformat(today) - dt.date.fromisoformat(days[-1])).days
    if lag > 10:
        problems.append(f"最后一个交易日 {days[-1]} 距今 {lag} 天，日历没跟上")
    return problems


def build(out: Path, *, years: int = DEFAULT_YEARS, today: str | None = None,
          dry_run: bool = False, source: str = "auto",
          merge: bool = True) -> dict:
    """取数 → 校验 → 落盘。返回写出去的 payload（`dry_run` 时不落盘）。

    `merge=True` 时与 `out` 里已有的日历**取并集**：备用源只有 5 年，
    不能因为一次"东财被封时的补救运行"把 11 年的日历截成 5 年。
    """
    today = today or dt.date.today().isoformat()
    days = set(fetch_days(years, today=today, source=source))
    kept = 0
    if merge and out.exists():
        try:
            old = json.loads(out.read_text(encoding="utf-8")).get("days") or []
        except (OSError, ValueError):
            old = []
        kept = len([d for d in old if d not in days])
        days |= {d for d in old if isinstance(d, str)}

    days = sorted(days)
    problems = check(days, today=today)
    if problems:
        raise RuntimeError("交易日历校验失败：" + "；".join(problems))

    span_days = (dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days
    warnings = []
    if span_days < NEEDED_YEARS * 365:
        warnings.append(f"只有 {span_days / 365:.1f} 年，够不着 {NEEDED_YEARS} 年——"
                        "估值会以 InsufficientDepth 结束，等上游恢复后重跑本脚本")

    payload = {
        "span": days[0],
        "end": days[-1],
        "days": days,
        # 下面两个键是给 `doctor` 审计的，kline-screen 的旧解析器不读它们
        "source": f"{source}",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "warnings": warnings,
    }
    if not dry_run:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in payload.items() if k != "warnings"},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(out)                          # 原子替换：半截 JSON 会让闸 5 直接拒启动
    payload["kept_from_old"] = kept
    return payload


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="生成长期交易日历（需联网，建议收盘后跑）")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS)
    ap.add_argument("--out", default="", help="默认写到 Config().state_dir/trading_calendar.json")
    ap.add_argument("--source", default="auto",
                    choices=("auto", "westock", "szse", "eastmoney"),
                    help="auto = westock → szse → eastmoney；szse 是权威源但按月要 144 次请求")
    ap.add_argument("--no-merge", action="store_true", help="不与已有日历取并集（会变短）")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写盘")
    args = ap.parse_args(argv)

    out = Path(args.out).expanduser() if args.out else Config().state_dir / CALENDAR_NAME
    try:
        payload = build(out, years=args.years, dry_run=args.dry_run,
                        source=args.source, merge=not args.no_merge)
    except Exception as e:                        # noqa: BLE001 - CLI 边界
        print(f"❌ {e}", file=sys.stderr)
        return 1
    for w in payload.get("warnings") or []:
        print(f"⚠️  {w}")
    print(f"{'（未写盘）' if args.dry_run else '✅'} {out}\n"
          f"   {payload['span']} .. {payload['end']}  {len(payload['days'])} 个交易日"
          f"（沿用旧日历 {payload.get('kept_from_old', 0)} 天）")
    return 0


if __name__ == "__main__":                        # pragma: no cover
    raise SystemExit(main())
