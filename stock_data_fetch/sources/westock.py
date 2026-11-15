"""腾讯自选股适配器：`finance` / `dividend` / `quote` / `profile` / `snapshot`。

它和 `tdx.py` 的关系：同一个传输（MCP），不同的上游。tdx 要 OAuth 令牌而拿不到，
westock 的令牌落在 `~/.config/westock/tokens.json`，所以它才是能用的那个。

**四层里它属于第 3 层（适配器）**：只负责"把上游返回变成行"，不 sleep、不写库。

## 五种返回形态，一律不统一（`2026-11-14` 实测）

| 工具 | `pick_data()` 之后的样子 | 还要再剥一层？ |
|---|---|---|
| `data_finance` | `{code, msg, data: {sh600519: {income,balance,cashflow}}}` | **是**，取 `.data` |
| `data_dividend` | `{code, start, end, plans: [...]}` | 否 |
| `data_quote` | `{sh600519: {...}, sz000651: {...}}` | 否 |
| `data_profile` | `{code, name, listedDate, industry, ...}` | 否 |
| `data_consensus` / `data_rating` | 直接是业务字段 | 否 |

统一它们要写一堆 `if`，而且上游哪天改一层就又错。**按工具各自剥**，剥错的后果是
"空表"——而空表会被 `empty_reason` 抓住，不会静默。

## 三个必须记住的上游坑

1. **`data_dividend` 只回"近 N 年"，`N` 默认 1。** 不传 `years` 就只拿一年分红，
   算 10 年复权因子会少乘若干次 `r`，而且**完全看不出来**（因子偏小，价格偏高）。
   这里按 `need.start` 反算年数，至少 11 年。
2. **`data_dividend` 不支持批量。** 传 `codes` 两只以上直接回空 `plans`（实测），
   所以它走逐只，其余四个走批量。
3. **财务三个表要按 `EndDate` 合并。** 上游给的是三条独立数组，同一报告期各占一行；
   `finance` 表是"一期一行、三表字段并排"，合并时必须用 `EndDate` 做键——
   用列表下标对齐会在"某期缺一张表"时错位，而且错得很安静。

## 令牌

`STOCKHUB_WESTOCK_TOKEN` > `~/.config/westock/tokens.json` 的 `accessToken`。
过期的处理是**自己 refresh**（refresh_token grant），不依赖 `~/.local/bin/westock`
那个个人脚本：数据层要能独立跑，不能因为某人改了家目录的脚本就起不来。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..errors import EmptyResult, SourceUnavailable
from ..need import Need
from .base import BatchError, Got
from .mcp import McpClient, read_token_file

log = logging.getLogger("stock_data_fetch.westock")

DEFAULT_URL = "https://stockbuddy.qq.com/cgi/cgi-bin/openai/mcp/mcp"
TOKEN_FILE = Path(os.environ.get("STOCKHUB_WESTOCK_TOKEN_FILE")
                  or Path.home() / ".config" / "westock" / "tokens.json")

# 注意 `or` 的位置：写成 `os.environ.get("A" or "默认值")` 的话，`or` 是 get() 的**参数**，
# 等于永远去查名为"默认值"的那个环境变量——于是这里恒为 None，续期时 `urlopen(None)`
# 抛一个 `ValueError: unknown url type: 'None'`。这个 bug 只有真去打一次上游才会现形，
# 而它恰好发生在"令牌过期"这条最需要它顶住的路径上。
TOKEN_URL = (os.environ.get("STOCKHUB_WESTOCK_TOKEN_URL")
             or "https://stockbuddy.qq.com/cgi/cgi-bin/openai/mcp/oauth/token")

BATCH = 40
"""单次最多几只（全量回补用）。实测 150 只也能回，但一批 150 只的响应体很大、限额也吃得猛，
拿 40 换"失败时只重取 40 只"，比一次拿满更划算。

**增量时放宽到 `DELTA_BATCH`**（2026-11-15）：增量补新每只只回 1~3 行，单请求体积小一个量级，
可以一次带更多码。原版 kline-screen-tool 的实测值是「全量 20 只/批、增量 60 只/批」
（`params.py:9,20`，注释：「服务端吞吐约 600 只/分钟，20 只/批贴着这个上限最稳」）。
"""

DELTA_BATCH = 60
"""增量补新（`need.points` 很小）时的单批只数。见 `BATCH` 的说明。"""


def batch_size(need: Need | None) -> int:
    """按本次要取的根数选批量：只补几天就放大到 60，整段回补仍用 40。

    阈值取 10 根：日增量只补 1–3 根，补洞通常也在一两周内；超过 10 根就说明这次是
    "回补一整段"（首次建库、换口径、或断更很久），响应体会随根数线性变大，
    这时回到 40 只/批更稳。实测 20 只 × 6 根 = 120 行，一批 0.6 秒。
    """
    points = int(getattr(need, "points", 0) or 0)
    return DELTA_BATCH if 0 < points <= 10 else BATCH

FIN_FIELDS = (
    # 利润表（income）：累计 / 单季 / TTM 三套并列，全部是上游原值
    "OperatingRevenue", "OperatingRevenue_Q", "OperatingRevenueTTM",
    "OperatingCost", "OperatingCost_Q", "OperatingCostTTM",
    "OperatingProfit", "OperatingProfit_Q", "OperatingProfitTTM",
    "NPParentCompanyOwners", "NPParentCompanyOwners_Q", "NPParentCompanyOwnersTTM",
)
BALANCE_FIELDS = (
    # 资产负债表（balance）：没有单季/TTM 概念
    "TotalFixedAsset",
    "TConstruInProcess",        # ← 在建工程。schema 列名是 CIP，见下面的 RENAME
    "ContractLiability",
)

RENAME = {"TConstruInProcess": "CIP"}
"""上游列名 → 库里列名。**全项目只有这一处改名**。

