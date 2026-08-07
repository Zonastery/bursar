"""Mirrors JS SDK ``tests/load-config-file.test.ts``."""

from __future__ import annotations

import os
import tempfile

import pytest

from bursar.errors import ConfigError
from bursar.load_config_file import load_config_file


def test_loads_json_file() -> None:
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        f.write('{"metering": {"models": {"a": "1"}}}')
        path = f.name
    try:
        result = load_config_file(path)
        assert result["metering"]["models"] == {"a": "1"}
    finally:
        os.unlink(path)


def test_loads_yaml_file() -> None:
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write('metering:\n  models:\n    a: "1"\n')
        path = f.name
    try:
        result = load_config_file(path)
        assert result["metering"]["models"] == {"a": "1"}
    finally:
        os.unlink(path)


def test_raises_on_missing_file() -> None:
    with pytest.raises(ConfigError, match="Config file not found"):
        load_config_file("/tmp/nope_bursar_test.json")


def test_raises_on_directory_path() -> None:
    with tempfile.TemporaryDirectory(suffix=".json") as path:
        with pytest.raises(ConfigError, match="is a directory"):
            load_config_file(path)


def test_raises_on_empty_json_file() -> None:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        with pytest.raises(ConfigError, match="Invalid JSON"):
            load_config_file(path)
    finally:
        os.unlink(path)


def test_raises_on_empty_yaml_file() -> None:
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        path = f.name
    try:
        with pytest.raises(ConfigError, match="is empty"):
            load_config_file(path)
    finally:
        os.unlink(path)


def test_raises_on_empty_json_object() -> None:
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        f.write("{}")
        path = f.name
    try:
        with pytest.raises(ConfigError, match="is empty"):
            load_config_file(path)
    finally:
        os.unlink(path)


def test_loads_yaml_with_unicode() -> None:
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write('metering:\n  models:\n    "gpt-4-türkçe": "input_tokens * 1"\n    "模型": "output_tokens * 2"\n')
        path = f.name
    try:
        result = load_config_file(path)
        assert "gpt-4-türkçe" in result["metering"]["models"]
        assert "模型" in result["metering"]["models"]
    finally:
        os.unlink(path)


def test_rejects_duplicate_yaml_keys() -> None:
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write("version: 1\nversion: 2\n")
        path = f.name
    try:
        with pytest.raises(ConfigError, match="duplicate key"):
            load_config_file(path)
    finally:
        os.unlink(path)


def test_raises_on_unsupported_format() -> None:
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
        f.write("hello")
        path = f.name
    try:
        with pytest.raises(ConfigError, match="Unsupported config file format"):
            load_config_file(path)
    finally:
        os.unlink(path)
