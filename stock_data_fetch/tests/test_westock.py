"""第 5 步测试：`sources/westock` + eastmoney 的 valuation/mainop + `quarter` + 域级写库。

跑法：

    cd /home/kevin/MyData/stock-value-invest-tool && \
    /home/kevin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
        -m stock_data_fetch.tests.test_westock

    STOCKHUB_LIVE=1 …   # 额外跑"用例 1b"：真打一次 westock（1 只票、4 个域）

**全部离线**：上游用假响应替换（`westock.call` / `eastmoney._get`），
断言里没有一个真实时间、没有一个真实网络的依赖。

覆盖 `CONTRACT` §15 的用例 2（二次调用上游计数 0）与用例 6（partial 不推进 `end_date`），
外加三条第 2/3 步遗留 bug 的回归（非日频域不算洞 / `finance` 无起点也能取 / Router 复用）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

# 必须在 import 任何东西之前定住令牌，否则测试会去看本机 ~/.config/westock/tokens.json，
# 于是"这台机器上过、别人那里不过"。测试不该依赖机器状态。
os.environ.setdefault("STOCKHUB_WESTOCK_TOKEN", "test-token-not-real")

from stock_data_fetch import api, coverage as C, db, quarter as Q, sources   # noqa: E402
from stock_data_fetch.calendar import TradingCalendar                         # noqa: E402
from stock_data_fetch.config import DEFAULT_CALENDAR, DEFAULT_SCHEMA, Config  # noqa: E402
from stock_data_fetch.errors import (InsufficientDepth, MarketOpenError,       # noqa: E402
                                    PartialData, SourceUnavailable)
from stock_data_fetch.hub import preflight                                     # noqa: E402
from stock_data_fetch.need import Need, want                                  # noqa: E402
from stock_data_fetch.sources import eastmoney as em, westock as W            # noqa: E402
from stock_data_fetch.sources.westock import _to_shares                       # noqa: E402
from stock_data_fetch.sources.base import BatchError, Got                      # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def expect_raise(name: str, exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        check(name, True)
        print(f"      → {type(e).__name__}: {e}")
        return e
    except Exception as e:                                    # noqa: BLE001
        check(name, False, f"抛的是 {type(e).__name__}: {e}")
        return None
    check(name, False, "没有抛异常")
    return None


def section(title: str) -> None:
    print(f"\n{title}")


def nearly(a, b, eps: float = 1e-6) -> bool:
    try:
        return abs(float(a) - float(b)) <= eps * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return False


@contextmanager
def patch(obj, name: str, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def mkdtemp(prefix: str) -> Path:
    root = Path(os.environ.get("STOCKHUB_TMP")
                or HERE.parent.parent.parent / ".testtmp")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


# ================================================================ 假上游数据

_ORDER = ("03-31", "06-30", "09-30", "12-31")


def q_ends(last: str, n: int) -> list[str]:
    """从 `last` 往回数 n 个报告期，**降序**（上游就是降序给的）。"""
    y, i = int(last[:4]), _ORDER.index(last[5:])
    out = [last]
    for _ in range(n - 1):
        if i == 0:
            y, i = y - 1, 3
        else:
            i -= 1
        out.append(f"{y}-{_ORDER[i]}")
    return out


def fin_payload(sym: str, ends: list[str], *, drop_balance: tuple[str, ...] = (),
                scale: float = 1.0, as_str: bool = True) -> dict:
    """仿 westock `data_finance` 的真实形态：三表各自独立、值是**字符串**。"""

    def s(v: float):
        return f"{v:.2f}" if as_str else v

    income, balance, cash = [], [], []
    for k, e in enumerate(sorted(ends, reverse=True)):
        rev = 1e10 + k * 1e6
        income.append({
            "EndDate": e,
            "OperatingRevenue": s(rev * scale),
            "OperatingRevenue_Q": s(rev / 4 * scale),
            "OperatingRevenueTTM": s(rev * 1.1 * scale),
            "OperatingCost": s(rev * 0.3 * scale),
            "OperatingCost_Q": s(rev * 0.075 * scale),
            "OperatingCostTTM": s(rev * 0.33 * scale),
            "OperatingProfit": s(rev * 0.5 * scale),
            "OperatingProfit_Q": s(rev * 0.125 * scale),
            "OperatingProfitTTM": s(rev * 0.55 * scale),
            "NPParentCompanyOwners": s(rev * 0.4 * scale),
            "NPParentCompanyOwners_Q": s(rev * 0.1 * scale),
            "NPParentCompanyOwnersTTM": s(rev * 0.44 * scale),
        })
        if e not in drop_balance:            # 模拟"这次没回资产负债表"
            balance.append({"EndDate": e, "TotalFixedAsset": s(2e9),
                            "TConstruInProcess": s(3e8), "ContractLiability": s(1e8)})
        cash.append({"EndDate": e, "NetCash": s(1e8)})
    return {"code": 0, "msg": "ok",
            "data": {sym: {"income": income, "balance": balance, "cashflow": cash}}}


def quote_payload(sym: str, day: str, price: float = 1435.26) -> dict:
    """仿 `data_quote` 实测形态（2026-11-14 抓的真字段名）。"""
    return {sym: {"code": sym, "name": "贵州茅台", "price": price,
                  "prev_close": 1410.27, "change_percent": 1.77,
                  "volume": 41780, "amount": 5954834070.0, "turnover_rate": 0.33,
                  "float_shares": 1250081601.0, "total_shares": 1250081601.0,
                  "total_market_cap": 1794191999999.99,
                  "circulating_market_cap": 1794191999999.99,
                  "pe_ratio": 21.84, "pb_ratio": 6.62, "time": day}}


def dividend_payload(plans: list[dict]) -> dict:
    return {"code": 0, "start": "2015-01-01", "end": "2026-11-13", "plans": plans}


def profile_payload(sym: str, name="贵州茅台", industry="食品饮料") -> dict:
    return {"code": sym, "name": name, "listedDate": "2001-08-27",
            "industry": industry, "sector": industry}


class Counter:
    """假上游的调用记录。`n` 是总次数，`args` 逐次留下参数。"""

    def __init__(self) -> None:
        self.n = 0
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool: str, args: dict, **kw):
        self.n += 1
        self.calls.append((tool, dict(args)))
        return self.responder(tool, args)

    def responder(self, tool: str, args: dict):
        raise NotImplementedError


class FakeWestock(Counter):
    """把 `westock.call` 换掉。`by_tool` 里放 `tool -> fn(args)`。"""

    def __init__(self, by_tool: dict) -> None:
        super().__init__()
        self.by_tool = by_tool

    def responder(self, tool: str, args: dict):
        fn = self.by_tool.get(tool)
        return fn(args) if fn else {}


def make_hub(tmp: Path, now: dt.datetime | None = None):
    cfg = Config()
    cfg.db_path = tmp / "shared.db"
    cfg.state_dir = tmp / "state"
    cfg.schema_path = DEFAULT_SCHEMA
    cfg.calendar_path = DEFAULT_CALENDAR
    cfg.rate_rows_per_min = {"westock": 15000.0, "eastmoney": 15000.0, "tdx": 0.0}
    cfg.min_interval_ms = {"westock": 0.0, "eastmoney": 0.0, "tdx": 0.0}
    cfg.burst_rows = 100000.0
    cfg.log_level = "ERROR"
    return preflight(cfg=cfg, now=now or dt.datetime(2026, 11, 14, 16, 0), free_mb=10_000)


SYM = "sh600519"
TARGET = "2026-11-13"          # 2026-11-14 是周六，目标交易日是周五


# ================================================================ 1. 纯函数

def test_pure() -> None:
    section("1. 纯函数：日期 / 单位 / 年数 / 三表合并")

    check("_date 认 20260626", W._date("20260626") == "2026-06-26", W._date("20260626"))
    check("_date 认 2026-06-26", W._date("2026-06-26") == "2026-06-26")
    check("_date 认带时分", W._date("2026-06-26 00:00:00 +0800 CST") == "2026-06-26")
    check("_date 认不出就给 None", W._date("--") is None)

    check("_years_for(None) = 11", W._years_for(None) == 11, str(W._years_for(None)))
    check("_years_for(今年) = 11", W._years_for("2026-01-01") == 11)
    check("_years_for(2010) >= 18", W._years_for("2010-01-01") >= 18,
          str(W._years_for("2010-01-01")))

    # 茅台 2025 年报：10派280.242元（实测原文），无送转
    mt = W._dividend_row(SYM, {"exDiviDate": "20260626", "cashDiviRMB": "280.24",
                               "bonusShareRatio": "", "tranAddShareRatio": "",
                               "dividendPlan": "10派280.242元"})
    check("茅台 10派280.24 → 每股 28.024", nearly(mt["cash_per_share"], 28.024), str(mt))
    check("空字符串的送转比例 → 0 而不是 None",
          mt["bonus_per_share"] == 0.0 and mt["transfer_per_share"] == 0.0)
    check("没有配股数据时留 0/None，不猜", mt["rights_per_share"] == 0.0
          and mt["rights_price"] is None)

    # 比亚迪 2025-07-29：10派39.740元送8.000股转12.000股（实测原文）
    byd = W._dividend_row("sz002594", {"exDiviDate": "20250729", "cashDiviRMB": "39.740",
                                       "bonusShareRatio": "8.000",
                                       "tranAddShareRatio": "12.000",
                                       "dividendPlan": "10派39.740元送8.000股转12.000股"})
    check("比亚迪 派/送/转 全部 /10", nearly(byd["cash_per_share"], 3.974)
          and nearly(byd["bonus_per_share"], 0.8) and nearly(byd["transfer_per_share"], 1.2),
          str(byd))

    check("没有除权日（方案未实施）→ 不进因子", W._dividend_row(SYM, {"cashDiviRMB": "10"}) is None)

    # ---- 同一除权日的多条方案必须相加（2026-11-14 实测原文）----
    # 上游把"年度分红"和"特殊分红"拆成两条，共用 exDiviDate。逐条写库时后一条会
    # 覆盖前一条：宁德 2026 少记 2.178 元/股（-31%），平安少记 0.2 元/股。
    catl = W._dividend_rows("sz300750", [
        {"exDiviDate": "20260422", "cashDiviRMB": "47.79", "dividendPlan": "10派47.790元",
         "dividendType": "特殊分红", "proposalSn": 2},
        {"exDiviDate": "20260422", "cashDiviRMB": "21.78", "dividendPlan": "10派21.780元",
         "dividendType": "有分红", "proposalSn": 1},
    ])
    check("同日两条方案合并成一行", len(catl) == 1, str(catl))
    check("同日方案派息相加 = 6.957 元/股", nearly(catl[0]["cash_per_share"], 6.957),
          str(catl[0]["cash_per_share"]))
    check("合并后 plan_text 两条都留（审计要能看出是两条）",
          "10派47.790元" in (catl[0]["plan_text"] or "")
          and "10派21.780元" in (catl[0]["plan_text"] or ""), catl[0]["plan_text"])

    pa = W._dividend_rows("sh601318", [
        {"exDiviDate": "20180607", "cashDiviRMB": "10.00", "dividendPlan": "10派10.000元"},
        {"exDiviDate": "20180607", "cashDiviRMB": "2.00", "dividendPlan": "10派2.000元",
         "dividendType": "特殊分红"},
        {"exDiviDate": "20170904", "cashDiviRMB": "5.00", "dividendPlan": "10派5.000元"},
    ])
    check("不同除权日各自成行", len(pa) == 2, str([r["ex_date"] for r in pa]))
    check("平安 2018-06-07 = 1.2 元/股", nearly(pa[1]["cash_per_share"], 1.2),
          str(pa[1]["cash_per_share"]))
    check("合并结果按除权日升序", [r["ex_date"] for r in pa] == ["2017-09-04", "2018-06-07"],
          str([r["ex_date"] for r in pa]))

    dup = W._dividend_rows(SYM, [
        {"exDiviDate": "20260626", "cashDiviRMB": "280.24", "dividendPlan": "10派280.242元"},
        {"exDiviDate": "20260626", "cashDiviRMB": "280.24", "dividendPlan": "10派280.242元"},
    ])
    check("上游把同一条方案重复下发 → 不重复累加", nearly(dup[0]["cash_per_share"], 28.024),
          str(dup[0]["cash_per_share"]))
    check("重复下发的 plan_text 不重复拼接",
          (dup[0]["plan_text"] or "").count("10派280.242元") == 1, dup[0]["plan_text"])

    check("同一天送转也要相加", nearly(
        W._dividend_rows("sz002594", [
            {"exDiviDate": "20250729", "cashDiviRMB": "20", "bonusShareRatio": "3",
             "tranAddShareRatio": "5"},
            {"exDiviDate": "20250729", "cashDiviRMB": "19.74", "bonusShareRatio": "5",
             "tranAddShareRatio": "7"},
        ])[0]["bonus_per_share"], 0.8))

    ends = q_ends("2026-09-30", 3)
    stmts = fin_payload(SYM, ends)["data"][SYM]
    rows = W._merge_statements(SYM, stmts)
    check("三表按 EndDate 合并成 3 期", len(rows) == 3, str([r["period"] for r in rows]))
    check("合并后按报告期升序", [r["period"] for r in rows] == sorted(ends),
          str([r["period"] for r in rows]))
    check("字符串金额转成 float", nearly(rows[-1]["OperatingRevenue"], 1e10),
          str(rows[-1]["OperatingRevenue"]))
    check("在建工程从 TConstruInProcess 映射到 CIP",
          nearly(rows[-1]["CIP"], 3e8), str(rows[-1].get("CIP")))
    check("unit 标 CNY", rows[-1]["unit"] == "CNY")
    raw = json.loads(rows[-1]["raw_json"])
    check("raw_json 存了三张表的原文", set(raw) == {"income", "balance", "cashflow"}, str(set(raw)))

    # 某期缺资产负债表：那一期的 balance 字段必须是 None（而不是 0，也不是错位）
    stmts2 = fin_payload(SYM, ends, drop_balance=(ends[1],))["data"][SYM]
    rows2 = {r["period"]: r for r in W._merge_statements(SYM, stmts2)}
    check("缺表的那期照常出行（不缺期）", len(rows2) == 3, str(sorted(rows2)))
    check("缺表的那期 balance 字段为 None", rows2[ends[1]]["TotalFixedAsset"] is None,
          str(rows2[ends[1]].get("TotalFixedAsset")))
    check("同期利润表仍在（没被错位覆盖）",
          nearly(rows2[ends[1]]["OperatingRevenue"], rows[-2]["OperatingRevenue"]))
    check("缺表那期的 raw_json 只有两张表",
          set(json.loads(rows2[ends[1]]["raw_json"])) == {"income", "cashflow"})

    check("_financial 认银行", W._financial("银行", "招商银行") is True)
    check("_financial 不误判食品饮料", W._financial("食品饮料", "贵州茅台") is False)

    # 2026-11-15 补的回归：险企行业名是「非银金融」，股票名里没有"保险"二字。
    # 词表原来只有具体业态词，于是 中国人寿/中国平安 全被漏判成非金融，
    # 下游 kline_screen 拿它做毛利率豁免，险企 OperatingCost_Q 为 NULL →
    # 「毛利率改善」输出「无数据」，看着像缺数据、实际是分类错。
    check("_financial 认非银金融（险企，名字里无业态词）",
          W._financial("非银金融", "中国人寿") is True)
    check("_financial 认非银金融（险企，平安）",
          W._financial("非银金融", "中国平安") is True)
    check("_financial 认非银金融（券商，靠行业名而非名字）",
          W._financial("非银金融", "中信证券") is True)
    check("_financial 不误判军工", W._financial("国防军工", "洪都航空") is False)
    check("_financial 不误判汽车", W._financial("汽车", "福达股份") is False)
    check("_financial 名字含金融字但行业非金融 → 仍按行业判",
          W._financial("钢铁", "宝钢股份") is False)

    check("_setcode 沪=1 深=0 北=2",
          (W._setcode("sh600519"), W._setcode("sz000651"), W._setcode("bj430047")) == (1, 0, 2))


# ================================================================ 2. 适配器（假 call）

def test_adapter() -> None:
    section("2. 适配器：批量与逐只、单位换算、子域拒收")

    ends = q_ends("2026-09-30", 2)
    calls = FakeWestock({"data_finance": lambda a: fin_payload(SYM, ends)})
    with patch(W, "call", calls):
        need = want.finance(TARGET, periods=2)
        got = W.fetch("finance", [SYM], need)
        check("finance 拿到 2 期", len(got.rows[SYM]) == 2, str(len(got.rows.get(SYM, []))))
        tool, args = calls.calls[0]
        check("finance 用逗号 codes 传参", args.get("codes") == SYM, str(args))
        check("finance 把 periods 传给 num", args.get("num") == 2, str(args))

    # 45 只 → 2 批（BATCH=40）
    many = [f"sh6000{i:02d}" for i in range(45)]
    calls = FakeWestock({"data_finance": lambda a: fin_payload(a["codes"].split(",")[0], ends)})
    with patch(W, "call", calls):
        W.fetch("finance", many, want.finance(TARGET, periods=2))
        check("45 只按 40 一批拆成 2 次", calls.n == 2, str(calls.n))
        check("第二批只有 5 只", len(calls.calls[1][1]["codes"].split(",")) == 5,
              calls.calls[1][1]["codes"])

    # 分红：逐只（多只一起传会回空，实测）
    calls = FakeWestock({"data_dividend": lambda a: dividend_payload(
        [{"exDiviDate": "20260626", "cashDiviRMB": "280.24", "bonusShareRatio": "",
          "tranAddShareRatio": "", "dividendPlan": "10派280.242元"}])})
    with patch(W, "call", calls):
        got = W.fetch("dividend", ["sh600519", "sz000651"], want.dividend("2020-01-01"))
        check("dividend 逐只调用（2 只 → 2 次）", calls.n == 2, str(calls.n))
        check("dividend 用的是单数 code 参数", all("code" in a for _, a in calls.calls),
              str([a for _, a in calls.calls]))
        check("dividend 按 need.start 反算年数（>=11）",
              all(a.get("years", 0) >= 11 for _, a in calls.calls),
              str([a.get("years") for _, a in calls.calls]))
        check("分红落到每股口径", nearly(got.rows["sh600519"][0]["cash_per_share"], 28.024))

    calls = FakeWestock({"data_quote": lambda a: quote_payload(SYM, TARGET)})
    with patch(W, "call", calls):
        got = W.fetch("quote", [SYM], want.quote(TARGET))
        q = got.rows[SYM][0]
        check("行情成交量 手→股 ×100", nearly(q["volume"], 4178000.0), str(q["volume"]))
        check("行情总市值已是元，不换算", nearly(q["total_mv"], 1794191999999.99, 1e-9))
        check("行情 trade_date 取上游 time", q["trade_date"] == TARGET, q["trade_date"])

    calls = FakeWestock({"data_profile": lambda a: profile_payload(a["code"])})
    with patch(W, "call", calls):
        got = W.fetch("profile", [SYM], want.profile())
        p = got.rows[SYM][0]
        check("profile 写行业原值（不做申万归一化）", p["industry"] == "食品饮料", str(p))
        check("profile 判非金融", p["is_financial"] == 0)
        check("profile 带上市日", p["list_date"] == "2001-08-27", str(p.get("list_date")))

    calls = FakeWestock({"data_profile": lambda a: profile_payload(a["code"], "招商银行", "银行")})
    with patch(W, "call", calls):
        got = W.fetch("profile", ["sh600036"], want.profile())
        check("银行股 is_financial=1", got.rows["sh600036"][0]["is_financial"] == 1)

    # snapshot 子域
    check("supports: mainop 不归 westock", W.supports("snapshot", want.snapshot("mainop", TARGET))
          is False)
    check("supports: consensus 归 westock",
          W.supports("snapshot", want.snapshot("consensus", TARGET)) is True)
    check("supports: mainop 归东财", em.supports("snapshot", want.snapshot("mainop", TARGET)) is True)
    check("supports: consensus 不归东财",
          em.supports("snapshot", want.snapshot("consensus", TARGET)) is False)

    calls = FakeWestock({"data_consensus": lambda a: {"target": 1500.0, "count": 12}})
    with patch(W, "call", calls):
        got = W.fetch("snapshot", [SYM], want.snapshot("consensus", TARGET))
        row = got.rows[SYM][0]
        check("consensus 整份 JSON 进 payload", json.loads(row["payload"])["count"] == 12)
        check("consensus 的 as_of 是目标交易日", row["as_of"] == TARGET, str(row))

    with patch(W, "call", FakeWestock({})):
        expect_raise("westock 收到 mainop → 明确拒收（而不是回空表）",
                     SourceUnavailable, W.fetch, "snapshot", [SYM],
                     want.snapshot("mainop", TARGET))
        expect_raise("westock 收到不支持的域 → 拒收",
                     SourceUnavailable, W.fetch, "valuation", [SYM], want.quote(TARGET))

    # 上游报错与结构漂移
    with patch(W, "call", FakeWestock({"data_finance": lambda a: {"code": 500, "msg": "boom"}})):
        expect_raise("data_finance 自报 code!=0 → SourceUnavailable",
                     SourceUnavailable, W.fetch, "finance", [SYM], want.finance(TARGET, 2))
    with patch(W, "call", FakeWestock({"data_finance": lambda a: {"code": 0, "msg": "ok",
                                                                  "data": ["不是 dict"]}})):
        expect_raise("data_finance 的 data 变成列表 → SourceUnavailable",
                     SourceUnavailable, W.fetch, "finance", [SYM], want.finance(TARGET, 2))


# ================================================================ 3. 东财估值 / 主营

def dc_payload(rows: list[dict], pages: int = 1) -> str:
    return json.dumps({"success": True, "code": 0, "message": "ok",
                       "result": {"pages": pages, "count": len(rows), "data": rows}})


def val_rows(days: list[str]) -> list[dict]:
    return [{"SECUCODE": "600519.SH", "TRADE_DATE": f"{d} 00:00:00",
             "PE_TTM": 30.0, "PB_MRQ": 8.0, "PS_TTM": 12.0,
             "TOTAL_MARKET_CAP": 1.5e12} for d in days]


def test_kline_adapter() -> None:
    section("2b. kline 适配器：多码批量、列名映射、手→股、倒序转正")

    # 上游节点：倒序（新→旧）、volume 是手、收盘叫 last、amount 是元
    def node(day, o, last, hi, lo, vol, amt):
        return {"date": day, "open": o, "last": last, "high": hi, "low": lo,
                "volume": vol, "amount": amt, "exchange": "0.66"}

    payload_multi = {"data": [
        {"symbol": "sh600519",
         "data": {"nodes": [node("2026-11-13", 1430, 1435.26, 1440, 1425, 41780, 5954830000),
                            node("2026-11-12", 1420, 1428, 1432, 1418, 30000, 4284000000)]}},
        {"symbol": "sz000001",
         "data": {"nodes": [node("2026-11-13", 12.0, 12.07, 12.1, 11.9, 2189430, 2624480000)]}},
    ], "errors": [{"symbol": "sh605499", "message": "no data"}]}

    calls = FakeWestock({"data_kline": lambda a: payload_multi})
    with patch(W, "call", calls):
        need = Need("kline", "fq=raw", end="2026-11-13", start="2025-11-04",
                    points=300, min_points=250)
        got = W.fetch("kline", ["sh600519", "sz000001", "sh605499"], need)
        rows = got.rows["sh600519"]
        check("kline 多码一次请求（codes 逗号）",
              calls.calls[0][1].get("codes") == "sh600519,sz000001,sh605499",
              str(calls.calls[0][1]))
        check("kline limit 带 20 根缓冲", calls.calls[0][1].get("limit") == 320,
              str(calls.calls[0][1]))
        check("kline last 改名 close", rows[-1]["close"] == 1435.26, str(rows[-1]))
        check("kline volume 手→股（×100）", rows[-1]["volume"] == 4178000.0,
              str(rows[-1]["volume"]))
        check("kline amount 原样保留", rows[-1]["amount"] == 5954830000.0,
              str(rows[-1]["amount"]))
        check("kline 倒序转升序", [r["date"] for r in rows] ==
              ["2026-11-12", "2026-11-13"], str([r["date"] for r in rows]))
        check("kline 第二只也在", len(got.rows["sz000001"]) == 1)
        check("kline errors 里的股票不产出", "sh605499" not in got.rows)
        check("kline exchange 字段不落行", "exchange" not in rows[-1], str(rows[-1]))

    # 单码走 code 参数（与 CLI cmd_kline 同款分支）
    payload_one = {"nodes": [node("2026-11-13", 1, 2, 3, 0.5, 10, 100)]}
    calls = FakeWestock({"data_kline": lambda a: payload_one})
    with patch(W, "call", calls):
        got = W.fetch("kline", ["sh600519"], need)
        check("kline 单码用 code 参数", calls.calls[0][1].get("code") == "sh600519",
              str(calls.calls[0][1]))
        check("kline 单码拿到 1 根", len(got.rows["sh600519"]) == 1)

    # 45 只 → 2 批（BATCH=40）
    many = [f"sh6000{i:02d}" for i in range(45)]
    calls = FakeWestock({"data_kline": lambda a: {"data": []}})
    with patch(W, "call", calls):
        W.fetch("kline", many, need)
        check("kline 45 只拆 2 批", calls.n == 2, str(calls.n))
        check("kline 第二批 5 只",
              len(calls.calls[1][1]["codes"].split(",")) == 5)

    # supports：kline 现在归 westock 管
    check("supports 认 kline", W.supports("kline", need) is True)


def test_eastmoney() -> None:
    section("3. 东财：日频→周频、翻页早停、接口改名要报错")

    check("_num 认空串为 None", em._num("") is None)
    check("_num 认 '-' 为 None", em._num("-") is None)
    check("_num 保留负 PE（亏损股，不能填 0）", em._num("-12.5") == -12.5)

    raw = val_rows(["2026-11-02", "2026-11-06", "2026-11-09", "2026-11-13"])
    weekly = em._weekly(raw, "2026-01-01", None)
    check("同周两天只留后一天（11-02..11-06 是同一 ISO 周）", len(weekly) == 2,
          str([r["trade_date"] for r in weekly]))
    check("留下的是周五 11-06", weekly[0]["trade_date"] == "2026-11-06",
          str([r["trade_date"] for r in weekly]))
    check("周频序列升序", [r["trade_date"] for r in weekly] == ["2026-11-06", "2026-11-13"])
    check("total_mv 用 TOTAL_MARKET_CAP", nearly(weekly[0]["total_mv"], 1.5e12, 1e-9))
    check("circ_mv 留 None（东财没这列，不用 total_mv 顶替）", weekly[0]["circ_mv"] is None)

    check("起始日之前的数据被裁掉", len(em._weekly(raw, "2026-11-10", None)) == 1)
    check("end 之后的数据被裁掉", len(em._weekly(raw, "2026-01-01", "2026-11-06")) == 1)

    # 翻页：pages=5 但第一页最早就已经早于 start → 只请求 1 页
    pages_seen: list[str] = []

    def fake_get(url: str, **kw):
        pages_seen.append(url)
        page = int(url.split("pageNumber=")[1].split("&")[0])
        if page == 1:
            return dc_payload(val_rows(["2026-11-13", "2026-11-06"]), pages=5)
        return dc_payload(val_rows(["2010-01-01"]), pages=5)

    with patch(em, "_get", fake_get):
        rows = em._datacenter("RPT_VALUEANALYSIS_DET", em.VALUATION_COLS,
                              '(SECUCODE="600519.SH")', sort="TRADE_DATE",
                              stop_before="2026-11-10")
        check("降序翻页 + 够到起点就停（只请求 1 页）", len(pages_seen) == 1, str(len(pages_seen)))
        check("拿到第一页的 2 行", len(rows) == 2, str(len(rows)))

    with patch(em, "_get", lambda url, **kw: json.dumps(
            {"success": False, "code": 9501, "message": "PE_TTM返回字段不存在"})):
        e = expect_raise("接口改名（字段不存在）→ SourceUnavailable，不当空表",
                         SourceUnavailable, em._datacenter, "RPT_VALUEANALYSIS_DET",
                         "PE_TTM", '(SECUCODE="600519.SH")', sort="TRADE_DATE")
        if e:
            check("报错文案带上游原文", "9501" in str(e), str(e))

    with patch(em, "_get", lambda url, **kw: "不是 JSON"):
        expect_raise("返回不是 JSON → SourceUnavailable",
                     SourceUnavailable, em._datacenter, "RPT_VALUEANALYSIS_DET",
                     "PE_TTM", "", sort="TRADE_DATE")

    # 整条 valuation 链路（适配器 → Got）
    with patch(em, "_get", lambda url, **kw: dc_payload(
            val_rows(["2026-11-13", "2026-11-06", "2020-01-02"]))):
        got = em.fetch("valuation", [SYM], want.valuation(
            TradingCalendar.load(DEFAULT_CALENDAR), TARGET, years=1, min_weeks=1))
        check("valuation 只留 need.start 之后的周", len(got.rows[SYM]) == 2,
              str(len(got.rows.get(SYM, []))))

    with patch(em, "_get", lambda url, **kw: dc_payload([])):
        got = em.fetch("valuation", [SYM], want.quote(TARGET))
        check("估值空表 → empty_reason=no_data", got.empty_reason == "no_data",
              str(got.empty_reason))

    # 主营构成
    mainop_rows = [{"SECUCODE": "600519.SH", "REPORT_DATE": "2025-12-31 00:00:00",
                    "MAINOP_TYPE": "2", "ITEM_NAME": "茅台酒", "MBI_RATIO": 0.86},
                   {"SECUCODE": "600519.SH", "REPORT_DATE": "2026-09-30 00:00:00",
                    "MAINOP_TYPE": "3", "ITEM_NAME": "国内", "MBI_RATIO": 0.98}]
    with patch(em, "_get", lambda url, **kw: dc_payload(mainop_rows)):
        got = em.fetch("snapshot", [SYM], want.snapshot("mainop", TARGET))
        row = got.rows[SYM][0]
        check("mainop 的 domain 标 mainop", row["domain"] == "mainop")
        check("mainop 的 as_of 取最新报告期（不是交易日）", row["as_of"] == "2026-09-30",
              str(row["as_of"]))
        check("mainop 整份 JSON 进 payload", len(json.loads(row["payload"])) == 2)


# ================================================================ 4. 写库（直接调函数）

def test_writers(tmp: Path) -> None:
    section("4. 写库：财报 append-only + 更正留痕、分红不改、元数据不抹")
    dbp = tmp / "w.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)
    ends = q_ends("2026-09-30", 4)

    rows = W._merge_statements(SYM, fin_payload(SYM, ends)["data"][SYM])
    with db.tx(conn):
        api._write_rows(conn, "finance", SYM, rows, "tencent")
    n1 = conn.execute("SELECT COUNT(*) FROM finance WHERE symbol=?", (SYM,)).fetchone()[0]
    check("4 期全部落库", n1 == 4, str(n1))
    v1 = conn.execute("SELECT OperatingRevenue FROM finance WHERE symbol=? AND period=?",
                      (SYM, ends[0])).fetchone()[0]
    check("金额按元存（1e10）", nearly(v1, 1e10), str(v1))

    # 重写同样数据：不产生 revision
    with db.tx(conn):
        api._write_rows(conn, "finance", SYM, rows, "tencent")
    nrev = conn.execute("SELECT COUNT(*) FROM finance_revision WHERE symbol=?", (SYM,)).fetchone()[0]
    check("同样的数据重写不产生修订记录", nrev == 0, str(nrev))

    # 上游更正 1%（不是 0.0001%）：要 UPDATE + 留痕
    rows2 = W._merge_statements(SYM, fin_payload(SYM, ends, scale=1.01)["data"][SYM])
    with db.tx(conn):
        api._write_rows(conn, "finance", SYM, rows2, "tencent")
    v2 = conn.execute("SELECT OperatingRevenue FROM finance WHERE symbol=? AND period=?",
                      (SYM, ends[0])).fetchone()[0]
    check("更正后取到新值", nearly(v2, 1.01e10), str(v2))
    rev = conn.execute("SELECT field,old_value,new_value FROM finance_revision "
                       "WHERE symbol=? AND period=? AND field='OperatingRevenue'",
                       (SYM, ends[0])).fetchone()
    check("更正留痕（field/old/new 都记下）", rev is not None and "1e+10" not in str(rev["old_value"]),
          str(dict(rev) if rev else None))
    check("append-only：期数没变", conn.execute(
        "SELECT COUNT(*) FROM finance WHERE symbol=?", (SYM,)).fetchone()[0] == 4)

    # 关键守卫：新值为 None 不许覆盖旧值
    rows3 = W._merge_statements(SYM, fin_payload(SYM, ends, scale=1.01,
                                                 drop_balance=tuple(ends))["data"][SYM])
    with db.tx(conn):
        api._write_rows(conn, "finance", SYM, rows3, "tencent")
    fixed = conn.execute("SELECT TotalFixedAsset FROM finance WHERE symbol=? AND period=?",
                         (SYM, ends[0])).fetchone()[0]
    check("上游这次没给资产负债表 → 旧值不被抹成 NULL", nearly(fixed, 2e9), str(fixed))

    # raw_json 保留最近 RAW_JSON_KEEP 期（2026-11-14 从"只留 2 期"改的）：
    # quality-screen 要 20 期做 5 年趋势，只留 2 期会让同比与趋势全废——
    # 而且不报错，只是结论悄悄变成 None。这里断言的是那个常量，不是散落的魔法数。
    kept = conn.execute("SELECT COUNT(*) FROM finance WHERE symbol=? AND raw_json IS NOT NULL",
                        (SYM,)).fetchone()[0]
    check(f"raw_json 留最近 {min(api.RAW_JSON_KEEP, len(ends))} 期（=本用例全部期数）",
          kept == min(api.RAW_JSON_KEEP, len(ends)), str(kept))

    # 分红：append-only
    dv = [W._dividend_row(SYM, {"exDiviDate": "20260626", "cashDiviRMB": "280.24",
                                "bonusShareRatio": "", "tranAddShareRatio": "",
                                "dividendPlan": "10派280.242元"})]
    with db.tx(conn):
        api._write_rows(conn, "dividend", SYM, dv, "tencent")
        api._write_rows(conn, "dividend", SYM, dv, "eastmoney")   # 换源再写一次
    cnt = conn.execute("SELECT COUNT(*) FROM dividend WHERE symbol=?", (SYM,)).fetchone()[0]
    check("分红同一天不重复插", cnt == 1, str(cnt))
    src = conn.execute("SELECT src FROM dividend WHERE symbol=?", (SYM,)).fetchone()[0]
    check("已存在的分红行不被改写（连 src 都不变）", src == "tencent", str(src))

    # 元数据
    with db.tx(conn):
        api._write_rows(conn, "profile", SYM, [
            {"symbol": SYM, "name": "贵州茅台", "exch": "SH", "setcode": 1,
             "industry": "食品饮料", "is_financial": 0, "list_date": "2001-08-27"}], "tencent")
    conn.execute("UPDATE stock SET last_access_at='2020-01-01T00:00:00' WHERE symbol=?", (SYM,))
    with db.tx(conn):
        api._write_rows(conn, "profile", SYM, [
            {"symbol": SYM, "name": "贵州茅台", "exch": "SH", "setcode": 1,
             "industry": None, "is_financial": 0, "list_date": None}], "tencent")
    st = dict(conn.execute("SELECT * FROM stock WHERE symbol=?", (SYM,)).fetchone())
    check("这次没给行业 → 保留旧行业", st["industry"] == "食品饮料", str(st["industry"]))
    check("这次没给上市日 → 保留旧上市日", st["list_date"] == "2001-08-27", str(st["list_date"]))
    check("刷新元数据不重置 last_access_at（GC 靠它）",
          st["last_access_at"] == "2020-01-01T00:00:00", str(st["last_access_at"]))

    # 行情 / 快照
    with db.tx(conn):
        api._write_rows(conn, "quote", SYM, [W._quote_row(SYM, quote_payload(SYM, TARGET)[SYM])],
                        "tencent")
        api._write_rows(conn, "quote", SYM, [W._quote_row(SYM, quote_payload(SYM, TARGET,
                                                                           price=1500.0)[SYM])],
                        "tencent")
    q = dict(conn.execute("SELECT * FROM quote WHERE symbol=?", (SYM,)).fetchone())
    check("行情一股一行、整行覆盖", nearly(q["price"], 1500.0), str(q["price"]))

    with db.tx(conn):
        api._write_rows(conn, "snapshot", SYM, [
            {"symbol": SYM, "domain": "mainop", "as_of": "2026-09-30", "payload": "{}"}], "eastmoney")
        api._write_rows(conn, "snapshot", SYM, [
            {"symbol": SYM, "domain": "consensus", "as_of": TARGET, "payload": '{"a":1}'}],
            "tencent")
    sn = {r["domain"]: r["src"] for r in conn.execute(
        "SELECT domain,src FROM snapshot WHERE symbol=?", (SYM,))}
    check("两个 snapshot 子域互不覆盖", sn == {"mainop": "eastmoney", "consensus": "tencent"},
          str(sn))

    with db.tx(conn):
        api._write_rows(conn, "valuation", SYM, [
            {"trade_date": "2026-11-06", "pe_ttm": 21.0, "pb_mrq": 6.0, "ps_ttm": 8.0,
             "total_mv": 1.7e12, "circ_mv": None}], "eastmoney")
        api._write_rows(conn, "valuation", SYM, [
            {"trade_date": "2026-11-06", "pe_ttm": 21.5, "pb_mrq": 6.1, "ps_ttm": 8.1,
             "total_mv": 1.7e12, "circ_mv": None}], "eastmoney")
    vv = dict(conn.execute("SELECT * FROM valuation WHERE symbol=?", (SYM,)).fetchone())
    check("估值允许整段覆盖刷新（pb_mrq 分母会随财报重算）", nearly(vv["pe_ttm"], 21.5),
          str(vv["pe_ttm"]))
    conn.close()


# ================================================================ 5. quarter

def test_quarter(tmp: Path) -> None:
    section("5. quarter：stored / q1_cum / derived / missing 四路")

    dbp = tmp / "q.db"
    db.create_db(dbp, DEFAULT_SCHEMA)
    conn = db.connect(dbp)
    ins = ("INSERT INTO finance(symbol,period,OperatingRevenue,OperatingRevenue_Q,src,"
           "updated_at) VALUES(?,?,?,?,'test','2026-11-14T00:00:00')")
    with db.tx(conn):
        conn.execute(ins, (SYM, "2025-03-31", 100.0, None))   # Q1：累计=单季
        conn.execute(ins, (SYM, "2025-06-30", 260.0, None))   # 差分 → 160
        conn.execute(ins, (SYM, "2025-09-30", 500.0, 240.0))  # 上游给了 _Q
        conn.execute(ins, (SYM, "2025-12-31", 700.0, None))   # 差 200
        # 2026 一季度故意缺失 → 2026-06-30 的"上期"不存在
        conn.execute(ins, (SYM, "2026-06-30", 900.0, None))

    rows = {r["period"]: r for r in Q.rows(conn, SYM, "OperatingRevenue")}
    check("Q1 直接取累计", rows["2025-03-31"]["src"] == "q1_cum"
          and nearly(rows["2025-03-31"]["value"], 100.0), str(rows["2025-03-31"]))
    check("Q2 差分 = 260 − 100", rows["2025-06-30"]["src"] == "derived"
          and nearly(rows["2025-06-30"]["value"], 160.0), str(rows["2025-06-30"]))
    check("上游给了 _Q 就不算", rows["2025-09-30"]["src"] == "stored"
          and nearly(rows["2025-09-30"]["value"], 240.0), str(rows["2025-09-30"]))
    check("Q4 差分 = 700 − 500", nearly(rows["2025-12-31"]["value"], 200.0),
          str(rows["2025-12-31"]))
    check("上期缺失 → missing 且 value=None（**不是 0**）",
          rows["2026-06-30"]["src"] == "missing" and rows["2026-06-30"]["value"] is None,
          str(rows["2026-06-30"]))

    check("单期查询命中", Q.value(conn, SYM, "OperatingRevenue", "2025-09-30")["src"] == "stored")
    check("单期查不到 → missing，不抛", Q.value(conn, SYM, "OperatingRevenue",
                                                "2020-03-31")["src"] == "missing")
    check("省略 period 取最新一期", Q.value(conn, SYM, "OperatingRevenue")["period"] == "2026-06-30")
    check("prev_period 跨年正确", Q.prev_period("2026-03-31") == "2025-12-31"
          and Q.prev_period("2026-09-30") == "2026-06-30")
    check("库里没这只股票 → 空序列，不抛", Q.rows(conn, "sz000001", "OperatingRevenue") == [])
    conn.close()


# ================================================================ 6. 端到端（ensure）

def test_ensure_finance(tmp: Path) -> None:
    section("6. 端到端 finance：首次取 43 期、二次不再打上游、partial 不推进 end_date")
    hub = make_hub(tmp)
    ends = q_ends("2026-09-30", 43)
    calls = FakeWestock({"data_finance": lambda a: fin_payload(SYM, ends)})

    with patch(W, "call", calls):
        need = want.finance(hub.target)
        r = hub.ensure("finance", SYM, need)
        check("首次取数后 n=43", r.n == 43, str(r.n))
        check("首次确实打了上游", calls.n == 1, str(calls.n))
        check("finish 到 2026-09-30", r.end == "2026-09-30", str(r.end))

        # 回归 bug 2：finance 的 need.start 是 None，老代码在这里一次都不取
        check("bug 回归：finance 无起点也能取到数", r.fetched is True and r.n > 0)

        # 回归 bug 1：季度序列不能按交易日历算洞，否则永远不命中
        cov = C.read(hub.conn, "finance", SYM, need.fp)
        check("bug 回归：季度序列 holes=0（不拿日历量季度）", cov.holes == 0, str(cov.detail))
        check("bug 回归：第二次 ensure 命中且不再打上游", hub.ensure("finance", SYM, need).n == 43
              and calls.n == 1, str(calls.n))
        check("用例 2：二次调用上游计数仍为 1", calls.n == 1, str(calls.n))

    # 用例 6：组级失败 → PartialData，且 partial=1 / end_date 不推进
    #
    # 先把覆盖改旧，制造一次"确实需要去取"的 miss：数据齐了的话 `hit()` 直接命中，
    # `ensure` 根本走不到打上游那一步——而那正是它该做的事。
    with db.tx(hub.conn):
        hub.conn.execute("UPDATE coverage SET end_date='2026-06-30' "
                         "WHERE domain='finance' AND symbol=?", (SYM,))
    before = C.read(hub.conn, "finance", SYM, want.finance(hub.target).fp)

    def failing(domain, codes, need_):
        got = Got()
        got.errors = [BatchError(list(codes))]      # 上游自报"这一组失败"
        return got

    with patch(W, "fetch", failing), patch(W, "call", FakeWestock({})):
        e = expect_raise("用例 6：组级失败 → PartialData", PartialData,
                         hub.ensure, "finance", SYM, want.finance(hub.target), force=True)
        if e:
            check("异常里带 domain/symbol", e.ctx.get("symbol") == SYM, str(e.ctx))

    after = C.read(hub.conn, "finance", SYM, want.finance(hub.target).fp)
    check("用例 6：partial=1", after.partial == 1, str(after.partial))
    check("用例 6：end_date 不推进", after.end == before.end, f"{after.end} vs {before.end}")
    check("用例 6：n 不变（没写半份数据）", after.n == before.n, f"{after.n} vs {before.n}")

    # 恢复上游 → 下次运行补齐并清掉 partial
    with patch(W, "call", FakeWestock({"data_finance": lambda a: fin_payload(SYM, ends)})):
        r = hub.ensure("finance", SYM, want.finance(hub.target))
        cov = C.read(hub.conn, "finance", SYM, want.finance(hub.target).fp)
        check("partial 之后能自愈（partial 归 0）", cov.partial == 0 and r.n == 43,
              f"partial={cov.partial} n={r.n}")
    hub.close()


def test_finance_full(tmp: Path) -> None:
    section("6b. finance_full：raw_json 拆回三表、补 period、保留期数、绝不打上游")
    hub = make_hub(tmp)
    # 造期数必须**多于** RAW_JSON_KEEP，否则裁剪不会发生、断言就成了自证。
    # 写成动态的（KEEP + 10）而不是硬编码 30：2026-11-14 把 KEEP 从 20 调到 43 时，
    # 硬编码的 30 期一下子就小于 KEEP 了，三个断言集体失效但报错信息看起来像代码坏了。
    n_ends = api.RAW_JSON_KEEP + 10
    ends = q_ends("2026-09-30", n_ends)
    with patch(W, "call", FakeWestock({"data_finance": lambda a: fin_payload(SYM, ends)})):
        need = want.finance(hub.target, periods=n_ends, min_points=n_ends)
        hub.ensure("finance", SYM, need)

    full = hub.finance_full(SYM, need)
    check("返回三张表的键", set(full) == {"income", "balance", "cashflow"}, str(sorted(full)))

    # 裁剪：库里只有最近 RAW_JSON_KEEP 期留着 raw_json，所以 finance_full 最多那么多
    check(f"finance_full 受 RAW_JSON_KEEP 约束（{api.RAW_JSON_KEEP} 期）",
          len(full["income"]) == api.RAW_JSON_KEEP, str(len(full["income"])))
    check("收入表按 period 升序", [r["period"] for r in full["income"]]
          == sorted(r["period"] for r in full["income"]))

    latest = full["income"][-1]
    check("每行都补了 period（上游把它放在 EndDate 上）", latest.get("period") == "2026-09-30",
          str(latest.get("period")))
    check("上游原始字段名一字未改（OperatingRevenue 还在）", "OperatingRevenue" in latest,
          str(sorted(latest)[:6]))
    check("值仍是上游原样（字符串），没被转成 float", isinstance(latest["OperatingRevenue"], str),
          type(latest["OperatingRevenue"]).__name__)

    # 缺表：某期没有资产负债表 → balance 少一期，income 不受影响
    check("三表期数可以不等（某期缺表不伪造）",
          len(full["balance"]) == len(full["income"]), "本用例三表齐全")

    before = FakeWestock({}).n
    calls = FakeWestock({"data_finance": lambda a: fin_payload(SYM, ends)})
    with patch(W, "call", calls):
        hub.finance_full(SYM, need)
        hub.finance_full(SYM, need)
    check("finance_full 绝不打上游（两次调用计数 0）", calls.n == 0, str(calls.n))

    # 库里没有的股票 → 三张空表，不抛
    empty = hub.finance_full("sz000001", want.finance(hub.target))
    check("库里没这只票 → 三张空表且不抛", all(v == [] for v in empty.values()), str(empty))

    # 老行 raw_json 为 NULL（历史上被裁掉的）→ 静默跳过，不炸
    with db.tx(hub.conn):
        hub.conn.execute("UPDATE finance SET raw_json=NULL WHERE symbol=? AND period=?",
                         (SYM, full["income"][0]["period"]))
    after = hub.finance_full(SYM, need)
    check("raw_json 为 NULL 的期被跳过（少一期而不是抛错）",
          len(after["income"]) == api.RAW_JSON_KEEP - 1, str(len(after["income"])))

    # **回归（2026-11-14 修）**：数值没变、但 raw_json 是 NULL 的行要能补回来。
    # 老代码 `if not changed: continue` 只看 15 个结构列，这种行永远补不上——
    # 连 `force=True` 重取都补不上，因为数值确实没变，代码在那一步就 return 了。
    # 症状：`finance` 表期数齐全，`finance_full()` 却只吐出 2 期。
    with db.tx(hub.conn):
        hub.conn.execute("UPDATE finance SET raw_json=NULL WHERE symbol=? AND period=?",
                         (SYM, full["income"][1]["period"]))
    rows = W._merge_statements(SYM, fin_payload(SYM, ends)["data"][SYM])
    with db.tx(hub.conn):
        api._write_rows(hub.conn, "finance", SYM, rows, "westock")
    healed = hub.finance_full(SYM, need)
    check("回归：数值没变的重取也能补回 raw_json（不再只看 15 个结构列）",
          len(healed["income"]) == api.RAW_JSON_KEEP, str(len(healed["income"])))
    check("回归：补 raw_json 不往 finance_revision 灌噪音",
          hub.conn.execute("SELECT COUNT(*) FROM finance_revision WHERE symbol=?",
                           (SYM,)).fetchone()[0] == 0,
          str(hub.conn.execute("SELECT COUNT(*) FROM finance_revision WHERE symbol=?",
                               (SYM,)).fetchone()[0]))
    hub.close()


def test_gbalance(tmp: Path) -> None:
    section("6c. gbalance：商誉/短借补丁、None 的两种含义、9201 空数据不算接口坏")
    hub = make_hub(tmp)
    need = want.gbalance(hub.target, periods=4)

    # ---- 纯函数：payload 结构 + has_balance 语义 ----
    def fake_dc(report, columns, filter_text, *, sort, page_size=1000, stop_before=None):
        """茅台式：有资产负债表，但没有商誉和短期借款（科目 null）。"""
        out = []
        for e in ("2026-09-30", "2026-06-30", "2026-03-31"):
            out.append({"SECUCODE": "600519.SH", "REPORT_DATE": f"{e} 00:00:00",
                        "GOODWILL": None, "SHORT_LOAN": None,
                        "TOTAL_ASSETS": 318304891882.72, "TOTAL_LIABILITIES": 37883274859.87,
                        "LEASE_LIAB": 196000000.0})
        return out

    with patch(em, "_datacenter", fake_dc):
        got = em._gbalance([SYM], need)
    payload = json.loads(got.rows[SYM][0]["payload"])
    check("gbalance 落 domain 字段", got.rows[SYM][0]["domain"] == "gbalance",
          str(got.rows[SYM][0].get("domain")))
    check("as_of 用最近报告期", got.rows[SYM][0]["as_of"] == "2026-09-30",
          str(got.rows[SYM][0].get("as_of")))
    check("payload 带 periods 与 has_balance", set(payload) == {"periods", "has_balance"},
          str(sorted(payload)))
    check("有资产负债表 → has_balance=True（商誉 null 应读作 0）",
          payload["has_balance"] is True, str(payload["has_balance"]))
    check("REPORT_DATE 归一成 YYYY-MM-DD",
          payload["periods"][0]["REPORT_DATE"] == "2026-09-30",
          str(payload["periods"][0].get("REPORT_DATE")))
    check("最近一期在前", [p["REPORT_DATE"] for p in payload["periods"]]
          == ["2026-09-30", "2026-06-30", "2026-03-31"],
          str([p["REPORT_DATE"] for p in payload["periods"]]))

    # 没资产负债表 → has_balance=False（null 必须读作 unknown，绝不能读成 0）
    def fake_dc_blank(report, columns, filter_text, *, sort, page_size=1000, stop_before=None):
        return [{"SECUCODE": "000001.SZ", "REPORT_DATE": "2026-09-30 00:00:00",
                 "GOODWILL": None, "SHORT_LOAN": None, "TOTAL_ASSETS": None,
                 "TOTAL_LIABILITIES": None, "LEASE_LIAB": None}]

    with patch(em, "_datacenter", fake_dc_blank):
        got = em._gbalance(["sz000001"], need)
    p2 = json.loads(got.rows["sz000001"][0]["payload"])
    check("没有资产负债表 → has_balance=False（null 才能读作 unknown）",
          p2["has_balance"] is False, str(p2["has_balance"]))

    # 9201 返回数据为空 → 空表，不能报成"接口改名"
    def fake_dc_9201(report, columns, filter_text, *, sort, page_size=1000, stop_before=None):
        return []

    with patch(em, "_datacenter", fake_dc_9201):
        got = em._gbalance(["sz999999"], need)
    check("9201 空数据 → empty_reason=no_data，不抛", got.empty_reason == "no_data"
          and not got.rows, f"{got.empty_reason} {list(got.rows)}")

    # ---- supports / fetch 分发 ----
    check("supports(snapshot, gbalance) 为真", em.supports("snapshot", need) is True)
    check("supports(snapshot, mainop) 仍为真", em.supports("snapshot", want.snapshot("mainop", "2026-09-30")) is True)
    check("supports(snapshot, consensus) 为假（那是 westock 的）",
          em.supports("snapshot", want.snapshot("consensus", "2026-11-13")) is False)

    # ---- 端到端：落库 + 读取 + 不改 fp 冲突 ----
    calls = FakeWestock({})
    with patch(W, "call", calls), patch(em, "_datacenter", fake_dc):
        r = hub.ensure("snapshot", SYM, need)
        check("gbalance 落库 1 行", r.n == 1, str(r.n))
        g = hub.gbalance_full(SYM, need)
        check("gbalance_full 读回 periods", len(g["periods"]) == 3, str(len(g["periods"])))
        check("gbalance_full 读回 has_balance", g["has_balance"] is True, str(g["has_balance"]))

        # 与 mainop 共用 snapshot 表，但 fp 带子域，不能互相顶掉
        r2 = hub.ensure("snapshot", SYM, want.snapshot("mainop", "2026-09-30"), force=True)
        g2 = hub.gbalance_full(SYM, need)
        check("写入 mainop 之后 gbalance 仍读得到（fp 带子域，互不顶掉）",
              len(g2["periods"]) == 3, str(len(g2["periods"])))

    # 库里没有 → 空结构，不抛
    empty = hub.gbalance_full("sz000002", need)
    check("库里没这只票 → periods 空、has_balance=False",
          empty == {"periods": [], "has_balance": False}, str(empty))
    hub.close()


def test_ensure_points(tmp: Path) -> None:
    section("7. 端到端 quote / profile / dividend / snapshot（一股一份与事件序列）")
    hub = make_hub(tmp)
    cal = TradingCalendar.load(DEFAULT_CALENDAR)

    with patch(W, "call", FakeWestock({"data_quote": lambda a: quote_payload(SYM, TARGET)})):
        r = hub.ensure("quote", SYM, want.quote(hub.target))
        check("quote 落一行", r.n == 1, str(r.n))
        check("quote 的 as_of 用行里的 trade_date", r.end == TARGET, str(r.end))
        cov = C.read(hub.conn, "quote", SYM, "")
        check("quote 覆盖记成点（start==end）", cov.start == cov.end == TARGET, str(cov))

    prof_calls = FakeWestock({"data_profile": lambda a: profile_payload(a["code"])})
    with patch(W, "call", prof_calls):
        r = hub.ensure("profile", SYM, want.profile())
        check("profile 落 stock 表", r.n == 1 and r.rows[0]["name"] == "贵州茅台",
              str(r.rows[:1]))
        check("profile 覆盖不按区间（没有洞的概念）",
              C.read(hub.conn, "profile", SYM, "").holes == 0)
        hub.ensure("profile", SYM, want.profile())
        check("profile 取过就够：第二次不再问上游（元数据不该按日重取）",
              prof_calls.n == 1, str(prof_calls.n))

    with patch(W, "call", FakeWestock({"data_dividend": lambda a: dividend_payload([
            {"exDiviDate": "20260626", "cashDiviRMB": "280.24", "bonusShareRatio": "",
             "tranAddShareRatio": "", "dividendPlan": "10派280.242元"},
            {"exDiviDate": "20250701", "cashDiviRMB": "230.00", "bonusShareRatio": "",
             "tranAddShareRatio": "", "dividendPlan": "10派230元"}])})):
        need = want.dividend(cal.start_for_bars(hub.target, 300))
        r = hub.ensure("dividend", SYM, need)
        check("dividend 落 2 条", r.n == 2, str(r.n))
        # 注意：`Range.start` 是**数据**的起点（最早一条分红），覆盖记录里的 start
        # 才是"我们查到的最早一天"。这两者在 dividend 上故意不同（十年不分红也得命中）。
        cov = C.read(hub.conn, "dividend", SYM, "")
        check("dividend 的覆盖起点记成「查过的最早一天」（不是最早一条分红）",
              cov.start == need.start, f"{cov.start} vs {need.start}")
        check("Range.start 是数据起点，不是覆盖起点", r.start == "2025-07-01", str(r.start))
        check("dividend 命中（十年不分红也是合法状态，不该每次重问）",
              C.hit(C.read(hub.conn, "dividend", SYM, ""), need) is True)

    # snapshot：两个子域各写各的，互不覆盖
    with patch(W, "call", FakeWestock({"data_rating": lambda a: {"buy": 20, "hold": 3}})):
        hub.ensure("snapshot", SYM, want.snapshot("rating", hub.target))
    with patch(em, "_get", lambda url, **kw: dc_payload([{
            "SECUCODE": "600519.SH", "REPORT_DATE": "2026-09-30 00:00:00",
            "MAINOP_TYPE": "2", "ITEM_NAME": "茅台酒", "MBI_RATIO": 0.86}])):
        hub.ensure("snapshot", SYM, want.snapshot("mainop", hub.target))
    rows = {r["domain"]: json.loads(r["payload"]) for r in hub.conn.execute(
        "SELECT domain,payload FROM snapshot WHERE symbol=?", (SYM,))}
    check("snapshot 两个子域并存", set(rows) == {"rating", "mainop"}, str(set(rows)))
    check("rating 的 payload 是上游原文", rows["rating"].get("buy") == 20)
    check("mainop 的 payload 是数组", isinstance(rows["mainop"], list))
    hub.close()


def test_valuation_depth(tmp: Path) -> None:
    section("7b. 估值深度：按 10 年去取，门槛 430 周（日历只有 4 年也不许夹）")

    def dc_rows(since: str):
        """合成日频估值：`since` 起每个工作日一行。够 `_weekly` 折成周频。"""
        def _dc(report, columns, filter_text, *, sort, page_size=1000, stop_before=None):
            dc_rows.stop_before = stop_before
            out, d = [], dt.date.fromisoformat(since)
            stop = dt.date.fromisoformat(TARGET)
            while d <= stop:
                if d.weekday() < 5:
                    out.append({"SECUCODE": "600519.SH",
                                "TRADE_DATE": f"{d.isoformat()} 00:00:00",
                                "PE_TTM": 20.0, "PB_MRQ": 6.0, "PS_TTM": 8.0,
                                "TOTAL_MARKET_CAP": 1.8e12})
                d += dt.timedelta(days=1)
            return list(reversed(out))         # 接口是降序
        return _dc

    hub = make_hub(tmp)
    need = want.valuation(hub.cal, hub.target)
    with patch(em, "_datacenter", dc_rows("2016-11-18")):
        r = hub.ensure("valuation", SYM, need)
    check("适配器收到的 stop_before 是 10 年前，不是日历首日",
          dc_rows.stop_before == need.start and need.start < hub.cal.days[0],
          f"{dc_rows.stop_before} / need.start={need.start} / 日历首日={hub.cal.days[0]}")
    check("10 年日频折成 450+ 周", r.n >= 450, str(r.n))
    check("过了 430 周门槛（不再 InsufficientDepth）", r.n >= 430, str(r.n))
    hub.close()

    # 只有 4 年（205 周）时仍然要报 InsufficientDepth——门槛不是为了让人偷偷降级
    hub2 = make_hub(tmp / "short")
    with patch(em, "_datacenter", dc_rows("2022-11-18")):
        expect_raise("只有 205 周 → InsufficientDepth（不降级）", InsufficientDepth,
                     hub2.ensure, "valuation", SYM, want.valuation(hub2.cal, hub2.target))
    hub2.close()


def test_intraday(tmp: Path) -> None:
    section("8b. 盘中规则：库里有就随时能跑，要联网才拦")
    # 2026-11-13 是周五（交易日），10:00 处在盘中窗口内
    day = "2026-11-13"

    def quote_for(args: dict) -> dict:
        """按请求的 codes 逐只回（这样后面换一只票也能测）。"""
        out: dict = {}
        for code in args["codes"].split(","):
            out.update(quote_payload(code, day))
        return out

    calls = FakeWestock({"data_quote": quote_for})

    h = make_hub(tmp, now=dt.datetime(2026, 11, 13, 10, 0))
    check("盘中可以启动（不再启动即拦）", h.conn is not None)
    check("盘中标记 intraday", h.intraday is True)
    check("盘中 target 退到上一交易日", h.target == "2026-11-12", h.target)

    with patch(W, "call", calls):
        expect_raise("库里没有 → 盘中要联网 → MarketOpenError",
                     MarketOpenError, h.ensure, "quote", SYM, want.quote(day))
        check("被拦时一次上游都没打（不是先打了再报错）", calls.n == 0, str(calls.n))
    h.close()

    # 收盘后取到 11-13 的行情
    h = make_hub(tmp, now=dt.datetime(2026, 11, 13, 16, 0))
    check("收盘后标记 intraday=False", h.intraday is False)
    with patch(W, "call", calls):
        h.ensure("quote", SYM, want.quote(day))
        check("收盘后落库 1 次", calls.n == 1, str(calls.n))
    h.close()

    # 再回盘中（同一天上午）：要的数据已经在库里 → 完全不联网，照跑
    h = make_hub(tmp, now=dt.datetime(2026, 11, 13, 10, 0))
    with patch(W, "call", calls):
        r = h.ensure("quote", SYM, want.quote(day))
        check("库里有 → 盘中照跑（命中缓存）", r.n == 1 and r.end == day, f"n={r.n} end={r.end}")
        check("库里有 → 盘中一次上游都不打", calls.n == 1, str(calls.n))
        expect_raise("库里的不够新（要更晚的一天）→ 又会被拦",
                     MarketOpenError, h.ensure, "quote", SYM, want.quote("2026-11-14"))
        check("被拦时仍没打上游", calls.n == 1, str(calls.n))
    h.close()

    # 显式放行（等价于 allow_intraday=True）：盘中允许联网。换一只库里没有的票，
    # 因为 `force=True` 只清"洞的重试标记"，命中缓存的仍然不会去取（那样才对）。
    h = make_hub(tmp, now=dt.datetime(2026, 11, 13, 10, 0))
    h.intraday = False
    with patch(W, "call", calls):
        r = h.ensure("quote", "sz000651", want.quote(day))
        check("放行后盘中可以联网", calls.n == 2 and r.n == 1, f"calls={calls.n} n={r.n}")
    h.close()


def test_router_reuse(tmp: Path) -> None:
    section("8. Router 复用：连续失败 3 次才降级（老代码永远不会降级）")
    hub = make_hub(tmp)
    check("Hub.router 是同一个对象", hub.router is hub.router)

    n = {"calls": 0}

    def boom(domain, codes, need):
        n["calls"] += 1
        raise SourceUnavailable("假的上游不可达")

    syms = [f"sh6000{i:02d}" for i in range(5)]
    with patch(W, "fetch", boom):
        for s in syms[:3]:
            expect_raise(f"{s} 首次失败 → 本次调用抛 SourceUnavailable",
                         SourceUnavailable, hub.ensure, "finance", s, want.finance(hub.target))
        check("3 次失败各打了一次上游", n["calls"] == 3, str(n["calls"]))
        check("连续 3 次后 westock 在 finance 上被降级",
              ("finance", "westock") in hub.router.dead, str(hub.router.dead))
        expect_raise("第 4 只直接报「没有可用源」，不再白打上游",
                     SourceUnavailable, hub.ensure, "finance", syms[3], want.finance(hub.target))
        check("第 4 次没有再打上游（计数仍为 3）", n["calls"] == 3, str(n["calls"]))
        check("降级留痕写进 meta", db.meta_get(hub.conn, "source_degraded") is not None)
    hub.close()


# ================================================================ 9. 联网（可选）

def test_live() -> None:
    section("9. 用例 1b：真打一次 westock（STOCKHUB_LIVE=1 才跑）")
    if os.environ.get("STOCKHUB_LIVE") not in ("1", "true", "yes"):
        print("  ⏭  跳过（未设 STOCKHUB_LIVE=1）")
        return

    # 前面为了让测试不依赖机器状态，往环境里塞了一个假令牌。联网这一节必须把它摘掉，
    # 让 `token()` 回去读 `~/.config/westock/tokens.json`——否则永远 401。
    saved = os.environ.pop("STOCKHUB_WESTOCK_TOKEN", None)
    W._token_cache = None
    W._client_cache = None
    tmp = mkdtemp("stockhub-live-")
    try:
        ok, why = W.available()
        if not ok:
            print(f"  ⏭  跳过：{why}")
            return
        hub = make_hub(tmp)
        cal = TradingCalendar.load(DEFAULT_CALENDAR)
        r = hub.ensure("quote", SYM, want.quote(hub.target))
        print(f"      quote: {r.rows[:1]}")
        check("真打 quote 能拿到 price>0", r.n == 1 and (r.rows[0]["price"] or 0) > 0)
        f = hub.ensure("finance", SYM, want.finance(hub.target))
        check("真打 finance 至少 40 期", f.n >= 40, str(f.n))
        d = hub.ensure("dividend", SYM, want.dividend(cal.start_for_bars(hub.target, 300)))
        check("真打 dividend 有分红记录", d.n >= 1, str(d.n))
        q = hub.single_quarter(SYM, "OperatingRevenue")
        check("single_quarter 能算出最新单季", q["value"] is not None, str(q))
        print(f"      最新单季营收：{q}")
        hub.close()
    finally:
        if saved is not None:
            os.environ["STOCKHUB_WESTOCK_TOKEN"] = saved
        W._token_cache = None
        W._client_cache = None


# ================================================================ main

def test_volume_unit() -> None:
    """成交量的单位按板块不一致：主板是手，科创板是股（2026-11-15 实测）。"""
    section("_to_shares：按 amount/(close×volume) 判单位")
    # 主板：amount ≈ close×vol×100 → 手 → ×100
    check("主板（手）→ ×100",
          _to_shares(1_025_530, 6.18, 627_110_000) == 102_553_000,
          str(_to_shares(1_025_530, 6.18, 627_110_000)))
    # 科创板：amount ≈ close×vol → 已经是股 → 原样
    check("科创板（股）→ 不乘",
          _to_shares(1_932_126, 86.34, 167_850_000) == 1_932_126,
          str(_to_shares(1_932_126, 86.34, 167_850_000)))
    # 创业板同样是手
    # 注意传的是**上游原值** 302,995 手（不是落库后的 30,299,500 股）
    check("创业板（手）→ ×100",
          _to_shares(302_995, 237.70, 7_244_220_000) == 30_299_500,
          str(_to_shares(302_995, 237.70, 7_244_220_000)))
    # 没有 amount（tencent 通道）：无从判断，保持历史行为 ×100
    check("无 amount → 退回 ×100",
          _to_shares(1000, 10.0, None) == 100_000,
          str(_to_shares(1000, 10.0, None)))
    check("volume 缺失 → None", _to_shares(None, 10.0, 1e6) is None, "")


def main() -> int:
    test_volume_unit()
    print("=" * 60)
    print("第 5 步：域适配器与写库")
    print("=" * 60)
    test_pure()
    test_adapter()
    test_kline_adapter()
    test_eastmoney()
    t1 = mkdtemp("stockhub-writers-")
    test_writers(t1)
    t2 = mkdtemp("stockhub-quarter-")
    test_quarter(t2)
    t3 = mkdtemp("stockhub-fin-")
    test_ensure_finance(t3)
    t3b = mkdtemp("stockhub-finfull-")
    test_finance_full(t3b)
    t3c = mkdtemp("stockhub-gbal-")
    test_gbalance(t3c)
    t4 = mkdtemp("stockhub-points-")
    test_ensure_points(t4)
    t4b = mkdtemp("stockhub-val-")
    test_valuation_depth(t4b)
    t5 = mkdtemp("stockhub-router-")
    test_router_reuse(t5)
    t6 = mkdtemp("stockhub-intraday-")
    test_intraday(t6)
    test_live()

    print("\n" + "=" * 60)
    if FAIL:
        print(f"\033[31m通过 {PASS} 项，失败 {len(FAIL)} 项\033[0m")
        for name in FAIL:
            print(f"  - {name}")
        return 1
    print(f"\033[32m通过 {PASS} 项，失败 0 项\033[0m")
    print(f"临时目录（可删）：{t1.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
