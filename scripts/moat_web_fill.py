# -*- coding: utf-8 -*-
"""用东财 F10「核心题材」正文，给「无标注」的票补一个 moat_type（机器判读，不是核实）。

为什么是东财而不是搜索引擎
--------------------------
用户 2026-11-16 的意思是"能快速填上就行，模糊一点没关系"，先试了搜索：
  · 百度 / 搜狗：直连 302 跳验证码，走不通；
  · bing（cn.bing.com）：能通，但摘要就一句"XX 是集研发生产销售为一体的供应商"，
    信息量不足以判断；16 只已知答案的样本几乎全军覆没；
  · 东财 `PC_HSF10/CoreConception/PageAjax`：一次请求拿到整段**年报原文**的
    「核心竞争力」「主营业务」。3600 只 130 秒抓完，且正文里带
    "全球第二""中国第一、全球第四""拥有采矿权""独家品种 3 个"这类硬表述。
所以数据源换成东财，判读仍然只靠关键词——**机器判读，精度有限**。

判读口径
--------
只认"硬"表述，自夸套话（国内领先/行业龙头/知名品牌）一律不算：
  · 技术壁垒：全球第X / 国内第X / 全球唯一 / 市占率 ≥ 两位数百分比 / 寡头垄断
  · 独家资源：拥有采矿权 / 资源储量 / 储量全球第X
  · 配额牌照：金融牌照 / 免税 / 血液制品 / 定点生产 / 军品
  · 独家品种：独家品种 / 独家剂型 / 国家保密 / 中药保护 / 一类新药
  · 特许经营：特许经营权 / 收费公路 / 收费权
  · 网络：网络效应 / 平台生态优势 / 用户数 X 亿
否定句要挡掉：年报里"报告期内公司**无**特许经营权…的取得、变更及**丧失**情形"
这种句子到处都是，不挡就会把没有特许权的公司判成特许经营。

用法
----
    python3 scripts/moat_web_fill.py --dry-run          # 只统计，不写库
    python3 scripts/moat_web_fill.py                    # 写入（只动「无标注」/「待核实」）
    python3 scripts/moat_web_fill.py --limit 200        # 先跑一小批看看
    python3 scripts/moat_web_fill.py --symbols sh600183,sz000001
    python3 scripts/moat_web_fill.py --refresh          # 忽略缓存重新抓

抓下来的正文缓存在 `state/moat_web_cache.jsonl`，重跑不重复请求。
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
import concurrent.futures as cf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_data_fetch import annotation as A  # noqa: E402

DB = ROOT / "shared.db"
CACHE = ROOT / "state" / "moat_web_cache.jsonl"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
DAY = "2026-11-16"
NOTE = f"东财F10核心题材机器判读（{DAY}），未经人工核实"
TODO = ("无标注", "待核实")

#: 顺序即优先级：先认"资源/特许"这类硬权利，再认份额，最后才是网络。
RULES: list[tuple[str, re.Pattern]] = [
    ("独家资源", re.compile(
        r"(拥有|持有|取得|旗下|所属|注入|坐拥)[^。；]{0,12}(采矿权|探矿权|矿山|矿区|矿井|盐湖|锂矿|磷矿|钾盐)|"
        r"(采矿权|探矿权)[^。；]{0,10}\d|"
        r"资源储量[^。；]{0,6}\d|储量[^。；]{0,8}(世界第|全球第|国内第|全国第|第[一二])|"
        r"稀土资源[^。；]{0,6}(世界|全球|全国)第")),
    ("特许经营", re.compile(
        r"特许经营权|收费公路|公路收费权|独家经营权|区域特许|独家特许|"
        r"(拥有|取得|享有)[^。；]{0,10}收费权")),
    ("配额牌照", re.compile(
        r"金融牌照|支付牌照|免税(店|品|业务)|血液制品|单采血浆|食盐定点|"
        r"卷烟生产|定点生产|民用爆炸物品|武器装备|军品(业务|生产|科研)|核材料|稀土配额|出口配额")),
    ("独家品种", re.compile(
        r"独家品种|独家生产|国家保密|中药保护品种|独家剂型|一类新药|独家规格")),
    ("技术壁垒", re.compile(
        r"全球第[一二三四五]|国内第[一二]|全国第[一二]|世界第[一二]|"
        r"全球(最大|唯一)|国内唯一|全国唯一|全球唯一|"
        r"市占率[^。；]{0,6}\d{2}(\.\d+)?%|市场份额[^。；]{0,6}\d{2}(\.\d+)?%|"
        r"(形成|具有|构建|处于)[^。；]{0,6}垄断|垄断(地位|格局|优势)|寡头(格局|垄断)|"
        r"行业标准[^。；]{0,4}制定")),
    ("网络", re.compile(
        r"网络效应|平台生态(优势|壁垒)|(用户|客户)(规模|数)[^。；]{0,6}\d+(\.\d+)?亿")),
]

#: 命中处往前 22 字里出现这些词，说明是"没有/丧失/未取得"——不能算数。
NEG_BEFORE = re.compile(r"不存在|未发生|未取得|未拥有|不涉及|无特许|无任何|没有|丧失|变化情形|等特许类")
#: 授予别人特许经营权（加盟模式）恰恰不是壁垒。
NEG_GRANT = re.compile(r"授予|加盟商?|输出管理|许可他人")


def _clean(x: str) -> str:
    return re.sub(r"\s+", " ", x or "").strip()


def fetch_body(symbol: str) -> str:
    """一次请求拿「核心竞争力」+「主营业务」原文；失败返回空串。"""
    url = ("https://emweb.securities.eastmoney.com/PC_HSF10/"
           f"CoreConception/PageAjax?code={symbol.upper()}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://emweb.securities.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    # 东财对大响应会无视 Accept-Encoding 直接 gzip、响应头也不标，按字节头自判。
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8", "ignore"))
    keep: dict[str, list[str]] = {}
    for it in (data.get("hxtc") or []):
        k = (it.get("KEY_CLASSIF") or "").strip()
        v = (it.get("MAINPOINT_CONTENT") or "").strip()
        # 「行业背景」讲的是行业景气，不是公司壁垒，不要。
        if k in ("核心竞争力", "主营业务") and v:
            keep.setdefault(k, []).append(v)
    return "\n".join("\n".join(v) for v in keep.values())


def judge(body: str) -> tuple[str, str]:
    """返回 (moat_type, 命中原句)；判不出返回 ("无标注", "")。"""
    if not body:
        return "无标注", ""
    for mtype, pat in RULES:
        for m in pat.finditer(body):
            s, e = max(0, m.start() - 40), min(len(body), m.end() + 50)
            sent = _clean(body[s:e].replace("\n", " "))
            if NEG_BEFORE.search(body[max(0, m.start() - 22):m.start()]):
                continue
            if NEG_GRANT.search(body[max(0, m.start() - 12):m.end() + 12]):
                continue
            return mtype, sent
    return "无标注", ""


def load_cache() -> dict[str, str]:
    got: dict[str, str] = {}
    if not CACHE.exists():
        return got
    for line in CACHE.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(line)
            got[o["symbol"]] = o["body"]
        except ValueError:
            continue
    return got


def _one(row: sqlite3.Row) -> tuple[str, str]:
    try:
        return row["symbol"], fetch_body(row["symbol"])
    except Exception:
        return row["symbol"], ""


def main() -> int:
    ap = argparse.ArgumentParser(description="用东财核心题材给「无标注」补 moat_type")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新抓正文")
    ap.add_argument("--symbols", default="", help="逗号分隔，只处理这几只")
    ap.add_argument("--include-judged", action="store_true",
                    help="连已有结论的行一起重判（默认只动 无标注/待核实）")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

    sql = ("select s.symbol, s.name from stock s "
           "join annotation an on an.symbol=s.symbol and an.kind='moat'")
    args: list[Any] = []
    if a.symbols:
        syms = [x.strip() for x in a.symbols.split(",") if x.strip()]
        sql += f" where s.symbol in ({','.join('?' * len(syms))})"
        args += syms
    elif not a.include_judged:
        sql += f" where an.moat_type in ({','.join('?' * len(TODO))})"
        args += list(TODO)
    sql += " order by s.symbol"
    if a.limit:
        sql += f" limit {a.limit}"
    rows = conn.execute(sql, args).fetchall()

    cache = {} if a.refresh else load_cache()
    todo = [r for r in rows if r["symbol"] not in cache]
    print(f"目标 {len(rows)} 只｜待抓 {len(todo)} 只｜缓存命中 {len(rows) - len(todo)} 只")

    t0 = time.time()
    if todo:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with cf.ThreadPoolExecutor(max_workers=a.workers) as ex, \
                CACHE.open("a", encoding="utf-8") as fp:
            for i, (sym, body) in enumerate(ex.map(_one, todo), 1):
                cache[sym] = body
                fp.write(json.dumps({"symbol": sym, "body": body},
                                    ensure_ascii=False) + "\n")
                if i % 500 == 0:
                    print(f"  抓取 {i}/{len(todo)}  {time.time()-t0:.0f}s", flush=True)
        print(f"抓取完成，耗时 {time.time()-t0:.0f}s")

    verdicts, empty = [], 0
    for r in rows:
        body = cache.get(r["symbol"]) or ""
        if not body:
            empty += 1
        mtype, ev = judge(body)
        verdicts.append((r["symbol"], r["name"], mtype, ev))

    dist = Counter(v[2] for v in verdicts)
    hit = [v for v in verdicts if v[2] != "无标注"]
    print(f"\n无正文 {empty} 只｜判出类型 {len(hit)}/{len(verdicts)}"
          + ("（dry-run，不写库）" if a.dry_run else ""))
    for k, v in dist.most_common():
        print(f"  {k:6s} {v}")

    print("\n命中样例：")
    for sym, name, mtype, ev in hit[:10]:
        print(f"  {sym} {(name or '')[:6]:6s} {mtype:5s}｜{ev[:80]}")

    if a.dry_run:
        conn.close()
        return 0

    for sym, name, mtype, ev in hit:
        A.put(conn, sym, moat_type=mtype,
              evidence=ev[:300],
              sources=[f"https://emweb.securities.eastmoney.com/PC_HSF10/"
                       f"CoreConception/Index?type=web&code={sym.upper()}"],
              note=NOTE)
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
