"""
foundf_db — FoundF 统一数据仓库层。

三层架构：
    Raw Layer      → data/raw/   （只写不变的原始数据，Parquet 格式）
    Warehouse Layer → finance.duckdb （结构化关系表，DuckDB）
    Analytics Layer → 物化视图/分析表 （位于 DuckDB 中，以 mv_ 前缀标识）
"""

from .warehouse import Warehouse, get_warehouse
from .schema import SCHEMA_SQL, ANALYTIC_VIEWS_SQL
from .migration import run_all_migrations
from .data_provider import DataProvider
from .event_store import InvestmentEventStore
from .research_report_store import ResearchReportStore
from .health import inspect_data_assets
from .market_watch import MarketWatchStore
from .broker_simulation_archive import BrokerSimulationArchive
from .walk_forward_input_store import (
    WalkForwardInputStore,
    inspect_walk_forward_inputs,
)

__all__ = [
    "Warehouse", "get_warehouse",
    "SCHEMA_SQL", "ANALYTIC_VIEWS_SQL",
    "run_all_migrations",
    "DataProvider",
    "InvestmentEventStore",
    "ResearchReportStore",
    "inspect_data_assets",
    "MarketWatchStore",
    "BrokerSimulationArchive",
    "WalkForwardInputStore",
    "inspect_walk_forward_inputs",
]
