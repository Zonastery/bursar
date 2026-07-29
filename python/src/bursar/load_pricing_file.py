"""File-based pricing config loader — mirrors JS SDK's ``load-pricing-file.ts``.

Loads a Bursar pricing configuration from a JSON or YAML file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bursar.errors import BursarImportError, ConfigError


def _read_clean(path: Path) -> str:
    if path.is_dir():
        raise ConfigError(f"Not a file (is a directory): {path}")
    try:
        return path.read_text("utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def _assert_non_empty_object(data: Any, path: Path) -> dict[str, Any]:
    if data is None:
        raise ConfigError(f"Pricing config is empty: {path}")
    if not isinstance(data, dict):
        raise ConfigError(f"Pricing config must be a JSON/YAML object, got {type(data).__name__}: {path}")
    if not data:
        raise ConfigError(f"Pricing config is empty: {path}")
    return data


def load_pricing_file(path: str | Path) -> dict[str, Any]:
    """Load a pricing configuration from a JSON or YAML file.

    Args:
        path: Path to the pricing config file (.json or .yaml/.yml).

    Returns:
        The parsed configuration dictionary.

    Raises:
        ConfigError: If the file is missing, is a directory, has an unsupported
            format, contains invalid content, or is empty.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Pricing file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        content = _read_clean(path)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
        return _assert_non_empty_object(parsed, path)

    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise BursarImportError(
                "PyYAML is required to load YAML pricing files. Install with: pip install pyyaml"
            ) from None
        content = _read_clean(path)
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
        return _assert_non_empty_object(parsed, path)

    raise ConfigError(f"Unsupported pricing file format: {suffix}. Expected .json, .yaml, or .yml")
