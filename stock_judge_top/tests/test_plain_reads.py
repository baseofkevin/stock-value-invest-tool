"""extras.plain_reads：旁证表第三列"一句话解读"的阈值断言。

解读和数值同源（同一份 extras），这里锁的是**分界行为**：
分位 30/70、距目标价 ±2%、命中/提示的措辞档位。改阈值时这里要跟着改。
"""
import unittest

from ..extras import plain_reads


class PlainReadsTest(unittest.TestCase):
    def test_valuation_band(self):
        def ex(p):
            return {"valuation": {"metrics": {
                "pe": {"percentile": p}, "pb": {"percentile": p},
                "ps": {"percentile": p}}}}
        self.assertIn("偏低", plain_reads(ex(10))["valuation"])
        self.assertIn("合理", plain_reads(ex(50))["valuation"])
        self.assertIn("偏高", plain_reads(ex(85))["valuation"])
        # 无分位（样本不足）只留白，不编结论
        self.assertEqual(plain_reads(ex(None))["valuation"], "—")

    def test_consensus_upside(self):
        self.assertIn("低于机构目标价 36.1%",
                      plain_reads({"consensus": {"upside": 0.361}})["consensus"])
        self.assertIn("高于机构目标价 5.0%",
                      plain_reads({"consensus": {"upside": -0.05}})["consensus"])
        self.assertIn("基本一致",
                      plain_reads({"consensus": {"upside": 0.01}})["consensus"])
        self.assertEqual(plain_reads({})["consensus"], "—")

    def test_risk_hit_watch(self):
        hit = {"risk": {"hit": 2, "watch": 1, "items": [1]}}
        self.assertIn("2 项命中", plain_reads(hit)["risk"])
        watch = {"risk": {"hit": 0, "watch": 1, "items": [1]}}
        self.assertIn("未见明显反证", plain_reads(watch)["risk"])
        clean = {"risk": {"hit": 0, "watch": 0, "items": [1]}}
        self.assertIn("未见反证", plain_reads(clean)["risk"])

    def test_cycle(self):
        non = {"cycle": {"is_cycle": False, "position_pct": 95}}
        self.assertIn("非周期股", plain_reads(non)["cycle"])
        hot = {"cycle": {"is_cycle": True, "position_pct": 85}}
        self.assertIn("偏热", plain_reads(hot)["cycle"])
        cold = {"cycle": {"is_cycle": True, "position_pct": 12}}
        self.assertIn("偏冷", plain_reads(cold)["cycle"])
        self.assertEqual(plain_reads({})["cycle"], "—")


if __name__ == "__main__":
    unittest.main()
