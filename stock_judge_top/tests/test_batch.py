# -*- coding: utf-8 -*-
"""批量模式测试：读 CSV、代码列识别、去重、参数校验。

只测纯函数与参数校验，**不联网**——批量真正跑一只股票要打上游，那是冒烟测试的事。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from stock_judge_top import batch, cli


def _write(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class ReadTargetsTest(unittest.TestCase):
    def test_header_code_column(self) -> None:
        path = _write("代码,名称\nsh600519,贵州茅台\nsz000001,平安银行\n")
        try:
            targets, warns = batch.read_targets(path)
        finally:
            os.unlink(path)
        self.assertEqual([t["code"] for t in targets], ["sh600519", "sz000001"])
        self.assertEqual(targets[0]["name"], "贵州茅台")
        self.assertEqual(warns, [])

    def test_accepts_alternate_column_names(self) -> None:
        for head in ("股票代码", "证券代码", "code", "Code", "symbol"):
            path = _write(f"{head},名称\n600519,贵州茅台\n")
            try:
                targets, _ = batch.read_targets(path)
            finally:
                os.unlink(path)
            self.assertEqual([t["code"] for t in targets], ["sh600519"], msg=head)

    def test_no_header_uses_first_column(self) -> None:
        path = _write("600519\n000001\n")
        try:
            targets, _ = batch.read_targets(path)
        finally:
            os.unlink(path)
        self.assertEqual([t["code"] for t in targets], ["sh600519", "sz000001"])

    def test_dedupes_keeping_order(self) -> None:
        """同一只出现两次只跑一次，且保留第一次的位置。"""
        path = _write("代码\n600519\n000001\nsh600519\n")
        try:
            targets, warns = batch.read_targets(path)
        finally:
            os.unlink(path)
        self.assertEqual([t["code"] for t in targets], ["sh600519", "sz000001"])
        self.assertTrue(any("重复" in w for w in warns))

    def test_bad_code_is_skipped_not_fatal(self) -> None:
        """一个坏代码不该让整份清单跑不了——记一条警告、继续。"""
        path = _write("代码\n600519\nXXXXXX\n000001\n")
        try:
            targets, warns = batch.read_targets(path)
        finally:
            os.unlink(path)
        self.assertEqual([t["code"] for t in targets], ["sh600519", "sz000001"])
        self.assertTrue(any("跳过" in w for w in warns))

    def test_empty_csv(self) -> None:
        path = _write("")
        try:
            targets, warns = batch.read_targets(path)
        finally:
            os.unlink(path)
        self.assertEqual(targets, [])
        self.assertTrue(warns)

    def test_header_only(self) -> None:
        path = _write("代码,名称\n")
        try:
            targets, _ = batch.read_targets(path)
        finally:
            os.unlink(path)
        self.assertEqual(targets, [])

    def test_returns_bj_and_star_codes(self) -> None:
        """北交所 920/8 开头、科创板 688 都要认对市场前缀。"""
        path = _write("代码\n688016\n920002\n")
        try:
            targets, _ = batch.read_targets(path)
        finally:
            os.unlink(path)
        self.assertEqual([t["code"] for t in targets], ["sh688016", "bj920002"])


class CliGuardTest(unittest.TestCase):
    """参数校验必须发生在取数之前，否则错用会先打一遍上游再报错。"""

    def test_csv_and_code_together(self) -> None:
        self.assertEqual(cli.main(["sh600519", "--csv", "x.csv"]), 2)

    def test_neither_code_nor_csv(self) -> None:
        self.assertEqual(cli.main([]), 2)

    def test_json_with_csv(self) -> None:
        self.assertEqual(cli.main(["--csv", "x.csv", "--json"]), 2)

    def test_help_does_not_touch_network(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