`FIN_FIELDS` / `BALANCE_FIELDS` 一律照抄上游原名（零映射风险），唯一的例外是在建工程：
上游叫 `TConstruInProcess`，`schema.sql` 里叫 `CIP`。改名落在 `_merge_statements` 里，
不改这里——这样"哪些字段要改名"一眼能数清（就一条）。
"""

FINANCIAL_WORDS = ("银行", "保险", "证券", "非银金融", "多元金融",
                   "信托", "期货", "租赁")
"""`is_financial` 的判定词。**这是个近似**——真要精确就得上申万三级，
但 quality 只用它切"金融股不适用 PE/PB"这一条，词表足够。上游改行业名时要回来看这里。

**`"非银金融"` 是 2026-11-15 补的，修的是一个静默漏判**：原来词表只有具体业态
（银行/保险/证券…），而 A 股保险股的一级行业叫「非银金融」、名字里又没有"保险"二字——

    中国人寿  非银金融  →  漏判 False   （应为 True）
    中国平安  非银金融  →  漏判 False   （应为 True）
    中信证券  非银金融  →  碰巧 True    （名字里含"证券"，靠运气）
    农业银行  银行      →  True         （唯一靠行业名正确命中的）

漏判的后果不是标签错，是**下游算错**：`kline_screen` 的「毛利率改善」拿 `is_financial`
做豁免，险企 `OperatingCost_Q` 是 NULL（没有毛利率语义），被判成非金融后硬算，
输出「无数据：无毛利率数据」——看起来像数据缺失，实际是分类错。
所以这里加行业名直判，不再依赖股票名里恰好含业态词。"""


# ---------------------------------------------------------------- 令牌

_token_cache: str | None = None
_client_cache: McpClient | None = None


def token(force: bool = False) -> str | None:
    global _token_cache
    if _token_cache and not force:
        return _token_cache
    _token_cache = os.environ.get("STOCKHUB_WESTOCK_TOKEN") or read_token_file(TOKEN_FILE)
    return _token_cache


def _refresh(stale: str | None) -> str | None:
    """用 refresh_token 换新令牌。成功返回新 accessToken（并写回令牌文件）。"""
    try:
        raw = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rt = raw.get("refreshToken")
    if not rt:
        return None
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": raw.get("clientId", ""),
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "stock_data_fetch/0.1 (+mcp)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, ValueError, OSError) as e:
        # `ValueError` 也接：URL 写坏时 `urlopen` 抛的是 `unknown url type`。
        # 这类问题必须降级成"续期失败"（返回 None），而不是一个穿过 router
        # 直达四个工具的未捕获异常。
        log.warning("westock 令牌续期失败：%s", e)
        return None
    if not payload.get("access_token"):
        log.warning("westock 令牌续期被拒：%s", str(payload)[:200])
        return None

    raw["accessToken"] = payload["access_token"]
    if payload.get("refresh_token"):
        raw["refreshToken"] = payload["refresh_token"]
    raw["expiresAt"] = int(dt.datetime.now().timestamp() * 1000) + \
        int(payload.get("expires_in", 3600)) * 1000
    try:                                  # 写回：宿主里的连接器也读同一个文件
        tmp = TOKEN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, TOKEN_FILE)
    except OSError as e:                  # 写不进去不影响本次调用
        log.debug("令牌写回失败（不影响本次）：%s", e)
        try:
            tmp.unlink()                  # noqa: F821 - 只在上面的 try 里定义
        except (OSError, UnboundLocalError):
            pass
    global _token_cache
    _token_cache = raw["accessToken"]
    log.info("westock 令牌已续期")
    del stale
    return _token_cache


def client() -> McpClient:
    global _client_cache
    t = token()
    if _client_cache is None or _client_cache.token != t:
        _client_cache = McpClient(DEFAULT_URL, token=t, timeout=90)
    return _client_cache


def available() -> tuple[bool, str]:
    """注册表用它决定要不要把本源摘掉。缺令牌 = 不可用，而不是"取到空"。"""
    if token():
        return True, ""
    return False, f"没有 westock 令牌（{TOKEN_FILE} 或 STOCKHUB_WESTOCK_TOKEN）"


def supports(domain: str, need: Need) -> bool:
    """`snapshot` 下我只管 `consensus` / `rating`；`mainop` 是东财的 `RPT_F10_FN_MAINOP`。

    没有这个钩子，`ensure(snapshot, mainop)` 会先选中我、抛一次 `SourceUnavailable`、
    记一次 strike，再换到东财——每只股票白打一次，日志里还像是"westock 出故障了"。
    """
    if domain == "snapshot":
        return _snapshot_domain(need) in ("consensus", "rating")
    return domain in ("kline", "finance", "dividend", "quote", "profile", "scan")


def _snapshot_domain(need: Need) -> str:
    return str(need.extra.get("snapshot_domain") or "").strip()


# ---------------------------------------------------------------- 调用

def call(tool: str, args: dict, *, tries: int = 2) -> dict | None:
    """调一次工具。401 时续期重试一次；其余失败返回 `None`（由上层记 partial）。

    **不要在适配器里 sleep 重试**（`base.FetchFn` 第 1 条）：重试与退避归 router，
    这里只做"令牌过期"这一种确定性修复。
    """
    c = client()
    try:
        return c.call(tool, args, tries=tries)
    except SourceUnavailable as e:
        if "401" not in str(e) and "OAuth" not in str(e):
            raise
        log.info("westock 令牌失效，续期后重试：%s", tool)
        if _refresh(token()) is None:
            raise SourceUnavailable(
                "westock 令牌已失效且无法续期。请重新登录 WorkBuddy 的腾讯自选股连接器",
                tool=tool) from e
        global _client_cache
        _client_cache = None
        return client().call(tool, args, tries=tries)


def fetch(domain: str, codes, need: Need) -> Got:
    """`base.FetchFn` 的实现。按域分发，哪个域不支持就报清楚。"""
    if domain == "kline":
        return _kline(list(codes), need)
    if domain == "finance":
        return _finance(list(codes), need)
    if domain == "dividend":
        return _dividend(list(codes), need)
    if domain == "quote":
        return _quote(list(codes), need)
    if domain == "profile":
        return _profile(list(codes), need)
    if domain == "snapshot":
        return _snapshot(list(codes), need)
    if domain == "scan":
        return _scan(list(codes), need)
    raise SourceUnavailable(f"westock 适配器不支持 {domain} 域", domain=domain)


# ---------------------------------------------------------------- kline

def _kf(v) -> float | None:
    """容错的数值转换。与 tencent 的 `_f` 不同：缺值给 `None` 不给 `0.0`——
    上游字段缺失时落 `NULL` 比"看起来是 0"更诚实（0 成交额会被换手折算当真值用）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_shares(vol: float | None, close: float | None, amount: float | None) -> float | None:
    """把上游的成交量统一成**股**。

    **单位按板块不一致**（2026-11-15 实测，同日同一请求）：

    | 代码 | amount | volume(上游) | amount/(close×vol) | 单位 |
    |---|---|---|---|---|
    | sh600019 宝钢 | 627,110,000 | 1,025,530 | **98.9** | 手（×100 才是股） |
    | sz300750 宁德 | 7,244,220,000 | 30,299,500 | **100.6** | 手 |
    | sh688016 心脉 | 167,850,000 | 1,932,126 | **1.006** | **股**（再 ×100 就错 100 倍） |
    | sh688552 南湖 | 402,490,000 | 11,162,580 | **1.01** | 股 |

    科创板（688/689）给的是股，其余是手。原来一律 ×100，于是 **466 只科创板**
    的成交量在库里放大 100 倍 —— `启动日量能倍数` 因为是比值不受影响，
    但 `20日换手` = 均成交额 ÷ 流通市值 直接错 100 倍（实测 sh688016 记成 110%，
    真值 1.1%），`--turn-pct` 一开就会把这些票全错杀或全错留。

    判据用 `amount / (close × volume)`：这是唯一不依赖"板块→单位"硬编码的办法
    （上游哪天统一了单位，这里自动跟上）。拿不到 amount 时退回 ×100（历史行为）：
    tencent 通道没有成交额字段，那些行本来就是按手给的。
    """
    if vol is None:
        return None
    if vol > 0 and close and amount and amount > 0:
        # `ratio` 用的是**上游原值**：是手时 amount ≈ close×vol×100（ratio≈100），
        # 已经是股时 amount ≈ close×vol（ratio≈1）。
        ratio = amount / (close * vol)
        return vol * 100.0 if ratio > 10 else vol
    return vol * 100.0


