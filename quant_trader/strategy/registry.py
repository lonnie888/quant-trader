"""Strategy registry: name -> class."""
from __future__ import annotations

from .base import Strategy
from .library.bollinger import BollingerStrategy
from .library.breakout import BreakoutStrategy
from .library.kdj import KDJStrategy
from .library.ma_cross import MACross
from .library.macd import MACDStrategy
from .library.mean_reversion import MeanReversionStrategy
from .library.pump_pullback import PumpPullbackStrategy
from .library.rsi import RSIStrategy
from .library.turtle import TurtleStrategy

REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (
        MACross, MACDStrategy, RSIStrategy, BollingerStrategy, KDJStrategy,
        TurtleStrategy, BreakoutStrategy, MeanReversionStrategy,
        PumpPullbackStrategy,
    )
}


def build(name: str, params: dict) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy: {name}")
    return REGISTRY[name](params=params)