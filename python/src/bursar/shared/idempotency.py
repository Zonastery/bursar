"""Validation shared by replay-safe Bursar mutations."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated

from pydantic import BeforeValidator, Field

MAX_IDEMPOTENCY_KEY_LENGTH = 255


def require_stable_key(value: object, field: str = "idempotency_key") -> str:
    """Return a valid caller-owned replay key without rewriting it."""
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(f"{field} must be a trimmed non-empty string of at most 255 characters")
    return value


def scope_stable_key(
    value: object,
    scope: object,
    *identity_parts: object,
    field: str = "idempotency_key",
) -> str:
    """Derive a bounded replay key for one deterministic child mutation."""
    stable_key = require_stable_key(value, field)
    if not isinstance(scope, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,47}", scope) is None:
        raise ValueError("idempotency key scope must match [a-z][a-z0-9-]{0,47}")
    encoded_parts: list[str] = []
    for part in identity_parts:
        if not isinstance(part, str) or not part or part != part.strip():
            raise ValueError("idempotency key identity parts must be trimmed non-empty strings")
        encoded_parts.append(f"{len(part)}#{part}")
    candidate = ":".join((stable_key, scope, *encoded_parts))
    if len(candidate) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()
    return f"bursar:{scope}:{digest}"


StableKey = Annotated[
    str,
    BeforeValidator(require_stable_key),
    Field(strict=True),
]

__all__ = [
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "StableKey",
    "require_stable_key",
    "scope_stable_key",
]
