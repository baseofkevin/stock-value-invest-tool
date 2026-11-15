"""通达信 MCP 适配器（`kline` 原材料）。

**这个源在本机当前不可用**，原因是鉴权而不是接口：`txmcp.tdx.com.cn:3001/txmcp`
要求一个 OAuth Bearer，而 WorkBuddy 的令牌握在宿主手里、不落盘（westock 的
`~/.config/westock/tokens.json` 是那条通道的特殊待遇）。实测：

    POST initialize → 401 {"error":"unauthorized",
                           "error_description":"Missing Bearer authorization..."}

所以本模块的 `AVAILABLE` 取决于是否配置了令牌：

    STOCKHUB_TDX_TOKEN     直接给令牌
    ~/.config/tdx/tokens.json   里的 accessToken（若将来那条通道也落了盘）

没有令牌时注册表会**过滤掉**它（`SOURCE.reason` 写明原因，`doctor` 原样打印），
router 也就不会白打三次 401。**不要**把它写成"没令牌就返回空表"——空表意味着
"这只股票没数据"，会把一个配置问题伪装成数据问题。

参数与解析是对着真实返回写的（2026-11-14 用 MCP 工具实采 600519）：

    Rows[0] = {"Data":"20261113", "Open":"1404.000000", ..., "Volume":41779.84,
               "RawVolume":4177984, "Unit":100, "Amount":5954833920}
    → Volume 是**手**（=RawVolume/100），RawVolume 才是**股**；Amount 是**元**。

`Volume × 100 == RawVolume` 这条恒等式在茅台/格力/比亚迪三只上都对得上，
换算是这个源独有的坑：库里的 `volume` 单位是**股**。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..errors import SourceUnavailable
from ..need import Need
from .base import Got
from .mcp import McpClient, read_token_file

log = logging.getLogger("stock_data_fetch.tdx")

URL = os.environ.get("STOCKHUB_TDX_URL") or "https://txmcp.tdx.com.cn:3001/txmcp"
TOKEN_FILE = Path(os.environ.get("STOCKHUB_TDX_TOKEN_FILE")
                  or Path.home() / ".config" / "tdx" / "tokens.json")

DAY = "4"          # period="4" 才是日线；默认是 "0"=5 分钟线，写错会拿到分钟数据
RAW = "0"          # tqFlag="0" 不复权。这是整个 kline 域的地基：只存 raw
MAX_BARS = 1000    # wantNum 上限

_client: McpClient | None = None


def token() -> str | None:
    return os.environ.get("STOCKHUB_TDX_TOKEN") or read_token_file(TOKEN_FILE)


def available() -> tuple[bool, str]:
    """(能不能用, 不能用的原因)。原因会进注册表的 `reason`，由 `doctor` 打印。"""
    if token():
        return True, ""
    return False, ("缺 OAuth 令牌（该端点 401）。设 STOCKHUB_TDX_TOKEN 后自动启用；"
                   "kline 现在走 westock，东财是第一备源")


def client() -> McpClient:
    global _client
    tok = token()
    if not tok:
        raise SourceUnavailable(available()[1])
    if _client is None or _client.token != tok:
        _client = McpClient(URL, token=tok, handshake=True, timeout=60.0)
    return _client


# ---------------------------------------------------------------- 代码映射

def split_symbol(symbol: str) -> tuple[str, int]:
    """`sh600519` → `("600519", 1)`。setcode：沪 1 / 深 0 / 北 2。

    与 `schema.stock.setcode` 同一套编码，不要在这里另立一套。
    """
    s = symbol.strip().lower()
    if s.startswith(("sh", "sz", "bj")):
        mkt, code = s[:2], s[2:]
    else:
        code = s
        mkt = "sh" if code[:1] in ("6", "5", "9") else ("bj" if code[:2] in ("43", "83", "87", "92") else "sz")
    if not code.isdigit():
        raise SourceUnavailable(f"代码格式不认识：{symbol}")
    return code, {"sh": 1, "sz": 0, "bj": 2}[mkt]


# ---------------------------------------------------------------- fetch

def fetch(domain: str, codes, need: Need) -> Got:
    """只做 `kline`。tdx 没有批量参数，只能逐只（`wantNum` 最多 1000 根）。"""
    if domain != "kline":
        raise SourceUnavailable(f"tdx 适配器没有 {domain} 域")
    got = Got()
    cli = client()
    for sym in codes:
        rows = _fetch_one(cli, sym, need)
        if rows:
            got.rows[sym] = rows
        else:
            got.empty_reason = got.empty_reason or "no_data"
    got.meta["endpoint"] = URL
    return got


def _fetch_one(cli: McpClient, symbol: str, need: Need) -> list[dict]:
    code, setcode = split_symbol(symbol)
    want = min(MAX_BARS, max(int(need.points or 300), 1))
    out: dict[str, dict] = {}
    startxh = 0
    for _ in range(8):                       # 分页上限，防止上游不认 startxh 时死循环
        data = cli.call("tdx_kline", {"code": code, "setcode": str(setcode), "period": DAY,
                                      "tqFlag": RAW, "wantNum": want, "startxh": startxh})
        if data is None:
            raise SourceUnavailable(f"{symbol} 取数失败：{cli.last_error or '无返回'}")
        rows = _rows(data)
        if not rows:
            break
        for r in rows:
            out[r["date"]] = r
        earliest = min(out)
        if need.start is None or earliest <= need.start or len(rows) < want:
            break
        startxh += len(rows)
    rows = sorted(out.values(), key=lambda r: r["date"])
    if need.start:
        rows = [r for r in rows if r["date"] >= need.start]
    if need.end:
        rows = [r for r in rows if r["date"] <= need.end]
    return rows


def _rows(data: dict) -> list[dict]:
    rows = data.get("Rows") or data.get("rows") or []
    out: list[dict] = []
    for x in rows:
        day = str(x.get("Data") or x.get("Date") or "")
        if len(day) != 8 or not day.isdigit():
            continue
        date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        vol = x.get("RawVolume")
        if vol is None:
            vol = _f(x.get("Volume")) * _f(x.get("Unit") or 100)
            vol = vol or 0.0
        out.append({"date": date,
                    "open": _f(x.get("Open")), "high": _f(x.get("High")),
                    "low": _f(x.get("Low")), "close": _f(x.get("Close")),
                    "volume": float(vol), "amount": _f(x.get("Amount"))})
    return out


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
