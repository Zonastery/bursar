from __future__ import annotations

from collections.abc import Callable
from typing import Any

DbQuery = Callable[[str, list[Any]], list[Any]]
