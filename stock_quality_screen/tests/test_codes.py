# -*- coding: utf-8 -*-
"""股票代码规则测试：`screen/codes.py`。

这是**全项目唯一一份**号段规则，也是用户输入的第一道关口。
以前同一件事写了三遍（fetch / batch / providers 各一份），加一个号段要改三处；
现在收拢到一个模块，测试的价值就落在这里：**一个号段漏判，四个工具一起错**。

重点盯三件容易错的事：
1. 号段归属 —— 920/4/8 是北交所，6/9 是上交所，其余深交所。920 必须排在 9 前面。
2. 显式前缀优先 —— `sz600519` 号段对不上，但调用方说了算，不按号段重算。
3. 两套输出格式 —— 腾讯要 `sh600519`，东财要 `600519.SH`，别串了。
"""
from __future__ import annotations

import unittest

from tests import helpers  # noqa: F401  （顺带把项目根塞进 sys.path）
from screen import codes


class TestMarketOf(unittest.TestCase):
    """`market_of()`：6 位数字 → 交易所前缀。"""

    def test_shanghai(self):
        for digits in ("600519", "601398", "603288", "605588", "688981", "900901"):
            with self.subTest(digits=digits):
                self.assertEqual(codes.market_of(digits), "sh")

    def test_shenzhen(self):
        for digits in ("000001", "002594", "300750", "301029"):
            with self.subTest(digits=digits):
                self.assertEqual(codes.market_of(digits), "sz")

    def test_beijing(self):
        for digits in ("920001", "430047", "830799", "870508", "400001"):
            with self.subTest(digits=digits):
                self.assertEqual(codes.market_of(digits), "bj")

    def test_920_beats_9_prefix(self):
        """920 是北交所，但 9 开头本应归上交所 —— 顺序错了它会跑到上交所。"""
        self.assertEqual(codes.market_of("920001"), "bj")
        self.assertEqual(codes.market_of("900901"), "sh")

    def test_8_and_4_are_beijing_not_shenzhen(self):
        """4/8 开头若不单独处理会落到「其余 → sz」，北交所股票就查不到。"""
        self.assertEqual(codes.market_of("800001"), "bj")
        self.assertEqual(codes.market_of("400001"), "bj")

    def test_returns_only_known_markets(self):
        for digits in ("600519", "000001", "920001", "430047", "300750"):
            with self.subTest(digits=digits):
                self.assertIn(codes.market_of(digits), codes.MARKETS)


class TestSplit(unittest.TestCase):
    """`split()`：拆成 (显式前缀或 None, 6 位数字)。"""

    def test_bare_digits(self):
        self.assertEqual(codes.split("600519"), (None, "600519"))

    def test_prefixed(self):
        self.assertEqual(codes.split("sh600519"), ("sh", "600519"))
        self.assertEqual(codes.split("SZ000001"), ("sz", "000001"))

    def test_suffix_form(self):
        """东财写法 `600519.SH` 也认，且算「显式指定」。"""
        self.assertEqual(codes.split("600519.SH"), ("sh", "600519"))
        self.assertEqual(codes.split("000001.sz"), ("sz", "000001"))

    def test_bj_suffix(self):
        self.assertEqual(codes.split("920001.BJ"), ("bj", "920001"))

    def test_whitespace_and_case(self):
        self.assertEqual(codes.split("  SH600519  "), ("sh", "600519"))

    def test_unknown_suffix_is_rejected(self):
        """`600519.HK` 不是 A 股写法 → 认不出，返回 None。

        实现里后缀不是 MARKETS 时会 `s.replace(".", "")` 再试一次，
        得到 `600519hk` —— 既不是 8 位带前缀、也不是 6 位纯数字，所以判负。
        """
        self.assertIsNone(codes.split("600519.HK"))
        self.assertIsNone(codes.split("000001.hk"))

    def test_multiple_dots_rejected(self):
        self.assertIsNone(codes.split("a.b.c"))
        self.assertIsNone(codes.split("600.519.SH"))

    def test_dots_are_treated_as_noise(self):
        """去掉后缀后仍会试着把点全部删掉：`60.0519` → `600519`。

        这条看着宽松，但它接的是真实场景：从 Excel 里粘出来的代码常带
        千分位或误打的小数点，能救回来比报错强。风险由「必须正好 6 位」兜住。
        """
        self.assertEqual(codes.split("60.0519"), (None, "600519"))
        self.assertEqual(codes.split("6.0.0.5.1.9"), (None, "600519"))

    def test_invalid(self):
        for bad in ("", None, "abc", "60051", "6005190", "sh60051",
                    "xx600519", "60051a"):
            with self.subTest(bad=bad):
                self.assertIsNone(codes.split(bad))