def _kline(codes: list[str], need: Need) -> Got:
    """日 K（raw 口径），`codes` 批量多码。

    2026-11-15 接入。此前 kline 只有东财（断连封禁期）与腾讯 HTTP（单码 +
    WAF 轮换封禁 + 令牌桶 ≈ 1 只/秒）两条腿，全市场补数慢到不可用；
    westock MCP 的 `data_kline` 支持 `codes` 逗号批量（40 只/请求）且走官方
    通道不受 WAF，是原版 kline-screen-tool 快的原因（它的 CLI 就是这么调的）。

    口径实测（2026-11-15，sh600519 与库内 tencent raw 逐点比对）：

    - **价格**：不传 `fq` 返回的就是 raw（1439.1/1480/1446.44… 与库内同值），
      不作废任何旧数据；
    - **volume 单位按板块不一致**（主板/创业板是手，科创板 688/689 是股），
      这里按 `amount/(close×volume)` 逐行判、统一成股，见 `_to_shares`；
    - **amount 有值**（tencent 通道没有、落库恒 0），正好补上这一列——
      judge_top 的换手折算、kline_screen 的 turnover20 都吃 amount；
    - **`last` 是收盘**（westock 列名），落库前改名 `close`；
    - 返回**倒序**（新→旧），这里排回升序。

    上游还有一个 `exchange`（换手率%）字段，**故意不落库**：hub kline 表没有
    这一列，judge_top 用 amount÷流通市值 折算（见 stock_judge_top/data.py §2.2）。
    """
    got = Got()
    start = need.start or ""
    end = need.end or ""
    # 与 tencent 适配器同款：多要 20 根给 min_bars 门槛留缓冲
    limit = max(1, int(need.points or 300) + 20)
    bsz = batch_size(need)                  # 全量 40 / 增量 60，见 BATCH 的说明
    for i in range(0, len(codes), bsz):
        chunk = codes[i:i + bsz]
        if len(chunk) == 1:
            d = call("data_kline", {"code": chunk[0], "start": start, "end": end,
                                    "period": "day", "limit": limit})
            if d is None:
                raise EmptyResult("data_kline 上游多次未返回数据（call 重试耗尽）")
            blocks = [{"symbol": chunk[0], "data": d}] if isinstance(d, dict) else []
        else:
            d = call("data_kline", {"codes": ",".join(chunk), "start": start,
                                    "end": end, "period": "day", "limit": limit})
            if d is None:
                raise EmptyResult("data_kline 上游多次未返回数据（call 重试耗尽）")
            blocks = (d.get("data") or []) if isinstance(d, dict) else []
            # 上游自报的**逐只**失败必须进 `got.errors`。
            # 不写的话 `router._fetch_with_retry` 永远看不到"哪几只缺"（它的重试起点是
            # `got.failed_symbols`，由 `errors[]` 派生），组级重试成了死代码；
            # `_pull` 的 `if got.errors: raise PartialData` 也永远不成立。
            for err in (d.get("errors") or []):
                sym = err.get("symbol")
                if not sym:
                    continue
                code = str(err.get("code") or "BATCH_ERROR")
                msg = str(err.get("message") or err)
                log.info("westock data_kline %s: %s", sym, msg)
                got.errors.append(BatchError([sym], code=code, message=msg))

        seen = set()
        for blk in blocks:
            sym = blk.get("symbol") or ""
            nodes = (blk.get("data") or {}).get("nodes") or []
            rows = []
            for n in nodes:
                day = str(n.get("date") or "")[:10]
                if len(day) != 10 or day[4] != "-":
                    continue
                close, amt = _kf(n.get("last")), _kf(n.get("amount"))
                rows.append({"date": day,
                             "open": _kf(n.get("open")), "close": close,
                             "high": _kf(n.get("high")), "low": _kf(n.get("low")),
                             "volume": _to_shares(_kf(n.get("volume")), close, amt),
                             "amount": amt})
            rows.sort(key=lambda r: r["date"])          # 上游倒序，排回升序
            if rows:
                got.rows[sym] = rows
                seen.add(sym)
            else:
                got.empty_reason = got.empty_reason or "no_data"
                seen.add(sym)                            # 回了块但 0 根：停牌/退市
        for sym in chunk:
            if sym not in seen:
                got.empty_reason = got.empty_reason or "no_data"
    return got


