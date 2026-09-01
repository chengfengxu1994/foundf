"""
universe_manager — 全市场指数成分管理（Point-in-Time）。

表结构:
    universe_membership(
        index_code  VARCHAR,   -- 'csi300' / 'csi500' / 'csi1000' / 'all_a'
                               -- 'hsi' / 'hk_connect' / 'sp500' / 'nasdaq100'
        symbol      VARCHAR,   -- 与 daily_price 一致的规范代码（A股6位数字 / '0700.HK'）
        in_date     DATE,      -- 进入成分的日期（首次被快照观察到的日期）
        out_date    DATE NULL, -- 移出日期（NULL = 当前仍是成分）
        source      VARCHAR,   -- 'akshare' / 'baostock' / 'static_csv'
        updated_at  TIMESTAMP
    )

Point-in-Time 语义（防未来数据泄露的硬约束）:
    - get_members(index_code, date) 只返回 in_date <= date < out_date 的记录；
      快照采集**绝不回填历史**：首次快照的所有成分 in_date 一律记为快照日，
      快照日之前的成分构成是"未知"，而不是今天的名单。
    - 成分变动通过相邻快照差分得到：新进写 in_date=快照日，
      移出仅在既有记录上填 out_date=快照日，历史区间不被改写。

网络不可用时的降级:
    load_static_snapshots() 从 universe_manager/data/*.csv 导入手工快照，
    格式见 data/README.md。
"""

from __future__ import annotations

import csv
import logging
from datetime import date as date_type
from pathlib import Path
from typing import Iterable

from foundf_db import Warehouse

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

#: 指数清单：code -> (市场, 说明)
INDICES: dict[str, tuple[str, str]] = {
    # A股
    "csi300": ("A", "沪深300"),
    "csi500": ("A", "中证500"),
    "csi1000": ("A", "中证1000"),
    "all_a": ("A", "全部A股"),
    # 港股
    "hsi": ("HK", "恒生指数"),
    "hk_connect": ("HK", "港股通标的"),
    # 美股
    "sp500": ("US", "标普500"),
    "nasdaq100": ("US", "纳斯达克100"),
}

DDL = """
CREATE TABLE IF NOT EXISTS universe_membership (
    index_code  VARCHAR NOT NULL,
    symbol      VARCHAR NOT NULL,
    in_date     DATE NOT NULL,
    out_date    DATE,
    source      VARCHAR NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (index_code, symbol, in_date)
);
CREATE INDEX IF NOT EXISTS idx_universe_index_date
    ON universe_membership(index_code, in_date, out_date);
"""


class UniverseManager:
    """指数成分的采集、维护与 Point-in-Time 查询。"""

    def __init__(self, warehouse: Warehouse):
        self.warehouse = warehouse
        for stmt in DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                self.warehouse.execute(stmt + ";")

    # ── PIT 查询 ────────────────────────────────────────

    def get_members(self, index_code: str, date: str) -> list[str]:
        """返回 date 当日 index_code 的成分列表（point-in-time）。

        仅使用 in_date <= date 且 (out_date 为空或 out_date > date) 的区间；
        快照日之前的成分不会被今天已知的名单回填。
        """
        rows = self.warehouse.query(
            "SELECT symbol FROM universe_membership "
            "WHERE index_code = ? AND in_date <= ? "
            "AND (out_date IS NULL OR out_date > ?) "
            "ORDER BY symbol",
            [index_code, date, date],
        )
        return [r["symbol"] for r in rows]

    def list_indices(self) -> list[dict]:
        """返回库中已有成分记录的指数及当前成分数。"""
        return self.warehouse.query(
            "SELECT index_code, "
            "COUNT(*) FILTER (out_date IS NULL) AS active_members, "
            "MIN(in_date) AS earliest_in_date, MAX(in_date) AS latest_in_date "
            "FROM universe_membership GROUP BY index_code ORDER BY index_code"
        )

    # ── 快照差分写入 ────────────────────────────────────

    def apply_snapshot(
        self,
        index_code: str,
        symbols: Iterable[str],
        snapshot_date: str,
        source: str,
    ) -> dict:
        """把一个快照（snapshot_date 当日观察到的成分名单）差分写入库。

        - 快照中有、库中 active 记录没有 → INSERT in_date=snapshot_date
        - 库中 active、快照中没有 → UPDATE out_date=snapshot_date（移出）
        - 幂等：同一快照重复执行结果不变。
        历史区间只追加 out_date，绝不修改既有 in_date。
        """
        new_set = {s.strip() for s in symbols if s and s.strip()}
        active = {
            r["symbol"]
            for r in self.warehouse.query(
                "SELECT symbol FROM universe_membership "
                "WHERE index_code = ? AND out_date IS NULL",
                [index_code],
            )
        }
        added = sorted(new_set - active)
        removed = sorted(active - new_set)
        for sym in added:
            self.warehouse.execute(
                "INSERT OR IGNORE INTO universe_membership "
                "(index_code, symbol, in_date, out_date, source) "
                "VALUES (?, ?, ?, NULL, ?)",
                [index_code, sym, snapshot_date, source],
            )
        for sym in removed:
            self.warehouse.execute(
                "UPDATE universe_membership SET out_date = ? "
                "WHERE index_code = ? AND symbol = ? AND out_date IS NULL",
                [snapshot_date, index_code, sym],
            )
        logger.info(
            "universe[%s] snapshot %s (%s): active=%d, +%d, -%d",
            index_code, snapshot_date, source, len(new_set), len(added), len(removed),
        )
        return {
            "index_code": index_code,
            "snapshot_date": snapshot_date,
            "snapshot_size": len(new_set),
            "added": len(added),
            "removed": len(removed),
        }

    # ── 采集 ────────────────────────────────────────────

    def refresh(
        self,
        index_codes: list[str] | None = None,
        snapshot_date: str | None = None,
    ) -> list[dict]:
        """在线采集最新成分并差分入库。单个指数失败不中断其他指数。"""
        from .collectors import collect

        codes = index_codes or list(INDICES)
        date_str = snapshot_date or date_type.today().isoformat()
        results = []
        for code in codes:
            try:
                symbols, source = collect(code)
                if not symbols:
                    raise RuntimeError("采集结果为空")
                results.append(self.apply_snapshot(code, symbols, date_str, source))
            except Exception as exc:  # 网络/接口失败：记录并跳过
                logger.warning("universe[%s] 采集失败，跳过: %s", code, exc)
                results.append(
                    {"index_code": code, "error": f"{type(exc).__name__}: {exc}"}
                )
        return results

    # ── 离线静态快照 ────────────────────────────────────

    def load_static_snapshots(self, data_dir: str | Path | None = None) -> list[dict]:
        """从 data/*.csv 导入手工快照（网络不可用时使用）。

        CSV 需含表头 index_code,symbol,snapshot_date，见 data/README.md。
        """
        directory = Path(data_dir) if data_dir else DATA_DIR
        results = []
        if not directory.exists():
            logger.warning("静态快照目录不存在: %s", directory)
            return results
        for csv_path in sorted(directory.glob("*.csv")):
            rows_by_index: dict[tuple[str, str], list[str]] = {}
            with open(csv_path, newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    key = (row["index_code"].strip(), row["snapshot_date"].strip())
                    rows_by_index.setdefault(key, []).append(row["symbol"].strip())
            for (index_code, snap_date), symbols in rows_by_index.items():
                results.append(
                    self.apply_snapshot(index_code, symbols, snap_date, "static_csv")
                )
        return results
