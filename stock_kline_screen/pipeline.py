"""选股主流程：准备 → 横截面 → 排除集 → K 线 → 裁决 → 标注 → 写盘。

流程骨架与原版 `kline_screen/pipeline.py` 一致，但每一步的取数都换成了共享库：

| 步骤 | 原版（westock CLI + JSON 缓存） | 本版（共享库） |
|---|---|---|
| 日历 | `trade-calendar`，4 年 | `hub.cal`（12 年，深交所权威腿） |
| 横截面 | 逐日 `screen condition`（服务端按规则筛） | `hub.scan_many`（**全市场**，本地过滤涨幅区间） |
| ST | 服务端 `risk_st` 标签 | `scan` 行里的 `name` 含 "ST" |
| 次新 | 服务端四桶标签并集 | `stock.list_date`（profile 域，**惰性**：只查裁决后的幸存者） |
| 日 K | 自管缓存 + 批量重试 + 二分 | `hub.ensure_many("kline", ...)`（配额/退避/覆盖全归 hub） |
| 行情/市值 | `quote --date` + `screen NegotiableMV` | `hub.ensure_many("quote", ...)`（当期一份） |
| 财务 | `data_finance` 三表 | `hub.ensure_many("finance", ...)` |
| 行业 | `data_sector` 申万表 | `stock.industry`（profile 域，上游一级口径） |
| 净流入/市值比 | `MainNetFlow@d*` / 流通市值@d* | **砍掉**（见模块尾「口径差异」） |

盘中闸沿用 hub 的语义（闸 1：读库不拦、联网才拦），收盘闸由 `S <= hub.target`
表达：`hub.target` 在收盘宽限之后才推到今天，所以基准日是未来日时这里会拒绝。
"""

from __future__ import annotations

import datetime as dt
import os
import sys

from stock_data_fetch.errors import HoleUnreachable, InsufficientDepth
from stock_data_fetch.need import want

from .params import (C2_TOL, C3_RATIO, KLINE_BARS,
                     KLINE_MIN_BARS, NEW_STOCK_YEARS,
                     TURN_WINDOW)
from .rules import find_start, judge, reason_cat
from .turnover import apply_turnover
from .output import print_summary, turn_cell, turn_star
from .turnaround import compute_turnaround


class CliError(Exception):
    """可直接展示给用户的参数/环境错误。"""


# ---------------------------------------------------------------- 入口

def run(a, hub, *, into: dict | None = None) -> int:
    """执行一次选股；返回进程退出码。`hub` 由 cli.py 的 preflight 给出。

    `into`：把这一次运行的内部状态（粗筛池、剔除账本、名单……）回填给调用方。
    原来是靠 `result_<S>.json` 把这些东西落盘再读回来验的，JSON 删掉之后
    测试与复盘都改成从这里拿——**不落盘也能看**，而且拿到的是同一份内存对象，
    不存在"盘上的和跑出来的不一致"。
    """
    st = {"a": a, "hub": hub}
    _prepare(st)
    _load_cross(st)
    _load_labels(st)
    _load_kline(st)
    _select(st)
    _check_new(st)
    _enrich_market(st)
    _annotate_turnaround(st)
    _annotate_industry(st)
    _write(st)
    if not getattr(st["a"], "quiet", False):
        print_summary(st)
    else:
        # 全流程（run.py all）末尾会起看板：名单在「选股」页看得更清楚，
        # 终端只留一句结论。单跑 select 时不加 --quiet，报表照旧。
        print(f"\n粗筛池 {st['pool_n']} 只 → 入选 {len(st['final'])} 只")
    hub.prune_scan()                       # 收尾顺手清过期的横截面，失败不抛
    hub.prune_kline()                      # 同上：K 线只留最近 N 根，失败不抛
    if into is not None:
        into.update(st)
    return 0


# ---------------------------------------------------------------- 1 准备

