# -*- coding: utf-8 -*-
"""外部 CSV 的读取（只读）。

CSV 现在只做一件事：**读出股票代码和名称**。原来的另外两个角色
（回写「综合评分 / HTML报告链接」列、给导航页提供行业等透传列）随 HTML
报告一起在 2026-11-15 删掉了——结果进 results.db，人看结果走 web 看板。

`CsvTable` 仍然保留原始列（`rows` 是完整的 dict），所以以后要在 CLI 里
读某一列不必再改这个模块。
"""
from __future__ import annotations

import codecs
import csv
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config

#: 跳过文案（硬门槛：上市不足 3 年）
SKIP_SCORE_TEXT = f"跳过：上市不足 {config.MIN_ANNUAL_YEARS} 年"


CODE_COLUMN_CANDIDATES = {
    "代码",
    "股票代码",
    "证券代码",
    "股票",
    "code",
    "stock_code",
    "stockcode",
    "symbol",
    "ts_code",
    "sec_code",
}


class CsvError(RuntimeError):
    """CSV 无法读取。"""


@dataclass
class CsvTable:
    """输入 CSV 的内存副本；只在进程内用，从不写回磁盘。"""

    path: Path
    fieldnames: list[str]
    rows: list[dict[str, str]]
    code_column: str


def _clean_header(value: Any) -> str:
    return str(value or "").strip().lstrip("\ufeff")


def _decode(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise CsvError("CSV 编码无法识别（支持 UTF-8 / GB18030）") from exc


def _find_code_column(fieldnames: list[str]) -> str:
    lookup = {_clean_header(name).casefold(): name for name in fieldnames if _clean_header(name)}
    for candidate in CODE_COLUMN_CANDIDATES:
        key = candidate.casefold()
        if key in lookup:
            return lookup[key]
    shown = "、".join(_clean_header(name) for name in fieldnames if _clean_header(name))
    raise CsvError(
        "CSV 中找不到股票代码列（可用表头：代码 / 股票代码 / 证券代码 / code）；"
        f"当前表头：{shown or '（空）'}"
    )


def read_csv_table(path: str | Path) -> CsvTable:
    """读取 CSV，保留原表头与行顺序（只读，不回写）。"""
    p = Path(os.path.expanduser(str(path))).resolve()
    if not p.is_file():
        raise CsvError(f"CSV 文件不存在：{p}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise CsvError(f"CSV 读取失败：{exc}") from exc
    text = _decode(data)

    reader = csv.DictReader(io.StringIO(text, newline=""))
    original_names = list(reader.fieldnames or [])
    if not original_names:
        raise CsvError("CSV 没有表头")
    fieldnames = [_clean_header(name) for name in original_names]
    if not any(fieldnames):
        raise CsvError("CSV 表头为空")
    code_column = _find_code_column(fieldnames)

    rows: list[dict[str, str]] = []
    for raw in reader:
        row: dict[str, str] = {}
        for original, name in zip(original_names, fieldnames):
            value = raw.get(original)
            row[name] = value if isinstance(value, str) else ""
        rows.append(row)
    if not rows:
        raise CsvError("CSV 没有数据行")
    return CsvTable(
        path=p,
        fieldnames=fieldnames,
        rows=rows,
        code_column=code_column,
    )
