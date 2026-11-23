"""CSV 参数化数据源（压测参数化）：解析 CSV 文本 + 顺序/随机抽样。

每个接口/步骤可维护一份 CSV（首行为列名，列名即变量名），执行时每次迭代
取一行按列名合并进渲染池——模板中的 {{列名}} 命中 CSV 值，优先于全局
variables（merged pool = {**全局, **csv 行}，见 locust_engine）。

取行语义：
- sequential：游标循环（0→n-1→0…），所有虚拟用户共享同一游标
  （与 JMeter CSV Data Set「所有线程共享」一致，每行恰好被消费一次后轮回）
- random：每次迭代独立随机取行（可能重复，适合无顺序要求的数据扰动）

限制（parse_csv 校验）：单份数据 ≤1MB、≤10000 行、≤20 列、首行列名非空且唯一。
"""

from __future__ import annotations

import csv as _csv
import io
import random
import threading

from scenario.model import CsvSpec

MAX_CSV_BYTES = 1024 * 1024
MAX_ROWS = 10000
MAX_COLUMNS = 20

CSV_MODES = ("sequential", "random")


class CsvParseError(ValueError):
    """CSV 数据非法（文案直接面向表单/任务失败信息）。"""


def parse_csv(text: str, label: str = "CSV") -> tuple[list[str], list[list[str]]]:
    """CSV 文本 → (列名列表, 数据行列表)。空文本返回 ([], [])。

    容忍行尾空行与半角引号包裹值；列数不齐按短行补空串（JMeter 同语义）。
    """
    if text is None or not text.strip():
        return [], []
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise CsvParseError(f"{label} 数据过大（上限 {MAX_CSV_BYTES // 1048576}MB）")
    reader = _csv.reader(io.StringIO(text))
    rows_raw = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows_raw:
        return [], []
    columns = [c.strip() for c in rows_raw[0]]
    if not columns or any(not c for c in columns):
        raise CsvParseError(f"{label} 首行必须为列名（列名即变量名，不允许空列名）")
    if len(set(columns)) != len(columns):
        raise CsvParseError(f"{label} 列名重复：{columns}")
    if len(columns) > MAX_COLUMNS:
        raise CsvParseError(f"{label} 列数不能超过 {MAX_COLUMNS}（当前 {len(columns)}）")
    rows = rows_raw[1:]
    if len(rows) > MAX_ROWS:
        raise CsvParseError(f"{label} 数据行数不能超过 {MAX_ROWS}（当前 {len(rows)}）")
    data = [row[:len(columns)] + [""] * (len(columns) - len(row)) for row in rows]
    return columns, data


class CsvDataset:
    """单接口/步骤的 CSV 数据集：线程安全的逐迭代取行。"""

    def __init__(self, columns: list[str], rows: list[list[str]],
                 mode: str = "sequential"):
        self.columns = columns
        self.rows = rows
        self.mode = mode if mode in CSV_MODES else "sequential"
        self._cursor = 0
        self._lock = threading.Lock()

    @classmethod
    def from_spec(cls, spec: CsvSpec | None, label: str = "CSV") -> CsvDataset | None:
        """CsvSpec → 数据集（空文本返回 None；解析错误抛 CsvParseError）。"""
        if spec is None or not (spec.text or "").strip():
            return None
        columns, rows = parse_csv(spec.text, label)
        if not rows:
            raise CsvParseError(f"{label} 没有数据行（首行列名之后至少 1 行数据）")
        return cls(columns, rows, spec.mode)

    def next_row(self) -> dict[str, str]:
        """取一行 → {列名: 值}；sequential 游标循环，random 随机取。"""
        if not self.rows:
            return {}
        with self._lock:
            if self.mode == "random":
                idx = random.randrange(len(self.rows))
            else:
                idx = self._cursor
                self._cursor = (self._cursor + 1) % len(self.rows)
        row = self.rows[idx]
        return dict(zip(self.columns, row, strict=False))
