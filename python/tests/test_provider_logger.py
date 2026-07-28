"""Mirrors JS SDK ``tests/provider-logger.test.ts``."""

from __future__ import annotations

from bursar.providers.types import normalize_provider_logger


def test_accepts_null_and_supplies_safe_noop_methods() -> None:
    logger = normalize_provider_logger(None)
    logger.debug("debug")
    logger.info("info")
    logger.warning("warning")
    logger.error("error")


def test_preserves_supplied_methods_while_filling_missing() -> None:
    calls: list[str] = []

    class _Logger:
        @staticmethod
        def debug(msg: str, ctx: dict | None = None) -> None:
            calls.append(f"debug:{msg}")

    logger = normalize_provider_logger(_Logger())
    logger.debug("event", {"value": 1})
    logger.info("ignored")

    assert calls == ["debug:event"]