# ---------------------------------------------------------------- finance

def _finance(codes: list[str], need: Need) -> Got:
    periods = int(need.extra.get("periods") or need.points or 43)
    got = Got()
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        d = call("data_finance", {"codes": ",".join(chunk), "fields": "all",
                                  "num": periods})
        # 同上：`call()` 重试耗尽返回 None 是**上游抽风**，不是"这只票没数据"。
        # 不区分的话会静默落成空表，上层只看到 EmptyResult，重试判断也就对了；
        # 但这里显式抛出来，日志里才能看出是"没拿到"而不是"上游说没有"。
        if d is None:
            raise EmptyResult("data_finance 上游多次未返回数据（call 重试耗尽）")
        per = _unwrap(d) or {}
        if not isinstance(per, dict):
            raise SourceUnavailable("data_finance 返回结构变了（data 不是 dict）")
        for sym in chunk:
            stmts = per.get(sym)
            if not isinstance(stmts, dict):
                got.empty_reason = got.empty_reason or "no_data"
                continue
            rows = _merge_statements(sym, stmts)
            if rows:
                got.rows[sym] = rows
            else:
                got.empty_reason = got.empty_reason or "no_data"
    return got


_FIN_COLS_ALL = tuple(FIN_FIELDS) + tuple(RENAME.get(c, c) for c in BALANCE_FIELDS)
"""合并后一行应该有的全部列名。**每行都先按这个形状补 None**，理由见下。"""


