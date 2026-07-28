"""Keep canonical configuration examples valid."""

from bursar.config import load_config_from_dict


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
                "rate_card": "standard",
            }
        },
    }

    assert load_config_from_dict(config).plans["free"].display_name == "Free"
