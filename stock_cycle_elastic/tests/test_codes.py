"""代码归一化测试（README.md「测试」）。

重点在**冲突写法**：`sz600519` 这种前缀与号段矛盾的输入，
必须保留显式前缀而不是按号段"聪明地"纠正 —— 用户明确写了 sz，
可能是刻意指定某个市场的同号代码；猜错比照抄更糟。
"""

import unittest

import tests.helpers  # noqa: F401
from cycle import codes


class TestMarketByNumber(unittest.TestCase):
    """纯 6 位数字按号段推断。"""

    def test_shanghai(self):
        for raw in ("600519", "688111", "900901", "601899", "603259"):
            with self.subTest(raw=raw):
                self.assertEqual(codes.market(raw), "sh", raw)

    def test_shenzhen(self):
        for raw in ("000001", "300750", "301101", "002594", "000858"):
            with self.subTest(raw=raw):
                self.assertEqual(codes.market(raw), "sz", raw)

    def test_beijing(self):
        for raw in ("830799", "430047", "920001", "871981"):
            with self.subTest(raw=raw):
                self.assertEqual(codes.market(raw), "bj", raw)

    def test_920_checked_before_9(self):
        """920 开头属于北交所，不能被 9 开头的"上交所"规则抢走 —— 规则顺序的关键。"""
        self.assertEqual(codes.market("920001"), "bj")
        self.assertEqual(codes.market("900901"), "sh")


class TestExplicitPrefix(unittest.TestCase):

    def test_prefix_forms(self):
        cases = (("sh600519", "sh"), ("sz000001", "sz"), ("bj920001", "bj"))
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(codes.market(raw), want)
                self.assertEqual(codes.parse(raw), raw[2:])

    def test_uppercase_prefix(self):
        self.assertEqual(codes.market("SH600519"), "sh")
        self.assertEqual(codes.parse("SH600519"), "600519")

    def test_conflicting_prefix_wins(self):
        """**回归测试**：sz600519 前缀与号段矛盾 → 保留 sz，不按号段改成 sh。

        如果实现"聪明"地纠正，这条会红。
        """
        self.assertEqual(codes.market("sz600519"), "sz")
        self.assertEqual(codes.secucode("sz600519"), "600519.SZ")
        # 对照：纯数字仍按号段推断
        self.assertEqual(codes.market("600519"), "sh")
        self.assertEqual(codes.secucode("600519"), "600519.SH")


class TestSuffix(unittest.TestCase):

    def test_suffix_forms(self):
        cases = (("600519.SH", "sh"), ("000001.SZ", "sz"), ("920001.BJ", "bj"))
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(codes.market(raw), want)
                self.assertEqual(codes.parse(raw), raw[:6])

    def test_lowercase_suffix(self):
        self.assertEqual(codes.market("600519.sh"), "sh")
        self.assertEqual(codes.parse("600519.sh"), "600519")

    def test_suffix_overrides_number_rule(self):
        """后缀与号段矛盾时同样保留后缀。"""
        self.assertEqual(codes.market("600519.SZ"), "sz")


class TestInvalid(unittest.TestCase):

    def test_raises_on_bad_input(self):
        bad = ("", "   ", "60051", "6005191", "abcdef", None, "600519.HK",
               "sh6005190", "600519.", ".600519", "600519.XX", "sh 600519")
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    codes.parse(raw)

    def test_error_message_contains_hint(self):
        with self.assertRaises(ValueError) as ctx:
            codes.parse("abc")
        self.assertIn("应为", str(ctx.exception))

    def test_is_valid_does_not_raise(self):
        self.assertTrue(codes.is_valid("600519"))
        self.assertTrue(codes.is_valid("sh600519"))
        self.assertFalse(codes.is_valid("60051"))
        self.assertFalse(codes.is_valid(None))
        self.assertFalse(codes.is_valid("abc"))


class TestDerivedForms(unittest.TestCase):

    def test_secucode(self):
        self.assertEqual(codes.secucode("600519"), "600519.SH")
        self.assertEqual(codes.secucode("000001"), "000001.SZ")
        self.assertEqual(codes.secucode("920001"), "920001.BJ")

    def test_with_prefix(self):
        self.assertEqual(codes.with_prefix("600519"), "sh600519")
        self.assertEqual(codes.with_prefix("000001"), "sz000001")

    def test_six_alias(self):
        self.assertEqual(codes.six("sh600519"), "600519")
        self.assertEqual(codes.six("600519.SH"), "600519")

    def test_roundtrip(self):
        for raw in ("600519", "000001", "920001", "300750"):
            with self.subTest(raw=raw):
                once = codes.secucode(raw)
                self.assertEqual(codes.parse(once), raw)
                twice = codes.with_prefix(once)
                self.assertEqual(codes.parse(twice), raw)

    def test_whitespace_tolerated(self):
        self.assertEqual(codes.parse("  600519  "), "600519")

    def test_integer_input(self):
        """CSV 里 pandas 读出来的可能是 int —— 不能被静默吞掉。"""
        self.assertEqual(codes.parse(600519), "600519")


class TestEastmoneyUrl(unittest.TestCase):
    """东方财富个股页 URL。

    拼错的 URL 不会抛异常，只会让用户点了跳到 404 —— 所以这条得逐字测。
    """

    def test_shanghai(self):
        self.assertEqual(
            codes.eastmoney_url("600519"),
            "https://quote.eastmoney.com/sh600519.html#fullScreenChart")

    def test_shenzhen(self):
        self.assertEqual(
            codes.eastmoney_url("000001"),
            "https://quote.eastmoney.com/sz000001.html#fullScreenChart")
        self.assertEqual(
            codes.eastmoney_url("300750"),
            "https://quote.eastmoney.com/sz300750.html#fullScreenChart")

    def test_beijing_uses_slash_path(self):
        """北交所是 `/bj/920001.html`，**不是** `bj920001.html`。

        沪深用前缀拼接、北交所用子路径，这是东方财富历史遗留的不一致。
        写成统一拼接的话北交所全部 404。
        """
        self.assertEqual(
            codes.eastmoney_url("920001"),
            "https://quote.eastmoney.com/bj/920001.html#fullScreenChart")

    def test_input_forms_are_equivalent(self):
        """带前缀、带后缀、纯数字 —— 都要拼出同一个 URL。"""
        expect = "https://quote.eastmoney.com/sh600519.html#fullScreenChart"
        for raw in ("600519", "sh600519", "SH600519", "600519.SH",
                    "600519.sh", " 600519 "):
            with self.subTest(raw=raw):
                self.assertEqual(codes.eastmoney_url(raw), expect)

    def test_invalid_code_returns_empty(self):
        """非法代码返回空串，**不抛异常** —— 索引页的失败行也要能渲染。"""
        for bad in ("", None, "abc", "12345", "1234567"):
            with self.subTest(raw=bad):
                self.assertEqual(codes.eastmoney_url(bad), "")

    def test_always_https(self):
        """必须 https：`http://` 会被浏览器当混合内容拦，或被中间人劫持。"""
        self.assertTrue(codes.eastmoney_url("600519").startswith("https://"))

    def test_fullscreen_anchor_present(self):
        """锚点让页面直接落到全屏 K 线，省一次点击。"""
        self.assertTrue(codes.eastmoney_url("600519").endswith("#fullScreenChart"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
