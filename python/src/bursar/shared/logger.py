"""Logger interface — mirrors JS SDK's ``shared/logger.ts``."""

from __future__ import annotations

import logging
from typing import Any, Protocol


class Logger(Protocol):
    """Logger interface matching the JS SDK's Logger type."""

    def debug(self, message: str, context: dict[str, Any] | None = None) -> None: ...

    def info(self, message: str, context: dict[str, Any] | None = None) -> None: ...

    def warn(self, message: str, context: dict[str, Any] | None = None) -> None: ...

    def error(self, message: str, context: dict[str, Any] | None = None) -> None: ...


class NormalizedLogger:
    """Wraps a standard library logger to match the JS Logger interface."""

    def __init__(self, logger: Logger | logging.Logger | str | None = None) -> None:
        self._logger = logging.getLogger(logger) if isinstance(logger, str) else logger

    def _call(
        self,
        method: str,
        message: str,
        context: dict[str, Any] | None,
    ) -> None:
        target = getattr(self._logger, method, None)
        if callable(target):
            if isinstance(self._logger, logging.Logger):
                target(
                    message,
                    extra={"context": context} if context else None,
                )
            else:
                target(message, context)

    def debug(self, message: str, context: dict[str, Any] | None = None) -> None:
        self._call("debug", message, context)

    def info(self, message: str, context: dict[str, Any] | None = None) -> None:
        self._call("info", message, context)

    def warn(self, message: str, context: dict[str, Any] | None = None) -> None:
        method = "warning" if isinstance(self._logger, logging.Logger) else "warn"
        self._call(method, message, context)

    def error(self, message: str, context: dict[str, Any] | None = None) -> None:
        self._call("error", message, context)


noop_logger = NormalizedLogger()


def normalize_logger(
    logger: Logger | logging.Logger | None = None,
) -> NormalizedLogger:
    return NormalizedLogger(logger)
