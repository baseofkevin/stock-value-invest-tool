"""东方财富适配器：`kline`（原材料）+ `valuation`（周频估值）+ `snapshot.mainop`（主营构成）。

**两个完全不同的东财**，别混为一谈：

| | `push2his`（行情） | `datacenter`（F10 / 数据中心） |
|---|---|---|
| 用途 | 日 K | 估值序列、主营构成、财务报表 |
| 鉴权 | 无，带公开 `ut` | 无 |
| 返回 | 逗号分隔字符串，值被抹位 | 浮点全精度、英文列名 |
| 深度 | 一次给全历史 | 分页，单接口上限 30 页 |

`kline` 走前者，`valuation` / `mainop` 走后者。**不要**拿 `push2his` 去取财务数据，
反过来也一样。

## 为什么 `kline` 主源是它而不是 tdx

（2026-11-14 实测后改的，纪要 §22）

- tdx MCP 端点要 OAuth Bearer，独立 Python 进程拿不到（`sources/tdx.py` 头部有 401 原文）；
- 东财 `push2his` 的 `fqt=0` **无鉴权**，一次请求给全历史（茅台 6042 行、2001-08-27 起）；
- 两者 raw 值**逐点相同**，连分位都没差：

      格力   2026-08-26  tdx tqFlag=0 41.58   东财 fqt=0 41.58
      比亚迪 2025-07-28  tdx tqFlag=0 337.00  东财 fqt=0 337.00
      比亚迪 2025-07-29  tdx tqFlag=0 111.42  东财 fqt=0 111.42

`SOURCES` §4.1 早就允许 `kline` raw 跨源混用（"raw 就是交易所成交价"），
所以换源不需要作废旧数据——这次换源连 `fp` 都没动。

## 两个必须记住的单位/口径坑

1. **成交量是"手"且被抹成整数**：茅台 2026-11-13 给 `41780`（tdx 同期 `41779.84` 手）。
   乘 100 还原成股，误差 2 股 / 418 万股（5e-7），对均线、换手率都无所谓；
2. **北交所老代码已迁移**：`43xxxx` / `83xxxx` / `87xxxx` 会返回
   `name="诺思兰德(已切换)"` 且 `klines=[]`。这是"代码搬去 `92xxxx` 了"，
   不是"停牌"，必须单独报 `code_moved`，否则会被记成一个永不消失的洞。

## 估值序列的两个实测事实（curl 验过，2026-11-14）

- `TOTAL_MARKET_CAP` **存在**（茅台 2018-01-02 为 `884174821530`，元，全精度），可填 `total_mv`；
- 流通市值 **没有对应列**：`CIRCULATE_MARKET_CAP` / `CIRC_MARKET_CAP` / `FLOAT_MARKET_CAP` /
  `FREE_MARKET_CAP` / `NOTLIMITED_MARKET_CAP` 全部返回 `9501 字段不存在`。
  所以 `circ_mv` 留 NULL，**不拿 `total_mv` 顶替**——对不全流通的股票那是个偏大的数，
  而偏大的流通市值会让换手率偏小、`judge` 的活跃度判定静默走偏。
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from ..errors import SourceUnavailable
from ..need import Need
from .base import Got

log = logging.getLogger("stock_data_fetch.eastmoney")

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
DATA_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
UT = "fa5fd1943c7b386f172d6893dbfba10b"   # 东财网页端公开的接口令牌，非个人凭证
UA = "Mozilla/5.0 (stock_data_fetch)"
REFERER = "https://quote.eastmoney.com/"
DATA_REFERER = "https://data.eastmoney.com/"
TIMEOUT = 20.0

VALUATION_COLS = "SECUCODE,TRADE_DATE,PE_TTM,PB_MRQ,PS_TTM,TOTAL_MARKET_CAP"
"""估值序列要的列。**`TOTAL_MARKET_CAP` 是实测加进来的**，`circ_mv` 没有（见模块头）。"""

MAINOP_COLS = ("SECUCODE,REPORT_DATE,MAINOP_TYPE,ITEM_NAME,MAIN_BUSINESS_INCOME,"
               "MBI_RATIO,MAIN_BUSINESS_COST")

MAX_PAGES = 30
"""`datacenter` 单接口单次最多 30 页——这是它自己的 `pages` 上限，不是我们设的。"""


def _split(symbol: str) -> tuple[str, str]:
    """`sh600519` / `600519` / `600519.SH` → (`sh`, `600519`)。"""
    s = symbol.strip().lower()
    if "." in s:                             # 600519.SH 这种反过来的写法
        code, _, mkt = s.partition(".")
        return (mkt if mkt in ("sh", "sz", "bj") else "sh"), code
    if s.startswith(("sh", "sz", "bj")):
        return s[:2], s[2:]
    mkt = "sh" if s[:1] in ("6", "5", "9") else (
        "bj" if s[:2] in ("43", "83", "87", "92") else "sz")
    return mkt, s


def secid(symbol: str) -> str:
    """`sh600519` → `1.600519`。东财的市场号：沪 1 / 深 0 / 北 0。"""
    mkt, code = _split(symbol)
    if not code.isdigit():
        raise SourceUnavailable(f"代码格式不认识：{symbol}")
    return ("1." if mkt == "sh" else "0.") + code


def secucode(symbol: str) -> str:
    """`sh600519` → `600519.SH`。`datacenter` 的 `filter` 用的是这种。"""
    mkt, code = _split(symbol)
    if not code.isdigit():
        raise SourceUnavailable(f"代码格式不认识：{symbol}")
    return f"{code}.{ {'sh': 'SH', 'sz': 'SZ', 'bj': 'BJ'}[mkt] }"


def supports(domain: str, need: Need) -> bool:
    """`snapshot` 下我有 `mainop` / `gbalance` / `gcashflow` 三个子域
    （`consensus`/`rating` 在 westock）。

    没有这个钩子，`ensure(snapshot, consensus)` 会先选中东财、抛一次
    `SourceUnavailable`、记一次 strike，再换到 westock——每只股票都白打一次。
    """
    if domain == "snapshot":
        return _snapshot_domain(need) in ("mainop", "gbalance", "gcashflow")
    return domain in ("kline", "valuation")


def fetch(domain: str, codes, need: Need) -> Got:
    """`kline` / `valuation` / `snapshot` 三个域。其余域明确报错，不留悬念。"""
    if domain == "kline":
        return _kline(list(codes), need)
    if domain == "valuation":
        return _valuation(list(codes), need)
    if domain == "snapshot":
        sub = _snapshot_domain(need)
        if sub == "gbalance":
            return _gbalance(list(codes), need)
        if sub == "gcashflow":
            return _gcashflow(list(codes), need)
        return _mainop(list(codes), need)
    raise SourceUnavailable(f"eastmoney 适配器不支持 {domain} 域", domain=domain)


def _kline(codes: list[str], need: Need) -> Got:
    """逐只取（东财这个接口没有批量参数），一次拿全区间。"""
    got = Got()
    for sym in codes:
        rows, reason = _fetch_one(sym, need)
        if rows:
            got.rows[sym] = rows
        elif reason:
            got.empty_reason = got.empty_reason or reason
    return got


def _fetch_one(symbol: str, need: Need, *,
               fqt: str = "0") -> tuple[list[dict], str | None]:
    """单只 K 线。

    `fqt`：`"0"` 不复权（**库里的地基，默认值，不许改**），`"1"` 前复权。
    前复权只给 `probe` 做口径对账用——两边都是"等比"才能逐点比，换成等差口径
    （westock 的 `fq=qfq` 就是等差）会天然差 `派现/股价`，那不是在查错。
    """
    beg = (need.start or "1990-01-01").replace("-", "")
    end = (need.end or dt.date.today().isoformat()).replace("-", "")
    params = {
        "secid": secid(symbol), "ut": UT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": "101",            # 101 = 日线
        "fqt": fqt,              # 0 = 不复权。库里的地基，不许改
        "beg": beg, "end": end, "lmt": "100000",
    }
    url = KLINE_URL + "?" + urllib.parse.urlencode(params)
    body = _get(url)

    try:
        payload = json.loads(body)
    except ValueError as e:
        raise SourceUnavailable(f"东财返回不是 JSON（{symbol}）：{body[:120]}") from e

    data = payload.get("data")
    if not data:                              # `data: null` = 代码不存在或已迁移
        return [], "no_data"
    name = str(data.get("name") or "")
    klines = data.get("klines") or []
    if not klines:
        return [], ("code_moved" if "已切换" in name else "no_data")

    rows: list[dict] = []
    for line in klines:
        p = str(line).split(",")
        if len(p) < 7 or len(p[0]) != 10:
            continue
        rows.append({"date": p[0],
                     "open": _f(p[1]), "close": _f(p[2]),
                     "high": _f(p[3]), "low": _f(p[4]),
                     "volume": _f(p[5]) * 100.0,     # 手 → 股
                     "amount": _f(p[6])})            # 已是元
    return rows, None


# ---------------------------------------------------------------- valuation

def _valuation(codes: list[str], need: Need) -> Got:
    """10 年周频估值。**入库即周频**：日频 2150 行换来的信息，对"十年分位"没有增量。"""
    got = Got()
    for sym in codes:
        rows, reason = _valuation_one(sym, need)
        if rows:
            got.rows[sym] = rows
        elif reason:
            got.empty_reason = got.empty_reason or reason
    return got


def _valuation_one(symbol: str, need: Need) -> tuple[list[dict], str | None]:
    start = need.start or "1990-01-01"
    raw = _datacenter("RPT_VALUEANALYSIS_DET", VALUATION_COLS,
                      f'(SECUCODE="{secucode(symbol)}")',
                      sort="TRADE_DATE", stop_before=start)
    if not raw:
        return [], "no_data"
    rows = _weekly(raw, start, need.end)
    return rows, (None if rows else "no_data")


def _weekly(raw: list[dict], start: str, end: str | None) -> list[dict]:
    """日频 → 周频：**每个 ISO 周留最后一个交易日**（`SCHEMA` §3 的 `valuation.trade_date`）。

    为什么是"每周最后一天"而不是"每周第一天"：`quality` 的 `_valuation()` 就是按
    ISO 周取最后交易日算分位的。这边取第一天，两边的分位会差一档，而差多少取决于
    当周涨跌——是那种"跑一年才发现名单不一样"的错。

    最后一根不满一周的 bar 也留着（它是当周目前为止的最新值），下次重取时会被替换。
    """
    best: dict[tuple[int, int], tuple[str, dict]] = {}
    for r in raw:
        day = _date(r.get("TRADE_DATE"))
        if not day or day < start or (end and day > end):
            continue
        try:
            key = dt.date.fromisoformat(day).isocalendar()[:2]
        except ValueError:                      # pragma: no cover - 脏日期
            continue
        cur = best.get(key)
        if cur is None or day > cur[0]:
            best[key] = (day, r)

    out = [{"trade_date": day,
            "pe_ttm": _num(r.get("PE_TTM")),
            "pb_mrq": _num(r.get("PB_MRQ")),
            "ps_ttm": _num(r.get("PS_TTM")),
            "total_mv": _num(r.get("TOTAL_MARKET_CAP")),
            "circ_mv": None}                   # 东财这个接口没有流通市值列
           for day, r in best.values()]
    out.sort(key=lambda x: x["trade_date"])
    return out


# ---------------------------------------------------------------- snapshot.mainop

def _mainop(codes: list[str], need: Need) -> Got:
    """主营构成。整份 JSON 塞进 `snapshot.payload`——它的字段（分产品/分地区/分行业）
    随上游变，建表就是给自己挖迁移坑（`SCHEMA` §3）。"""
    years = int(need.extra.get("mainop_years") or 4)
    since = f"{dt.date.today().year - years}-12-31"
    got = Got()
    for sym in codes:
        raw = _datacenter("RPT_F10_FN_MAINOP", MAINOP_COLS,
                          f'(SECUCODE="{secucode(sym)}")(REPORT_DATE>=\'{since}\')',
                          sort="REPORT_DATE", page_size=500)
        if not raw:
            got.empty_reason = got.empty_reason or "no_data"
            continue
        as_of = max((_date(r.get("REPORT_DATE")) or "") for r in raw)
        got.rows[sym] = [{"symbol": sym, "domain": "mainop", "as_of": as_of,
                          "payload": json.dumps(raw, ensure_ascii=False)}]
    return got


# ---------------------------------------------------------------- snapshot.gbalance

GBALANCE_COLS = ("SECUCODE,REPORT_DATE,GOODWILL,SHORT_LOAN,TOTAL_ASSETS,"
                 "TOTAL_LIABILITIES,LEASE_LIAB")
"""东财资产负债表里 weStock 不提供的列。

