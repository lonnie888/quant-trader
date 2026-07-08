"""Parameter search helpers (grid by default; Bayesian stub provided)."""
from __future__ import annotations

import itertools
import random
from typing import Callable, Iterable, Any

import numpy as np


def grid_search(
    space: dict[str, list[Any]],
    evaluator: Callable[[dict], float],
    seed: int | None = None,
) -> tuple[dict, float]:
    """Exhaustively search a discrete grid."""
    keys = list(space.keys())
    values = [space[k] for k in keys]
    best_params: dict | None = None
    best_score = -float("inf")
    for combo in itertools.product(*values):
        params = dict(zip(keys, combo))
        score = float(evaluator(params))
        if score > best_score:
            best_score = score
            best_params = params
    return best_params or {}, best_score


def random_search(
    space: dict[str, list[Any]],
    evaluator: Callable[[dict], float],
    n_iter: int = 50,
    seed: int | None = None,
) -> tuple[dict, float]:
    rng = random.Random(seed)
    keys = list(space.keys())
    best_params: dict | None = None
    best_score = -float("inf")
    for _ in range(n_iter):
        params = {k: rng.choice(space[k]) for k in keys}
        score = float(evaluator(params))
        if score > best_score:
            best_score = score
            best_params = params
    return best_params or {}, best_score