def _prepare(st) -> None:
    a, hub = st["a"], st["hub"]
    if a.n < 0:
        raise CliError(
            f"错误：--n 必须 ≥ 0（当前 {a.n}）；每周用 4、每 3 日用 2、每日用 0")
    if a.chg_lo > a.chg_hi:
        raise CliError(f"错误：--chg-lo {a.chg_lo} > --chg-hi {a.chg_hi}")
    if not (0 <= a.turn_pct < 1):
        raise CliError(
            "错误：--turn-pct 必须在 [0,1)（0=关闭；0.2/0.4/0.6=保留池内最低比例）")
    if a.turn_min < 0 or a.turn_max < a.turn_min:
        raise CliError(
            f"错误：--turn-min/--turn-max 不合法（{a.turn_min}/{a.turn_max}）")

    days = hub.cal.days
    S = a.date or hub.target
    if S not in days:
        prev = max((d for d in days if d <= S), default=None)
        raise CliError(f"错误：基准日 {S} 不是交易日；最近交易日为 {prev or '无'}")
    # 收盘闸：hub.target 只在收盘宽限（15:05）之后才推到当天，
    # 所以「S 晚于 target」只剩一种可能——用户把 --date 填到了未来。
    if S > hub.target:
        raise CliError(
            f"错误：基准日 {S} 晚于当前可取到的交易日 {hub.target}。"
            f"当日数据要到收盘后（{hub.cfg.market_close[0]:02d}:"
            f"{hub.cfg.market_close[1]:02d}）才是定稿，此时选出的名单不可信")

    hist = [d for d in days if d <= S]
    if len(hist) < a.n + 1:
        raise CliError(f"错误：基准日 {S} 之前交易日不足（需要 {a.n+1} 天）")
    need = hist[-(a.n + 1):]
    span = f"{need[0]}_{need[-1]}"
    print(f"      基准日 {S}，扫描周期 K={a.n+1}，覆盖 {need[0]} ~ {need[-1]}"
          f"（{len(need)} 个交易日）")
    st.update({"S": S, "hist": hist, "need": need, "span": span})


# ---------------------------------------------------------------- 2 横截面

def _load_cross(st) -> None:
    a, hub, need = st["a"], st["hub"], st["need"]
    print(f"[2/6] 横截面粗筛（{len(need)} 天）...", flush=True)
    # 全市场横截面一次一天；涨跌幅过滤在本地做（共享库不做任何规则过滤）。
    scans = hub.scan_many(need)
    cross_by_day, pool_set = {}, set()
    for d in need:
        kept = {}
        for r in scans[d]:
            chg = r.get("pct_chg")
            if chg is None:
                continue
            if a.chg_lo - 1e-9 <= chg <= a.chg_hi + 1e-9:
                kept[r["symbol"]] = r
        cross_by_day[d] = kept
        pool_set.update(kept)
        print(f"      {d}: {len(scans[d])} 只有价，{len(kept)} 只达标")

    # 北交所 30% 涨跌幅：CHG_HI=20.5 不覆盖，达标的 bj 票会在 20.5~30% 被砍掉。
    bj_n = sum(1 for d in need for c in cross_by_day[d] if str(c).startswith("bj"))
    if bj_n:
        sys.stderr.write(
            f"      ⚠️ 粗筛池出现 {bj_n} 个北交所代码（bj*）：当前 --chg-hi="
            f"{a.chg_hi}% 不覆盖北交所 30% 涨跌幅，请改用 --chg-hi 30.5\n")

    st.update({"cross_by_day": cross_by_day,
               "pool": sorted(pool_set), "pool_n": len(pool_set)})


# ---------------------------------------------------------------- 3 排除集（ST）

def _load_labels(st) -> None:
    """ST 标签从横截面的名称里来——**零额外请求**，而且是"d* 当日"的名称。

    原版用服务端 `risk_st` 标签。名字含 "ST" 覆盖 ST / *ST / S*ST / SST 全部形态；
    正常 A 股名称是纯中文，误伤概率可以忽略。

    次新**不在这里**：判次新要 `list_date`（profile 域），对全池（可能 4000 只）
    逐只取太贵；而裁决走到 ⑤ 之前池子已被 ①②③④ 砍到几十只——
    挪到 `_check_new` 惰性求值。
    """
    need, hub = st["need"], st["hub"]
    print("[3/6] 排除集（ST 从名称判，次新惰性到裁决后）...", flush=True)
    # scan 的结果不挂在 st 上（一天 5000 行，挂五天就是 25000 行的引用链），
    # 这里按日只读重查——命中覆盖后零请求。
    st_by_day = {}
    for d in need:
        rows = hub.scan_cached(d)
        st_by_day[d] = {r["symbol"] for r in rows
                        if "ST" in (r.get("name") or "")}
    print(f"      ST {len(st_by_day[need[-1]])} 只（按名称，{need[-1]}）")
    st["st_by_day"] = st_by_day


