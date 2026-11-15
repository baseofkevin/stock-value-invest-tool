"""腾讯行情适配器：`kline` 的**备源**（无鉴权直连 HTTP）。

## 为什么要有它

`eastmoney.push2his` 是 K 线主源，但它有一条实测过的坏脾气：**突发配额用完就整段拒连**
（`SSL_read: unexpected eof`，0.2 秒内关闭，`SOURCES` §5.7），2026-11-14 实测持续 20 分钟
以上不恢复。而库里的 K 线只有它**一条腿**（tdx 在独立进程 401、westock 的 `data_kline`
只回 1212 根且返回的是前复权）。所以东财一封，四个工具全部停摆。

这条腿补的就是那个空档：**同上游（腾讯）的另一条传输**，无令牌、无鉴权、一次给 900 根。

## 三个实测事实（2026-11-14，curl 逐点对过账）

1. **`fq` 参数留空就是不复权**。这是它与 westock MCP 那条通道的本质区别：

       格力 sz000651  2026-08-26  close 41.58   ← 与 tdx tqFlag=0 / 东财 fqt=0 相同
       格力 sz000651  2026-08-27  close 39.27   ← 除权日直接跳（10 派 20 元的除权参考价）

   对比 `web.ifzq.gtimg.cn/.../fqkline/get?...qfq` 会给前复权（41.58 − 2.00 = 39.58）。
   **所以这条通道能当 raw 源，westock 那条不能。**

2. **元组顺序是 `[date, open, close, high, low, volume]`**，第 2 位是**开**、第 3 位是**收**。
   按"开高低收"的常识去解会把收盘价当最高价，而均线、涨幅全都不报错——只会悄悄错。

3. **`volume` 是手**（茅台 2026-11-13 `41780` 手 = 417.8 万股，与 `quote.volume` 一致），
   入库 ×100 换成股，与 `kline.volume` 的口径对齐。

5 只票 × 300 根（`sh600007/9/10/11/12`）与库里东财的行**逐点相同**（OHLC + 成交量，
bad=0），所以换源不作废任何旧数据（`fp` 仍是 `fq=raw`）。

## 与东财的字段差异

| 字段 | 东财 `fqt=0` | 本适配器 |
|---|---|---|
| `open/close/high/low` | 有 | 有（逐点相同） |
| `volume` | 手 → ×100 | 手 → ×100 |
| `amount` | 有（元） | **没有** → 落库为 `0`（见下） |

`amount` 缺失是这条通道的硬限制：它的 6 元组里根本没有成交额（除权日那根多出来的
第 7 位是分红 dict，不是数字——实测 `sz000001` 60 根里 59 个 6 元组、1 个 7 元组）。
`schema.sql` 里 `kline.amount` 是 `NOT NULL DEFAULT 0`，所以只能落 0，
**不拿"成交量 × 收盘价"去补**：那个估算值看起来像真值，会被下游当成成交额用。

> ⚠️ 因此：`amount = 0` 有两种含义——"上游明确给了 0" 与 "这一行来自 tencent 通道"。
> 区分办法是 `kline.src`。**目前四个工具都不读 `amount`**（换手率用 `volume / float_shares`）；
> 将来要读，先按 `src` 过滤，或给它换成可空列。

> 除权日那一根 bar 的元组后面会多带一个 dict（`{"cqr": "2026-08-27",
> "FHcontent": "10派20元", ...}`），是本通道白送的分红信息。**这里不解析它**：
> 分红归 `dividend` 域（westock `data_dividend`），两处都收会造出第二份真相。

## 域名不能只写一个（2026-11-15 补）

`web.ifzq.gtimg.cn` 被腾讯 WAF 拉黑了，任何请求都回 501 + 跳转 `waf.tencent.com`。
因为写死了这一个域名，kline 域整段瘫掉（跑批 4304 只里 3896 只取数失败，最终只命中 407）。
现在按 `KLINE_HOSTS` 的顺序做**域名级故障转移**，细节见那个常量的注释。
"""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from ..errors import SourceUnavailable
from ..need import Need
from .base import Got

log = logging.getLogger("stock_data_fetch.tencent")

