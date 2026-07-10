"""Top-level config loader: returns simple namespaces usable across the project."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import yaml

log = logging.getLogger(__name__)


# Config search order:
#   1. explicit path argument
#   2. QT_CONFIG_PATH env var
#   3. ./config/settings.yaml (cwd)
#   4. ./quant_trader/config/settings.yaml (legacy in-package)
#   5. <repo>/config/settings.yaml (parent walk)
DEFAULT_SETTINGS_CANDIDATES = [
    "config/settings.yaml",
    "quant_trader/config/settings.yaml",
]


def _find_config(explicit: str | None) -> str | None:
    """Search known locations for a settings.yaml. Returns path or None."""
    if explicit:
        return explicit
    env_path = os.environ.get("QT_CONFIG_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    cwd = Path.cwd()
    for cand in DEFAULT_SETTINGS_CANDIDATES:
        p = cwd / cand
        if p.exists():
            return str(p)
    # Walk up to find config/settings.yaml
    for parent in [cwd, *cwd.parents]:
        p = parent / "config" / "settings.yaml"
        if p.exists():
            return str(p)
    return None


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


def load_settings(path: str | None = None):
    """Load settings from a config file. Falls back to known locations.

    Returns a SimpleNamespace. Missing file → empty SimpleNamespace.
    """
    resolved = _find_config(path)
    if resolved is None:
        log.warning("no settings.yaml found, using empty defaults")
        return _to_ns({})
    raw = _load_yaml(resolved)
    return _to_ns(raw)


def load_strategies(path: str | None = None):
    """Load strategies.yaml. Search candidates if path not given."""
    if path is None:
        cwd = Path.cwd()
        for cand in ["config/strategies.yaml", "quant_trader/config/strategies.yaml"]:
            p = cwd / cand
            if p.exists():
                path = str(p)
                break
    if path is None:
        return {}
    return _load_yaml(path).get("strategies", {})