# ---------------------------------------------------------------- 4 K 线

def _load_kline(st) -> None:
    hub, pool, S = st["hub"], st["pool"], st["S"]
    cross_by_day = st["cross_by_day"]
    print(f"[4/6] 日K线：候选并集 {len(pool)} 只 ...", flush=True)
    need_k = want.kline(hub.cal, S, bars=KLINE_BARS, min_bars=KLINE_MIN_BARS)

    # ① 批量预取（2026-11-15）：一次把整批交给 westock MCP 的多码通道——
    # 与原版 kline-screen-tool 同一条通道（它的 CLI 就是 `kline,".join(码表)"`）。
    # 逐只 `ensure_many` 会被压在 ~1 只/秒（单码请求 + 东财断连/腾讯 WAF 冷却），
    # 4304 只的池子根本跑不完。预取只写库不判定；成功的股票 coverage 已命中。
    pre = hub.ensure_bulk("kline", pool, need_k)
    km = {sym: r.rows for sym, r in pre.ok.items()}
    if pre.ok:
        print(f"      批量预取（westock 多码）：{len(pre.ok)} 只落库")

    # ② 兜底：预取没拿到的（停牌/次新/限频）逐只 ensure，拿正式的异常分类
    # （InsufficientDepth/HoleUnreachable → depth_bad；其余 → hard）。
    rest = [c for c in pool if c not in km]
    batch = hub.ensure_many("kline", rest, need_k) if rest else None
    if batch:
        km.update({sym: r.rows for sym, r in batch.ok.items()})

    # 失败分两类，处置完全不同：
    #
    # **历史不够**（`InsufficientDepth` / `HoleUnreachable`）——次新股只有几十根、
    # 长期停牌股中间有洞。这不是"取数失败"，上游就该给这么多；原版用服务端四桶次新
    # 标签把这类股票提前剔掉，本版靠 `MIN_HISTORY=70` 在 `find_start` 里剔。
    # 把它们当致命错误会**整跑中止**：2026-11-14 真跑时 `sh601091`（上市两个月）
    # 就是 36 根，直接把 566 只的候选池打断。
    #
    # **取不到**（`PartialData` / `ThrottleTimeout` / 源不可达）——那是另一回事，
    # 基准日有成交却拿不到 K 线，绝不能当成停牌放过去。
    failed: dict[str, Exception] = dict(pre.failed)
    if batch:
        failed.update(batch.failed)
    failed = {s: e for s, e in failed.items() if s not in km}   # 兜底成功的不算失败
    depth_bad: dict[str, Exception] = {}
    hard: dict[str, Exception] = {}
    for sym, exc in failed.items():
        (depth_bad if isinstance(exc, (InsufficientDepth, HoleUnreachable))
         else hard)[sym] = exc

    if depth_bad:
        print(f"      ⚠️ {len(depth_bad)} 只历史不足（次新/长期停牌），按「K线不足」剔除："
              f"{', '.join(sorted(depth_bad)[:8])}"
              f"{'…' if len(depth_bad) > 8 else ''}")
    if hard:
        print(f"      ⚠️ {len(hard)} 只 K 线取数失败（已剔除）："
              f"{', '.join(sorted(hard)[:8])}{'…' if len(hard) > 8 else ''}")
    hit_cnt = sum(1 for c in pool if km.get(c))
    print(f"      命中 {hit_cnt}/{len(pool)}")

    critical = sorted(set(hard) & set(cross_by_day[S]))
    if critical:
        raise RuntimeError(
            f"基准日有成交但取不到 K 线：{critical[:8]}"
            f"{'…' if len(critical) > 8 else ''}。"
            f"不能按停牌处理，请重跑或检查上游。")
    st.update({"km": km, "km_missing": sorted(hard), "depth_bad": depth_bad})


# ---------------------------------------------------------------- 5 裁决