KLINE_HOSTS = ("web.ifzq.gtimg.cn", "ifzq.gtimg.cn", "proxy.finance.qq.com")
"""同一个端点的三个域名。**这里的顺序只是"初值"，不是"优先级"**。

2026-11-15 实测：三个域名会**交替被腾讯 WAF 拉黑**，被拉黑时任何 `param` 都回 `501`，
响应体不是 JSON 而是跳转脚本——

    <script>...window.location.href="https://waf.tencent.com/501page.html?u=..."+...

这是**域名级**封禁，与 `param` 内容、`Referer`、URL 长度、代码号段统统无关
（参数矩阵逐项对过：短/长 param、带/不带 referer、qfq/raw 全是 501）。

> 教训：我一开始按"哪个域名当时能通"把它写死成主域名（`ifzq` 排第一），
> 跑批十分钟后 `ifzq` 和 `proxy` 双双 501，只剩 `web` 还活着——
> **写死顺序等于把可用性赌在一次探测上**。所以现在改成运行时探测 + 结果排序，
> 见 `_ranked_hosts()`。

三个域名返回体逐字节相同，都是 raw 不复权：

    https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=...
    https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=...
    https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param=...

`proxy.finance.qq.com` 多一段 `/ifzqgtimg` 路径前缀。
"""

KLINE_PATH = "/appstock/app/fqkline/get"
"""后两段路径，三个域名共用（`proxy.finance.qq.com` 的前缀在下面单独拼）。"""

_HOST_PREFIX = {"proxy.finance.qq.com": "/ifzqgtimg"}
"""`proxy` 那条要把来源站名拼进路径，其余直接接 `KLINE_PATH`。"""

KLINE_URL = "https://" + KLINE_HOSTS[0] + KLINE_PATH
"""初值域名拼出来的完整 URL。保留这个名字：日志与旧测试都按它取址。
**不要拿它直接发请求**——走 `_get_path()`，它会挑当时可用的域名。"""

WAF_MARK = "waf.tencent.com"
"""WAF 拦截页的特征串。命中即判定"这个域名被拉黑了"，换下一个域名，别重试。"""

UA = "Mozilla/5.0 (stock_data_fetch)"
TIMEOUT = 20.0

PROBE_TIMEOUT = 8.0
"""探测用的短超时。探测只为回答"这个域名现在还活着吗"，不值得等 20 秒。"""

PROBE_SYMBOL = "sh600519"
"""探测用的代码。**必须是长期存在的大票**，否则"200 但没有数据"会被误判成域名挂了。

用茅台而不是随便一只：它的日 K 永远有，`day` 数组不会空，
所以探测能干净地区分「域名活着」、「域名被 WAF 拦」、「域名活着但这只票没数据」。
"""

PROBE_TTL = 300.0
"""探测结果的保质期（秒）。5 分钟。

太短：每只票都重新探测（4304 只 × 3 域名 = 1.3 万次白打，反而把 WAF 招来）。
太长：域名中途被封，之后几千只票都撞在同一个死域名上（就是这次跑批的翻车方式）。
5 分钟是实测出来的折中——一次跑批约 10 分钟，期间重新探测 2 次，能及时换边。
"""

_host_state: dict[str, tuple[bool, float]] = {}
"""`host -> (是否可用, 上次探测时刻)`。进程内缓存探测结果。"""

_revive_after = PROBE_TTL
"""被拉黑的域名**多久后允许再试**。与 `PROBE_TTL` 同值：既然要定期重探活着的域名，
被封的也一并重探——WAF 封禁是有窗口期的，永久拉黑一个域名就等于自断一条腿。"""


def reset_state() -> None:
    """清空域名健康状态。测试与长跑进程换日时用。"""
    _host_state.clear()

MAX_BARS = 900
"""单次上限。实测 900 根可回，900 之前按 `count` 精确给（320 / 900 都试过）。
要更长得靠 `start`/`end` 分段——但 `kline` 的滚动深度只有 300 根，用不到。"""


def available() -> tuple[bool, str]:
    """无鉴权，恒可用。真要探测就得联网，而 `preflight` 不许依赖网络（§6.5）。"""
    return True, ""


def fetch(domain: str, codes, need: Need) -> Got:
    if domain != "kline":
        raise SourceUnavailable(f"tencent 适配器不支持 {domain} 域", domain=domain)
    # 每轮取数前探一次（有 `PROBE_TTL` 缓存，不会真的每轮都打）。
    # 放在这里而不是 `_kline_one` 里：一轮就是"一次批量"，是最合适的探测时机。
    probe()
    got = Got()
    for sym in codes:
        rows, reason = _kline_one(sym, need)
        if rows:
            got.rows[sym] = rows
        elif reason:
            got.empty_reason = got.empty_reason or reason
    return got