**为什么要有这个子域**：weStock 的 `data_finance` 即使 `fields:"all"` 也不返回
`GoodWill`（商誉）与 `ShortTermLoan`（短期借款）——实测 58/52/22 个字段里都没有。
quality-screen 的「商誉/净资产」与「现金短债比」两项安全检查正好要这两个，
原版只能把商誉按 0 处理（`compute.py` 的 `goodwill_imputed` 恒为 True）。

`TOTAL_ASSETS` 一起取是**判空必需的**：见 `_gbalance` 里"有表无科目 = 0"那段。
"""


def _gbalance(codes: list[str], need: Need) -> Got:
    """资产负债表补丁（商誉 + 短期借款），按报告期取最近几期。

    **`None` 的两种含义必须分开**（这是本函数唯一容易写错的地方）：

    - 有资产负债表（`TOTAL_ASSETS` 非空）但某科目是 `None` → 该公司**确实**没有这个科目
      （茅台就没有商誉、也没有短期借款），应判为 **0**。
    - 连 `TOTAL_ASSETS` 都拿不到 → 是真的没数据，应判为 **unknown**，绝不能当 0。
      当成 0 的后果是"商誉/净资产 = 0%"被判定为安全——一个查不出来的风险被报成通过。

    所以 payload 里额外放一个 `has_balance` 布尔量，让下游不必再猜：
    它在本次请求里至少有一期拿到了 `TOTAL_ASSETS` 就是 True。
    """
    periods = int(need.extra.get("periods") or 20)
    got = Got()
    for sym in codes:
        raw = _datacenter("RPT_F10_FINANCE_GBALANCE", GBALANCE_COLS,
                          f'(SECUCODE="{secucode(sym)}")',
                          sort="REPORT_DATE", page_size=max(periods, 1))
        if not raw:
            got.empty_reason = got.empty_reason or "no_data"
            continue
        # 上游是升序翻页到头，这里统一成"最近一期在前"，下游不用自己再排
        raw.sort(key=lambda r: _date(r.get("REPORT_DATE")) or "", reverse=True)
        rows = []
        for r in raw[:periods]:
            r = dict(r)
            r["REPORT_DATE"] = _date(r.get("REPORT_DATE"))
            rows.append(r)
        has_balance = any(r.get("TOTAL_ASSETS") is not None for r in rows)
        as_of = rows[0].get("REPORT_DATE") or ""
        payload = {"periods": rows, "has_balance": has_balance}
        got.rows[sym] = [{"symbol": sym, "domain": "gbalance", "as_of": as_of,
                          "payload": json.dumps(payload, ensure_ascii=False)}]
    return got


# ---------------------------------------------------------------- snapshot.gcashflow

GCASHFLOW_COLS = "SECUCODE,REPORT_DATE,CONSTRUCT_LONG_ASSET"
"""东财现金流量表里的「购建固定资产、无形资产和其他长期资产支付的现金」（capex）。

