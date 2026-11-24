# -*- coding: utf-8 -*-
"""`stock_market_trend.store_raw` 的写入边界：只碰临时库，不联网。

单独一个文件，是因为隔壁 `test_stock_market_trend.py` 明说了"不碰库"，
把 SQL 相关的东西塞进去会打破那条约定。

这里钉的是两件很容易写错、错了又很贵的事：

1. **同 ISO 周只留最新那根周线**。周中跑会落一根"当周至今"的半根 bar，
   下一次周五跑完整周落在**新日期**上，upsert 顶不掉它——不清理的话，
   那根只有一天的假周线会永久留在序列中间，把 MA 窗口和 52 周中位数一起带歪。
2. **节假日前那根不能被误删**。它（周三/周四）在自己的 ISO 周里是唯一一行，
   清理规则不该碰到它，更不该碰到别的周。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from stock_market_trend import store_raw


def _connect() -> tuple[sqlite3.Connection, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = store_raw.connect(path)
    # `watermark` 本来是 `stock_data_fetch` 建的，这里给个最小替身。
    conn.executescript(
        "create table if not exists watermark(domain text, scope text, "
        "last_ok_date text, updated_at text, primary key(domain, scope));")
    return conn, path


def _weeks(*pairs: tuple[str, float]) -> list[dict]:
    return [{"date": d, "close": 4000.0, "turnover": t} for d, t in pairs]


def _dates(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "select date from market_index_week where symbol='sh000300' order by date")]


class WeekPartialBarTest(unittest.TestCase):

    def test_partial_bar_is_replaced_by_the_full_week(self):
        """周一那根 0.61% 的半根，下周五必须被完整周顶掉，而不是并存。"""
        conn, path = _connect()
        try:
            store_raw.save_week(conn, _weeks(("2026-11-20", 2.70)))
            store_raw.save_week(conn, _weeks(("2026-11-20", 2.70),
                                             ("2026-11-23", 0.61)))
            self.assertEqual(_dates(conn), ["2026-11-20", "2026-11-23"])
            store_raw.save_week(conn, _weeks(("2026-11-23", 0.61),
                                             ("2026-11-27", 2.85)))
            got = _dates(conn)
        finally:
            conn.close()
            os.remove(path)
        self.assertEqual(got, ["2026-11-20", "2026-11-27"])

    def test_holiday_week_end_is_kept(self):
        """节假日前最后一个交易日（周三/周四）是那一周唯一的 bar，不能被删。"""
        conn, path = _connect()
        try:
            store_raw.save_week(conn, _weeks(("2026-06-12", 2.3),
                                             ("2026-06-18", 3.64),   # 端午前，周四
                                             ("2026-06-26", 5.11)))  # 下一周
            got = _dates(conn)
            total = conn.execute("select count(*) from market_index_week").fetchone()[0]
        finally:
            conn.close()
            os.remove(path)
        self.assertEqual(got, ["2026-06-12", "2026-06-18", "2026-06-26"])
        self.assertEqual(total, 3)

    def test_same_run_twice_is_idempotent(self):
        """同一批数据写两遍不该长出第二行（upsert 不是 insert）。"""
        conn, path = _connect()
        try:
            rows = _weeks(("2026-11-13", 2.46), ("2026-11-20", 2.70))
            store_raw.save_week(conn, rows)
            store_raw.save_week(conn, rows)
            got = _dates(conn)
        finally:
            conn.close()
            os.remove(path)
        self.assertEqual(got, ["2026-11-13", "2026-11-20"])


class DayTableTest(unittest.TestCase):

    def test_day_roundtrip(self):
        """日K 只 upsert，不清同周：一周五根都要在。"""
        conn, path = _connect()
        try:
            rows = [{"date": d, "close": 3900.0 + i, "turnover": 0.5, "pct_chg": 0.1}
                    for i, d in enumerate(["2026-11-16", "2026-11-17",
                                           "2026-11-18", "2026-11-19",
                                           "2026-11-20"])]
            store_raw.save_day(conn, rows)
            store_raw.save_day(conn, rows)
            back = store_raw.read_day(conn)
        finally:
            conn.close()
            os.remove(path)
        self.assertEqual(len(back), 5)
        self.assertEqual(back[0]["date"], "2026-11-16")
        self.assertEqual(back[-1]["turnover"], 0.5)


if __name__ == "__main__":
    unittest.main()
