"""File-based Bursar config loader — mirrors JS SDK's ``load-config-file.ts``.

Loads a Bursar configuration from a JSON or YAML file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from bursar.errors import ConfigError


class _StrictYamlLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
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


class _DuplicateJsonKeyError(ValueError):
    """Internal parse error used to preserve JSON source context."""


def _construct_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise _DuplicateJsonKeyError(f"duplicate key: {key!r}")
        mapping[key] = value
    return mapping


def _read_clean(path: Path) -> str:
    if path.is_dir():
        raise ConfigError(f"Not a file (is a directory): {path}")
    try:
        return path.read_text("utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def _assert_non_empty_object(data: Any, source: str | Path) -> dict[str, Any]:
    if data is None:
        raise ConfigError(f"Bursar config is empty: {source}")
    if not isinstance(data, dict):
        raise ConfigError(f"Bursar config must be a JSON/YAML object, got {type(data).__name__}: {source}")
    if not data:
        raise ConfigError(f"Bursar config is empty: {source}")
    return data


def load_config_text(content: str, *, is_yaml: bool, source: str | Path = "<string>") -> dict[str, Any]:
    """Parse one complete JSON or YAML config document.

    Duplicate object keys are rejected in both formats. Silently taking the
    final value is unsafe for financial configuration and makes reviews
    misleading, especially when the duplicate is nested far from the root.
    """

    if is_yaml:
        try:
            # This loader subclasses SafeLoader solely to add duplicate-key rejection.
            parsed = yaml.load(content, Loader=_StrictYamlLoader)  # noqa: S506
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {source}: {exc}") from exc
    else:
        try:
            parsed = json.loads(content, object_pairs_hook=_construct_json_object)
        except (json.JSONDecodeError, _DuplicateJsonKeyError) as exc:
            raise ConfigError(f"Invalid JSON in {source}: {exc}") from exc
    return _assert_non_empty_object(parsed, source)


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
        return load_config_text(_read_clean(path), is_yaml=False, source=path)

    if suffix in (".yaml", ".yml"):
        return load_config_text(_read_clean(path), is_yaml=True, source=path)

    raise ConfigError(f"Unsupported config file format: {suffix}. Expected .json, .yaml, or .yml")
