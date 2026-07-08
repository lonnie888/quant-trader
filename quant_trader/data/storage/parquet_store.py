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
        path = self._path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Merge with existing data: keep newest per timestamp
        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, df])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined.sort_index(inplace=True)
            combined.to_parquet(path)
        else:
            df.to_parquet(path)

    def load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        path = self._path(symbol, timeframe)
        if not path.exists():
            return pd.DataFrame()
        return pd.read_parquet(path)

    def list_symbols(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted([d.name for d in self.root.iterdir() if d.is_dir()])