def _select(st) -> None:
    a, need, S, pool = st["a"], st["need"], st["S"], st["pool"]
    cross_by_day, st_by_day = st["cross_by_day"], st["st_by_day"]
    km = st["km"]
    depth_bad = st.get("depth_bad") or {}
    print("[5/6] 逐条裁决 ...", flush=True)
    hit, rej_raw, rej_code = [], {}, {}
    for c in pool:
        # 历史不够的股票不去 find_start：一来它必然返回 None（没有 bar），二来
        # 报告里要能看出"是被上游数据深度剔的，不是被规则剔的"。
        if c in depth_bad:
            why0 = "K线历史不足（上游只有部分历史）"
            rej_raw[why0] = rej_raw.get(why0, 0) + 1
            rej_code[c] = ["FS", why0]
            continue
        name = next((cross_by_day[d][c].get("name") for d in need
                     if c in cross_by_day[d]), "")
        det, why0 = find_start(c, name, km.get(c, []), S)
        if det is None:
            rej_raw[why0] = rej_raw.get(why0, 0) + 1
            rej_code[c] = ["FS", why0]
            continue
        # 次新集合先传空：list_date 要等幸存者出来才批量取（见 _check_new）。
        ok, why = judge(det, km[c], S, a.n, cross_by_day, st_by_day, {},
                        a.k_vol, a.ma60_gap, C2_TOL, C3_RATIO,
                        a.chg_lo, a.chg_hi)
        if ok:
            hit.append(det)
        else:
            rej_raw[why] = rej_raw.get(why, 0) + 1
            rej_code[c] = ["JD", why]
    st.update({"hit": hit, "rej_raw": rej_raw, "rej_code": rej_code, "rej": {}})


# ---------------------------------------------------------------- 5b 次新复检

def _check_new(st) -> None:
    """对裁决幸存者批量取 profile，用 `list_date` 判次新（上市不足 3 年）。

    原版的四桶标签（5 天/3 月/1 年/3 年）并集就是「未满 3 年」；
    `list_date` 是静态事实，d* 日与 S 日用同一判定（3 年边界跨 5 天窗口的
    概率可忽略——真跨了，原版按日标签也会把两边判成不同结果，未必更对）。

    profile 取回来顺便给后面两步用：`industry`（行业标注）、
    `is_financial`（毛利率「不适用」判定），一份三用。
    """
    hub, hit, S = st["hub"], st["hit"], st["S"]
    st["profile"] = {}
    if not hit:
        return
    print(f"      次新复检：对 {len(hit)} 只幸存者取 profile ...", flush=True)
    batch = hub.ensure_many("profile", [x["code"] for x in hit], want.profile())
    profile = {sym: r.rows[0] for sym, r in batch.ok.items() if r.rows}
    if batch.failed:
        print(f"      ⚠️ {len(batch.failed)} 只 profile 取数失败"
              f"（次新/行业/金融判定按「未知」处理）："
              f"{', '.join(sorted(batch.failed)[:8])}")
    st["profile"] = profile

    cutoff_new = (dt.date.fromisoformat(S)
                  - dt.timedelta(days=int(NEW_STOCK_YEARS * 365.25))).isoformat()
    drop = []
    for x in hit:
        p = profile.get(x["code"])
        listed = (p or {}).get("list_date")
        if listed and listed > cutoff_new:
            drop.append(x)
    for x in drop:
        why = f"⑤ 次新（{profile[x['code']]['list_date']} 上市，未满{NEW_STOCK_YEARS:g}年）"
        st["rej_raw"][why] = st["rej_raw"].get(why, 0) + 1
        st["rej_code"][x["code"]] = ["JD", why]
    st["hit"] = [x for x in hit if x not in drop]
    if drop:
        print(f"      次新剔除 {len(drop)} 只")


# ---------------------------------------------------------------- 6 行情/换手

