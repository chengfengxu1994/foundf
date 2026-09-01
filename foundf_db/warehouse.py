"""
DuckDB 数据仓库连接管理器。

提供：
- 单例 Warehouse 类，管理 finance.duckdb 连接
- 表创建/迁移
- 分析视图创建
- 批量插入辅助方法
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb

from .schema import ANALYTIC_VIEWS_SQL, SCHEMA_SQL


class Warehouse:
    """DuckDB 数据仓库连接管理器。

    使用示例:
        w = Warehouse("/data/finance.duckdb")
        w.init()                      # 创建表 + 视图
        w.insert("daily_price", rows) # 批量插入
        df = w.query("SELECT * FROM mv_portfolio_pnl")
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: duckdb.DuckDBPyConnection | None = None

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self._conn = duckdb.connect(str(self.db_path))
        return self._conn

    # ── 初始化和迁移 ──────────────────────────────────────

    def init(self) -> None:
        """初始化仓库：执行 schema DDL 并创建分析视图。"""
        self.conn.execute("SET timezone='Asia/Shanghai'")
        for statement in SCHEMA_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                self.conn.execute(stmt + ";")
        for statement in ANALYTIC_VIEWS_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                self.conn.execute(stmt + ";")

    def table_exists(self, name: str) -> bool:
        rows = self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ? AND table_schema = 'main'",
            [name],
        ).fetchone()
        return rows is not None

    def view_exists(self, name: str) -> bool:
        rows = self.conn.execute(
            "SELECT 1 FROM information_schema.views WHERE table_name = ? AND table_schema = 'main'",
            [name],
        ).fetchone()
        return rows is not None

    # ── 数据操作 ───────────────────────────────────────────

    def insert(self, table: str, rows: list[dict[str, Any]], conflict_strategy: str = "ignore") -> int:
        """批量插入字典列表到指定表。返回插入行数。

        conflict_strategy:
            'ignore' — 冲突时跳过（INSERT OR IGNORE，默认）
            'replace' — 冲突时替换（INSERT OR REPLACE）
        """
        if not rows:
            return 0
        columns = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in columns)
        col_list = ", ".join(columns)
        values = [[row.get(c) for c in columns] for row in rows]
        if conflict_strategy == "replace":
            sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
        else:
            sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
        self.conn.executemany(sql, values)
        return len(values)

    def upsert(self, table: str, rows: list[dict[str, Any]], conflict_columns: list[str]) -> int:
        """带冲突处理的批量插入。"""
        if not rows:
            return 0
        columns = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in columns)
        col_list = ", ".join(columns)
        updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in conflict_columns)
        conflict = ", ".join(conflict_columns)
        values = [[row.get(c) for c in columns] for row in rows]
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        )
        self.conn.executemany(sql, values)
        return len(rows)

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """执行查询并返回字典列表。"""
        if params:
            result = self.conn.execute(sql, params)
        else:
            result = self.conn.execute(sql)
        columns = [desc[0] for desc in result.description] if result.description else []
        return [dict(zip(columns, row)) for row in result.fetchall()]

    def query_df(self, sql: str, params: list[Any] | None = None):
        """执行查询并返回 Pandas DataFrame。"""
        if params:
            return self.conn.execute(sql, params).fetchdf()
        return self.conn.execute(sql).fetchdf()

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        """执行无返回值的 SQL 语句。"""
        if params:
            self.conn.execute(sql, params)
        else:
            self.conn.execute(sql)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ── 全局默认仓库 ──────────────────────────────────────

_DEFAULT_WAREHOUSE: Warehouse | None = None


def get_warehouse(db_path: str | Path | None = None) -> Warehouse:
    """获取/创建默认全局仓库实例。"""
    global _DEFAULT_WAREHOUSE
    if _DEFAULT_WAREHOUSE is None:
        path = db_path or os.getenv("DUCKDB_PATH", "data/finance.duckdb")
        _DEFAULT_WAREHOUSE = Warehouse(path)
        _DEFAULT_WAREHOUSE.init()
    return _DEFAULT_WAREHOUSE
