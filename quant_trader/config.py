"""Top-level config loader: returns simple namespaces usable across the project."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import yaml


def _load_yaml(path):
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _to_ns(d):
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _to_ns(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_to_ns(x) for x in d]
    return d


def load_settings(path="config/settings.yaml"):
    raw = _load_yaml(path)
    return _to_ns(raw)


def load_strategies(path="config/strategies.yaml"):
    return _load_yaml(path).get("strategies", {})