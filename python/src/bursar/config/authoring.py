"""Normalize public catalog shorthand before semantic validation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from bursar.errors import ConfigError


def _normalize_credits(result: dict[str, Any]) -> None:
    credits = result.setdefault("credits", {})
    if not isinstance(credits, dict):
        raise ConfigError("credits must be an object")
    has_signup = "on_signup" in credits
    signup = credits.pop("on_signup", None)
    if "buckets" not in credits:
        credits["buckets"] = {"default": {"priority": 0}}
        credits.setdefault("default_bucket", "default")
    if has_signup and signup is not None:
        if not isinstance(credits.get("default_bucket"), str):
            raise ConfigError("credits.on_signup requires credits.default_bucket")
        programs = credits.setdefault("grant_programs", {})
        if (
            not isinstance(programs, dict)
            or "signup" in programs
            or any(
                isinstance(program, dict) and program.get("trigger") == "account_created"
                for program in programs.values()
            )
        ):
            raise ConfigError("credits.on_signup conflicts with an account_created grant program")
        programs["signup"] = {
            "trigger": "account_created",
            "awards": [{"amount": signup, "bucket": credits["default_bucket"]}],
        }


def _normalize_measures(operation: dict[str, Any]) -> None:
    measures = operation.get("measures")
    if isinstance(measures, dict):
        for definition in measures.values():
            if isinstance(definition, dict):
                definition.setdefault("required", True)


def _normalize_pricing(result: dict[str, Any]) -> None:
    pricing = result.get("pricing")
    if isinstance(pricing, dict) and isinstance(pricing.get("operations"), dict):
        cards = pricing.setdefault("rate_cards", {})
        if not isinstance(cards, dict):
            raise ConfigError("pricing.rate_cards must be an object")
        shorthand: dict[str, Any] = {}
        for key, operation in pricing["operations"].items():
            if not isinstance(operation, dict):
                continue
            _normalize_measures(operation)
            charge = None
            if "charge" in operation:
                charge = operation.pop("charge")
            if charge is not None:
                shorthand[key] = {"unmatched": {"action": "charge", "charge": charge}}
        if shorthand:
            if not cards:
                cards["standard"] = {"operations": {}}
            for card_key, card in cards.items():
                if not isinstance(card, dict):
                    raise ConfigError(f"pricing.rate_cards.{card_key} must be an object")
                if card.get("extends") is not None:
                    continue
                operations = card.setdefault("operations", {})
                if not isinstance(operations, dict):
                    raise ConfigError(f"pricing.rate_cards.{card_key}.operations must be an object")
                for key, value in shorthand.items():
                    operations.setdefault(key, value)


def _normalize_plans(result: dict[str, Any]) -> None:
    pricing = result.get("pricing")
    plans = result.get("plans", {})
    if isinstance(plans, dict):
        for plan in plans.values():
            if not isinstance(plan, dict):
                continue
            plan.setdefault("tier", 0)
            if isinstance(pricing, dict) and isinstance(pricing.get("rate_cards"), dict):
                cards = pricing["rate_cards"]
                if len(cards) == 1 and plan.get("rate_card") is None:
                    plan["rate_card"] = next(iter(cards))


def _infer_provider_types(result: dict[str, Any]) -> None:
    commerce = result.get("commerce")
    if not isinstance(commerce, dict):
        return
    providers = commerce.get("providers")
    offers = commerce.get("offers")
    if not isinstance(providers, dict) or not isinstance(offers, dict):
        return
    for offer in offers.values():
        references = offer.get("providers") if isinstance(offer, dict) else None
        if not isinstance(references, dict):
            continue
        for key, reference in references.items():
            provider = providers.get(key)
            if not isinstance(reference, dict) or not isinstance(provider, dict) or "type" in reference:
                continue
            provider_type = provider.get("type")
            kind = (
                {"stripe": "stripe_price", "dodo": "dodo_product"}.get(provider_type)
                if isinstance(provider_type, str)
                else None
            )
            if kind is not None:
                reference["type"] = kind


def _scope_scalar_ids(result: dict[str, Any], environment: str | None) -> None:
    if environment is None:
        return
    commerce = result.get("commerce", {})
    if not isinstance(commerce, dict) or not isinstance(commerce.get("offers", {}), dict):
        return
    for offer in commerce.get("offers", {}).values():
        if not isinstance(offer, dict) or not isinstance(offer.get("providers"), dict):
            continue
        for reference in offer["providers"].values():
            if not isinstance(reference, dict):
                continue
            for field in ("price_id", "product_id", "external_id"):
                value = reference.get(field)
                if isinstance(value, str):
                    reference[field] = {environment: value}


def normalize_config(data: dict[str, Any], environment: str | None = None) -> dict[str, Any]:
    if environment is not None and environment not in ("test", "live", "sandbox"):
        raise ConfigError("environment must be test, live, or sandbox")
    result = deepcopy(data)
    _normalize_credits(result)
    _normalize_pricing(result)
    _normalize_plans(result)
    _scope_scalar_ids(result, environment)
    _infer_provider_types(result)
    return result
