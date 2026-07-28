"""Shared config parsing utilities — mirrors JS SDK's ``config/parse-utils.ts``."""

from __future__ import annotations

from bursar.config.types import (
    Charge,
    ExpressionCharge,
    GraduatedCharge,
    PackageCharge,
    PerUnitCharge,
    SumCharge,
    VolumeCharge,
)


def _charge_measure_names(charge: Charge) -> set[str]:
    if isinstance(charge, (PerUnitCharge, PackageCharge, GraduatedCharge, VolumeCharge)):
        return {charge.measure}
    if isinstance(charge, SumCharge):
        return set().union(*(_charge_measure_names(component) for component in charge.components))
    return set()


def _expression_charges(charge: Charge) -> list[ExpressionCharge]:
    if isinstance(charge, ExpressionCharge):
        return [charge]
    if isinstance(charge, SumCharge):
        return [item for component in charge.components for item in _expression_charges(component)]
    return []
