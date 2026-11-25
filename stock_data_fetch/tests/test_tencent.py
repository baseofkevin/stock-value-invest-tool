"""`sources/tencent.py` 测试（**离线**：`_get` 换成假响应，一次网络都不打）。

跑法：

    cd /home/kevin/MyData/stock_value_invest_tool
    python3 -m stock_data_fetch.tests.test_tencent

这组用例盯的是三件"错了不会报错、只会悄悄错"的事：

1. **元组顺序**是 `[date, open, close, high, low, volume]`——把第 3 位当最高价，
   均线和涨幅照样算得出来，只是全错；
2. **`fq` 段必须留空**（不复权）。写成 `qfq` 就是前复权，与库里的 raw 混起来，
   复权因子会被乘第二遍；
3. **成交量是手**，不 ×100 会让换手率小 100 倍。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve()
PROJECT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT))

from stock_data_fetch import sources                          # noqa: E402
from stock_data_fetch.errors import SourceUnavailable         # noqa: E402
from stock_data_fetch.need import Need                        # noqa: E402
from stock_data_fetch.sources import tencent as T             # noqa: E402

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


def section(title: str) -> None:
    print(f"\n{title}")


def need(points: int = 300, start: str = "2025-08-18", end: str = "2026-11-13") -> Need:
    return Need("kline", "fq=raw", end, start, points, 71)


def body(symbol: str, bars: list) -> str:
    return json.dumps({"code": 0, "msg": "", "data": {symbol: {"day": bars}}})


# 真实返回（2026-11-13 茅台，curl 实测）：顺序 date, open, close, high, low, volume(手)
MAOTAI = [["2026-11-12", "1420.000", "1418.000", "1425.000", "1408.000", "30112.000"],
          ["2026-11-13", "1404.000", "1435.260", "1437.690", "1394.000", "41780.000"]]


def test_parse() -> None:
    section("解析：元组顺序、单位换算、成交额留空")
    with patch.object(T, "_get", lambda url: body("sh600519", MAOTAI)):
        got = T.fetch("kline", ["sh600519"], need())

    rows = got.rows["sh600519"]
    check("两只 bar 都解析出来", len(rows) == 2, str(len(rows)))
    r = rows[-1]
    check("open 取第 2 位（1404.00）", r["open"] == 1404.0, str(r["open"]))
    check("close 取第 3 位（1435.26）", r["close"] == 1435.26, str(r["close"]))
    check("high 取第 4 位（1437.69）", r["high"] == 1437.69, str(r["high"]))
    check("low 取第 5 位（1394.00）", r["low"] == 1394.0, str(r["low"]))
    check("volume 手→股（41780 手 = 417.8 万股）", r["volume"] == 4178000.0, str(r["volume"]))
    check("amount 留 None（这条通道没有成交额）", r["amount"] is None, str(r["amount"]))
    check("日期是 YYYY-MM-DD", r["date"] == "2026-11-13", r["date"])
    check("没有多出来的键", set(r) == {"date", "open", "close", "high", "low",
                                       "volume", "amount"}, str(sorted(r)))


def test_order_and_noise() -> None:
    section("乱序与噪声行")
    bars = [MAOTAI[1], MAOTAI[0],                      # 故意倒序
            ["2026-11-14"],                            # 短元组 → 跳过
            ["20261115", "1", "1", "1", "1", "1"],     # 脏日期 → 跳过
            [["2026-11-13", "1", "1", "1", "1", "1"]]]
    with patch.object(T, "_get", lambda url: body("sh600519", bars)):
        got = T.fetch("kline", ["sh600519"], need())
    rows = got.rows["sh600519"]
    check("只剩 2 根有效 bar", len(rows) == 2, str(len(rows)))
    check("按日期升序", [x["date"] for x in rows] == ["2026-11-12", "2026-11-13"],
          str([x["date"] for x in rows]))


def test_request() -> None:
    section("请求参数：fq 段留空、count 带缓冲、起止日期都传、逗号不许被编码")
    seen: list[str] = []

    def fake(url: str) -> str:
        seen.append(url)
        return body("sh600519", MAOTAI)

    with patch.object(T, "_get", fake):
        T.fetch("kline", ["sh600519"], need(points=300))
    url = seen[0]
    check("URL 里的逗号是原样（%2C 会让上游回 501，实测 566 只全挂）",
          ",day," in url and "%2C" not in url, url)
    param = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["param"][0]
    parts = param.split(",")
    check("第 1 段是代码", parts[0] == "sh600519", param)
    check("第 2 段是 day", parts[1] == "day", param)
    check("起止日期都传了", parts[2] == "2025-08-18" and parts[3] == "2026-11-13", param)
    check("count = points + 20（给 min_bars 留缓冲）", parts[4] == "320", param)
    check("第 6 段为空 = 不复权（写成 qfq 就毁了 raw 不变式）", parts[5] == "", param)

    seen.clear()
    with patch.object(T, "_get", fake):
        T.fetch("kline", ["sh600519"], need(points=1200))
    check("count 有上限 900（再大上游也不给）",
          urllib.parse.parse_qs(
              urllib.parse.urlparse(seen[0]).query)["param"][0].split(",")[4] == "900",
          seen[0])


def test_errors() -> None:
    section("异常与空表")
    with patch.object(T, "_get", lambda url: body("sh600519", [])):
        got = T.fetch("kline", ["sh600519"], need())
    check("空 day → 不写行 + empty_reason=no_data",
          "sh600519" not in got.rows and got.empty_reason == "no_data", str(got))

    with patch.object(T, "_get", lambda url: json.dumps({"code": 0, "data": {}})):
        got = T.fetch("kline", ["bj430047"], need())
    check("data 里没这只 → no_data（北交所迁移代码就是这样）",
          got.empty_reason == "no_data" and not got.rows, str(got))

    with patch.object(T, "_get", lambda url: json.dumps({"code": 2001, "msg": "参数错误"})):
        try:
            T.fetch("kline", ["sh600519"], need())
            check("code != 0 → 抛", False)
        except SourceUnavailable as e:
            check("code != 0 → 抛（不当成空表）", "2001" in str(e), str(e))

    with patch.object(T, "_get", lambda url: "<html>502</html>"):
        try:
            T.fetch("kline", ["sh600519"], need())
            check("非 JSON → 抛", False)
        except SourceUnavailable:
            check("非 JSON → 抛（不当成空表）", True)

    try:
        T.fetch("finance", ["sh600519"], need())
        check("不支持的域 → 抛", False)
    except SourceUnavailable as e:
        check("不支持的域 → 抛", "finance" in str(e), str(e))


def test_registry() -> None:
    section("注册表：kline 的第二条腿")
    names = [s.name for s in sources.available("kline")]
    check("tencent 出现在 kline 的可用源里", "tencent" in names, str(names))
    check("排在东财之后（东财仍是主源）",
          names.index("eastmoney") < names.index("tencent"), str(names))
    check("不声明 finance 域（声明了就会每只票白打一次）",
          "finance" not in sources.get("tencent").domains, str(sources.get("tencent").domains))
    check("upstream 记的是 tencent（与 westock 同上游，raw 可混）",
          sources.get("tencent").upstream == "tencent", sources.get("tencent").upstream)
    check("fetch_fn 能拿到适配器", sources.fetch_fn("tencent") is T.fetch)
    ok, why = T.available()
    check("available() 恒真（无鉴权，且不许在启动路径联网探测）", ok and why == "", why)


def test_row_shape_matches_writer() -> None:
    section("写库契约：行形状与 api._write_rows(kline) 要的列一致")
    from stock_data_fetch import api
    with patch.object(T, "_get", lambda url: body("sh600519", MAOTAI)):
        got = T.fetch("kline", ["sh600519"], need())
    row = got.rows["sh600519"][0]
    # api 的 kline 写入用的是命名参数展开，缺一个键会在真跑时报 TypeError
    cols = {"symbol", "date", "open", "close", "high", "low", "volume", "amount", "src"}
    check("适配器给出的列是 writer 列的子集",
          set(row) | {"symbol", "src"} <= cols, str(sorted(set(row))))
    check("api 模块里有 _write_rows（契约没改名）", hasattr(api, "_write_rows"))


def host_of(url: str) -> str:
    """从 URL 里取出 host。

    **不能对 URL 做子串匹配**：`ifzq.gtimg.cn` 是 `web.ifzq.gtimg.cn` 的子串，
    用 `in` 判断会把两个域名认成一个（这条把测试坑过一次：web 明明探测成功
    却被记成失败，因为它的 URL 里含 `ifzq.gtimg.cn`）。
    """
    return urllib.parse.urlparse(url).hostname or ""


def waf_error(url: str = "u") -> SourceUnavailable:
    """构造一个"被 WAF 拦了"的 SourceUnavailable，形状与真实 `_get` 抛的一致。

    `waf` / `status` / `transient` 一律走 `ctx`（`StockfetchError.__init__` 只认 `**ctx`），
    与 `router.py:142` 的读法一致：`e.ctx.get("transient")`。
    """
    return SourceUnavailable(f"腾讯 HTTP 501（WAF）：{url}",
                             status=501, transient=False, waf=True)


class CleanState:
    """每个用例开头把域名健康状态清干净，避免用例之间互相污染（都是模块级全局）。"""

    def __enter__(self):
        T.reset_state()
        return self

    def __exit__(self, *exc):
        T.reset_state()
        return False


def test_rank_by_health() -> None:
    section("域名按实测可用性排序：能用的在前，不能用的在后")
    with CleanState():
        check("初始三个域名都在候选里", set(T._ranked_hosts()) == set(T.KLINE_HOSTS),
              str(T._ranked_hosts()))
        check("未探明时保持 KLINE_HOSTS 初值顺序",
              T._ranked_hosts() == list(T.KLINE_HOSTS), str(T._ranked_hosts()))

        bad, good = T.KLINE_HOSTS[0], T.KLINE_HOSTS[1]
        T._mark(bad, False)
        order = T._ranked_hosts()
        check("标死的域名排到末尾", order[-1] == bad, str(order))
        check("活的域名提到前面", order[0] == good, str(order))
        check("排序不过滤（死的仍在列表里，WAF 解封后能复活）",
              set(order) == set(T.KLINE_HOSTS), str(order))

    with CleanState():
        # 两个死一个活
        for h in T.KLINE_HOSTS[:2]:
            T._mark(h, False)
        order = T._ranked_hosts()
        check("只剩一个活域名时它排第一", order[0] == T.KLINE_HOSTS[2], str(order))
        check("两个死域名按原顺序垫在后面",
              order[1:] == list(T.KLINE_HOSTS[:2]), str(order))

    with CleanState():
        # 过期后自动复活：把它按"很久以前测的"写进去
        T._host_state[T.KLINE_HOSTS[0]] = (False, time.monotonic() - T._revive_after - 1)
        check("超过 _revive_after 的死亡记录自动复活（排回原位）",
              T._ranked_hosts()[0] == T.KLINE_HOSTS[0], str(T._ranked_hosts()))


def test_waf_failover() -> None:
    section("WAF 域名封禁：换域名而不是整源放弃（2026-11-15 的真事故）")

    # 主域名回 WAF、下一个域名正常 → 要用上下一个域名的数据。
    # 注意：测试替换的是 `_get` 本身，所以这里要模拟**它对 `_get_path` 的契约**——
    # 抛带 `waf=True` 的 SourceUnavailable，而不是裸的 HTTPError（后者是 `_get` 内部消化掉的）。
    hit: list[str] = []
    bad = T.KLINE_HOSTS[0]

    def fake(url: str, *, timeout=None) -> str:
        hit.append(url)
        if host_of(url) == bad:
            raise waf_error(url)
        return body("sh600519", MAOTAI)

    with CleanState():
        with patch.object(T, "_get", fake):
            got = T.fetch("kline", ["sh600519"], need())
        check("被 WAF 拦的域名换掉后，数据拿到", got.rows.get("sh600519"), str(list(got.rows)))
        check("被拦域名记进健康表（标死）",
              T._host_state.get(bad, (True, 0))[0] is False, str(T._host_state))
        check("被拦域名之后才试别的域名", host_of(hit[0]) == bad, str(hit[:2]))

        # 第二只票不该再把它排前面
        hit.clear()
        with patch.object(T, "_get", fake):
            T.fetch("kline", ["sh600519"], need())
        check("坏域名已排到末尾（不再每只票先撞一次）",
              host_of(hit[0]) != bad, str([host_of(u) for u in hit[:2]]))

    # 全部域名被拦 → 抛（交给 router 换源），且标成永久错误不重试
    def all_waf(url: str, *, timeout=None) -> str:
        raise waf_error(url)

    with CleanState():
        with patch.object(T, "_get", all_waf):
            try:
                T.fetch("kline", ["sh600519"], need())
                check("全部域名被拦 → 抛", False)
            except SourceUnavailable as e:
                check("全部域名被拦 → 抛，且 transient=False（不浪费冷却）",
                      e.ctx.get("transient") is False, f"ctx={e.ctx}")
                check("异常里交代了是 WAF 的锅", "WAF" in str(e), str(e))

    # 501 但**不是** WAF 页 → 保持原语义，不许伪装成"换域名能好"
    calls: list[str] = []

    def plain_501(url: str, *, timeout=None) -> str:
        calls.append(url)
        raise SourceUnavailable("腾讯 HTTP 501", status=501, transient=False)

    with CleanState():
        with patch.object(T, "_get", plain_501):
            try:
                T.fetch("kline", ["sh600519"], need())
                check("非 WAF 的 501 → 抛", False)
            except SourceUnavailable as e:
                check("非 WAF 的 501 不标 waf 标记（别把真错误当域名问题）",
                      not e.ctx.get("waf"), str(e.ctx))
        # 非 WAF 的 501 只让「探测」把它降级（探测时任何失败都不该优先使用），
        # 但**不能**被当成 WAF 封禁——否则 `_get_path` 会以为"换个域名就好了"。
        # 这里断言：抛出来的异常不带 waf 语义，且 fetch 直接失败而不是偷偷换域名重试。
        check("非 WAF 的 501 期间 _get 只被调用一次（没把它当域名问题去换）",
              len(calls) <= len(T.KLINE_HOSTS) + 1,
              f"calls={len(calls)}（探测 {len(T.KLINE_HOSTS)} 次 + 取数 1 次）")


def test_probe() -> None:
    section("显式探测：批量开跑前先定序，别让第一只票去撞死域名")
    with CleanState():
        seen: list[str] = []

        def fake(url: str, *, timeout=None) -> str:
            seen.append(url)
            if host_of(url) == T.KLINE_HOSTS[1]:
                raise waf_error(url)
            return body(T.PROBE_SYMBOL, MAOTAI)

        with patch.object(T, "_get", fake):
            order = T.probe()
        check("探测打遍了所有域名", len(seen) == len(T.KLINE_HOSTS), str(len(seen)))
        check("探测用短超时（不为一句话等 20 秒）",
              T.PROBE_TIMEOUT < T.TIMEOUT, f"{T.PROBE_TIMEOUT} vs {T.TIMEOUT}")
        check("探测后坏域名排末尾", order[-1] == T.KLINE_HOSTS[1], str(order))
        check("探测后好域名排前面", order[0] == T.KLINE_HOSTS[0], str(order))

        # 缓存：TTL 内再调不该重新打网络
        seen.clear()
        with patch.object(T, "_get", fake):
            T.probe()
        check("TTL 内复用缓存（不重复打网络）", not seen, str(seen))

        # force 无视缓存
        seen.clear()
        with patch.object(T, "_get", fake):
            T.probe(force=True)
        check("force=True 会重探", len(seen) == len(T.KLINE_HOSTS), str(len(seen)))

    # 探测本身失败（网络断）也不该抛：探测只是排序，不能挡住后面的取数
    with CleanState():
        def boom(url: str, *, timeout=None) -> str:
            raise SourceUnavailable("腾讯连接中断：timed out", transient=True)

        with patch.object(T, "_get", boom):
            try:
                order = T.probe()
                check("探测全失败也不抛（探测只是排序）", True)
                check("全失败时仍返回完整域名列表", len(order) == len(T.KLINE_HOSTS),
                      str(order))
            except SourceUnavailable:
                check("探测全失败也不抛（探测只是排序）", False)


def test_fetch_probes_first() -> None:
    section("fetch 入口先探测（一次批量一次探测，不逐只）")
    with CleanState():
        calls: list[str] = []

        def fake(url: str, *, timeout=None) -> str:
            calls.append(url)
            return body(url.split("=")[-1].split(",")[0] or T.PROBE_SYMBOL, MAOTAI)

        with patch.object(T, "_get", fake):
            T.fetch("kline", ["sh600519", "sz000001", "sh601318"], need())

        probe_calls = [c for c in calls if ",,,20," in c]   # 探测用的固定 param
        check("一批 3 只只探测一轮（3 个域名）",
              len(probe_calls) == len(T.KLINE_HOSTS), str(len(probe_calls)))
        check("探测之外确实为每只票各打了一次",
              len(calls) == len(T.KLINE_HOSTS) + 3, str(len(calls)))


def test_real_get_waf() -> None:
    section("真实 _get 对 WAF 拦截页的识别（不打网络：urlopen 换成抛 HTTPError）")

    def raiser(code: int, payload: bytes):
        def fake_open(req, timeout=None):
            e = urllib.error.HTTPError(req.full_url, code, "x", {}, None)
            e.read = lambda n=-1: payload
            raise e
        return fake_open

    waf_page = b'<script>window.location.href="https://waf.tencent.com/501page.html"</script>'
    with patch.object(T.urllib.request, "urlopen", raiser(501, waf_page)):
        try:
            T._get("https://example.invalid/x")
            check("真实 _get：501 + WAF 页 → 抛", False)
        except SourceUnavailable as e:
            check("真实 _get 认得出 WAF 页（ctx.waf=True）", e.ctx.get("waf") is True, str(e.ctx))
            check("WAF 命中时 transient 按 501 的规则为 False",
                  e.ctx.get("transient") is False, str(e.ctx))

    with patch.object(T.urllib.request, "urlopen", raiser(501, b"not implemented")):
        try:
            T._get("https://example.invalid/x")
            check("真实 _get：501 非 WAF 页 → 抛", False)
        except SourceUnavailable as e:
            check("真实 _get 对非 WAF 的 501 不给 waf 标记", not e.ctx.get("waf"), str(e.ctx))

    with patch.object(T.urllib.request, "urlopen", raiser(429, waf_page)):
        try:
            T._get("https://example.invalid/x")
            check("真实 _get：429 → 抛", False)
        except SourceUnavailable as e:
            check("429 即使带 WAF 字样也不当域名封禁（那是限频，冷却才对）",
                  not e.ctx.get("waf") and e.ctx.get("transient") is True, str(e.ctx))

    # 正常 200 不走 HTTPError 分支（`raises` 只能伪造错误码），这条路径由 test_parse 覆盖。


def test_url_build() -> None:
    section("URL 拼装")
    check("proxy 域名拼出 /ifzqgtimg 前缀",
          T._url_for("proxy.finance.qq.com", T.KLINE_PATH, "param=x").startswith(
              "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?"),
          T._url_for("proxy.finance.qq.com", T.KLINE_PATH, "param=x"))
    check("普通域名不带前缀", "/ifzqgtimg" not in
          T._url_for("ifzq.gtimg.cn", T.KLINE_PATH, "param=x"),
          T._url_for("ifzq.gtimg.cn", T.KLINE_PATH, "param=x"))
    check("KLINE_URL 指向 KLINE_HOSTS 第一个域名（只是初值，不决定实际请求）",
          T.KLINE_HOSTS[0] in T.KLINE_URL, T.KLINE_URL)


def main() -> int:
    test_parse()
    test_order_and_noise()
    test_request()
    test_errors()
    test_registry()
    test_row_shape_matches_writer()
    test_rank_by_health()
    test_waf_failover()
    test_probe()
    test_fetch_probes_first()
    test_real_get_waf()
    test_url_build()

    print("\n" + "=" * 60)
    if FAIL:
        print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
        for name in FAIL:
            print(f"  - {name}")
        return 1
    print(f"通过 {PASS} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
