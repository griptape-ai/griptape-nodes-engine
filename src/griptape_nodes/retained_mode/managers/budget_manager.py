"""BudgetManager - Attributes outbound calls to a project, and gates and reports direct provider spend.

The project chain is read from the project manager per request, so a project switch between two
calls shows up on the second. Calls through Griptape Cloud carry the attribution header and Cloud
enforces budgets itself; `griptape_nodes.utils.budget_refusal` turns its refusal into something
an artist can act on. A direct provider call bypasses Cloud, so this manager asks
`POST /api/budget-checks` before it and posts `POST /api/spend` after it.

Project ids travel exactly as stored -- unstripped, uncut, never repaired.

The header value stays out of `result_details`, which is logged. That is all this module can do
about disclosure: `broadcast_result` is a field any caller can flip, post-dispatch hooks see the
full result, and worker forwarding puts the Success payload on the response topic.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx2

from griptape_nodes.drivers.cloud_credentials import (
    MISSING_CREDENTIAL_MESSAGE,
    resolve_cloud_base_url,
    resolve_cloud_credential,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.budget_events import (
    ATTRIBUTION_SCHEMA_VERSION,
    BudgetAccessRequest,
    BudgetAccessResultFailure,
    BudgetAccessResultSuccess,
    GetAttributionContextRequest,
    GetAttributionContextResultFailure,
    GetAttributionContextResultSuccess,
    ReportUsageRequest,
    ReportUsageResultFailure,
    ReportUsageResultSuccess,
)
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.budget_refusal import (
    BudgetExceededError,
    describe,
    log_line,
    refusal_from_check,
)
from griptape_nodes.utils.http_utils import is_retryable_httpx_error, request_with_retry

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")

BUDGET_CHECKS_PATH = "/api/budget-checks"
SPEND_PATH = "/api/spend"

# The check blocks the node, so it gets one short attempt and fails open rather than retrying.
CHECK_TIMEOUT_SECONDS = 5.0
REPORT_TIMEOUT_SECONDS = 10.0
REPORT_MAX_ATTEMPTS = 3

# Cloud's length caps on the free-text descriptors. A value over its cap is left out rather than
# sent, so one long name cannot get the whole report rejected.
_DESCRIPTOR_CAPS = {"provider": 64, "model": 255, "activity_type": 255}


def _build_attribution_payload(project_chain: list[str]) -> dict[str, Any]:
    """Build the decoded payload for a project chain, which may be empty.

    An empty chain omits `tags` and the bare `{"v": 1}` still goes out, for forward-compatibility.
    """
    if not project_chain:
        return {"v": ATTRIBUTION_SCHEMA_VERSION}
    return {"v": ATTRIBUTION_SCHEMA_VERSION, "tags": {"project": list(project_chain)}}


def _encode_attribution_payload(payload: dict[str, Any]) -> str | None:
    """Encode a payload as base64url with padding kept, or None when it cannot be encoded.

    A project id derived from a filesystem path whose bytes are not valid UTF-8 holds lone
    surrogates that `str.encode` refuses. There is no partial form to fall back to, so the whole
    header is given up.

    Returns None rather than raising, because a handler exception becomes a `GenericResultFailure`,
    which ignores `failure_log_level`. The traceback stays: `UnicodeEncodeError` names the
    offending codepoint and position, the only pointer to which entry of a deep chain is bad.
    """
    try:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        logger.warning(
            "Attribution payload could not be encoded as UTF-8; sending no attribution header.",
            exc_info=True,
        )
        return None
    return base64.urlsafe_b64encode(raw).decode("ascii")


class BudgetManager(EngineScoped):
    """Attributes credit-consuming calls, and checks and reports direct provider spend with Cloud."""

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        """Initialize the BudgetManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
            engine: The owning Engine, used to resolve peer managers.
        """
        super().__init__(engine)
        # Reports post off the caller's thread. Not an asyncio task: a sync dispatch runs the
        # handler under a short-lived loop that would take the task down with it.
        self._report_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="budget-report")
        event_manager.register_request_handlers(self)

    @handles(GetAttributionContextRequest)
    def on_get_attribution_context_request(
        self,
        request: GetAttributionContextRequest,  # noqa: ARG002
    ) -> GetAttributionContextResultSuccess | GetAttributionContextResultFailure:
        """Describe the current project as an encoded attribution header.

        Both failures send no header rather than a bare `{"v": 1}`, so the caller gets a Failure
        it can act on instead of a Success carrying an empty chain. Neither blocks the call. Not
        knowing which project to bill is not a reason to refuse work: the spend is legitimate, it
        just lands unattributed. A budget refusal is the opposite case -- Cloud has already
        declined the call -- and that one fails the node.

        Both log at WARNING rather than the ERROR a bare `result_details` string would default to.
        Neither condition clears on its own, so an ERROR would repeat once per metered call for
        something the artist cannot act on and that did not stop the work.
        """
        project_chain = self._resolve_project_chain()
        if project_chain is None:
            return GetAttributionContextResultFailure(
                result_details=ResultDetails(
                    message=(
                        "Attempted to describe an outbound request for budget attribution. Failed because the "
                        "current project's ancestry could not be read. The request can proceed, but this spend "
                        "will not be attributed."
                    ),
                    level=logging.WARNING,
                )
            )

        header_value = _encode_attribution_payload(_build_attribution_payload(project_chain))
        if header_value is None:
            return GetAttributionContextResultFailure(
                result_details=ResultDetails(
                    message=(
                        "Attempted to describe an outbound request for budget attribution. Failed because a "
                        "project id contains characters that cannot be sent in a request header. The request "
                        "can proceed, but this spend will not be attributed."
                    ),
                    level=logging.WARNING,
                )
            )

        return GetAttributionContextResultSuccess(
            header_value=header_value,
            project_chain=list(project_chain),
            result_details=(
                f"Successfully described the attribution context for an outbound request "
                f"({len(project_chain)} project(s) in the chain)."
            ),
        )

    @handles(BudgetAccessRequest)
    def on_budget_access_request(
        self, request: BudgetAccessRequest
    ) -> BudgetAccessResultSuccess | BudgetAccessResultFailure:
        """Ask Cloud whether a direct provider call fits its budgets; fail open unless Cloud says no."""
        correlation_id = uuid.uuid4().hex
        body: dict[str, Any] = {
            "tags": self._build_tags(request.node_type, request.node_id),
            "model": request.model_id,
            "correlation_id": correlation_id,
        }
        if request.estimated_cost_micro_usd is not None:
            body["estimated_cost_micro_usd"] = request.estimated_cost_micro_usd

        try:
            response = self._post(BUDGET_CHECKS_PATH, body, timeout=CHECK_TIMEOUT_SECONDS, max_attempts=1)
        except _NoCredentialError:
            return self._unchecked(correlation_id, f"Failed because {MISSING_CREDENTIAL_MESSAGE}")
        except httpx2.HTTPError as exc:
            return self._unchecked(correlation_id, f"Failed because Griptape Cloud could not be reached ({exc}).")

        try:
            result = response.json()
        except ValueError:
            return self._unchecked(correlation_id, "Failed because Griptape Cloud's answer could not be read.")
        if not isinstance(result, dict) or not isinstance(result.get("allowed"), bool):
            return self._unchecked(correlation_id, "Failed because Griptape Cloud's answer could not be read.")

        if result["allowed"] is False:
            refusal = refusal_from_check(result)
            message = describe(refusal)
            logger.warning(log_line(refusal))
            blocked_by = result.get("blocked_by")
            if not isinstance(blocked_by, list):
                blocked_by = []
            return BudgetAccessResultFailure(
                blocked_by=[entry for entry in blocked_by if isinstance(entry, dict)],
                correlation_id=correlation_id,
                result_details=message,
                exception=BudgetExceededError(message, refusal),
            )

        remaining = result.get("effective_remaining_credits")
        if isinstance(remaining, bool) or not isinstance(remaining, int):
            remaining = None
        return BudgetAccessResultSuccess(
            correlation_id=correlation_id,
            checked=True,
            effective_remaining_credits=remaining,
            result_details="Griptape Cloud cleared a direct model call against its budgets.",
        )

    @handles(ReportUsageRequest)
    def on_report_usage_request(
        self, request: ReportUsageRequest
    ) -> ReportUsageResultSuccess | ReportUsageResultFailure:
        """Queue a usage report for a direct provider call and return without waiting on Cloud."""
        if request.declared_cost_micro_usd < 0:
            return self._report_not_sent(
                f"Failed because the declared cost {request.declared_cost_micro_usd} is negative."
            )
        if resolve_cloud_credential(self.engine.secrets_manager) is None:
            return self._report_not_sent(f"Failed because {MISSING_CREDENTIAL_MESSAGE}")

        # Minted once and captured in `body`, so every retry sends the same key.
        idempotency_key = uuid.uuid4().hex
        body: dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "declared_cost_micro_usd": request.declared_cost_micro_usd,
            "tags": self._build_tags(request.node_type, request.node_id),
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        for name, cap in _DESCRIPTOR_CAPS.items():
            value = getattr(request, name)
            if value is not None and len(value) <= cap:
                body[name] = value
        if request.correlation_id is not None:
            body["correlation_id"] = request.correlation_id

        self._report_executor.submit(self._send_report, body)
        return ReportUsageResultSuccess(
            idempotency_key=idempotency_key,
            result_details="Queued a usage report for a direct model call.",
        )

    def _resolve_project_chain(self) -> list[str] | None:
        """Resolve the current project's ancestry as ids, leaf-first.

        Returns None when the chain cannot be read and `[]` when no project is open; the two are
        different answers and must not collapse, for the reason the handler above gives.

        Ids rather than names, because an id survives a rename where a name would re-point that
        project's spend. The `<system-defaults>` sentinel is not a project and is skipped, matched
        on the stripped id.
        """
        try:
            chain = self.engine.project_manager.get_project_chain()
        except Exception:
            logger.warning("Could not resolve the project chain for budget attribution.", exc_info=True)
            return None

        return [entry.id for entry in chain if entry.id.strip() != SYSTEM_DEFAULTS_KEY]

    def _build_tags(self, node_type: str | None, node_id: str | None) -> dict[str, Any]:
        """Build the `tags` body field Cloud reads attribution from, leaving out what is unknown."""
        tags: dict[str, Any] = {}
        project_chain = self._resolve_project_chain()
        if project_chain:
            tags["project"] = project_chain
        context_manager = self.engine.context_manager
        if context_manager.has_current_workflow():
            tags["workflow"] = context_manager.get_current_workflow_name()
        candidates = {
            "node_type": node_type,
            "node_id": node_id,
            "engine_id": self.engine.get_engine_id(),
            "session_id": self.engine.get_session_id(),
        }
        tags.update({key: value for key, value in candidates.items() if value is not None})
        return tags

    def _post(self, path: str, body: dict[str, Any], *, timeout: float, max_attempts: int) -> httpx2.Response:
        """POST to Griptape Cloud. Raises `_NoCredentialError` or `httpx2.HTTPError`."""
        secrets_manager = self.engine.secrets_manager
        credential = resolve_cloud_credential(secrets_manager)
        if credential is None:
            raise _NoCredentialError
        return request_with_retry(
            "POST",
            urljoin(resolve_cloud_base_url(secrets_manager), path),
            max_attempts=max_attempts,
            should_retry=_is_retryable_report_error,
            json=body,
            headers={"Authorization": f"Bearer {credential}"},
            timeout=timeout,
        )

    def _send_report(self, body: dict[str, Any]) -> None:
        """Post a usage report with bounded retries; log and drop it if Cloud cannot take it."""
        try:
            self._post(SPEND_PATH, body, timeout=REPORT_TIMEOUT_SECONDS, max_attempts=REPORT_MAX_ATTEMPTS)
        except _NoCredentialError:
            logger.warning("Dropped a usage report: %s", MISSING_CREDENTIAL_MESSAGE)
        except httpx2.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (HTTPStatus.BAD_REQUEST, HTTPStatus.CONFLICT):
                # The engine built a body Cloud rejects, or reused a key: a bug here, not in Cloud.
                logger.error(
                    "Griptape Cloud rejected usage report %s with HTTP %s: %s",
                    body["idempotency_key"],
                    status,
                    exc.response.text,
                )
                return
            logger.warning(
                "Dropped usage report %s: Griptape Cloud answered HTTP %s: %s",
                body["idempotency_key"],
                status,
                exc.response.text,
            )
        except httpx2.HTTPError as exc:
            logger.warning("Dropped usage report %s: %s", body["idempotency_key"], exc)

    def _unchecked(self, correlation_id: str, reason: str) -> BudgetAccessResultSuccess:
        """Clear a call Cloud could not be asked about. Logged at WARNING, since the work goes ahead."""
        return BudgetAccessResultSuccess(
            correlation_id=correlation_id,
            checked=False,
            result_details=ResultDetails(
                message=(
                    f"Attempted to check a direct model call against your budgets. {reason} "
                    "The call will proceed without a budget check."
                ),
                level=logging.WARNING,
            ),
        )

    def _report_not_sent(self, reason: str) -> ReportUsageResultFailure:
        return ReportUsageResultFailure(
            result_details=ResultDetails(
                message=f"Attempted to report the cost of a direct model call. {reason} The node is unaffected.",
                level=logging.WARNING,
            )
        )


class _NoCredentialError(Exception):
    """No Griptape Cloud credential is configured."""


def _is_retryable_report_error(exc: BaseException) -> bool:
    """Retry transient failures, and 429 too: Cloud throttles reports per license."""
    if isinstance(exc, httpx2.HTTPStatusError) and exc.response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        return True
    return is_retryable_httpx_error(exc)
