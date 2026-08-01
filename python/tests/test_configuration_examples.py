"""Keep canonical configuration examples valid."""

import re
from pathlib import Path

import yaml

from bursar.config import load_config_from_dict


def test_documentation_canonical_yaml_examples_validate() -> None:
    repository = Path(__file__).resolve().parents[2]
    examples = [
        repository / "README.md",
        repository / "docs" / "docs" / "concepts" / "configuration.mdx",
    ]

    for example in examples:
        source = example.read_text(encoding="utf-8")
        match = re.search(r"```yaml\n(.*?)\n```", source, flags=re.DOTALL)
        assert match is not None

        config = yaml.safe_load(match.group(1))

        assert load_config_from_dict(config).plans["free"].display_name == "Free"


def test_generic_operation_example_validates() -> None:
    config = {
        "version": 1,
        "pricing": {
            "operations": {
                "image_generation": {
                    "measures": {"images": {"unit": "image"}},
                    "dimensions": {"model": {"type": "string"}},
                }
            },
            "rate_cards": {
                "standard": {
                    "operations": {
                        "image_generation": {
                            "rules": [
                                {
                                    "when": {
                                        "model": {
                                            "op": "prefix",
                                            "value": "dall-e",
                                        }
                                    },
                                    "charge": {
                                        "type": "per_unit",
                                        "measure": "images",
                                        "rate": "20",
                                    },
                                }
                            ],
                            "unmatched": {
                                "action": "charge",
                                "charge": {
                                    "type": "per_unit",
                                    "measure": "images",
                                    "rate": "10",
                                },
                            },
                        }
                    }
                }
            },
        },
        "credits": {
            "accounting": {
                "unit": "credit",
                "scale": 6,
                "rounding": "half_up",
            },
            "buckets": {
                "purchased": {
                    "priority": 10,
                    "expiry": {"type": "never"},
                }
            },
            "default_bucket": "purchased",
        },
        "plans": {
            "free": {
                "display_name": "Free",
                "rank": 0,
                "rate_card": "standard",
            }
        },
    }

    assert load_config_from_dict(config).plans["free"].display_name == "Free"
