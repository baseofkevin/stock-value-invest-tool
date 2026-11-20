"""上游抓取：四个源，全部用标准库（本项目工具不引第三方依赖）。

为什么是这四个接口
------------------
1. **东财 `push2his` 周K**：一次给 11 年周线，**`f61` 就是周换手率**。
   中证官方不给换手率，而这套里"地量"那条只能靠换手率判断（成交额会被点位高低带偏）。
2. **中证 `index-perf`**：官方估值，一次 970KB 给 2015 至今的**日频**收盘 + 滚动PE，
   `peg` 字段 0 空值。比第三方的 PE 更权威，而且跟收盘同源，趋势和估值口径自洽。
3. **东财 `RPTA_WEB_TREASURYYIELD`**：10 年国债收益率，`filter` 卡到 2015，7 页取完。
4. **乐咕破净统计**：一次性返回 2005 至今的全A破净家数/总数，不用自己攒。

三个必须知道的边界
------------------
* **中证滞后一到两个交易日**（2026-11-20 实测：东财已有 11-20 收盘，中证最新 11-19）。
  所以结果行里的 `data_asof` 取"最旧的那一路"，页面照实标"数据截至"。
* **乐咕用的是页面上的固定 token**，跟 akshare 里那个一样。它变了就 403——
  这一路失败不影响趋势和估值，降级路径在 `__main__.py`。
* **东财周K的"当周"是实时更新的**。周五盘后跑拿到的是完整一周；盘中跑会拿到半周，
  所以这个包只该在盘后跑（`run.py all` 本来就是盘后）。
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config

# ---------------------------------------------------------------- 基础

def _get(url: str, params: dict | None, *, referer: str | None = None,
         timeout: int = config.TIMEOUT) -> dict:
    """带 UA + 重试的 GET。第二次重试只针对网络层异常，不针对 4xx。"""
    qs = urllib.parse.urlencode(params or {})
    full = f"{url}?{qs}" if qs else url
    headers = {"User-Agent": config.UA, "Accept": "application/json,*/*"}
    if referer:
        headers["Referer"] = referer

    last: Exception | None = None
    for attempt in range(config.RETRY):
        try:
            req = urllib.request.Request(full, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # 4xx 是"问错了"，重试不会变对
            if 400 <= e.code < 500:
                raise
            last = e
        except Exception as e:  # noqa: BLE001  网络层什么都可能
            last = e
        if attempt + 1 >= config.RETRY:
            break
    raise RuntimeError(f"请求失败：{full[:80]}…（{last}）")


def _f(v, default=None):
    """上游的数值可能是字符串、'-'、None。一律退化成 None，不猜。"""
    try:
        if v in (None, "", "-", "--"):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _d8(text: str) -> str | None:
    """`20261119` → `2026-11-19`。"""
    t = str(text or "").strip()
    if len(t) != 8 or not t.isdigit():
        return None
    return f"{t[:4]}-{t[4:6]}-{t[6:]}"


# ---------------------------------------------------------------- 1. 周K

WEEK_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
WEEK_REFERER = "https://quote.eastmoney.com/"
WEEK_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def hs300_week(limit: int = 600) -> list[dict]:
    """沪深300 周K。列序：日期,开,收,高,低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率。"""
    data = _get(WEEK_URL, {
        "secid": "1.000300",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": WEEK_FIELDS,
        "klt": "102",          # 102 = 周
        "fqt": "0",
        "end": "20500101",
        "lmt": str(limit),
    }, referer=WEEK_REFERER)

    klines = ((data.get("data") or {}).get("klines")) or []
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 11:
            continue
        rows.append({
            "date": p[0], "open": _f(p[1]), "close": _f(p[2]),
            "high": _f(p[3]), "low": _f(p[4]),
            "volume": _f(p[5]), "amount": _f(p[6]),
            "amplitude": _f(p[7]), "pct_chg": _f(p[8]),
            "turnover": _f(p[10]),
        })
    if not rows:
        raise RuntimeError("东财周K 返回空")
    return rows


# ---------------------------------------------------------------- 2. 日频 PE

PERF_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"


def hs300_perf(start: str = "2015-01-01", end: str | None = None) -> list[dict]:
    """中证官方：日频收盘 + 滚动市盈率。`peg` 就是 PE(TTM)。"""
    end = end or dt.date.today().isoformat()
    data = _get(PERF_URL, {
        "indexCode": "000300",
        "startDate": start.replace("-", ""),
        "endDate": end.replace("-", ""),
    }, referer="https://www.csindex.com.cn/", timeout=config.PERF_TIMEOUT)

    rows = []
    for item in (data.get("data") or []):
        day = _d8(item.get("tradeDate"))
        if not day:
            continue
        rows.append({
            "date": day,
            "close": _f(item.get("close")),
            "pe_ttm": _f(item.get("peg")),
            "sample_size": _f(item.get("consNumber")),
        })
    if not rows:
        raise RuntimeError("中证 index-perf 返回空")
    return rows


# ---------------------------------------------------------------- 3. 10年国债

BOND_URL = "https://datacenter.eastmoney.com/api/data/get"
BOND_TOKEN = "894050c76af8597a853f5b408b759f5d"
BOND_PAGE = 500


def bond10y(start: str = config.BOND_START) -> list[dict]:
    """10 年国债收益率（%）。东财把中美各期限塞在一张表里，`EMM00166466` 是中国的 10 年。"""
    rows: list[dict] = []
    page = 1
    while page <= 40:
        data = _get(BOND_URL, {
            "type": "RPTA_WEB_TREASURYYIELD",
            "sty": "ALL",
            "st": "SOLAR_DATE",
            "sr": "-1",
            "token": BOND_TOKEN,
            "p": str(page), "ps": str(BOND_PAGE),
            "pageNo": str(page), "pageNum": str(page),
            "filter": f"(SOLAR_DATE>='{start}')",
        }, referer="https://data.eastmoney.com/")

        result = data.get("result") or {}
        batch = result.get("data") or []
        for item in batch:
            day = str(item.get("SOLAR_DATE") or "")[:10]
            val = _f(item.get("EMM00166466"))
            if day and val is not None:
                rows.append({"date": day, "value": val})
        pages = int(result.get("pages") or 1)
        if page >= pages or not batch:
            break
        page += 1

    if not rows:
        raise RuntimeError("东财国债收益率返回空")
    rows.sort(key=lambda r: r["date"])
    return rows


# ---------------------------------------------------------------- 4. 破净率

BELOW_URL = "https://legulegu.com/stockdata/below-net-asset-statistics-data"
BELOW_TOKEN = "325843825a2745a2a8f9b9e3355cb864"


def below_na(market: str = "1") -> list[dict]:
    """全A破净股统计（2005 至今）。`marketId=1` 是全部A股。"""
    data = _get(BELOW_URL, {"marketId": market, "token": BELOW_TOKEN},
                referer="https://www.legulegu.com/stockdata/below-net-asset-statistics")

    rows = []
    for item in data if isinstance(data, list) else []:
        day = str(item.get("date") or "")[:10]
        below = _f(item.get("belowNetAsset"))
        total = _f(item.get("totalCompany"))
        if not day or below is None or not total:
            continue
        rows.append({
            "date": day,
            "value": round(below / total * 100, 4),
            "below": int(below),
            "total": int(total),
        })
    if not rows:
        raise RuntimeError("乐咕破净率返回空")
    rows.sort(key=lambda r: r["date"])
    return rows