class TestExplicitPrefixWins(unittest.TestCase):
    """显式前缀不按号段重算 —— 调用方说了算。"""

    def test_mismatched_prefix_is_kept(self):
        """`sz600519` 号段对不上（600 该是 sh），但明确指定了就用 sz。"""
        self.assertEqual(codes.normalize("sz600519"), "sz600519")

    def test_deduces_when_prefix_absent(self):
        self.assertEqual(codes.normalize("600519"), "sh600519")
        self.assertEqual(codes.normalize("000001"), "sz000001")
        self.assertEqual(codes.normalize("920001"), "bj920001")


class TestNormalize(unittest.TestCase):
    """`normalize()`：→ 腾讯用的 `sh600519`。"""

    def test_all_input_forms_converge(self):
        for raw in ("600519", "sh600519", "SH600519", "600519.SH",
                    "600519.sh", " 600519 "):
            with self.subTest(raw=raw):
                self.assertEqual(codes.normalize(raw), "sh600519")

    def test_invalid_raises_with_example(self):
        """报错要带「应该长什么样」，否则用户不知道该怎么改。"""
        with self.assertRaises(ValueError) as cm:
            codes.normalize("abc")
        msg = str(cm.exception)
        self.assertIn("600519", msg)
        self.assertIn("sh600519", msg)

    def test_returns_lowercase_market(self):
        self.assertTrue(codes.normalize("SH600519").startswith("sh"))

    def test_output_shape(self):
        for raw in ("600519", "000001", "920001"):
            with self.subTest(raw=raw):
                out = codes.normalize(raw)
                self.assertEqual(len(out), 8)
                self.assertIn(out[:2], codes.MARKETS)
                self.assertTrue(out[2:].isdigit())


class TestSix(unittest.TestCase):
    def test_strips_prefix(self):
        self.assertEqual(codes.six("sh600519"), "600519")
        self.assertEqual(codes.six("600519.SH"), "600519")
        self.assertEqual(codes.six("000001"), "000001")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            codes.six("nope")


class TestSecucode(unittest.TestCase):
    """东财 datacenter 用的 `600519.SH`。"""

    def test_shapes(self):
        self.assertEqual(codes.secucode("600519"), "600519.SH")
        self.assertEqual(codes.secucode("sh600519"), "600519.SH")
        self.assertEqual(codes.secucode("000001"), "000001.SZ")
        self.assertEqual(codes.secucode("920001"), "920001.BJ")

    def test_market_is_uppercase(self):
        self.assertTrue(codes.secucode("600519").endswith(".SH"))
        self.assertNotIn(".sh", codes.secucode("600519"))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            codes.secucode("abc")


class TestEastmoneyUrl(unittest.TestCase):
    """`eastmoney_url()`：**认不出就返回空串，不抛错**。"""

    def test_sh_sz_url(self):
        self.assertIn("quote.eastmoney.com/sh600519.html",
                      codes.eastmoney_url("600519"))
        self.assertIn("quote.eastmoney.com/sz000001.html",
                      codes.eastmoney_url("000001"))

    def test_bj_uses_path_form(self):
        """北交所是 `/bj/920001.html`，不是 `bj920001.html` —— 后者是 404。"""
        url = codes.eastmoney_url("920001")
        self.assertIn("/bj/920001.html", url)
        self.assertNotIn("bj920001.html", url)

    def test_invalid_returns_empty_not_raise(self):
        """批量里代码列可能混进空行或备注：标「无链接」比整批失败合适。"""
        for bad in ("", None, "abc", "备注", "60051"):
            with self.subTest(bad=bad):
                self.assertEqual(codes.eastmoney_url(bad), "")

    def test_url_is_https(self):
        self.assertTrue(codes.eastmoney_url("600519").startswith("https://"))


class TestConsistency(unittest.TestCase):
    """几个出口必须对同一只股票给出同一家交易所。"""

    def test_market_agrees_across_functions(self):
        for raw in ("600519", "000001", "300750", "688981", "920001",
                    "430047", "900901"):
            with self.subTest(raw=raw):
                norm = codes.normalize(raw)
                secu = codes.secucode(raw)
                url = codes.eastmoney_url(raw)
                market, digits = norm[:2], norm[2:]
                self.assertEqual(secu, f"{digits}.{market.upper()}")
                # 北交所的东财路径是 `/bj/920001.html`，不含 `bj920001`
                if market == "bj":
                    self.assertIn(f"/bj/{digits}.html", url)
                else:
                    self.assertIn(f"/{market}{digits}.html", url)


if __name__ == "__main__":
    unittest.main()
