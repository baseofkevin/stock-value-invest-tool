# -*- coding: utf-8 -*-
"""把「巨潮检索 → 下载 PDF → 抽出命中段落」串起来，给垄断性核实提供原文摘录。

和 `verify_moat.py` 的分工
--------------------------
`verify_moat.py` 只列公告标题和链接（30 秒看完，但常常看不出结论）；
这个脚本往下走一步：把公告 PDF 抓下来、`pdftotext` 转文字、定位关键词所在段落，
打印**原句**。核实垄断性靠的是原句，不是标题。

选哪几篇读
----------
一只票可能命中几十篇公告，全读不现实。排序规则：
  招股说明书 / 募集说明书 / 重大合同 / 许可资质 > 年报 > 其他公告
理由：前几类会把"牌照、特许经营权、独家品种"这些**权利**写清楚，
年报里同一句话往往只是复述。

用法
----
    python3 scripts/moat_evidence.py --symbols sh600660,sh600125
    python3 scripts/moat_evidence.py --batch 1        # 115 条清单的第 1 批（每批 8 只）
    python3 scripts/moat_evidence.py --batch 1 --pages 2   # 每篇最多看 2 处命中
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # scripts 不是包，直接 import 同目录模块

from verify_moat import KEYWORDS, hits  # noqa: E402

DB = ROOT / "shared.db"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

#: 标题里带这些词的公告，把"权利"写得更清楚，优先读。
PRIORITY = ("招股说明书", "募集说明书", "重大合同", "中标", "许可", "资质",
            "特许", "收购", "资产重组", "可行性", "年报", "年度报告")


def rank(h: dict) -> tuple:
    for i, kw in enumerate(PRIORITY):
        if kw in h["title"]:
            return (i, h["date"])
    return (len(PRIORITY), h["date"])


def grab(url: str, dest: Path, timeout: float = 40.0) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r, dest.open("wb") as f:
            shutil.copyfileobj(r, f)
        return dest.stat().st_size > 2048
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def extract(pdf: Path, keywords: list[str], per_kw: int = 2) -> list[str]:
    """pdftotext → 逐页找关键词 → 截前后各 90 字。"""
    try:
        text = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True, text=True, timeout=90).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    pages = text.split("\f")
    out = []
    for kw in keywords:
        n = 0
        for i, page in enumerate(pages, 1):
            flat = re.sub(r"\s+", " ", page)
            pos = flat.find(kw)
            while pos >= 0 and n < per_kw:
                seg = flat[max(0, pos - 90): pos + 110]
                out.append(f"p{i} [{kw}] …{seg.strip()}…")
                n += 1
                pos = flat.find(kw, pos + 1)
            if n >= per_kw:
                break
    return out


def pending_115(conn) -> list[str]:
    """115 条已标垄断（不含「无标注」）的票，买入档排前面。"""
    rows = conn.execute(
        "select symbol from annotation where kind='moat' "
        "and moat_type not in ('无标注','待核实') order by symbol").fetchall()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="巨潮公告原文摘录（垄断性核实辅助）")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--symbols", default="")
    ap.add_argument("--batch", type=int, default=0, help="115 条清单的第 N 批，每批 8 只")
    ap.add_argument("--size", type=int, default=8, help="每批只数")
    ap.add_argument("--pdfs", type=int, default=3, help="每只最多读几篇公告")
    ap.add_argument("--pages", type=int, default=2, help="每个关键词最多摘几处")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

    if a.symbols:
        todo = [s.strip() for s in a.symbols.split(",") if s.strip()]
    elif a.batch:
        all115 = pending_115(conn)
        todo = all115[(a.batch - 1) * a.size: a.batch * a.size]
        print(f"115 条清单共 {len(all115)} 只，第 {a.batch} 批取 {len(todo)} 只")
    else:
        raise SystemExit("给 --symbols 或 --batch")

    tmp = Path(tempfile.mkdtemp(prefix="moatpdf-"))
    try:
        for symbol in todo:
            row = conn.execute(
                "select s.name, a.moat_type, a.evidence from annotation a "
                "left join stock s on s.symbol=a.symbol "
                "where a.symbol=? and a.kind='moat'", (symbol,)).fetchone()
            name = (row["name"] if row else "") or symbol
            mtype = (row["moat_type"] if row else "") or "待核实"
            kws = KEYWORDS.get(mtype, KEYWORDS["待核实"])
            print(f"\n{'='*76}\n{symbol} {name}｜标注 {mtype}｜关键词 {kws}")
            if row and row["evidence"]:
                print(f"  现有 evidence：{row['evidence'][:100]}")

            cands = sorted(hits(symbol, name, kws, 3), key=rank)
            if not cands:
                print("  检索无候选")
                continue
            for h in cands[:a.pdfs]:
                pdf = tmp / f"{h['url'].rsplit('/', 1)[-1]}"
                if not grab(h["url"], pdf):
                    print(f"  [{h['date']}] {h['title'][:50]} → 下载失败")
                    continue
                segs = extract(pdf, kws, a.pages)
                print(f"  [{h['date']}] {h['title'][:56]}")
                print(f"      {h['url']}")
                if not segs:
                    print("      （正文未出现关键词，可能是扫描件或表述不同）")
                for s in segs[:4]:
                    print(f"      · {s}")
                pdf.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
