"""Optional framework and provider integrations for Bursar."""

from bursar.integrations import _warnings as _dependency_warnings  # noqa: F401
from bursar.integrations.ai import ProviderReceipt, ProviderReceiptSource

__all__ = ["ProviderReceipt", "ProviderReceiptSource"]
