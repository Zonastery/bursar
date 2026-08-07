"""File-based Bursar config loader — mirrors JS SDK's ``load-config-file.ts``.

Loads a Bursar configuration from a JSON or YAML file.
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
        raise ConfigError(f"Bursar config is empty: {path}")
    if not isinstance(data, dict):
        raise ConfigError(f"Bursar config must be a JSON/YAML object, got {type(data).__name__}: {path}")
    if not data:
        raise ConfigError(f"Bursar config is empty: {path}")
    return data


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Load a Bursar configuration from a JSON or YAML file.

    Args:
        path: Path to the Bursar config file (.json or .yaml/.yml).

    Returns:
        The parsed configuration dictionary.

    Raises:
        ConfigError: If the file is missing, is a directory, has an unsupported
            format, contains invalid content, or is empty.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

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
                "PyYAML is required to load YAML config files. Install with: pip install pyyaml"
            ) from None
        content = _read_clean(path)

        class _StrictYamlLoader(yaml.SafeLoader):
            pass

        def _construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
            mapping: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise yaml.YAMLError(f"duplicate key: {key!r}")
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        _StrictYamlLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            _construct_mapping,
        )
        try:
            parsed = yaml.load(content, Loader=_StrictYamlLoader)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
        return _assert_non_empty_object(parsed, path)

    raise ConfigError(f"Unsupported config file format: {suffix}. Expected .json, .yaml, or .yml")