def _blank_row(symbol: str, period: str) -> dict:
    return {"symbol": symbol, "period": period, "raw_json_parts": {},
            "_core": False, **{c: None for c in _FIN_COLS_ALL}}


def _merge_statements(symbol: str, stmts: dict) -> list[dict]:
    """三条数组按 `EndDate` 合并成"一期一行"。

    两个刻意的选择：

    1. **每行都补齐全部列名（值为 None）**，而不是"哪张表来了就只放哪张表的键"。
       行形状随数据变化是最难查的一类 bug：下游 `row["TotalFixedAsset"]` 有时 KeyError、
       有时拿到上一期的值。形状固定下来，缺就是 None，一眼能看出来。
    2. **只有现金流量表的期不出行**。`finance` 表里没有现金流量表的列，那种行的 15 个
       字段全是 NULL；把它插进去会抬高 `coverage.n`，于是"43 期里有 5 期是空壳"这种股票
       照样过 `>= 40` 的门槛——报告不报错，只是每一期的同比都变成了 None。
    """
    by_period: dict[str, dict] = {}
    for table, cols in (("income", FIN_FIELDS), ("balance", BALANCE_FIELDS)):
        for row in stmts.get(table) or []:
            period = _date(row.get("EndDate"))
            if not period:
                continue
            dst = by_period.setdefault(period, _blank_row(symbol, period))
            dst["_core"] = True                  # 有利润表或资产负债表 → 这一期算数
            for c in cols:
                # 唯一一处改名：TConstruInProcess → CIP（见 RENAME）
                dst[RENAME.get(c, c)] = _f(row.get(c))
            dst["raw_json_parts"][table] = row
    # 现金流量表只进 raw_json（schema 里没有它的列），但要挂到同一期上
    for row in stmts.get("cashflow") or []:
        period = _date(row.get("EndDate"))
        if not period:
            continue
        by_period.setdefault(period, _blank_row(symbol, period))["raw_json_parts"]["cashflow"] = row

    out = []
    for period in sorted(by_period):
        r = by_period[period]
        if not r.pop("_core"):
            continue                             # 空壳期，见上面第 2 条
        parts = r.pop("raw_json_parts", {})
        r["unit"] = "CNY"
        r["raw_json"] = json.dumps(parts, ensure_ascii=False) if parts else None
        out.append(r)
    return out


# ---------------------------------------------------------------- dividend

def _dividend(codes: list[str], need: Need) -> Got:
    """**逐只**（`data_dividend` 传多个 `codes` 会回空，实测）。

    年数按 `need.start` 反算，最少 11 年：`years` 默认是 1，漏传就只拿一年，
    因子少乘几次 `r` 且看不出来（见模块头第 1 条）。
    """
    years = _years_for(need.start)
    got = Got()
    for sym in codes:
        d = call("data_dividend", {"code": sym, "years": years})
        plans = (d or {}).get("plans") or []
        rows = _dividend_rows(sym, plans)
        if rows:
            got.rows[sym] = rows
        else:                             # 十年不分红是合法状态，不当错误
            got.empty_reason = got.empty_reason or "no_data"
    return got


def _dividend_rows(symbol: str, plans: list[dict]) -> list[dict]:
    """按 `ex_date` 聚合，**一个除权日一行**。见 `_dividend_row` 上的说明。

    两条方案"完全一样"时按重复下发处理，只留一条——否则同一条被上游发两遍就会
    派息翻倍。判据是 `(plan_text, 派/送/转/配)` 全等，比单看 `plan_text` 严：
    真正的"两条不同方案恰好同样多钱"几乎不存在，而重复下发是实测见过的。
    """
    by: dict[str, dict] = {}
    for plan in plans:
        row = _dividend_row(symbol, plan)
        if not row:
            continue
        es = row["ex_date"]
        cur = by.get(es)
        if cur is None:
            by[es] = row
            continue
        if _same_proposal(cur, row):
            continue
        for k in _SUM_FIELDS:
            cur[k] = (cur.get(k) or 0.0) + (row.get(k) or 0.0)
        pieces = [p for p in (cur.get("plan_text"), row.get("plan_text")) if p]
        joined: list[str] = []
        for p in pieces:
            if p not in joined:           # 同一条方案被上游重复下发时不重复拼
                joined.append(p)
        cur["plan_text"] = " + ".join(joined)[:200] or None
    return [by[k] for k in sorted(by)]


def _same_proposal(a: dict, b: dict) -> bool:
    return a.get("plan_text") == b.get("plan_text") and all(
        abs((a.get(k) or 0.0) - (b.get(k) or 0.0)) < 1e-12 for k in _SUM_FIELDS)


_SUM_FIELDS = ("cash_per_share", "bonus_per_share", "transfer_per_share", "rights_per_share")


