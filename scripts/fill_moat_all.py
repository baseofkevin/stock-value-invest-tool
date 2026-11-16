# -*- coding: utf-8 -*-
"""全市场 moat 记录铺底 + 客观字段补全（可重复跑，幂等）。

为什么要这一步
--------------
`annotation` 现在只有 320 条，而 A 股有 5000 多只。缺记录在页面上表现为
「—」，和「查过、确实没有」看起来一样——把"缺口"和"结论"混在一起，
正是这批标注最容易被误读的地方。

所以这里给全市场每只票建一条记录，但 `moat_type` 写 **「待核实」**：
它不是结论（不加分、报告里不渲染垄断块），只表示"有记录、没核过"。
等逐只用巨潮/交易所公告核完，再改成 6 类垄断或「无标注」。

哪些字段是"网络能拿到的"
------------------------
两个东财 F10 接口，一只票两次请求：

  1. `CompanySurvey/PageAjax`      → 行业(EM2016)、经营范围、公司简介、董事长/法人
  2. `ShareholderResearch/PageAjax` → `sjkzr`：实际控制人 + 持股比例

这两样是**客观登记信息**，可以直接落库。`evidence`（垄断依据）不填——
那是判断，必须来自公告原文。
巨潮（cninfo）在这里只用来挂 `sources`？不用：F10 的两个链接本身就是
这些字段的出处，写清楚比挂一个"看起来像证据"的公告链接更诚实。

不覆盖已有记录
--------------
已经有 moat 记录的票（320 只，含 113 条人工核实、207 条批量初标）**一律跳过**，
所以这个脚本可以反复跑，只补缺口。

用法
----
    python3 scripts/fill_moat_all.py --dry-run --limit 5   # 先看 5 只写成什么样
    python3 scripts/fill_moat_all.py                       # 全量，约 1 万次请求
    python3 scripts/fill_moat_all.py --workers 8 --limit 500
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_data_fetch import annotation as A  # noqa: E402

DB = ROOT / "shared.db"
TODAY = dt.date.today().isoformat()

SURVEY = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={code}"
HOLDER = "https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax?code={code}"
PAGE = ("https://emweb.securities.eastmoney.com/PC_HSF10/{page}/Index?type=web&code={code}")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://emweb.securities.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

NOTE = "全市场铺底（{day}）：实控人/行业/主营取自东财 F10 登记信息；垄断性待逐只核实"


def fetch_json(url: str, timeout: float = 10.0, retries: int = 2):
    """容错取 JSON：失败重试，最终失败返回 None（缺字段比整批挂掉好）。

    东财对**大响应**会无视 `Accept-Encoding` 直接 gzip，不按 `Content-Encoding`
    老实标注——上面那 45 只就是这么失败的（前 5000 只小响应是明文，让人以为
    通道没问题）。所以这里按字节头 `\\x1f\\x8b` 自己判，不信响应头。
    """
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ValueError, OSError, gzip.BadGzipFile):
            if i == retries:
                return None
            time.sleep(0.4 * (i + 1) + random.random() * 0.3)
    return None


def em_code(symbol: str) -> str:
    """`sh600519` → `SH600519`（东财 F10 的 code 参数）。"""
    return symbol[:2].upper() + symbol[2:]


def one(symbol: str, fallback_name: str = "") -> dict:
    """抓一只票的客观字段。返回 `ok=False` 表示两次请求都废了。

    `fallback_name` 来自 `scan` 表——F10 偶尔限流，但行情里一定有名字，
    不能让 `stock.name` 落成代码本身（那会让页面显示 "sh600609"）。
    """
    code = em_code(symbol)
    survey = fetch_json(SURVEY.format(code=code)) or {}
    holder = fetch_json(HOLDER.format(code=code)) or {}

    mine = (survey.get("jbzl") or [{}])[0]
    ctrl_row = (holder.get("sjkzr") or [{}])[0]

    industry = ""
    em2016 = (mine.get("EM2016") or "").strip()
    if em2016:                      # "食品饮料-饮料-白酒" → 取一级
        industry = em2016.split("-")[0].strip()

    return {
        "symbol": symbol,
        "name": (mine.get("SECURITY_NAME_ABBR") or "").strip() or fallback_name,
        "industry": industry,
        "business": (mine.get("BUSINESS_SCOPE") or "").strip(),
        "controller": (ctrl_row.get("HOLDER_NAME") or "").strip(),
        "ratio": ctrl_row.get("HOLD_RATIO"),
        "ok": bool(mine),
        "sources": [PAGE.format(page="CompanySurvey", code=code),
                    PAGE.format(page="ShareholderResearch", code=code)],
    }


def upsert_stock(conn, rec: dict) -> None:
    """只补**缺的**票，不动已有 320 行的行业/主营（那是申万口径，别混）。

    唯一的例外：上一轮取数失败会把 `name` 落成代码本身（`sh600609`），
    这里顺手用 scan 的名称修回来。
    """
    sym = rec["symbol"]
    row = conn.execute("select name from stock where symbol=?", (sym,)).fetchone()
    if row is not None:
        if (not row[0] or row[0] == sym) and rec["name"] and rec["name"] != sym:
            conn.execute("update stock set name=?, updated_at=? where symbol=?",
                         (rec["name"], TODAY, sym))
        return
    conn.execute(
        "INSERT INTO stock(symbol,name,exch,setcode,industry,is_financial,"
        "list_date,updated_at,last_access_at,sector,business) "
        "VALUES(?,?,?,?,?,0,NULL,?,NULL,NULL,?)",
        (sym, rec["name"] or sym, sym[:2].upper(), 1 if sym.startswith("sh") else 0,
         rec["industry"] or None, TODAY, rec["business"] or None))


def write_annotation(conn, rec: dict) -> None:
    note = NOTE.format(day=TODAY)
    if not rec["ok"]:
        note += "；这只票 F10 取数失败，字段缺失"
    A.put(conn, rec["symbol"],
          moat_type="待核实",
          evidence=None,
          controller=rec["controller"] or None,
          sources=rec["sources"],
          verified=None,          # 明确不写日期：没核实过就不能挂核实日期
          note=note)


def main() -> int:
    ap = argparse.ArgumentParser(description="全市场 moat 铺底（待核实）+ 客观字段补全")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试用）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    ap.add_argument("--refresh-failed", action="store_true",
                    help="只重跑上次取数失败的票（note 里带「取数失败」）")
    ap.add_argument("--fill-controller", action="store_true",
                    help="只给 controller 为空的票补实控人（一次请求/只）")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

    day = conn.execute("select max(date) from scan").fetchone()[0]
    if not day:
        raise SystemExit("scan 表是空的，先跑一次选股把全市场横截面落库")
    universe = [r[0] for r in conn.execute(
        "select symbol from scan where date=? order by symbol", (day,))]
    # 名称兜底表：F10 限流时也不能让 stock.name 变成代码。
    scan_name = {r[0]: (r[1] or "") for r in conn.execute(
        "select symbol, name from scan where date=?", (day,))}

    if a.fill_controller:
        todo = [r[0] for r in conn.execute(
            "select symbol from annotation where kind='moat' "
            "and (controller is null or controller='') order by symbol")]
        print(f"补实控人：{len(todo)} 只（只发 ShareholderResearch 一次请求）")
    elif a.refresh_failed:
        todo = [r[0] for r in conn.execute(
            "select symbol from annotation where kind='moat' "
            "and note like '%取数失败%' order by symbol")]
        print(f"重跑上次失败的 {len(todo)} 只")
    else:
        have = {r[0] for r in conn.execute(
            "select symbol from annotation where kind='moat'")}
        todo = [s for s in universe if s not in have]
    if a.limit:
        todo = todo[:a.limit]

    total_have = conn.execute(
        "select count(*) from annotation where kind='moat'").fetchone()[0]
    print(f"横截面 {day}：全市场 {len(universe)} 只｜已有 moat 记录 {total_have} 只"
          f"｜本次要铺 {len(todo)} 只｜并发 {a.workers}"
          + ("（dry-run，不写库）" if a.dry_run else ""))

    done = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(one, s, scan_name.get(s, "")): s for s in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            rec = fut.result()
            if rec["ok"]:
                done += 1
            else:
                fail += 1
            if a.dry_run:
                if i <= 5:
                    print(f"\n[{i}] {rec['symbol']} {rec['name']}｜行业 {rec['industry']}"
                          f"｜实控人 {rec['controller'] or '（接口无）'}"
                          f"｜主营 {rec['business'][:60]}…")
                    print(f"     sources {rec['sources'][0]}")
            elif a.fill_controller:
                # 只补实控人：不动 moat_type / note，避免把已经判好的类型覆盖回「待核实」。
                if rec["controller"]:
                    A.put(conn, rec["symbol"], controller=rec["controller"])
                    done += 1
                if i % 50 == 0:
                    conn.commit()
                    print(f"  {i}/{len(todo)}  有实控人 {done}  接口无 {i - done}", flush=True)
            else:
                upsert_stock(conn, rec)
                write_annotation(conn, rec)
                if i % 50 == 0:
                    conn.commit()
                    rate = i / max(time.time() - t0, 1e-6)
                    print(f"  {i}/{len(todo)}  成功 {done} 失败 {fail}  "
                          f"{rate:.1f} 只/秒  剩余约 {(len(todo)-i)/max(rate,1e-6)/60:.0f} 分钟",
                          flush=True)
    if not a.dry_run:
        conn.commit()

    print(f"\n完成：成功 {done} 只，失败 {fail} 只，用时 {(time.time()-t0)/60:.1f} 分钟")
    if not a.dry_run:
        rows = conn.execute(
            "select moat_type, count(*) from annotation where kind='moat' "
            "group by 1 order by 2 desc").fetchall()
        print("annotation 现状：")
        for k, v in rows:
            print(f"  {k:8s} {v}")
        print(f"stock 表 {conn.execute('select count(*) from stock').fetchone()[0]} 只")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
