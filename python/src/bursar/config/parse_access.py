"""Admission/entitlements config parser — mirrors JS SDK's ``config/parse-access.ts``."""

from __future__ import annotations

from bursar.config.types import (
    AdmissionConfig,
    EntitlementsConfig,
    _validate_map_keys,
)


def _validate_admission(admission: AdmissionConfig) -> AdmissionConfig:
    _validate_map_keys(admission.policies, "admission.policies")
    return admission


def _validate_entitlements(entitlements: EntitlementsConfig) -> EntitlementsConfig:
    _validate_map_keys(entitlements.features, "entitlements.features")
    return entitlements
