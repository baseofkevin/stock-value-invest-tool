"""股票代码归一化 —— 全项目唯一一份实现（README.md「代码归一化」）。

号段规则（与交易所一致）：
    920 / 4 / 8 开头  → 北交所（bj）
    6 / 9 开头        → 上交所（sh），含 688 科创板与 900 B 股
    其余 6 位数字     → 深交所（sz），含 000 / 002 / 300

支持输入形式：
    600519 / sh600519 / 600519.SH / sz000001 / 000001.SZ ...

设计要点：
  * **显式前缀优先**：`sz600519` 前缀与号段矛盾时，保留前缀、不按号段"聪明地"纠正。
    因为用户明确写了 sz，可能是刻意指定；猜错比照抄更糟。
  * 非法输入抛 ValueError，message 里带"应为"，供 CLI 拼错误提示。
"""

import re

#: 6 位纯数字
_NUM6 = re.compile(r"^\d{6}$")
#: 带前缀：sh600519 / bj920001
_PREFIXED = re.compile(r"^(sh|sz|bj)(\d{6})$", re.IGNORECASE)
#: 带后缀：600519.SH
_SUFFIXED = re.compile(r"^(\d{6})\.(sh|sz|bj)$", re.IGNORECASE)

#: 按号段推断交易所的规则：前缀字符串 -> 交易所
_MARKET_RULES = (
    (("920", "4", "8"), "bj"),
    (("6", "9"), "sh"),
)

VALID_MARKETS = ("sh", "sz", "bj")

CODE_FORMAT_HINT = "应为 6 位数字，或 sh600519 / 600519.SH 这类带前缀/后缀的写法"


def _market_by_number(six: str) -> str:
    """按号段推断交易所。6 位数字的输入已校验过。"""
    for prefixes, market in _MARKET_RULES:
        for p in prefixes:
            if six.startswith(p):
                return market
    return "sz"


def parse(raw) -> str:
    """解析任意输入形式，返回 6 位代码。

    >>> parse("600519")
    '600519'
    >>> parse("sh600519")
    '600519'
    >>> parse("600519.SH")
    '600519'

    非法输入抛 ValueError。
    """
    if raw is None:
        raise ValueError(f"股票代码不能为空，{CODE_FORMAT_HINT}")
    text = str(raw).strip()
    if not text:
        raise ValueError(f"股票代码不能为空，{CODE_FORMAT_HINT}")

    m = _PREFIXED.match(text)
    if m:
        return m.group(2)

    m = _SUFFIXED.match(text)
    if m:
        return m.group(1)

    if _NUM6.match(text):
        return text

    raise ValueError(f"无法识别的股票代码 {text!r}，{CODE_FORMAT_HINT}")


def market(raw) -> str:
    """返回交易所前缀 'sh' / 'sz' / 'bj'。

    显式前缀（sh/sz/bj）优先；只有纯数字或后缀形式才按号段推断。
    注意后缀形式 `600519.SZ` 也保留后缀指定的交易所 —— 与 `_SUFFIXED` 的语义一致。
    """
    if raw is None:
        raise ValueError(f"股票代码不能为空，{CODE_FORMAT_HINT}")
    text = str(raw).strip()

    m = _PREFIXED.match(text)
    if m:
        return m.group(1).lower()

    m = _SUFFIXED.match(text)
    if m:
        return m.group(2).lower()

    six = parse(text)
    return _market_by_number(six)


def secucode(raw) -> str:
    """返回 `600519.SH` 形式（大写后缀），接口与展示用。

    >>> secucode("600519")
    '600519.SH'
    """
    return f"{parse(raw)}.{market(raw).upper()}"


def with_prefix(raw) -> str:
    """返回 `sh600519` 形式 —— MCP 工具的 code 参数要这种写法。

    >>> with_prefix("600519")
    'sh600519'
    """
    return f"{market(raw)}{parse(raw)}"


def six(raw) -> str:
    """`six()` 的语义化别名：只取 6 位数字。"""
    return parse(raw)


def is_valid(raw) -> bool:
    """不抛异常的校验，供 CSV 批量时逐行判断。"""
    try:
        parse(raw)
        return True
    except (ValueError, TypeError):
        return False


def eastmoney_url(raw) -> str:
    """东方财富个股页 URL；代码非法时返回空串（不抛异常）。

    `#fullScreenChart` 让页面直接落到全屏 K 线上，少点一次。
    北交所是个坑：东方财富用的是 `quote.eastmoney.com/bj/920001.html`（带斜杠），
    不是 `bj920001.html`。沪深才是不带斜杠的 `sh600519.html`。
    拼错的 URL 不会报错，只会在点击后跳到 404 —— 所以这里有专门的测试盯着。

    >>> eastmoney_url("600519")
    'https://quote.eastmoney.com/sh600519.html#fullScreenChart'
    >>> eastmoney_url("920001")
    'https://quote.eastmoney.com/bj/920001.html#fullScreenChart'
    """
    try:
        digits = parse(raw)
        exchange = market(raw)
    except (ValueError, TypeError):
        return ""
    if exchange == "bj":
        return f"https://quote.eastmoney.com/bj/{digits}.html#fullScreenChart"
    return f"https://quote.eastmoney.com/{exchange}{digits}.html#fullScreenChart"