def _enrich_market(st) -> None:
    a, hub, S, hist = st["a"], st["hub"], st["S"], st["hist"]
    cross_by_day, km = st["cross_by_day"], st["km"]
    hit = st["hit"]
    print("[6/6] 取行情 / 计算换手 / 写结果 ...", flush=True)
    turn_meta = {"启用": False, "原因": "无候选"}
    final, rej2 = [], {}
    q: dict = {}
    turn_pool_n = 0

    if hit:
        # **整个粗筛池**都要流通市值，不只是入选股。换手分位是「池内分位」——
        # 只有池里每只都算得出换手，分位才有意义。原先只给入选股取 quote、
        # 却拿 pool 建 cap_snapshot，池内非入选股的 circ_mv 恒为 None、
        # 换手恒为 (None,0)，于是分母缩成了「入选名单」：
        # 池内样本 ≈ 入选数，`--turn-pct` 在名单 <20 只时永远不触发硬过滤。
        # 2026-11-14 与原版对照（11-13 一 期粗筛池 4302 只）才暴露出来。
        #
        # 代价是请求数从「入选数/40」变成「池内股票数/40」（566 只 ≈ 15 批）；
        # quote 的批次已经由 hub 的适配器一次 40 只聚合，不是每股一个请求。
        # **必须走 ensure_bulk，不能 ensure_many**（2026-11-15 修）：
        # `ensure_many` 是"逐只 ensure"的循环，而 quote 的适配器本身支持 40 只/请求
        # （`westock._quote` 里有 `for i in range(0, len(codes), BATCH)`）——逐只
        # 调用等于把这个能力整个丢掉：4304 只 = 4304 次单码请求，必然把服务端打成
        # 限频（真跑实测："quote sh603130 失败：data_quote 上游多次未返回数据"连刷）。
        # `ensure_bulk` 把整批交给适配器，请求数降到 ⌈4304/40⌉ ≈ 83 次。
        pool_codes = list(st["pool"])
        need_q = want.quote(S)
        hub.ensure_bulk("quote", pool_codes, need_q)   # 只负责"把缺的补进库"
        # 再从库里统一读一遍：`ensure_bulk` 的 `Batch.ok` **只含本次真去抓的那些**，
        # 命中的一只也不返回（它没有 `ensure` 那种"从库里读出来"的语义）。
        # 直接拿 `ok` 当结果，会让"库里已有 1035 只"这批凭空消失——
        # 实测症状是"入选股流通市值缺失 38/38 只"，而库里明明存着。
        q = {}
        for sym in pool_codes:
            rows = hub.read("quote", sym, need_q).rows
            if rows:
                q[sym] = rows[0]

        # 入选股缺市值 → 中止（名单里的换手列会错）。
        # 池内非入选股缺市值 → 只让它退出分位分母，不中止：它不影响任何入选股
        # 的换手值，只影响分位的分母规模，而分母规模已经打印出来了。
        missing_cap = [x["code"] for x in hit
                       if not (q.get(x["code"]) or {}).get("circ_mv")]
        if missing_cap:
            raise RuntimeError(
                f"入选股流通市值缺失 {len(missing_cap)}/{len(hit)} 只（如 "
                f"{missing_cap[:5]}）。换手率无法计算，"
                f"终止以免产出不可信名单。")
        pool_missing = len(pool_codes) - len(q)
        if pool_missing:
            sys.stderr.write(
                f"      ⚠️ 池内 {pool_missing}/{len(pool_codes)} 只取不到行情，"
                f"退出换手分位分母（分位基于剩余 {len(q)} 只计算）\n")

        # 展示用「最新收盘价」直接取 S 日横截面的 close——口径精确对齐基准日，
        # 不受 quote 是「当期一份」的影响（补跑历史日时 quote 里存的其实是
        # 最新交易日的市值，close 用 scan 的更准）。
        scan_s = {r["symbol"]: r for r in hub.scan_cached(S)}

        cap_snapshot = {c: (q.get(c) or {}).get("circ_mv") for c in pool_codes}
        cal20 = hist[-TURN_WINDOW:]
        kept, turn_meta, dropped = apply_turnover(
            hit, st["pool"], km, cap_snapshot, cal20,
            a.turn_pct, a.turn_min, a.turn_max)
        if turn_meta.get("启用"):
            for code, why in dropped:
                rej2[why] = rej2.get(why, 0) + 1
                st["rej_code"][code] = ["TN", why]
            hit = kept
            print(f"      换手过滤：{turn_meta['保留档位']}，剔除线 "
                  f"{turn_meta['剔除线%']}%（池内 {turn_meta['池内样本']} 只），"
                  f"剔除 {len(dropped)} 只、保留 {len(kept)} 只；"
                  f"{turn_meta['自适应']}")
            if len(kept) < a.turn_min:
                sys.stderr.write(
                    f"      ⚠️ 换手过滤后仅 {len(kept)} 只（<{a.turn_min}）："
                    f"可加 --turn-pct 0.6 再放宽，或接受本期名单偏少\n")
        else:
            hit = kept
            print(f"      换手标注：池内 {turn_meta.get('池内样本', 0)} 只，"
                  f"{turn_meta['原因']}；名单按换手分位升序排列")
        turn_pool_n = turn_meta.get("池内样本", 0)

        for x in hit:
            r = q.get(x["code"], {})
            cap = r.get("circ_mv")
            tmv = r.get("total_mv")
            chg = (cross_by_day.get(x["d_star"], {}).get(x["code"]) or {}).get("pct_chg")
            x.update({
                "chg": chg,
                "r20": round(x["close"] / x["ma20"] * 100 - 100, 2),
                "r60": round(x["close"] / x["ma60"] * 100 - 100, 2),
                "m20_m60": round(x["ma20"] / x["ma60"], 4),
                "float_cap_yi": round(cap / 1e8, 2) if cap else None,
                "total_cap_yi": round(tmv / 1e8, 2) if tmv else None,
                # S 日横截面的收盘价：与基准日严格同日（quote 的 price 在补跑
                # 历史日时会是最新交易日的价，不能用）。
                "price": (scan_s.get(x["code"]) or {}).get("close"),
            })
            final.append(x)

    for k, v in rej2.items():
        st["rej_raw"][k] = st["rej_raw"].get(k, 0) + v
    for why, n in st["rej_raw"].items():
        k = reason_cat(why)
        st["rej"][k] = st["rej"].get(k, 0) + n

    # 计数断言：pool 里每只股票有且只有一种去向（入选 / 某一阶段被剔）。
    n_rej = sum(st["rej_raw"].values())
    final_codes = {x["code"] for x in final}
    pool = st["pool"]
    if len(pool) != len(final) + n_rej:
        raise RuntimeError(
            f"计数不自洽：粗筛池 {len(pool)} ≠ 入选 {len(final)} + 剔除 {n_rej}")
    if set(st["rej_code"]) & final_codes:
        raise RuntimeError(
            f"同一只股票既入选又被剔除：{sorted(set(st['rej_code']) & final_codes)[:5]}")
    if set(st["rej_code"]) | final_codes != set(pool):
        miss = sorted(set(pool) - (set(st["rej_code"]) | final_codes))
        extra = sorted((set(st["rej_code"]) | final_codes) - set(pool))
        raise RuntimeError(
            f"逐股去向不完整：池中无去向 {miss[:5]}；池外多出 {extra[:5]}")
    st.update({"q": q, "turn_meta": turn_meta, "final": final,
               "turn_pool_n": turn_pool_n})


