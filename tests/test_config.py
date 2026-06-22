"""Tests for YAML config parsing and validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ducto.config import (
    ConfigError,
    load_config_from_dict,
    load_config_from_path,
)


class TestConfigValidation:
    """Tests for config loading and validation."""

    def test_valid_full_config(self) -> None:
        """Loading a full config dict populates all sections."""
        config = load_config_from_dict(
            {
                "version": 1,
                "models": {"gpt-4": "input_tokens * 0.01"},
                "tools": {"_default": "tool_calls * 0.1"},
            }
        )
        assert config.version == 1
        assert config.models["gpt-4"] == "input_tokens * 0.01"

    def test_minimal_config(self) -> None:
        """Minimal config with only version and models works."""
        config = load_config_from_dict(
            {
                "version": 1,
                "models": {"_default": "input_tokens * 0.001"},
            }
        )
        assert config.models["_default"] == "input_tokens * 0.001"

    def test_invalid_expression_raises_error(self) -> None:
        """An expression with disallowed syntax raises ConfigError."""
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict(
                {
                    "version": 1,
                    "models": {"gpt-4": "lambda x: x"},
                }
            )

    def test_rejects_unknown_version(self) -> None:
        """Unknown version number raises ConfigError."""
        with pytest.raises(ConfigError, match="version"):
            load_config_from_dict(
                {
                    "version": 999,
                    "models": {"_default": "input_tokens * 1"},
                }
            )

    def test_missing_models_raises_error(self) -> None:
        """Missing models section raises ConfigError."""
        with pytest.raises(ConfigError, match="models"):
            load_config_from_dict({"version": 1})

    def test_negative_fixed_cost_raises_error(self) -> None:
        """Negative fixed cost values raise pydantic ValidationError."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": 1,
                    "models": {"_default": "input_tokens * 1"},
                    "fixed": {"bad_job": -5},
                }
            )

    def test_tool_specific_costs(self) -> None:
        """Tool-specific expression strings are stored correctly."""
        config = load_config_from_dict(
            {
                "version": 1,
                "models": {"_default": "input_tokens * 1"},
                "tools": {"_default": "tool_calls * 0", "web_search": "web_search_calls * 2"},
            }
        )
        assert config.tools["web_search"] == "web_search_calls * 2"

    def test_fixed_costs_are_positive(self) -> None:
        """Positive fixed cost values are accepted."""
        config = load_config_from_dict(
            {
                "version": 1,
                "models": {"_default": "input_tokens * 1"},
                "fixed": {"roadmap_gen": 20, "topic_gen": 10},
            }
        )
        assert config.fixed["roadmap_gen"] == 20

    def test_load_config_from_path(self, tmp_path: Path) -> None:
        """Loading from a YAML file path works."""
        yaml_file = tmp_path / "pricing.yaml"
        yaml_file.write_text("""\
version: 1
models:
  _default: "input_tokens * 1"
""")
        config = load_config_from_path(str(yaml_file))
        assert config.models["_default"] == "input_tokens * 1"
