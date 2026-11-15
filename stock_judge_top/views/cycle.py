"""周期位置卡片（只在判断①判定为周期股时出现）。

为什么要这张卡片
----------------
周期股是顶部判断里最危险的一类：业绩和股价同时见顶，跌下来没有估值托底。
四因子只回答"股价现在在哪、交易挤不挤"，回答不了"这一轮景气走到哪了"。

判定口径照抄 `stock_cycle_elastic`（阈值见 `extras.CYCLE`）：
    判断① 毛利率 σ ≥ 5pp 或 CV ≥ 20%  →  是周期股
    判断② 逐季 营业利润环比 ÷ 营收环比 的中位数（≥2 强、<2 弱、跨零多是「不稳定」）
    判断③ 产能周转率的 40 季分位（<40 低位、>60 高位）

**不给总分、不给买卖结论**：三个判断各自出档位，怎么读由使用者决定。
非周期股时这张卡片不出（判①不成立，②③没有解释力），报告与以前一样只有四张。
"""

from ..extras import CYCLE


def _pct_text(v):
    return "—" if v is None else f"{v:.1f}%"


def cycle_view(r):
    cy = (r.get("extras") or {}).get("cycle") or {}
    if not cy.get("is_cycle"):
        return None

    gm_std, gm_cv = cy.get("gm_std"), cy.get("gm_cv")
    elastic, elastic_label = cy.get("elastic"), cy.get("elastic_label")
    pos_pct, pos_label = cy.get("position_pct"), cy.get("position_label")

    # 色调只看"位置"：高位最值得警惕，低位最不需要担心；弹性与波动只作背景
    tone = {"高位": "warn", "中位": "info", "低位": "muted"}.get(pos_label, "muted")
    state = f"周期·{pos_label}"

    headline = (f"产能周转率处于自身 {_pct_text(pos_pct)} 分位（{pos_label}），"
                f"弹性{elastic_label}")

    cycle_calc = [
        ("判断① 毛利率 σ（20季）", "—" if gm_std is None else f"{gm_std:.2f}pp"),
        ("判断① 毛利率 CV", _pct_text(gm_cv)),
        ("判断① 毛利率中位", _pct_text(cy.get("gm_median"))),
        ("判断② 弹性比值（8季中位）",
         "—" if elastic is None else f"{elastic:.2f}"),
        ("判断② 档位", elastic_label or "—"),
        ("判断③ 产能周转率分位", _pct_text(pos_pct)),
        ("判断③ 档位", pos_label or "—"),
        ("季度样本", f"{cy.get('quarters') or 0} 季"),
    ]

    note = (
        "三个判断是分开的，程序不合并。读法：判断③在高位说明产能利用率处在"
        "自身十年高分位（周期偏热），判断②强说明利润对收入敏感（景气放大），"
        "两者同时成立时，股价同样在 250 日高位就值得警惕。"
        "已知偏差：产能周转率的分子是营收不是产量，产品涨价会让它虚高。"
    )
    if elastic_label == "不稳定":
        note += "本股判断②为「不稳定」：近 8 季有较多季度营业利润跨零，比值只能参考。"

    return {
        "key": "cycle", "title": "周期位置",
        "subtitle": "判断①判定为周期股 → 额外看景气走到哪（不影响上面四张）",
        "state": state, "tone": tone,
        "headline": headline,
        "metrics": [
            ("产能周转率分位", _pct_text(pos_pct)),
            ("周期位置", pos_label or "—"),
            ("弹性", elastic_label or "—"),
            ("毛利率 σ", "—" if gm_std is None else f"{gm_std:.2f}pp"),
        ],
        "calc": cycle_calc,
        "note": note,
        "wide": False,
    }