**为什么单开一个子域**：weStock 的 `data_finance` 现金流表 22 个字段里没有这一项
（只有 `NetInvestCashFlow` 投资活动净额，口径完全不同——那个是"净流入"，
含着处置资产收回的现金，拿来算 FCF 会把 capex 算小、FCF 算大）。

quality-screen 的 `compute.build_financials()` 只用 **12-31 年报行**的值：
东财这个字段是「年初至今累计」，同一年的 Q1/Q2/Q3 是递增的中间值，
混进来会把年度 capex 低估（原版注释实测 600519：2025 年报 31.28 亿 vs Q1 9.01 亿）。
"""


def _gcashflow(codes: list[str], need: Need) -> Got:
    """capex 补丁，按报告期取最近几期。`REPORT_DATE` 归一成 `YYYY-MM-DD`，最近一期在前。"""
    periods = int(need.extra.get("periods") or 20)
    got = Got()
    for sym in codes:
        raw = _datacenter("RPT_F10_FINANCE_GCASHFLOW", GCASHFLOW_COLS,
                          f'(SECUCODE="{secucode(sym)}")',
                          sort="REPORT_DATE", page_size=max(periods, 1))
        if not raw:
            got.empty_reason = got.empty_reason or "no_data"
            continue
        raw.sort(key=lambda r: _date(r.get("REPORT_DATE")) or "", reverse=True)
        rows = []
        for r in raw[:periods]:
            r = dict(r)
            r["REPORT_DATE"] = _date(r.get("REPORT_DATE"))
            rows.append(r)
        as_of = rows[0].get("REPORT_DATE") or ""
        got.rows[sym] = [{"symbol": sym, "domain": "gcashflow", "as_of": as_of,
                          "payload": json.dumps({"periods": rows}, ensure_ascii=False)}]
    return got


# ---------------------------------------------------------------- datacenter 通用

def _datacenter(report: str, columns: str, filter_text: str, *,
                sort: str, page_size: int = 1000,
                stop_before: str | None = None) -> list[dict]:
    """`datacenter.eastmoney.com` 的分页读取。**降序翻页**，够到 `stop_before` 就停。

    为什么要"降序 + 早停"：升序翻页要翻完 30 页才知道最新值在哪，而降序第一页就是最新
    十天。估值需求只覆盖最近 10 年（约 2400 个交易日 / 3 页），早停能省掉启动时那几秒。

    **`success: false` 一律抛 `SourceUnavailable`**，不当空表：这些 `reportName` 没有
    官方文档，改名/改列是常态（实测列名拼错返回 `code 9501`）。当空表处理的话，
    症状是"这只股票的估值序列没有了"，而不是"接口变了"。
    """
    rows: list[dict] = []
    page = 1
    while page <= MAX_PAGES:
        params = {"reportName": report, "columns": columns, "pageNumber": page,
                  "pageSize": page_size, "sortColumns": sort, "sortTypes": "-1",
                  "source": "HSF10", "client": "PC", "filter": filter_text}
        body = _get(DATA_URL + "?" + urllib.parse.urlencode(params), referer=DATA_REFERER)
        try:
            payload = json.loads(body)
        except ValueError as e:
            raise SourceUnavailable(f"东财 {report} 返回不是 JSON：{body[:120]}") from e
        if payload.get("success") is False:
            # `9201 返回数据为空` 是**这只票没数据**，不是接口坏了：
            # 代码写错/退市的股票、或该报告根本没有这一项，上游都回这个。
            # 当成 `SourceUnavailable` 的后果是"一个不存在的代码被报成接口改名了"，
            # 于是有人去改 reportName——改完仍然为空，白折腾。空表交给 `empty_reason`。
            code = str(payload.get("code"))
            msg = str(payload.get("message") or "")
            if code == "9201" or "为空" in msg:
                break
            raise SourceUnavailable(
                f"东财 {report} 接口报错（{payload.get('code')}）：{payload.get('message')}。"
                "多半是 reportName / columns 改名了")
        result = payload.get("result") or {}
        data = result.get("data") or []
        if not data:
            break
        rows.extend(data)
        if stop_before and min((_date(r.get(sort)) or "9999-99-99") for r in data) < stop_before:
            break
        if page >= min(int(result.get("pages") or 1), MAX_PAGES):
            break
        page += 1
    return rows


def _snapshot_domain(need: Need) -> str:
    return str(need.extra.get("snapshot_domain") or "").strip()


def _date(v) -> str | None:
    """`"2018-01-02 00:00:00"` / `20260626` → `2018-01-02`。"""
    if v is None:
        return None
    s = str(v).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def _num(v) -> float | None:
    """可空浮点。**与 `_f` 不同**：估值为负/缺失时给 `None`，不给 0.0。

    PE_TTM 为 0 和 PE_TTM 为 None 在分位计算里是两件事：0 会被当成"极度便宜"，
    None 会被跳过。亏损股的 PE 是负的或空的，填 0 会把它们全推到分位最左边。
    """
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get(url: str, *, referer: str = REFERER) -> str:
    """单独抽出来，测试里换成假响应，不必真的联网。

    **必须接住 `RemoteDisconnected`**：实测连续取 20 只之后，东财会直接掐连接
    （不是返回 4xx，而是 `http.client.RemoteDisconnected`，它是 `OSError` 的子类，
    `URLError` 接不住）。漏接就会以一个"看不懂的异常"穿过 router 直达四个工具。
    这类错误带 `transient=True`，router 会当作限频去冷却，而不是当成"源坏了"。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise SourceUnavailable(f"东财 HTTP {e.code}", status=e.code,
                                transient=e.code in (429, 500, 502, 503, 504)) from e
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise SourceUnavailable(f"东财连接中断：{e}", transient=True) from e


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
