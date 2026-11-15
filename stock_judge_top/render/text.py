"""终端报告。"""
from .format import fmt_pct


def _px(v):
    return f"{v:.2f}" if isinstance(v, (int, float)) else "—"


def print_report(r):
    kline = r.get("kline") or {}
    name = f" {r.get('name')}" if r.get("name") else ""
    print("=" * 78)
    print(f"四因子独立观察  |  {r.get('code')}{name}")
    print(f"基准日 {r.get('date')}   K线最后交易日 {kline.get('date') or '—'}   "
          f"收盘 {_px(r.get('close'))}")
    print(f"250日高点 {_px(r.get('high250'))}（{r.get('high250_date') or '—'}，"
          f"距今 {r.get('days_since_high') if r.get('days_since_high') is not None else '—'}"
          f" 个交易日）   距高点 {fmt_pct(r.get('pos250'))}")
    print("说明：以下 4 个因子各自独立展示，程序不合并总分，也不自动判断顶部。")
    for v in r.get("views") or []:
        print("-" * 78)
        print(f"{v.get('title')}：{v.get('state')}  |  {v.get('headline')}")
        for b in v.get("blocks") or []:
            print(f"  [{b.get('title')}] {b.get('subtitle')}")
            for label, value in b.get("results", []):
                print(f"    结果  {label:<12}{value}")
            if b.get("sources"):
                print("    数据来源：")
                for label, value in b["sources"]:
                    print(f"      {label:<20}{value}")
            if b.get("formulas"):
                print("    计算：")
                for formula in b["formulas"]:
                    print(f"      {formula}")
        if v.get("metrics"):
            print("  指标：")
            for label, value in v["metrics"]:
                print(f"    {label:<14}{value}")
        if v.get("calc"):
            print("  计算数据：")
            for label, value in v["calc"]:
                print(f"    {label:<14}{value}")
        print(f"  说明：{v.get('note') or ''}")
    print("-" * 78)
    if r.get("warnings"):
        print("数据与口径提示：")
        for w in r["warnings"]:
            print(f"  · {w}")
    print("=" * 78)
    print("程序只展示 4 个因子的独立结果，不输出总分，不替你做综合判断。")
