"""Scoped dependency warning policy for optional framework integrations."""

from __future__ import annotations

import warnings


def configure_google_adk_import_warnings() -> None:
    """Hide only ADK's unavoidable deprecated-config import warning.

    Google ADK 2.6.3 still imports deprecated YAML configuration models while
    loading ``google.adk``. Bursar does not use those models. Keep this filter
    exact and limited to that upstream message; all other deprecations remain
    visible.
    """

    warnings.filterwarnings(
        "ignore",
        message=r"^BaseAgentConfig is deprecated and will be removed in future versions\..*",
        category=DeprecationWarning,
    )


configure_google_adk_import_warnings()