def _kline_one(symbol: str, need: Need) -> tuple[list[dict], str | None]:
    """单只日 K。`param` 的五段是 `代码,周期,起,止,根数,复权`——**第 6 段留空 = 不复权**。

    `count` 给 `need.points + 20`（多要 20 根，让 `min_bars` 那道门槛有缓冲），
    上限 `MAX_BARS`。起止日期也给：补洞时区间可能很短，只给 `count` 会白拿一大段。
    """
    want_bars = int(need.points or 300) + 20
    count = max(1, min(MAX_BARS, want_bars))
    start = need.start or ""
    end = need.end or ""
    param = f"{symbol},day,{start},{end},{count},"
    # **逗号不能被 %2C 编码**：`urlencode` 默认会编码，而这条端点在参数里看到 `%2C`
    # 直接回 `501 Not Implemented`（2026-11-14 真跑时 566 只全部 501，整个 kline 域瘫掉）。
    # 所以这里手工拼查询串，只 `quote` 一次并放行逗号。
    query = "param=" + urllib.parse.quote(param, safe=",:-_")
    body = _get_path(KLINE_PATH, query)

    try:
        payload = json.loads(body)
    except ValueError as e:
        raise SourceUnavailable(f"腾讯返回不是 JSON（{symbol}）：{body[:120]}") from e
    if payload.get("code") not in (0, "0", None):
        raise SourceUnavailable(f"腾讯返回 code={payload.get('code')}（{symbol}）："
                                f"{payload.get('msg')}")
    data = (payload.get("data") or {}).get(symbol)
    if not isinstance(data, dict):
        return [], "no_data"
    # `fq` 留空 → `day`；给了 qfq/hfq 才会出现 `qfqday` / `hfqday`。
    bars = data.get("day")
    if bars is None:
        bars = data.get("qfqday") or data.get("hfqday") or []
    if not bars:
        # 空数组是"代码已迁移/退市/停牌至今"，不是参数错——北交所 `bj430047`（诺思兰德）
        # 就是这样：`qt` 里还有报价，`day` 是空的，因为它的历史在 `bj920xxx` 名下。
        return [], "no_data"

    rows: list[dict] = []
    for b in bars:
        if not isinstance(b, (list, tuple)) or len(b) < 6:
            continue
        day = str(b[0])[:10]
        if len(day) != 10 or day[4] != "-":
            continue
        # 顺序：date, open, close, high, low, volume(手)
        rows.append({"date": day,
                     "open": _f(b[1]), "close": _f(b[2]),
                     "high": _f(b[3]), "low": _f(b[4]),
                     "volume": _f(b[5]) * 100.0,      # 手 → 股
                     "amount": None})                 # 这条通道没有成交额（落库为 0）
    rows.sort(key=lambda r: r["date"])               # 上游已升序，这里只是保险
    return rows, (None if rows else "no_data")


def _url_for(host: str, path: str, query: str) -> str:
    """把域名、路径、查询串拼成完整 URL。`proxy` 那条域名要插一段来源站前缀。"""
    return f"https://{host}{_HOST_PREFIX.get(host, '')}{path}?{query}"


def _alive(host: str, now: float) -> bool:
    """缓存里这个域名是否算可用。过期或不认识 → 先当作"未探明"，排在后面但不排除。

    **未探明不能当死**：把没探过的域名直接排除，第一次调用就无域名可用了。
    """
    got = _host_state.get(host)
    if got is None:
        return True
    ok, at = got
    if now - at > _revive_after:
        return True                      # 过期：重新给它机会，下次失败再记
    return ok


def _ranked_hosts() -> list[str]:
    """按**缓存里的实测健康度**排序：活的在前，死的在后。

    排序而不是过滤，是这次的关键决定：
    - 过滤（死域名剔除）→ WAF 封禁有窗口期，全被拉黑时无域名可用，整源报废；
    - 排序 → 活域名优先被用，死域名垫底只在"实在没别人了"时才试，
      且每 `PROBE_TTL` 秒自动恢复候选资格。WAF 一解封就自己爬回队首。

    同一健康度内保持 `KLINE_HOSTS` 的原顺序（`sort` 是稳定的），
    避免每次调用顺序乱跳、日志对不上。
    """
    now = time.monotonic()
    return sorted(KLINE_HOSTS, key=lambda h: 0 if _alive(h, now) else 1)


def _mark(host: str, ok: bool) -> None:
    """记一次域名健康度。只在"确实打到上游并拿到结论"时调用。"""
    _host_state[host] = (ok, time.monotonic())


