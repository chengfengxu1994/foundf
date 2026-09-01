"""
Raw 原始数据层管理。

设计文档要求：
    data/raw/
    ├── cn_stock/       # A股原始数据（parquet）
    ├── hk_stock/       # 港股原始数据（parquet）
    ├── us_stock/       # 美股原始数据（parquet）
    ├── macro/          # 宏观指标数据（parquet/CSV）
    ├── news/           # 新闻原始存档（HTML/JSON）
    ├── announcement/   # 公告原始文件（PDF/HTML）
    └── financial/      # 财报原始数据（parquet）

原则：永不修改，只追加新文件。用于数据追溯、模型重新训练、策略复盘。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


# 原始数据层的标准目录结构
RAW_SUBDIRS = {
    "cn_stock": "A股原始数据（日线/分钟线）",
    "hk_stock": "港股原始数据（日线/分钟线）",
    "us_stock": "美股原始数据（日线/分钟线）",
    "macro": "宏观指标数据",
    "news": "新闻原始存档",
    "announcement": "公告原始文件",
    "financial": "财报原始数据",
}


def ensure_raw_dirs(base: str | Path = "data/raw") -> dict[str, Path]:
    """创建 raw 层目录结构，返回 {名称: 路径} 映射。"""
    base_path = Path(base)
    result = {}
    for name, _desc in RAW_SUBDIRS.items():
        path = base_path / name
        path.mkdir(parents=True, exist_ok=True)
        result[name] = path
    return result


def raw_path(symbol: str, market: str, base: str | Path = "data/raw") -> Path:
    """返回某个标的的原始数据 parquet 文件路径。

    market 映射:
        'A', 'ETF_CN'  → cn_stock/
        'HK_CONNECT', 'HK_ETF' → hk_stock/
        'US'  → us_stock/
    """
    base_path = Path(base)
    if market in ("A", "ETF_CN"):
        sub = "cn_stock"
    elif market in ("HK_CONNECT", "HK_ETF", "HK_STOCK"):
        sub = "hk_stock"
    elif market == "US":
        sub = "us_stock"
    else:
        sub = "cn_stock"  # fallback
    dir_path = base_path / sub
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path / f"{symbol}.parquet"


def save_raw_data(
    df: pd.DataFrame,
    symbol: str,
    market: str,
    base: str | Path = "data/raw",
    source: str = "unknown",
) -> Path:
    """将 DataFrame 保存为 raw 层 parquet（合并已有数据，永不覆写）。"""
    path = raw_path(symbol, market, base)
    if path.exists():
        existing = pd.read_parquet(path)
        # 确保日期列一致
        for col in ("date", "datetime"):
            if col in existing and col in df:
                existing[col] = pd.to_datetime(existing[col])
                df[col] = pd.to_datetime(df[col])
        df = pd.concat([existing, df], ignore_index=True)
        date_col = "date" if "date" in df.columns else "datetime"
        df = df.sort_values(date_col).drop_duplicates(subset=[date_col], keep="last").reset_index(drop=True)
    df["fetched_at"] = datetime.now(timezone.utc).isoformat()
    df["source"] = source
    df.to_parquet(path, index=False)
    return path


def save_raw_news(
    title: str,
    content: str,
    source_name: str,
    symbol: str | None = None,
    base: str | Path = "data/raw",
) -> Path:
    """将新闻保存为 raw 层 JSON Lines 文件。"""
    base_path = Path(base) / "news"
    base_path.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = base_path / f"{today}.jsonl"
    content_hash = hashlib.sha256(f"{title}|{content[:200]}".encode()).hexdigest()[:16]
    record = {
        "title": title,
        "content": content[:5000],
        "source": source_name,
        "symbol": symbol,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def list_raw_symbols(market: str, base: str | Path = "data/raw") -> list[str]:
    """列出 raw 层中某一市场的所有标的代码。"""
    market_map = {
        "A": "cn_stock", "ETF_CN": "cn_stock",
        "HK_CONNECT": "hk_stock", "HK_ETF": "hk_stock", "HK_STOCK": "hk_stock",
        "US": "us_stock",
    }
    sub = market_map.get(market, "cn_stock")
    dir_path = Path(base) / sub
    if not dir_path.exists():
        return []
    return sorted(p.stem for p in dir_path.glob("*.parquet"))
