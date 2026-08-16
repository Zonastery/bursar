"""Google ADK plugin for replay-safe dynamic model billing."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bursar.bursar import CreditsCapability
from bursar.credits.service_types import BeginBilledOperationOptions
from bursar.credits.types import CreditMetadata
from bursar.errors import (
    BursarError,
    LeaseExpiredError,
    LeaseNotFoundError,
    bursar_error_public_message,
    is_retryable_bursar_error,
)
from bursar.integrations.ai import ProviderReceipt, ProviderReceiptSource
from bursar.metrics import UsageMetrics
from bursar.retry import BursarRetryOptions, retry_bursar_operation
from bursar.shared.logger import Logger, NormalizedLogger, normalize_logger

try:
    from google.adk.plugins import BasePlugin
    from google.genai import types as genai_types
except ImportError as error:  # pragma: no cover - exercised by packaging, not an installed extra
    from bursar.errors import BursarImportError

    raise BursarImportError("Google ADK support requires 'bursar[google-adk]'") from error

if TYPE_CHECKING:
    from google.adk.models.llm_response import LlmResponse


SubjectResolver = Callable[[Any], str | None]
BillingSelector = Callable[[Any, Any], bool]
AdmissionMessageFactory = Callable[[BaseException | None], str]
MetadataFactory = Callable[[Any], CreditMetadata | None]

_GENERIC_BILLING_FAILURE = "Billing service is temporarily unavailable. Please try again."


def _default_subject_resolver(callback_context: Any) -> str | None:
    user_id = getattr(callback_context, "user_id", None)
    if user_id:
        return str(user_id)
    session = getattr(callback_context, "session", None)
    session_user_id = getattr(session, "user_id", None)
    return str(session_user_id) if session_user_id else None


def _default_admission_message(error: BaseException | None) -> str:
    return bursar_error_public_message(error) if isinstance(error, BursarError) else _GENERIC_BILLING_FAILURE


def _non_negative_decimal(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        return Decimal(0)
    try:
        result = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal(0)
    return result if result.is_finite() and result >= 0 else Decimal(0)


class BursarPlugin(BasePlugin):
    """Map Google ADK model hooks onto Bursar's lease lifecycle.

    The plugin performs financial admission and settlement only. ADK/OpenTelemetry
    integrations remain responsible for operational telemetry such as latency,
    logs, retry spans, and prompt/response capture.

    ``estimate`` is also the accounting schema for the adapter: only measures
    and dimensions present on it are forwarded to Bursar. This keeps the plugin
    compatible with each application's declared pricing operation.
    """

    def __init__(
        self,
        credits: CreditsCapability,
        *,
        estimate: UsageMetrics,
        operation_type: str | None = None,
        feature: str | None = None,
        provider: str | None = None,
        ttl: int | None = None,
        receipt_source: ProviderReceiptSource | None = None,
        subject_resolver: SubjectResolver | None = None,
        should_bill: BillingSelector | None = None,
        admission_message: AdmissionMessageFactory | None = None,
        metadata_factory: MetadataFactory | None = None,
        reference_type: str = "adk_invocation",
        operation_key_prefix: str = "adk-model",
        state_namespace: str = "default",
        retry_options: BursarRetryOptions | None = None,
        logger: Logger | None = None,
        name: str = "bursar",
    ) -> None:
        super().__init__(name=name)
        if not estimate.measures:
            raise ValueError("estimate must declare at least one billing measure")
        if not operation_key_prefix.strip():
            raise ValueError("operation_key_prefix must not be empty")
        if not state_namespace.strip():
            raise ValueError("state_namespace must not be empty")
        if not reference_type.strip():
            raise ValueError("reference_type must not be empty")

        self._credits = credits
        self._estimate = estimate.model_copy(deep=True)
        self._operation_type = operation_type or estimate.operation
        self._feature = feature
        self._provider = provider
        self._ttl = ttl
        self._receipt_source = receipt_source
        self._subject_resolver = subject_resolver or _default_subject_resolver
        self._should_bill = should_bill or (lambda _context, _request: True)
        self._admission_message = admission_message or _default_admission_message
        self._metadata_factory = metadata_factory
        self._reference_type = reference_type
        self._operation_key_prefix = operation_key_prefix
        self._state_prefix = f"_bursar_model_leases:{state_namespace}:"
        self._retry_options = retry_options or BursarRetryOptions()
        self._logger: NormalizedLogger = normalize_logger(logger)
        self._active_operation: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"bursar_adk_operation_{id(self)}",
            default=None,
        )

    async def before_run_callback(self, *, invocation_context: Any) -> None:
        """Replay completed settlements left by an interrupted prior run."""

        user_id, state = self._invocation_scope(invocation_context)
        if user_id and state is not None:
            await self._settle_ready_leases(state, user_id)

    async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> LlmResponse | None:
        """Reserve a lease before provider transport begins."""

        try:
            if not self._should_bill(callback_context, llm_request):
                return None
        except Exception as error:
            self._logger.error("adk_billing_selector_failed", {"error_type": type(error).__name__})
            return self._admission_denied(error)

        user_id = self._resolve_subject(callback_context)
        state = getattr(callback_context, "state", None)
        session = getattr(callback_context, "session", None)
        invocation_id = str(getattr(callback_context, "invocation_id", "") or "")
        if not user_id or state is None or session is None or not invocation_id:
            self._logger.error(
                "adk_billing_context_missing",
                {
                    "has_user": bool(user_id),
                    "has_state": state is not None,
                    "has_session": session is not None,
                    "has_invocation": bool(invocation_id),
                },
            )
            return self._admission_denied(None)

        stale_receipt = self._finish_receipt_capture()
        if stale_receipt is not None:
            await self._complete_active_call(callback_context, llm_response=None, receipt=stale_receipt)
        await self._settle_ready_leases(state, user_id)
        await self._release_unpriced_leases(state, user_id, invocation_id)

        operation_key = f"{self._operation_key_prefix}:{invocation_id}:{uuid4()}"
        estimate = self._estimate_for_model(getattr(llm_request, "model", None))
        try:
            metadata = self._base_metadata(callback_context, invocation_id)
            operation = await asyncio.to_thread(
                retry_bursar_operation,
                self._credits.begin_billed_operation,
                user_id,
                BeginBilledOperationOptions(
                    estimate=estimate,
                    operation_key=operation_key,
                    operation_type=self._operation_type,
                    ttl=self._ttl,
                    feature=self._feature,
                    metadata=metadata,
                ),
                retry_options=self._retry_options,
            )
        except asyncio.CancelledError:
            raise
        except BursarError as error:
            self._logger.warn(
                "adk_billing_reserve_failed",
                {"code": error.code, "retryable": error.retryable, "user_id": user_id},
            )
            return self._admission_denied(error)
        except Exception as error:
            self._logger.error(
                "adk_billing_reserve_failed",
                {"error_type": type(error).__name__, "user_id": user_id},
            )
            return self._admission_denied(error)

        lease_key = self._lease_key(invocation_id)
        pending = self._lease_entries(state, invocation_id)
        pending.append(
            {
                "lease_id": operation.lease_id,
                "operation_key": operation_key,
                "metadata": metadata.model_dump(mode="json", exclude_none=True),
            }
        )
        state[lease_key] = pending
        self._active_operation.set(operation_key)
        self._begin_receipt_capture()
        return None

    async def after_model_callback(self, *, callback_context: Any, llm_response: Any) -> None:
        """Settle a completed provider call, waiting for the final stream part."""

        if getattr(llm_response, "partial", False):
            return
        receipt = self._finish_receipt_capture()
        await self._complete_active_call(callback_context, llm_response=llm_response, receipt=receipt)

    async def on_model_error_callback(
        self,
        *,
        callback_context: Any,
        llm_request: Any,
        error: Exception,
    ) -> None:
        """Settle a provider success receipt or release an unbilled failure."""

        del error
        receipt = self._finish_receipt_capture()
        if receipt is not None:
            await self._complete_active_call(
                callback_context,
                llm_response=None,
                receipt=receipt,
                request_model=getattr(llm_request, "model", None),
            )
            return
        await self._release_active_call(callback_context)

    async def on_agent_error_callback(self, *, agent: Any, callback_context: Any, error: Exception) -> None:
        """Clean up a lease when a later agent callback fails before transport."""

        del agent, error
        await self._cleanup_failed_invocation(callback_context)

    async def on_run_error_callback(self, *, invocation_context: Any, error: Exception) -> None:
        """Best-effort cleanup for errors escaping the complete ADK run."""

        del error
        receipt = self._finish_receipt_capture()
        user_id, state = self._invocation_scope(invocation_context)
        invocation_id = str(getattr(invocation_context, "invocation_id", "") or "")
        if user_id and state is not None and invocation_id:
            if receipt is not None:
                await self._complete_scoped_call(
                    state,
                    user_id,
                    invocation_id,
                    llm_response=None,
                    receipt=receipt,
                )
            await self._settle_ready_leases(state, user_id)
            await self._release_unpriced_leases(state, user_id, invocation_id)
        self._active_operation.set(None)

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        """Release holds for downstream short-circuits that skipped model hooks."""

        receipt = self._finish_receipt_capture()
        user_id, state = self._invocation_scope(invocation_context)
        invocation_id = str(getattr(invocation_context, "invocation_id", "") or "")
        if user_id and state is not None and invocation_id:
            if receipt is not None:
                await self._complete_scoped_call(
                    state,
                    user_id,
                    invocation_id,
                    llm_response=None,
                    receipt=receipt,
                )
            await self._settle_ready_leases(state, user_id)
            await self._release_unpriced_leases(state, user_id, invocation_id)
        self._active_operation.set(None)

    def _resolve_subject(self, context: Any) -> str | None:
        try:
            return self._subject_resolver(context)
        except Exception as error:
            self._logger.error("adk_billing_subject_resolution_failed", {"error_type": type(error).__name__})
            return None

    def _base_metadata(self, context: Any, invocation_id: str) -> CreditMetadata:
        custom = self._metadata_factory(context) if self._metadata_factory else None
        values = custom.model_dump(exclude_none=True) if custom else {}
        values.setdefault("reference_type", self._reference_type)
        values.setdefault("reference_id", invocation_id)
        return CreditMetadata.model_validate(values)

    def _admission_denied(self, error: BaseException | None) -> LlmResponse:
        try:
            message = self._admission_message(error)
        except Exception as message_error:
            self._logger.error(
                "adk_billing_admission_message_failed",
                {"error_type": type(message_error).__name__},
            )
            message = _GENERIC_BILLING_FAILURE
        from google.adk.models.llm_response import LlmResponse

        return LlmResponse(
            content=genai_types.Content(role="model", parts=[genai_types.Part(text=message)]),
            error_code="ADMISSION_DENIED",
            error_message=message,
        )

    def _begin_receipt_capture(self) -> None:
        if self._receipt_source is None:
            return
        try:
            self._receipt_source.begin()
        except Exception as error:
            self._logger.error("adk_provider_receipt_begin_failed", {"error_type": type(error).__name__})

    def _finish_receipt_capture(self) -> ProviderReceipt | None:
        if self._receipt_source is None:
            return None
        try:
            return self._receipt_source.finish()
        except Exception as error:
            self._logger.error("adk_provider_receipt_finish_failed", {"error_type": type(error).__name__})
            return None

    async def _cleanup_failed_invocation(self, callback_context: Any) -> None:
        receipt = self._finish_receipt_capture()
        if receipt is not None:
            await self._complete_active_call(callback_context, llm_response=None, receipt=receipt)
            return
        await self._release_active_call(callback_context)

    async def _complete_active_call(
        self,
        callback_context: Any,
        *,
        llm_response: Any | None,
        receipt: ProviderReceipt | None,
        request_model: object | None = None,
    ) -> None:
        user_id = self._resolve_subject(callback_context)
        state = getattr(callback_context, "state", None)
        invocation_id = str(getattr(callback_context, "invocation_id", "") or "")
        if not user_id or state is None or not invocation_id:
            self._active_operation.set(None)
            return

        await self._complete_scoped_call(
            state,
            user_id,
            invocation_id,
            llm_response=llm_response,
            receipt=receipt,
            request_model=request_model,
        )

    async def _complete_scoped_call(
        self,
        state: Any,
        user_id: str,
        invocation_id: str,
        *,
        llm_response: Any | None,
        receipt: ProviderReceipt | None,
        request_model: object | None = None,
    ) -> None:

        lease_key = self._lease_key(invocation_id)
        pending = self._lease_entries(state, invocation_id)
        entry = self._active_entry(pending)
        if entry is None:
            self._logger.debug("adk_billing_settle_skipped", {"reason": "no_lease"})
            self._active_operation.set(None)
            return

        metrics = self._actual_metrics(receipt, llm_response, request_model)
        metadata = self._settlement_metadata(entry, receipt)
        entry["metrics"] = metrics.model_dump(mode="json")
        entry["metadata"] = metadata.model_dump(mode="json", exclude_none=True)
        state[lease_key] = pending
        self._active_operation.set(None)
        await self._settle_ready_leases(state, user_id, lease_keys=[lease_key])

    async def _release_active_call(self, callback_context: Any) -> None:
        user_id = self._resolve_subject(callback_context)
        state = getattr(callback_context, "state", None)
        invocation_id = str(getattr(callback_context, "invocation_id", "") or "")
        if not user_id or state is None or not invocation_id:
            self._active_operation.set(None)
            return
        lease_key = self._lease_key(invocation_id)
        pending = self._lease_entries(state, invocation_id)
        entry = self._active_entry(pending)
        if entry is not None:
            resolved = await self._release_lease(user_id, str(entry["lease_id"]))
            if resolved:
                self._remove_entry(state, lease_key, pending, entry)
        self._active_operation.set(None)

    def _active_entry(self, pending: list[dict[str, object]]) -> dict[str, object] | None:
        operation_key = self._active_operation.get()
        if operation_key:
            matched = next((entry for entry in pending if entry.get("operation_key") == operation_key), None)
            if matched is not None:
                return matched
        return next((entry for entry in pending if not isinstance(entry.get("metrics"), Mapping)), None)

    def _actual_metrics(
        self,
        receipt: ProviderReceipt | None,
        llm_response: Any | None,
        request_model: object | None,
    ) -> UsageMetrics:
        allowed_measures = set(self._estimate.measures)
        values = dict(self._estimate.measures)
        usage = getattr(llm_response, "usage_metadata", None) if llm_response is not None else None

        if usage is not None:
            input_tokens = _non_negative_decimal(getattr(usage, "prompt_token_count", 0))
            output_tokens = _non_negative_decimal(getattr(usage, "candidates_token_count", 0))
            reported_total = getattr(usage, "total_token_count", None)
            standard = {
                "calls": Decimal(1),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": (
                    _non_negative_decimal(reported_total)
                    if reported_total is not None
                    else input_tokens + output_tokens
                ),
                "cache_read_tokens": _non_negative_decimal(getattr(usage, "cached_content_token_count", 0)),
                "reasoning_tokens": _non_negative_decimal(getattr(usage, "thoughts_token_count", 0)),
            }
            values.update({key: value for key, value in standard.items() if key in allowed_measures})
        elif receipt is None:
            self._logger.warn("adk_billing_usage_missing", {"action": "settle_estimate"})

        if receipt is not None:
            values.update({key: value for key, value in receipt.metrics.measures.items() if key in allowed_measures})

        tool_measures = self._tool_measures(llm_response)
        values.update({key: value for key, value in tool_measures.items() if key in allowed_measures})

        allowed_dimensions = set(self._estimate.dimensions)
        dimensions = dict(self._estimate.dimensions)
        if receipt is not None:
            dimensions.update(
                {key: value for key, value in receipt.metrics.dimensions.items() if key in allowed_dimensions}
            )
        if "provider" in allowed_dimensions and self._provider and not dimensions.get("provider"):
            dimensions["provider"] = self._provider
        if "model" in allowed_dimensions and not (receipt and receipt.metrics.dimensions.get("model")):
            response_model = getattr(llm_response, "model_version", None) if llm_response is not None else None
            model = response_model or request_model
            if model:
                dimensions["model"] = str(model)

        return UsageMetrics(operation=self._estimate.operation, measures=values, dimensions=dimensions)

    def _tool_measures(self, llm_response: Any | None) -> dict[str, Decimal]:
        if llm_response is None:
            return {}
        try:
            calls = llm_response.get_function_calls()
            names = [str(call.name) for call in calls if getattr(call, "name", None)]
        except Exception as error:
            self._logger.warn("adk_billing_tool_usage_failed", {"error_type": type(error).__name__})
            return {}
        return {
            "tool_calls": Decimal(len(names)),
            "web_search_calls": Decimal(sum("search" in name.casefold() for name in names)),
            "code_exec_calls": Decimal(
                sum(
                    "code" in name.casefold() or "exec" in name.casefold() or "sandbox" in name.casefold()
                    for name in names
                )
            ),
        }

    def _estimate_for_model(self, model: object | None) -> UsageMetrics:
        estimate = self._estimate.model_copy(deep=True)
        if model and "model" in estimate.dimensions:
            estimate.dimensions["model"] = str(model)
        if self._provider and "provider" in estimate.dimensions:
            estimate.dimensions["provider"] = self._provider
        return estimate

    def _settlement_metadata(
        self,
        entry: Mapping[str, object],
        receipt: ProviderReceipt | None,
    ) -> CreditMetadata:
        stored = entry.get("metadata")
        base = dict(stored) if isinstance(stored, Mapping) else {}
        receipt_values = receipt.metadata.model_dump(exclude_none=True) if receipt and receipt.metadata else {}
        values = {**base, **receipt_values}
        for key in ("reference_type", "reference_id"):
            if base.get(key) is not None:
                values[key] = base[key]
        try:
            return CreditMetadata.model_validate(values)
        except ValueError as error:
            self._logger.warn(
                "adk_provider_receipt_metadata_invalid",
                {"error_type": type(error).__name__},
            )
            return CreditMetadata.model_validate(base)

    async def _settle_ready_leases(
        self,
        state: Any,
        user_id: str,
        *,
        lease_keys: list[str] | None = None,
    ) -> None:
        keys = lease_keys or [key for key in self._state_keys(state) if key.startswith(self._state_prefix)]
        for lease_key in keys:
            invocation_id = lease_key.removeprefix(self._state_prefix)
            pending = self._lease_entries(state, invocation_id)
            for entry in tuple(pending):
                payload = entry.get("metrics")
                if not isinstance(payload, Mapping):
                    continue
                lease_id = str(entry["lease_id"])
                operation_key = str(entry["operation_key"])
                try:
                    metrics = UsageMetrics.model_validate(dict(payload))
                    metadata_payload = entry.get("metadata")
                    metadata = CreditMetadata.model_validate(
                        dict(metadata_payload) if isinstance(metadata_payload, Mapping) else {}
                    )
                except (TypeError, ValueError) as error:
                    resolved = await self._release_lease(user_id, lease_id)
                    if resolved:
                        self._remove_entry(state, lease_key, pending, entry)
                    self._logger.error(
                        "adk_billing_state_invalid",
                        {"lease_id": lease_id, "error_type": type(error).__name__},
                    )
                    continue

                try:
                    operation = self._credits.resume_billed_operation(
                        user_id,
                        lease_id,
                        operation_key,
                        feature=self._feature,
                        metadata=metadata,
                    )
                    result = await asyncio.to_thread(
                        retry_bursar_operation,
                        operation.settle,
                        metrics,
                        retry_options=self._retry_options,
                    )
                except asyncio.CancelledError:
                    raise
                except (LeaseExpiredError, LeaseNotFoundError) as error:
                    self._remove_entry(state, lease_key, pending, entry)
                    self._logger.warn(
                        "adk_billing_lease_terminal",
                        {"lease_id": lease_id, "code": error.code},
                    )
                except BursarError as error:
                    if not is_retryable_bursar_error(error):
                        self._remove_entry(state, lease_key, pending, entry)
                    self._logger.warn(
                        "adk_billing_settle_failed",
                        {"lease_id": lease_id, "code": error.code, "retryable": error.retryable},
                    )
                except Exception as error:
                    self._logger.error(
                        "adk_billing_settle_failed",
                        {"lease_id": lease_id, "error_type": type(error).__name__},
                    )
                else:
                    self._remove_entry(state, lease_key, pending, entry)
                    self._logger.info(
                        "adk_billing_settled",
                        {
                            "lease_id": lease_id,
                            "amount": str(abs(result.amount)),
                            "usage_id": getattr(result, "usage_charge_id", None),
                        },
                    )

    async def _release_unpriced_leases(self, state: Any, user_id: str, invocation_id: str) -> None:
        lease_key = self._lease_key(invocation_id)
        pending = self._lease_entries(state, invocation_id)
        for entry in tuple(pending):
            if isinstance(entry.get("metrics"), Mapping):
                continue
            if await self._release_lease(user_id, str(entry["lease_id"])):
                self._remove_entry(state, lease_key, pending, entry)

    async def _release_lease(self, user_id: str, lease_id: str) -> bool:
        try:
            await asyncio.to_thread(
                retry_bursar_operation,
                self._credits.release,
                user_id,
                lease_id,
                retry_options=self._retry_options,
            )
            return True
        except asyncio.CancelledError:
            raise
        except BursarError as error:
            self._logger.warn(
                "adk_billing_release_failed",
                {"lease_id": lease_id, "code": error.code, "retryable": error.retryable},
            )
            return not is_retryable_bursar_error(error)
        except Exception as error:
            self._logger.error(
                "adk_billing_release_failed",
                {"lease_id": lease_id, "error_type": type(error).__name__},
            )
            return False

    def _lease_key(self, invocation_id: str) -> str:
        return f"{self._state_prefix}{invocation_id}"

    def _lease_entries(self, state: Any, invocation_id: str) -> list[dict[str, object]]:
        key = self._lease_key(invocation_id)
        value = state.get(key)
        entries: list[dict[str, object]] = []
        if isinstance(value, list):
            for candidate in value:
                if not isinstance(candidate, Mapping):
                    continue
                lease_id = candidate.get("lease_id")
                operation_key = candidate.get("operation_key")
                if not isinstance(lease_id, str) or not isinstance(operation_key, str) or not operation_key:
                    continue
                entry: dict[str, object] = {"lease_id": lease_id, "operation_key": operation_key}
                for field in ("metrics", "metadata"):
                    if isinstance(candidate.get(field), Mapping):
                        entry[field] = dict(candidate[field])
                entries.append(entry)
        if entries:
            state[key] = entries
        else:
            self._clear_state_key(state, key)
        return entries

    @staticmethod
    def _state_keys(state: Any) -> list[str]:
        if isinstance(state, Mapping):
            return [str(key) for key in state]
        to_dict = getattr(state, "to_dict", None)
        if callable(to_dict):
            snapshot = to_dict()
            if isinstance(snapshot, Mapping):
                return [str(key) for key in snapshot]
        return []

    @staticmethod
    def _clear_state_key(state: Any, key: str) -> None:
        if isinstance(state, dict):
            state.pop(key, None)
        else:
            state[key] = []

    def _remove_entry(
        self,
        state: Any,
        lease_key: str,
        pending: list[dict[str, object]],
        entry: dict[str, object],
    ) -> None:
        try:
            pending.remove(entry)
        except ValueError:
            return
        if pending:
            state[lease_key] = pending
        else:
            self._clear_state_key(state, lease_key)

    def _invocation_scope(self, invocation_context: Any) -> tuple[str | None, Any | None]:
        user_id = self._resolve_subject(invocation_context)
        session = getattr(invocation_context, "session", None)
        state = getattr(session, "state", None) if session is not None else None
        return user_id, state


__all__ = ["BursarPlugin"]