# ---------------------------------------------------------------- 7 业绩转变标注

def _annotate_turnaround(st) -> None:
    hub, final, S = st["hub"], st["final"], st["S"]
    profile = st["profile"]
    turn, turn_failed = None, []
    if final:
        print("      业绩转变标注（finance 域）...")
        # `periods=44` 不是为了本工具（业绩转变只要 8 期），是为了**别把下游饿死**：
        # 覆盖率只记 `n`，而 `hit()` 只比 `n >= min_points`。这里是整条流水线里
        # 第一个碰 `finance` 域的，若只要 20 期，之后 `stock_cycle_elastic` 的判断③
        # （产能周转率 10 年分位，要 40 个「最近 4 季之和」样本 = 43 期）就会安静地
        # 退化成 4~5 年分位——报告照出，只是标签悄悄变了。
        # `min_points` 仍是 20：次新股本来就凑不满，不该因此判死。
        batch = hub.ensure_many("finance", [x["code"] for x in final],
                                want.finance(S, periods=44, min_points=20))
        fin = {sym: r.rows for sym, r in batch.ok.items() if r.rows}
        turn_failed = sorted(batch.failed)
        if not fin and turn_failed:
            print("      ⚠️ 财务数据获取失败：「毛利率改善」列留空（选股结果不受影响）",
                  file=sys.stderr)
            turn = None
        else:
            is_fin = {c: bool((profile.get(c) or {}).get("is_financial"))
                      for c in fin}
            turn = compute_turnaround(fin, is_fin)
            if turn_failed:
                print(f"      ⚠️ {len(turn_failed)} 只财务数据缺失，标「无数据」："
                      f"{', '.join(turn_failed[:8])}"
                      f"{'…' if len(turn_failed) > 8 else ''}", file=sys.stderr)
    for x in final:
        m = (turn or {}).get(x["code"])
        if m is None:
            x.update({"turn_p1": "无数据" if turn is not None else "",
                      "turn_detail": None, "turn_gm": ""})
            continue
        x["turn_p1"] = m["p1_label"]
        x["turn_detail"] = m
        x["turn_gm"] = m.get("gm_desc") or ""
    if turn is not None and final:
        order = ["是", "否", "无数据", "不适用"]
        cnt: dict[str, int] = {}
        for x in final:
            v = x.get("turn_p1") or "无数据"
            cnt[v] = cnt.get(v, 0) + 1
        print("      ①毛利率环比改善：" + " ｜ ".join(
            f"{k} {cnt[k]} 只" for k in order if cnt.get(k)))
    # 补跑历史基准日时的已知限制（turnaround.py 模块头第 2 条）：
    if final and S != hub.target:
        sys.stderr.write(
            f"      ⚠️ 补跑历史基准日（{S}）：毛利率标注用的是库里最新报告期，"
            f"未按披露日回溯（当期运行无此问题）\n")
    st.update({"turn": turn, "turn_failed": turn_failed})


