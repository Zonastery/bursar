from __future__ import annotations

from collections.abc import Callable
from typing import Any

CallProc = Callable[[str, list[Any]], list[Any]]
QueryFn = Callable[[str, list[Any]], list[Any]]