def _get_path(path: str, query: str) -> str:
    """按实测健康度依次尝试域名，取到数据就返回。

    流程：排序 → 逐个试 → WAF 命中就记"死"并换下一个 → 全部失败才抛。

    为什么这样写（而不是固定顺序）：见 `KLINE_HOSTS` 的注释。域名级 WAF 封禁会
    **在跑批中途发生**，写死的主域名一旦中途阵亡，后面几千只票就全废。
    排序 + 定期重探让选择权跟着实测走。
    """
    last: SourceUnavailable | None = None
    for host in _ranked_hosts():
        try:
            body = _get(_url_for(host, path, query))
        except SourceUnavailable as e:
            if e.ctx.get("waf"):
                # WAF 拉黑是域名级的：换域名有用，重试/冷却都没用。不记 strike、不抛。
                _mark(host, False)
                log.warning("腾讯 %s 被 WAF 拦截（HTTP %s），改用其它域名",
                            host, e.ctx.get("status", "?"))
                last = e
                continue
            raise
        # 拿到响应不代表域名健康（可能回了非 JSON），但传输层是通的 —— 记活。
        # 真正的业务错误（code!=0、非 JSON）交给调用方，不该因此把域名判死。
        _mark(host, True)
        return body
    if last is not None:
        dead = [h for h in KLINE_HOSTS if _host_state.get(h, (True, 0))[0] is False]
        raise SourceUnavailable(
            f"腾讯全部域名均被 WAF 拦截（{', '.join(dead) or '无'}）",
            transient=False) from last
    raise SourceUnavailable("腾讯没有可用域名", transient=False)


def probe(force: bool = False) -> list[str]:
    """显式探测一轮所有域名，把健康度写进缓存，返回**探测后的排序**。

    为什么需要显式探测：`_get_path` 的排序只能在"已经失败过"之后才准。
    跑批的第一只票面前，三个域名都是"未探明"，只能按初值顺序撞——
    撞上死域名就白丢一只票的配额，还要多等一次往返。

    批量取数前调一次（一次 3 个请求、每个最多 `PROBE_TIMEOUT` 秒），
    后面的几千只票就都跑在活域名上。`need` 里没有"批量开始"这个概念，
    所以由调用方或本模块的 `fetch()` 在首次调用时惰性触发，见 `_probed`。

    `force=True` 无视 `PROBE_TTL` 直接重探（换日、长跑脚本周期性刷）。
    """
    now = time.monotonic()
    if not force and _probed:
        fresh = all(now - _host_state.get(h, (True, 0.0))[1] <= PROBE_TTL
                    for h in KLINE_HOSTS)
        if fresh:
            return _ranked_hosts()

    query = "param=" + urllib.parse.quote(f"{PROBE_SYMBOL},day,,,20,", safe=",:-_")
    for host in KLINE_HOSTS:
        try:
            _get(_url_for(host, KLINE_PATH, query), timeout=PROBE_TIMEOUT)
            _mark(host, True)
        except SourceUnavailable as e:
            _mark(host, False)
            if not e.ctx.get("waf"):
                # 非 WAF 的失败（网络断了、超时）也记死，但只影响排序：
                # 它是瞬时的，过期后自动复活，不会永久丢掉这条腿。
                log.debug("腾讯 %s 探测失败：%s", host, e)
        except Exception as e:                         # noqa: BLE001
            _mark(host, False)
            log.debug("腾讯 %s 探测异常：%s", host, e)
    globals()["_probed"] = True
    order = _ranked_hosts()
    log.info("腾讯域名探测结果（优先→兜底）：%s", " → ".join(order))
    return order


_probed = False
"""本轮探测过没有。惰性探测的闸，避免每只票都探一轮。"""


def _get(url: str, *, timeout: float = TIMEOUT) -> str:
    """抽出来便于测试替身。`RemoteDisconnected` 与 `SSL EOF` 都按**瞬断**上报（transient），
    由 router 决定"重发一次 / 冷却 / 换源"——适配器不自己 sleep、不自己重试。

    **501 是 WAF 拦截页**，不是"接口没实现"：域名被拉黑时 501 的响应体
    是跳转 `waf.tencent.com` 的脚本（见 `KLINE_HOSTS`）。所以把这种 501 标成
    `ctx.waf=True`，让 `_get_path` 去换域名，而不是当作永久错误整源放弃。

    `timeout` 可覆写：`probe()` 用 `PROBE_TIMEOUT` 短超时，不想为探测等满 20 秒。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://gu.qq.com/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        waf = _looks_like_waf(e)
        raise SourceUnavailable(f"腾讯 HTTP {e.code}", status=e.code,
                                transient=e.code in (429, 500, 502, 503, 504),
                                waf=waf) from e
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise SourceUnavailable(f"腾讯连接中断：{e}", transient=True) from e


def _looks_like_waf(e: urllib.error.HTTPError) -> bool:
    """读一小段响应体，看是不是 WAF 拦截页。

    501 之外也认（WAF 可能升到 403/412），但**只对 4xx/501 读体**：
    正常 200 走的是另一条分支，不在这里。读失败就当不是 WAF——宁可当普通错误，
    也不能把一个真·永久错误误判成"换个域名就好了"而无限试下去。
    """
    if e.code not in (403, 412, 418, 501):
        return False
    try:
        head = e.read(2048).decode("utf-8", "replace")
    except Exception:                                  # noqa: BLE001 —— 读不出来就算了
        return False
    return WAF_MARK in head


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