# ---------------------------------------------------------------- 8 行业标注

def _annotate_industry(st) -> None:
    final, profile = st["final"], st["profile"]
    for x in final:
        x["sw1"] = (profile.get(x["code"]) or {}).get("industry") or "未分类"
    unknown = [x["code"] for x in final if x["sw1"] == "未分类"]
    if unknown:
        sys.stderr.write(
            f"      ⚠️ {len(unknown)} 只无行业归属"
            f"（{', '.join(unknown[:5])}{'…' if len(unknown) > 5 else ''}），"
            f"CSV 记「未分类」\n")


# ---------------------------------------------------------------- 9 落库

def _write(st) -> None:
    """只落 results.db，不再产出 CSV。

    CSV 原来有两个角色：给人看，和给下游三个工具当输入。2026-11-15 起两个
    都被 results.db 接走（`--from-select` 读同一批 `select_result`），
    于是这里只保留三件事：排好顺序、计数、写库。
    """
    final = st["final"]
    # 展示顺序：池内换手分位升序（最冷在前）；分位 None 的排最后。
    final.sort(key=lambda x: (x.get("turn20_rank") is None,
                              x.get("turn20_rank") or 0.0, x["code"]))
    st.update({"n_csv": len(final)})
    _save_results(st, final, st["S"])


def _save_results(st, final, S) -> None:
    """把入选名单写进结果库（`x` 就是名单里那个 dict，不另做映射层）。"""
    try:
        from stock_data_fetch import results
    except Exception:                            # noqa: BLE001
        return
    rows = [{
        "symbol": x["code"], "name": x.get("name"),
        "industry": x.get("sw1"), "launch_date": x.get("d_star"),
        "days_since_launch": x.get("age"), "vol_multiple": x.get("vcross"),
        "launch_gain_pct": x.get("chg"), "ma20_gap_pct": x.get("r20"),
        "ma60_gap_pct": x.get("r60"),
        "gross_margin_up": turn_cell(x.get("turn_p1", ""), x.get("turn_gm", "")),
        "turnover20": x.get("turn20"), "pool_turnover_rank": x.get("turn20_rank"),
        "pool_size": st.get("pool_n"), "stars": turn_star(x.get("turn20_rank")),
        "close": x.get("price"), "market_cap_yi": x.get("total_cap_yi"),
        "payload": x,
    } for x in final]
    results.save("select", rows, S, argv="stock_kline_screen",
                 note=f"粗筛池 {st.get('pool_n')} 只 / 入选 {len(final)} 只")
