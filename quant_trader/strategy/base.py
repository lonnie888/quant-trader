"""Strategy base class and signal definitions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import pandas as pd


class Side(IntEnum):
    FLAT = 0
    LONG = 1
    SHORT = -1


@dataclass
class Signal:
    timestamp: pd.Timestamp
    side: Side
    reason: str = ""

    def __post_init__(self):
        if self.side not in (Side.FLAT, Side.LONG, Side.SHORT):
            raise ValueError(f"invalid side: {self.side}")


class Strategy:
    """Base class for all strategies.

    Subclasses must implement `generate_signals(df, params) -> pd.Series`
    where the series index is the dataframe index and values are Side.
    """

    name: str = "base"

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = params or {}

    # ---- subclasses override ----
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    # ---- helpers ----
    def _side_series(self, df: pd.DataFrame, raw: pd.Series) -> pd.Series:
        s = raw.reindex(df.index).fillna(0).astype(int)
        s = s.where(s.isin([Side.LONG.value, Side.SHORT.value, Side.FLAT.value]), other=Side.FLAT.value)
        return s
