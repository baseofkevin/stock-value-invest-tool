# -*- coding: utf-8 -*-
"""按经营范围/行业规则，把 moat_type 粗分一遍（模糊判定，不读公告）。

口径（用户 2026-11-16 明确："简单处理，不需要那么严格，大概、模糊都可以"）
--------------------------------------------------------------------
这是**粗判**，不是核实。判定依据只有两样：`stock.business`（经营范围原文）
和 `stock.industry`（东财一级行业）。规则按"越具体越优先"排序，先命中先定，
后面的不再看——比如"血液制品"命中「配额牌照」后，不会再被"许可证"重复判一次。

已知局限（写在 note 里，别假装没有）
  · **技术壁垒判不出来**：份额数据不在经营范围里。所以未被规则命中的公司
    一律记「无标注」，其中一定漏掉真龙头（宁德时代这类靠规则是抓不到的）。
  · 「独家品种」只有表述里带"独家/保密/中药保护"才抓得到，实际独家品种
    更多藏在药品批件里。
  · 规则没有出现过的类型（网络）不自动判。

用法
----
    python3 scripts/classify_moat.py --dry-run        # 只看分布
    python3 scripts/classify_moat.py                  # 写入（只改「待核实」的行）
    python3 scripts/classify_moat.py --overwrite      # 连旧的结论一起重判
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_data_fetch import annotation as A  # noqa: E402

DB = ROOT / "shared.db"
DAY = "2026-11-16"
NOTE = "规则粗判（经营范围关键词，{day}），未读公告——模糊判定，仅供粗筛"

#: 顺序即优先级。每条是 (类型, 正则, 一句依据)。
#: 踩过的坑（第一版）：
#:   · 把"保险""烟草""危险化学品经营"放进来 → 机场因为"保险兼业代理"被判成牌照、
#:     国贸因为"零售烟草"被判成牌照、包钢因为"危化品经营"被判成牌照。全是附带业务。
#:     所以这些词必须收窄到"专卖/定点/特许"这类**主业性**表述。
#:   · 特许经营要排在配额牌照前面：机场/港口的经营范围里几乎都有"保险兼业代理"。
RULES: list[tuple[str, re.Pattern, str]] = [
    ("特许经营", re.compile(
        r"特许经营|专营权|收费公路|路桥收费|燃气|天然气|供水|自来水|"
        r"供电|电网|机场|港口|码头|索道|景区|教材|"
        r"出版|IPTV|有线电视|广播电视|城市轨道交通|殡葬"),
     "区域排他经营/特许权"),
    #: 「配额」这两个字**不能单独出现**：经营范围里它是进出口套话，而且多是否定式。
    #: 2026-11-16 第一版把裸「配额」放进来，99 只被误判成配额牌照，原文分别是
    #: "涉及配额、许可证管理"（大名城）、"从事**非**配额许可证管理…出口业务"
    #: （生益科技）、"不涉及国营贸易、进出口配额许可证"（江西铜业）——全是反义。
    #: 所以只保留"稀土配额/出口配额"这种真的配额制表述。
    ("配额牌照", re.compile(
        r"免税|血液制品|单采血浆|食盐定点|卷烟生产|"
        r"定点生产|民用爆炸物品|军品|武器装备|核材料|稀土配额|出口配额"),
     "行政许可/配额类准入"),
    ("独家品种", re.compile(
        r"独家品种|独家生产|国家保密|保密配方|中药保护"), "独家品种/保密配方"),
    ("独家资源", re.compile(
        r"采矿权|探矿权|采矿|矿区|资源储量|地质勘查|盐湖|稀土|"
        r"磷矿|锂矿|钾盐|油气田|林权"), "矿产资源/自然垄断"),
]

#: 行业兜底：经营范围写得含糊（"一般项目：…凭营业执照依法自主开展经营"）时，
#: 用行业给一个宽口径的判断。宁可粗，不要空着。
INDUSTRY_FALLBACK = {
    "公用事业": ("特许经营", "公用事业多为区域特许经营（水电气）"),
    "交通运输": ("特许经营", "交通基础设施多为特许经营/收费权"),
    "传媒": ("特许经营", "出版/广电/IPTV 属牌照经营"),
    "银行": ("配额牌照", "金融牌照"),
    "非银金融": ("配额牌照", "金融牌照"),
    "国防军工": ("配额牌照", "军品科研生产许可"),
}


def classify(business: str, industry: str) -> tuple[str, str]:
    """按命中位置加权取最高分的那一类。

    为什么要加权：经营范围里"主业"和"附带业务"混在一起。第一版按规则顺序取第一个
    命中，结果包钢因末尾的"污水处理"被判成特许经营、中国国贸因"零售烟草"被判成
    牌照。经营范围通常把主业写在最前面，所以命中的位置越靠前、越可能是真主业。
    """
    biz = business or ""
    best: tuple[int, int, str, str, str] | None = None   # (score, -order, 类型, 命中词, 依据)
    for order, (mtype, pat, why) in enumerate(RULES):
        score, first_word = 0, ""
        for m in pat.finditer(biz):
            pos = m.start()
            score += 4 if pos < 80 else (2 if pos < 200 else 1)
            if not first_word:
                first_word = m.group(0)
        if score <= 0:
            continue
        cand = (score, -order, mtype, first_word, why)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best:
        _, _, mtype, word, why = best
        return mtype, f"经营范围命中「{word}」：{why}"
    fb = INDUSTRY_FALLBACK.get(industry or "")
    if fb:
        return fb[0], f"行业「{industry}」兜底：{fb[1]}"
    return "无标注", "经营范围未见牌照/特许/独家/资源类表述"


def main() -> int:
    ap = argparse.ArgumentParser(description="按规则粗分 moat_type（模糊判定）")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true",
                    help="连旧结论一起重判（默认只动「待核实」）")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "select s.symbol, s.name, s.industry, s.business, a.moat_type "
        "from stock s left join annotation a on a.symbol=s.symbol and a.kind='moat'").fetchall()

    todo, skipped = [], 0
    for r in rows:
        cur = r["moat_type"]
        if cur and cur != "待核实" and not a.overwrite:
            skipped += 1
            continue
        mtype, why = classify(r["business"] or "", r["industry"] or "")
        todo.append((r["symbol"], r["name"], cur, mtype, why))

    dist = Counter(t[3] for t in todo)
    print(f"待处理 {len(todo)} 只｜跳过已有结论 {skipped} 只"
          + ("（dry-run）" if a.dry_run else ""))
    for k, v in dist.most_common():
        print(f"  {k:6s} {v}")
    print("\n样例：")
    for sym, name, cur, mtype, why in todo[:6]:
        print(f"  {sym} {name[:6]:6s} {cur or '—':5s} → {mtype:5s} {why}")

    if a.dry_run:
        conn.close()
        return 0

    for sym, name, cur, mtype, why in todo:
        A.put(conn, sym, moat_type=mtype, evidence=why,
              verified=None, note=NOTE.format(day=DAY))
    conn.commit()

    print("\n写入后 annotation 现状：")
    for k, v in conn.execute(
            "select moat_type, count(*) from annotation where kind='moat' "
            "group by 1 order by 2 desc"):
        print(f"  {k:6s} {v}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
