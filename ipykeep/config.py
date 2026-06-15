"""Configuration loading for ipykeep.

Reads the ``[tool.ipykeep]`` (and ``[tool.ipykeep.inspection]``) table from
``ipykeep.toml`` or ``pyproject.toml`` in the current working directory.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - fallback for older interpreters
    import tomli as tomllib


@dataclass
class Config:
    notebook: str | None = None
    inspect_timeout_s: float = 5.0
    inspect_sample_rows: int = 5
    watch_debounce_ms: int = 500
    log_level: str = "INFO"
    summarizers: list[str] = field(default_factory=list)
    serve: bool = False
    server_command: str = "lab"
    server_port: int = 0


def _read_table(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    table = data.get("tool", {}).get("ipykeep", {})
    return table if isinstance(table, dict) else {}


def load_config(start: Path | None = None) -> Config:
    """Load config from ipykeep.toml then pyproject.toml in ``start`` (cwd)."""
    base = Path(start) if start is not None else Path.cwd()
    table: dict[str, Any] = {}
    for name in ("ipykeep.toml", "pyproject.toml"):
        candidate = base / name
        if candidate.is_file():
            table = _read_table(candidate)
            if table:
                break

    inspection = table.get("inspection", {})
    if not isinstance(inspection, dict):
        inspection = {}

    cfg = Config()
    if "notebook" in table:
        cfg.notebook = str(table["notebook"])
    cfg.inspect_timeout_s = float(table.get("inspect_timeout_s", cfg.inspect_timeout_s))
    cfg.inspect_sample_rows = int(table.get("inspect_sample_rows", cfg.inspect_sample_rows))
    cfg.watch_debounce_ms = int(table.get("watch_debounce_ms", cfg.watch_debounce_ms))
    cfg.log_level = str(table.get("log_level", cfg.log_level))
    cfg.serve = bool(table.get("serve", cfg.serve))
    cfg.server_command = str(table.get("server_command", cfg.server_command))
    cfg.server_port = int(table.get("server_port", cfg.server_port))
    summarizers = inspection.get("summarizers", [])
    if isinstance(summarizers, list):
        cfg.summarizers = [str(s) for s in summarizers]
    return cfg
