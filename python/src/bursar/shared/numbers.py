"""Cross-SDK integer constraints.

JavaScript represents JSON integers as IEEE-754 numbers. Public values shared
between the Python and JavaScript SDKs must therefore stay within the exact
safe-integer range.
"""

from typing import Annotated

from pydantic import Field

MAX_SAFE_INTEGER = 9_007_199_254_740_991

SafeInteger = Annotated[
    int,
    Field(strict=True, ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER),
]
NonNegativeSafeInteger = Annotated[
    int,
    Field(strict=True, ge=0, le=MAX_SAFE_INTEGER),
]
PositiveSafeInteger = Annotated[
    int,
    Field(strict=True, ge=1, le=MAX_SAFE_INTEGER),
]

__all__ = [
    "MAX_SAFE_INTEGER",
    "NonNegativeSafeInteger",
    "PositiveSafeInteger",
    "SafeInteger",
]
