"""Parquet-based local storage for OHLCV and funding data."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


def _safe(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


class ParquetStore:
    def __init__(self, root: str = "./data_store"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, timeframe: str) -> Path:
        return self.root / _safe(symbol) / f"{timeframe}.parquet"

    def save(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        try:
            path = self._path(symbol, timeframe)
            path.parent.mkdir(parents=True, exist_ok=True)
            # 直接覆盖写入（不合并），提高稳定性
            df.to_parquet(path)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("store save failed %s/%s: %s", symbol, timeframe, e)

    def load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        path = self._path(symbol, timeframe)
        if not path.exists():
            return pd.DataFrame()
        return pd.read_parquet(path)

    def list_symbols(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted([d.name for d in self.root.iterdir() if d.is_dir()])
