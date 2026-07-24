"""Keep the canonical configuration examples valid."""

from bursar.config import load_config_from_dict


def test_generic_operation_example_validates() -> None:
    config = {
        "version": 1,
        "usage": {
            "operations": {"image_generation": {"measures": ["images"], "dimensions": ["model"]}},
            "rate_cards": {
                "standard": {
                    "prices": {
                        "image_generation": [
                            {"match": {"model": {"prefix": "dall-e"}}, "formula": "images * 20"},
                            {"default": True, "formula": "images * 10"},
                        ]
                    }
                }
            },
        },
        "credits": {
            "buckets": {"purchased": {}},
            "spend_order": ["purchased"],
            "default_bucket": "purchased",
        },
        "plans": {"free": {"display_name": "Free", "rate_card": "standard"}},
    }
    assert load_config_from_dict(config).plans["free"].display_name == "Free"