def _dividend_row(symbol: str, plan: dict) -> dict | None:
    """**一条方案** → 一行。同一除权日的多条必须由 `_dividend_rows` 求和，不能逐条写库。

    上游把"年度分红"和"特殊分红"拆成两条，共用同一个 `exDiviDate`（`proposalSn` 1/2、
    `dividendType` 有分红/特殊分红）。2026-11-14 冒烟实测：

        平安 sh601318  2018-06-07  10派10.000 + 10派2.000  = 1.200 元/股
        宁德 sz300750  2024-04-30  10派20.110 + 10派30.170 = 5.028 元/股
        宁德 sz300750  2026-04-22  10派21.780 + 10派47.790 = 6.957 元/股

    逐条写库时 `(symbol, ex_date)` 主键让后一条 `ON CONFLICT DO UPDATE` 掉前一条，
    于是库里只剩一条——宁德 2026 少记 2.178 元/股（-31%），平安少记 0.2 元/股。
    复权因子随之偏大，历史收益率被系统性抬高，而日志里只有一句"与库里不一致"。
    """
    ex = _date(plan.get("exDiviDate"))
    if not ex:
        return None                       # 没有除权日 = 还没实施，不进因子
    return {
        "symbol": symbol,
        "ex_date": ex,
        # 上游一律"每 10 股"，入库一律"每股"
        "cash_per_share": (_f(plan.get("cashDiviRMB")) or 0.0) / 10.0,
        "bonus_per_share": (_f(plan.get("bonusShareRatio")) or 0.0) / 10.0,
        "transfer_per_share": (_f(plan.get("tranAddShareRatio")) or 0.0) / 10.0,
        "rights_per_share": 0.0,          # 配股这个工具不给，留 0 而不是猜
        "rights_price": None,
        "plan_text": (plan.get("dividendPlan") or "")[:200] or None,
    }


