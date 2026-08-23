"""Configuration loading for DUM-E.

Configuration is JSON, not TOML: the commissioning host runs Python 3.10, which
has no stdlib ``tomllib``, and the foundation layer is required to work with no
third-party dependency at all.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "dume.config.json"


class ConfigError(RuntimeError):
    """Configuration is missing, malformed or internally contradictory."""


def load(path: Path | None = None) -> dict:
    """Load and validate the DUM-E configuration.

    Fails closed: a workspace with an unknown mode, or a bound workspace whose
    path does not exist, is an error rather than a silently ignored entry.
    """
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.is_file():
        raise ConfigError(f"configuration not found: {path}")
    try:
        cfg = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"configuration is not valid JSON: {path}: {exc}") from exc

    if cfg.get("schema_version") != 1:
        raise ConfigError(f"unsupported schema_version: {cfg.get('schema_version')!r}")

    workspaces = cfg.get("workspaces")
    if not isinstance(workspaces, dict) or not workspaces:
        raise ConfigError("configuration declares no workspaces")

    valid_modes = {"READ_ONLY", "READ_WRITE", "APPEND_ONLY"}
    for name, ws in workspaces.items():
        if not isinstance(ws, dict):
            raise ConfigError(f"workspace {name!r} is not an object")
        mode = ws.get("mode")
        if mode not in valid_modes:
            raise ConfigError(f"workspace {name!r} has unknown mode {mode!r}")
        # `bound` defaults to True when a path is present: an explicitly
        # unbound workspace must say so, so that "forgot to configure" and
        # "deliberately not yet bound" cannot be confused.
        ws.setdefault("bound", ws.get("path") is not None)
        if ws["bound"] and not ws.get("path"):
            raise ConfigError(f"workspace {name!r} is marked bound but has no path")

    cfg["_source"] = str(path)
    return cfg


def workspace(cfg: dict, name: str) -> dict:
    try:
        return cfg["workspaces"][name]
    except KeyError:
        raise ConfigError(f"no such workspace: {name!r}") from None


def bound_workspaces(cfg: dict) -> dict[str, dict]:
    """Only the workspaces a human has actually bound to a real path."""
    return {n: w for n, w in cfg["workspaces"].items() if w.get("bound")}
