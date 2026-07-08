"""Automatic strategy instance generator from a YAML config."""
from __future__ import annotations

import itertools
from typing import Any

import yaml

from ..registry import build


def _expand(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    return [value]


def load_strategies_config(path: str) -> dict[str, dict]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f).get("strategies", {})


def generate_instances(path: str) -> list[tuple[str, dict, object]]:
    """Yield (name, params, strategy_instance) for every param combination.

    Long-only and long-short variants are kept (controlled by `side`).
    """
    cfg = load_strategies_config(path)
    out: list[tuple[str, dict, object]] = []
    for name, spec in cfg.items():
        if not spec.get("active", True):
            continue
        params = spec.get("params", {}) or {}
        keys = list(params.keys())
        value_lists = [_expand(params[k]) for k in keys]
        for combo in itertools.product(*value_lists):
            p = dict(zip(keys, combo))
            out.append((name, p, build(name, p)))
    return out