def _years_for(start: str | None) -> int:
    if not start:
        return 11
    try:
        days = (dt.date.today() - dt.date.fromisoformat(start)).days
    except ValueError:
        return 11
    return max(11, days // 365 + 2)


# ---------------------------------------------------------------- quote

def _quote(codes: list[str], need: Need) -> Got:
    got = Got()
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        d = call("data_quote", {"codes": ",".join(chunk)})
        # `call()` 重试耗尽后会返回 None——**这不是"接口改名"**。
        # 实测：同一只股票单独查全都正常，39 只一次批量也正常，但批量跑 39 只票时
        # 零星几只（sz301395 / sh603725 / sh603138 …）会返回 None，隔一会儿再查又有。
        # 所以它是上游间歇性抽风，报成 EmptyResult 让上层有机会重试；
        # 报成 SourceUnavailable 会让 router 直接降级、上层也拿不到重试信号，
        # 症状是"报告能出但少了行情"或"整只票失败"。
        if d is None:
            # **只判这一批失败，不要把整批拖下水**（2026-11-15 修）。
            #
            # 原来这里 `raise EmptyResult`：40 只一批里只要有一批抽风，整次
            # `data_quote` 调用就抛出去——而调用方一次可能递进来 3000+ 只
            # （kline_screen 的 [6/6] 要给整个粗筛池取流通市值），于是一批坏掉
            # 就全军覆没（真跑实测：3271 只全失败，换手率算不出来，整跑终止）。
            #
            # 记进 `got.errors` 之后，router 的组级重试只会重取这 40 只
            # （`_fetch_with_retry` 按 `failed_symbols` 再发一次），失败面从
            # "整批"缩到"这一批"。
            got.errors.append(BatchError(
                chunk, message="data_quote 上游多次未返回数据（call 重试耗尽）"))
            continue
        if not isinstance(d, dict):
            # 真·结构变更（比如哪天改成返回 list）：这时重试也没用，必须报上去。
            raise SourceUnavailable(
                f"data_quote 返回结构变了（{type(d).__name__}，不是 dict）")
        for sym in chunk:
            row = d.get(sym)
            if not isinstance(row, dict) or not row:
                got.empty_reason = got.empty_reason or "no_data"
                continue
            got.rows[sym] = [_quote_row(sym, row, need)]
    return got


def _quote_row(symbol: str, r: dict, need: Need | None = None) -> dict:
    """实测字段（2026-11-14，茅台）：`time` 是 `"2026-11-13"` 这种纯日期。

    `volume` 是**手**（`41780` 手 × 100 = 4,178,000 股，对应 `turnover_rate=0.33%`
    与 12.5 亿流通股，逐项对得上），所以乘 100 换成股。`amount` 已是元。
    """
    day = _date(r.get("time")) or (_date(r.get("date")))
    if not day and need is not None:
        day = need.end or ""                     # 上游哪天真去掉了 time，也别把空串写进库
    return {
        "symbol": symbol,
        "trade_date": day or "",
        "price": _f(r.get("price")),
        "prev_close": _f(r.get("prev_close")),
        "pct_chg": _f(r.get("change_percent")),
        "volume": (_f(r.get("volume")) or 0.0) * 100.0,   # 手 → 股
        "amount": _f(r.get("amount")),
        "turnover": _f(r.get("turnover_rate")),
        "float_shares": _f(r.get("float_shares")),
        "total_shares": _f(r.get("total_shares")),
        "total_mv": _f(r.get("total_market_cap")),
        "circ_mv": _f(r.get("circulating_market_cap")),
        "pe_ttm": _f(r.get("pe_ratio")),
        "pb": _f(r.get("pb_ratio")),
    }


# ---------------------------------------------------------------- scan

def _scan(codes: list[str], need: Need) -> Got:
    """某交易日的**全市场横截面**。一次请求拿全部，`codes` 不用。

    工具是 `tool_filter`（线上名 `stock_filter_query`），就是原 kline-screen 调的
    `westock screen condition` 的 MCP 版。它**支持历史日期**——这一条决定了 `scan`
    能不能当横截面域用：东财的 `clist` 只给"当前"，补不了上周五。

    ## 三个必须校验的东西

    1. **不能截断**。上游 `limit` 超了不报错，只少给，而返回体里的 `totalStocks`
       是**满足表达式的真实总数**（实测 `ClosePrice > 0` 时 5031）。不比对就会得到
       "2026-11-13 只有 4600 只股票在交易"这种结论，而且一路安静地产出一份
       少了 400 只候选的名单。
    2. **`ok: false` 与"返回的不是 JSON"都要抛**，不能当空表。后者是超时的表现：
       服务端 2 秒没算完就回一段中文异常文本（`RoundTrip timeout ... cost: 2.0003s`），
       当空表处理的话症状是"这一天全市场一只票都没有"。
    3. **字段缺了要抛**。`tool_filter` 只返回**表达式引用到的列**（`config.SCAN_EXPR`
       里那个条件的唯一作用就是"请求 ClosePrice 与 ChangePCT 这一族"）。哪天有人改了
       表达式，返回体里就少一列，于是整列全是 `None`——而"整列全是 None"和
       "上游确实没给"在调用方看起来一样。这里在适配器边界上就拦住。

    ## 服务端有个 2 秒硬超时

    超时是"表达式条件数 × 返回行数"的乘积：全市场 + 1 个条件 2.4 秒拿回 5031 行；
    全市场 + 5 个条件直接挂。所以 `SCAN_EXPR` 只有一条条件，改之前先看
    `config.py` 里那张实测表。
    """
    from ..config import SCAN_EXPR, SCAN_LIMIT, SCAN_ORDERBY

    day = need.end or need.start
    if not day:
        raise SourceUnavailable("scan 域需要一个交易日，但 need.end / need.start 都是空的")

    got = Got()
    orderby, order = SCAN_ORDERBY
    d = call("tool_filter", {"expression": SCAN_EXPR,
                             "date": day, "limit": SCAN_LIMIT, "market": "hs",
                             "orderby": orderby, "order": order})
    if d is None:
        raise SourceUnavailable(f"tool_filter 对 {day} 没有返回（限频、令牌失效或服务端超时）")
    if not isinstance(d, dict):
        raise SourceUnavailable(f"tool_filter 返回结构变了（不是 dict）：{type(d).__name__}")
    if d.get("ok") is False:
        raise SourceUnavailable(
            f"tool_filter 对 {day} 报错：{d.get('message') or d.get('error') or d}")
    body = d.get("data") if isinstance(d.get("data"), dict) else d

    stocks = body.get("stocks")
    if not isinstance(stocks, list):
        raise SourceUnavailable(f"tool_filter 的 data.stocks 不是数组（{day}）")
    total = body.get("totalStocks")
    if isinstance(total, int) and total > len(stocks):
        last = _f((stocks[-1] or {}).get("ChangePCT")) if stocks else None
        raise SourceUnavailable(
            f"{day} 的横截面被截断：满足条件的有 {total} 只，只回来 {len(stocks)} 只"
            f"（最后一名的涨幅 {last}%）。"
            f"请调大 config.SCAN_LIMIT（当前 {SCAN_LIMIT}）"
            f"——注意 limit 也在服务端 2 秒超时的乘积里，别一次跳太多")

    rows = [_scan_row(day, r) for r in stocks if isinstance(r, dict) and r.get("code")]
    rows = [r for r in rows if r is not None]
    if not rows:
        # 非交易日会走到这里。不当错误——是不是交易日该由调用方的日历说了算，
        # 这里只如实说"这一天上游没给任何一只票"。
        got.empty_reason = "no_data"
        return got

    _check_scan_fields(rows, day)
    got.rows[SCAN_ROW_KEY] = rows
    return got


SCAN_ROW_KEY = "*"
"""`Got.rows` 的键。必须与 `api.SCAN_SYMBOL` 一致——`_pull` 用 `got.rows.get(symbol)` 取。"""

# 这几列算不出来，粗筛就是错的。
_SCAN_REQUIRED = ("close", "pct_chg")


def _scan_row(day: str, r: dict) -> dict | None:
    """一行横截面。字段名 → 表列名，单位统一成"元 / 百分数"。"""
    symbol = str(r.get("code") or "").strip()
    if not symbol:
        return None
    return {"date": day, "symbol": symbol,
            "name": (str(r.get("name") or "").strip() or None),
            "close": _f(r.get("ClosePrice")),
            "pct_chg": _f(r.get("ChangePCT"))}


def _check_scan_fields(rows: list[dict], day: str) -> None:
    """整列全空 → 抛。**按列判，不按行判**：单只票缺某个值是正常的，
    整列都缺只可能是"表达式不再引用这个字段"，也就是上游根本没返回它。"""
    for col in _SCAN_REQUIRED:
        if all(r.get(col) is None for r in rows):
            raise SourceUnavailable(
                f"{day} 的横截面里 {col} 整列为空（{len(rows)} 只票）。"
                f"多半是 config.SCAN_EXPR 不再引用它对应的字段——"
                f"上游只返回表达式引用到的列")


# ---------------------------------------------------------------- profile

def _profile(codes: list[str], need: Need) -> Got:
    """`profile` 写的是 `stock` 表（元数据），不是某个域的表。**逐只**（无批量参数）。

    上游 `data_profile` 返回的字段（2026-11-14 实测 sz300026）：

        {"code","name","listedDate","business","website","industry","sector",
         "issuePrice","regCapital","establishDate","chairman","regAddress",...}

    这里只留 `stock` 表存得下的几个。**`sector` 和 `business` 是 v4 才补的**：
    重构 quality-screen 时发现报告第一节「生意本身」空着、行业退化成
    「暂无披露」——上游一直有 `business`（"药品和医用医疗器械的研发、生产和销售。"）
    和 `sector`，是共享层没存。加这两列的理由写在 `db._add_stock_columns`。
    """
    got = Got()
    for sym in codes:
        d = call("data_profile", {"code": sym})
        if not isinstance(d, dict) or not d.get("name"):
            got.empty_reason = got.empty_reason or "no_data"
            continue
        industry = (d.get("industry") or "").strip() or None
        got.rows[sym] = [{
            "symbol": sym,
            "name": d["name"],
            "exch": _exch(sym),
            "setcode": _setcode(sym),
            "industry": industry,
            # sector 只做 industry 的备用（下游 `profile.get("industry") or
            # profile.get("sector")`），上游两个值多数时候相同。
            "sector": (d.get("sector") or "").strip() or None,
            # 主营业务描述：长文本（几十到几百字），原样存，不裁剪。
            "business": (d.get("business") or "").strip() or None,
            "is_financial": 1 if _financial(industry, d["name"]) else 0,
            "list_date": _date(d.get("listedDate")),
        }]
    return got


def _financial(industry: str | None, name: str) -> bool:
    text = (industry or "") + (name or "")
    return any(w in text for w in FINANCIAL_WORDS)


def _exch(symbol: str) -> str:
    return {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(symbol[:2], "SZ")


def _setcode(symbol: str) -> int:
    """通达信市场码：1=沪 0=深 2=北。"""
    return {"sh": 1, "sz": 0, "bj": 2}.get(symbol[:2], 0)


# ---------------------------------------------------------------- snapshot

def _snapshot(codes: list[str], need: Need) -> Got:
    """`consensus` / `rating`。`mainop` 不在这个源（它是东财的 `RPT_F10_FN_MAINOP`）。"""
    which = _snapshot_domain(need)
    tool = {"consensus": "data_consensus", "rating": "data_rating"}.get(which)
    if tool is None:
        raise SourceUnavailable(
            f"westock 没有 {which or '(空)'} 这个 snapshot 子域；mainop 走 eastmoney",
            domain="snapshot")
    got = Got()
    for sym in codes:
        d = call(tool, {"code": sym})
        if not isinstance(d, dict) or not d:
            got.empty_reason = got.empty_reason or "no_data"
            continue
        got.rows[sym] = [{
            "symbol": sym, "domain": which, "as_of": need.end or "",
            "payload": json.dumps(d, ensure_ascii=False),
        }]
    return got


# ---------------------------------------------------------------- 工具

def _unwrap(d: dict | None) -> dict | None:
    """`data_finance` 多一层 `{code, msg, data}`。其余工具不需要这一层。

    判据是"有没有信封痕迹"（`msg` 或 `code`+`data`），**不是**"有没有 `data` 键"：
    上游报错时恰好**不**带 `data`（`{"code":500,"msg":"..."}`），
    按"有 data 才算信封"会把它当成正常返回，然后一路安静地变成"该股没数据"。
    """
    if not isinstance(d, dict):
        return None
    if "msg" in d or ("code" in d and "data" in d):
        if d.get("code") not in (0, None):
            raise SourceUnavailable(f"data_finance 上游报错：{d.get('msg')}")
        return d.get("data") or {}
    return d


def _date(v) -> str | None:
    """`20260626` / `2026-06-26` / `2026-06-26 00:00:00 +0800 CST` → `2026-06-26`。"""
    if v is None:
        return None
    s = str(v).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
