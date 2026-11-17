"""上游抓取：东财 `datacenter` 的 `RPT_VALUEANALYSIS_DET` **按日横截面**。

一次 6 个请求拿回全市场 5500+ 只，每行一只票，带收盘价 / 涨跌幅 / PE(TTM) /
PB(MRQ) / PS(TTM) / 总市值 / 流通市值。

## 为什么是这一个接口

它除了支持"按 `SECUCODE` 取一只票的十年序列"（数据层 `valuation` 域用的那种），
还支持 `filter=(TRADE_DATE='2026-11-16')` 的**按日横截面**。实测 5579 行、6 页。

换别家都没有 PS：腾讯自选股 `data_quote` 只给 `pe_ratio` / `pb_ratio`；
东财 `push2` 行情接口只有一个"营业收入"原始字段，自己除等于再打 5000 次。

## 三个必须知道的边界

1. **当天要等收盘后才有数据。** 这个报告是按交易日算出来的，盘中查"今天"返回
   `9201 返回数据为空`（2026-11-17 10:11 实测）。不是错误，是没算完——
   这里返回空列表，由调用方决定是报错还是换一天。
2. **非交易日同样是空。** 2026-11-14（周六）实测为空。是不是交易日由调用方判断，
   这里不猜。
3. **整天一份必须是同一天。** `filter` 写对了不会有别的日期，但"上游忽略了请求的
   日期 / 把昨收当今天"这类错配一旦发生，症状是库里同一天混着两天收盘价，而且
   没有任何报错。所以校验的是**上游原值**（`TRADE_DATE`），不是映射后的行——
   行里的 `date` 是我们自己填的，拿它比等于自己跟自己比。
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config

DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DATA_REFERER = "https://data.eastmoney.com/"
UA = "Mozilla/5.0 (stock_price_show)"

#: 沪深300。`1.` 是东财的沪市前缀（`0.` 是深市）。
INDEX_SECID = "1.000300"
INDEX_SYMBOL = "sh000300"
INDEX_NAME = "沪深300"
INDEX_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
INDEX_REFERER = "https://quote.eastmoney.com/"

#: 要的列。`SECUCODE`（`600519.SH`）是唯一能拼出 `sh600519` 的字段——
#: `SECURITY_CODE`（`600519`）丢了市场，沪深的 000xxx 会撞在一起。
COLUMNS = ("SECUCODE,TRADE_DATE,SECURITY_NAME_ABBR,CLOSE_PRICE,CHANGE_RATE,"
           "PE_TTM,PB_MRQ,PS_TTM,TOTAL_MARKET_CAP,NOTLIMITED_MARKETCAP_A")

#: B 股代码前缀，**入库前剔掉**。
#:
#: 这个接口把沪深 B 股也一起返回（`900xxx.SH` / `200xxx.SZ`），而项目里其余所有数据
#: （`stock` / `kline` / `scan`）都只有 A 股 + 北交所。留着它们会让"全市场多少只有价"
#: 多出约 100 只，与其它表对不上账。按前缀剔除而不是白名单——白名单会在交易所
#: 新增代码段（如 2024 年的 `920xxx`）时把新股静默漏掉。
B_SHARE_PREFIX = (("sh", "900"), ("sz", "200"))


class SourceError(RuntimeError):
    """上游不可用（接口改名、网络断、返回不是 JSON）。**与"这一天没数据"分开。**"""


def fetch_day(day: str, *, page_size: int | None = None, get=None) -> list[dict]:
    """取 `day` 的**全市场**横截面，返回 `price` 表的行（可能为空）。

    `get` 只为测试注入（默认走 `_get`）。空列表表示"这一天上游没有数据"
    ——盘中查今天、非交易日、上游还没算完，都是这种情形，由调用方报错。
    """
    size = int(page_size or config.PAGE_SIZE)
    raw: list[dict] = []
    page = 1
    reader = get or _get
    while page <= config.MAX_PAGES:
        body = reader(_url(day, page, size), referer=DATA_REFERER)
        try:
            payload = json.loads(body)
        except ValueError as e:
            raise SourceError(f"东财估值横截面返回的不是 JSON：{str(body)[:120]}") from e
        if payload.get("success") is False:
            # `9201 返回数据为空` 是**这一天没有数据**，不是接口坏了。
            # 当成 SourceError 会误导成"接口改名了"，于是有人去改 reportName——
            # 改完仍然为空，白折腾。
            msg = str(payload.get("message") or "")
            if str(payload.get("code")) == "9201" or "为空" in msg:
                break
            raise SourceError(f"东财接口报错（{payload.get('code')}）：{msg}。"
                              "多半是 reportName / columns 改名了")
        result = payload.get("result") or {}
        data = result.get("data") or []
        if not data:
            break
        raw.extend(data)
        pages = int(result.get("pages") or 1)
        if page >= min(pages, config.MAX_PAGES):
            break
        page += 1

    off = sorted({d for d in (_date(r.get("TRADE_DATE")) for r in raw)
                  if d and d != day})
    if off:
        raise SourceError(
            f"{day} 的估值横截面里混进了别的交易日（{', '.join(off[:3])}）。"
            "多半是上游把请求的日期忽略了，与 filter 一起复查")

    rows = [r for r in (_row(raw_row, day) for raw_row in raw) if r is not None]
    return rows


def fetch_index(day: str, *, get=None) -> dict | None:
    """取 `day` 的沪深300收盘（东财 `push2his` 日 K），没有返回 `None`。

    与全市场横截面分开取：指数不在 `RPT_VALUEANALYSIS_DET` 里，而且它不是
    "某只股票"，不该混进 `price`（会多出一只没有 PE/PB/PS 的"票"，把
    `price_stat` 的家数和涨跌幅统计带偏）。所以单独一张 `index_price`。

    非交易日 / 当天 K 线还没生成时接口给 `data: null` 或空 `klines`，这里返回
    `None`，由调用方决定是警告还是跳过；接口报错才抛 `SourceError`。
    """
    body = (get or _get)(_index_url(day), referer=INDEX_REFERER)
    try:
        payload = json.loads(body)
    except ValueError as e:
        raise SourceError(f"东财沪深300 K 线返回的不是 JSON：{str(body)[:120]}") from e
    if payload.get("rc") not in (0, None):
        raise SourceError(f"东财沪深300接口报错（rc={payload.get('rc')}）")
    data = payload.get("data") or {}
    for line in data.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 9 or parts[0] != day:
            continue
        # f51..f61 = 日期,开,收,高,低,量,额,振幅,涨跌幅,涨跌额,换手
        return {
            "date": day,
            "symbol": INDEX_SYMBOL,
            "name": str(data.get("name") or INDEX_NAME),
            "close": _num(parts[2]),
            "pct_chg": _num(parts[8]),
            "src": "eastmoney",
        }
    return None


def _index_url(day: str) -> str:
    params = {"secid": INDEX_SECID,
              "fields1": "f1,f2,f3,f4,f5,f6",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
              "klt": "101", "fqt": "1",
              "beg": day.replace("-", ""), "end": day.replace("-", "")}
    return INDEX_URL + "?" + urllib.parse.urlencode(params)


def _url(day: str, page: int, size: int) -> str:
    params = {"reportName": "RPT_VALUEANALYSIS_DET", "columns": COLUMNS,
              "pageNumber": page, "pageSize": size,
              "sortColumns": "SECURITY_CODE", "sortTypes": "1",
              "source": "HSF10", "client": "PC",
              "filter": f"(TRADE_DATE='{day}')"}
    return DATA_URL + "?" + urllib.parse.urlencode(params)


def _row(raw: dict, day: str) -> dict | None:
    """一行横截面 → `price` 表的一行。**缺失一律 None，不填 0。**

    `CHANGE_RATE` 上游是**百分数**（`-1.548886737657` = 跌 1.55%），直接落库不做换算;
    `TOTAL_MARKET_CAP` / `NOTLIMITED_MARKETCAP_A` 单位是元。
    """
    symbol = _symbol_of(raw.get("SECUCODE"))
    if symbol is None:
        return None
    return {
        "date": day,
        "symbol": symbol,
        "name": (str(raw.get("SECURITY_NAME_ABBR") or "").strip() or None),
        "close": _num(raw.get("CLOSE_PRICE")),
        "pct_chg": _num(raw.get("CHANGE_RATE")),
        "pe_ttm": _num(raw.get("PE_TTM")),
        "pb_mrq": _num(raw.get("PB_MRQ")),
        "ps_ttm": _num(raw.get("PS_TTM")),
        "total_mv": _num(raw.get("TOTAL_MARKET_CAP")),
        "circ_mv": _num(raw.get("NOTLIMITED_MARKETCAP_A")),
        # 落库时写进 `src`，与 `updated_at` 一起留痕：将来若换源/加备源，
        # 库里能直接看出某一天是哪条路取回来的。
        "src": "eastmoney",
    }


def _symbol_of(secucode) -> str | None:
    """`"600519.SH"` → `"sh600519"`；B 股与认不出的返回 `None`。"""
    s = str(secucode or "").strip().upper()
    if "." not in s:
        return None
    code, _, mkt = s.rpartition(".")
    if not code.isdigit():
        return None
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt)
    if prefix is None:
        return None
    if any(prefix == p and code.startswith(c) for p, c in B_SHARE_PREFIX):
        return None
    return prefix + code


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
    """可空浮点。亏损股的 PE 是负的或空的，**填 0 会被当成"极度便宜"**。"""
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get(url: str, *, referer: str = DATA_REFERER) -> str:
    """单独抽出来，测试里换掉，不必真的联网。

    **必须接住 `RemoteDisconnected`**：连续请求后东财会直接掐连接（不是返回 4xx，
    而是 `http.client.RemoteDisconnected`，它是 `OSError` 的子类，`URLError` 接不住）。
    漏接就会以一个"看不懂的异常"穿过 CLI 直达用户。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    try:
        with urllib.request.urlopen(req, timeout=config.TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise SourceError(f"东财 HTTP {e.code}") from e
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise SourceError(f"东财连接中断：{e}") from e


__all__ = ["fetch_day", "fetch_index", "SourceError", "COLUMNS", "B_SHARE_PREFIX",
           "INDEX_SYMBOL", "INDEX_NAME"]
