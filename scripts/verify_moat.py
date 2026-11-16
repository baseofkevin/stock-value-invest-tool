# -*- coding: utf-8 -*-
"""按票拉巨潮公告候选，给「垄断性核实」提供可点的证据清单。

为什么要工具而不是一只只手查
--------------------------
核实一只票的动作是固定的：拿公司名 + 该垄断类型的关键词去巨潮全文检索，
把命中的公告标题和 PDF 链接列出来，人（或模型）读一遍再写结论。
115 只 × 4~6 个关键词，手查必然漏，也不可复现。

这个脚本**只负责取证，不负责下结论**：它打印候选公告，`--apply` 才写库，
且写库时要求调用方显式给 `--moat-type` 和 `--evidence`，避免"跑一遍就自动
把 4700 只判成无垄断"。

用法
----
    # 看证据（不写库）
    python3 scripts/verify_moat.py --symbol sh600660
    python3 scripts/verify_moat.py --pending --limit 5        # 从「待核实」里挑
    python3 scripts/verify_moat.py --symbol sh600660 --apply \
        --moat-type 技术壁垒 --evidence "汽车玻璃全球份额约34%（2025年报/公告口径）" \
        --source <公告PDF链接>

关键词从哪来
------------
按 `moat_type` 分组的行业惯用语（牌照/配额/专营/独家/采矿权/市占率…）。
巨潮全文检索是"整篇公告匹配"，命中标题不一定含关键词，所以同时列标题与
命中片段，便于判断是不是真证据。
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_data_fetch import annotation as A  # noqa: E402

DB = ROOT / "shared.db"
SEARCH = "http://www.cninfo.com.cn/new/fulltextSearch/full"
PDF = "http://static.cninfo.com.cn/"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": "http://www.cninfo.com.cn/new/fulltextSearch",
    "X-Requested-With": "XMLHttpRequest",
}

#: 每种垄断形态对应的检索词。窄词优先——"垄断""壁垒""专营"这种词满屏都是套话，
#: 命中了也证明不了什么（实测：铁龙物流的"专营"命中的是"国家有专项专营规定的除外"）。
KEYWORDS = {
    "特许经营": ["特许经营权", "特许经营", "收费公路", "BOT", "专营权"],
    "配额牌照": ["经营许可证", "牌照", "配额", "定点生产", "准入资质"],
    "独家资源": ["采矿权", "资源储量", "探矿权", "矿区"],
    "独家品种": ["独家品种", "国家保密", "中药保护品种", "独家生产"],
    "技术壁垒": ["市场占有率", "市场份额", "全球领先", "行业第一"],
    "网络": ["用户规模", "平台生态", "网络效应"],
    "待核实": ["特许经营权", "经营许可证", "独家品种", "市场占有率"],
}


def fetch(url: str, body: bytes, timeout: float = 15.0):
    req = urllib.request.Request(url, data=body, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8", "replace"))


def search(keyword: str, page: int = 1) -> list[dict]:
    body = urllib.parse.urlencode({
        "searchkey": keyword, "sdate": "", "edate": "",
        "isfulltext": "true", "sortName": "pubdate", "sortType": "desc",
        "pageNum": str(page),
    }).encode()
    try:
        d = fetch(SEARCH, body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        print(f"  ! 检索失败 {keyword}: {e}")
        return []
    return d.get("announcements") or []


def hits(symbol: str, name: str, keywords: list[str], per_kw: int) -> list[dict]:
    """逐个「公司名 + 关键词」检索，只留本公司的公告，按 announcementId 去重。

    两个细节都是踩出来的：
    1. 只用关键词检索是全市场排序，本公司公告排在几十页之后——命中率≈0。
       拼上公司名之后，命中数就落到个位数到几十，够看。
    2. **不能只按 secCode 过滤**：A+H 公司（福耀玻璃）返回的是港股代码
       `03606`，拿 A 股 `600660` 比会全军覆没。名称匹配更稳。
    """
    code = symbol[2:]
    out, seen = [], set()
    for kw in keywords:
        for a in search(f"{name} {kw}")[:200]:
            sec_name = (a.get("secName") or "").strip()
            if str(a.get("secCode")) != code and sec_name != name:
                continue
            aid = a.get("announcementId")
            if aid in seen:
                continue
            seen.add(aid)
            out.append({
                "kw": kw,
                "title": (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", ""),
                "date": time.strftime("%Y-%m-%d", time.localtime(
                    (a.get("announcementTime") or 0) / 1000)),
                "url": PDF + (a.get("adjunctUrl") or ""),
            })
            if len(out) >= per_kw * len(keywords):
                return out
        time.sleep(0.2)
    return out


def pick_pending(conn, limit: int) -> list[tuple[str, str, str]]:
    """待核实里挑票：优先已标垄断的（有旧的判断，核实性价比最高）。"""
    rows = conn.execute(
        "select a.symbol, coalesce(s.name, a.symbol), a.moat_type "
        "from annotation a left join stock s on s.symbol=a.symbol "
        "where a.kind='moat' and a.moat_type='待核实' "
        "order by (s.industry is null), a.symbol limit ?", (limit,)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="取巨潮公告证据，辅助垄断性核实")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--symbol", action="append", default=[],
                    help="股票代码，可重复给；也可写 sh600660,sz002594")
    ap.add_argument("--pending", action="store_true", help="从「待核实」里按代码顺序挑")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--per-kw", type=int, default=3, help="每个关键词最多留几条")
    ap.add_argument("--apply", action="store_true", help="把结论写回 annotation")
    ap.add_argument("--moat-type", default=None)
    ap.add_argument("--evidence", default=None)
    ap.add_argument("--source", action="append", default=[], help="证据链接，可重复")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

    symbols: list[str] = []
    for s in a.symbol:
        symbols += [x.strip() for x in s.split(",") if x.strip()]
    todo = [(s, "", "") for s in symbols] if symbols else pick_pending(conn, a.limit)

    for symbol, name, mtype in todo:
        row = conn.execute("select name from stock where symbol=?", (symbol,)).fetchone()
        name = name or (row[0] if row else "") or symbol
        mtype = mtype or (conn.execute(
            "select moat_type from annotation where symbol=? and kind='moat'",
            (symbol,)).fetchone() or ["待核实"])[0]
        kws = KEYWORDS.get(mtype, KEYWORDS["待核实"])
        print(f"\n=== {symbol} {name}｜当前标注：{mtype}｜检索词 {kws}")
        found = hits(symbol, name, kws, a.per_kw)
        if not found:
            print("  （没命中：可能是关键词不对，也可能真没有这类公告——后者本身就是弱证据）")
        for h in found:
            print(f"  [{h['kw']}] {h['date']} {h['title'][:60]}")
            print(f"        {h['url']}")

        if a.apply:
            if not (a.moat_type and a.evidence):
                print("  ! --apply 需要同时给 --moat-type 和 --evidence，这条不写")
                continue
            A.put(conn, symbol, moat_type=a.moat_type, evidence=a.evidence,
                  sources=a.source or [h["url"] for h in found[:2]],
                  verified=time.strftime("%Y-%m-%d"),
                  note="按巨潮公告核实（scripts/verify_moat.py）")
            print(f"  ✓ 已写入：{a.moat_type}")
    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
