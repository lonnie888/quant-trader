"""Strategy registry: name -> class."""
from __future__ import annotations

from .base import Strategy
from .library.bollinger import BollingerStrategy
from .library.breakout import BreakoutStrategy
from .library.kdj import KDJStrategy
from .library.ma_cross import MACross
from .library.macd import MACDStrategy
from .library.mean_reversion import MeanReversionStrategy
from .library.mean_reversion_15m import MeanReversion15mStrategy
from .library.pump_pullback import PumpPullbackStrategy
from .library.rsi import RSIStrategy
from .library.turtle import TurtleStrategy
from .library.pump_pullback_opt import PumpPullbackOptStrategy

REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (
        MACross, MACDStrategy, RSIStrategy, BollingerStrategy, KDJStrategy,
        TurtleStrategy, BreakoutStrategy, MeanReversionStrategy,
        MeanReversion15mStrategy,
        PumpPullbackStrategy, PumpPullbackOptStrategy,
    )
}


def build(name: str, params: dict) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy: {name}")
    return REGISTRY[name](params=params)