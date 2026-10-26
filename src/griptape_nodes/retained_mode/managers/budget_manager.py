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
import queue
import threading
import uuid
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, NamedTuple
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
from griptape_nodes.retained_mode.request_handlers import RequestScope, handles
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
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

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
        # A daemon thread, so engine exit never waits on queued reports. Not an asyncio task: a
        # sync `handle_request` runs the handler on a short-lived loop that would cancel it.
        self._reports: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        threading.Thread(target=self._post_reports, name="budget-report", daemon=True).start()
        event_manager.register_request_handlers(self)

    @handles(GetAttributionContextRequest, scope=RequestScope.HOST)
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

    @handles(BudgetAccessRequest, scope=RequestScope.OPTIONAL_WORKFLOW)
    async def on_budget_access_request(
        self, request: BudgetAccessRequest, workflow: WorkflowRuntime | None = None
    ) -> BudgetAccessResultSuccess | BudgetAccessResultFailure:
        """Ask Cloud whether a direct provider call fits its budgets; fail open unless Cloud says no.

        The call is tagged with `workflow`, the workflow it is made for, when there is one.
        """
        correlation_id = uuid.uuid4().hex
        body: dict[str, Any] = {
            "tags": self._build_tags(request.node_type, request.node_id, workflow),
            "model": request.model,
            "correlation_id": correlation_id,
        }
        if request.estimated_cost_micro_usd is not None:
            body["estimated_cost_micro_usd"] = request.estimated_cost_micro_usd

        try:
            url, headers = self._cloud_target(BUDGET_CHECKS_PATH)
            async with httpx2.AsyncClient(timeout=CHECK_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
        except _NoCredentialError:
            return self._unchecked(request.model, correlation_id, f"Failed because {MISSING_CREDENTIAL_MESSAGE}")
        except httpx2.HTTPStatusError as exc:
            return self._refused_over_http(request.model, correlation_id, exc.response)
        # InvalidURL is not an HTTPError: a mistyped GT_CLOUD_BASE_URL must still fail open.
        except (httpx2.HTTPError, httpx2.InvalidURL):
            return self._unchecked(request.model, correlation_id, "Failed because Griptape Cloud could not be reached.")

        result = _decision(response)
        if result is None:
            return self._unchecked(
                request.model, correlation_id, "Failed because Griptape Cloud's answer could not be read."
            )

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
            result_details=f"Griptape Cloud cleared a direct call to model '{request.model}' against its budgets.",
        )

    @handles(ReportUsageRequest, scope=RequestScope.OPTIONAL_WORKFLOW)
    def on_report_usage_request(
        self, request: ReportUsageRequest, workflow: WorkflowRuntime | None = None
    ) -> ReportUsageResultSuccess | ReportUsageResultFailure:
        """Queue a usage report for a direct provider call and return without waiting on Cloud.

        The report is tagged with `workflow`, the workflow the call was made for, when there is one.
        """
        problem = _shape_problem(request)
        if problem is not None:
            return self._report_not_sent(request.model, f"Failed because {problem}")
        if resolve_cloud_credential(self.engine.secrets_manager) is None:
            return self._report_not_sent(request.model, f"Failed because {MISSING_CREDENTIAL_MESSAGE}")

        # Minted once and captured in `body`, so every retry sends the same key.
        idempotency_key = uuid.uuid4().hex
        body: dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "tags": self._build_tags(request.node_type, request.node_id, workflow),
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        if request.declared_cost_micro_usd is None:
            body["usage"] = _usage_body(request)
        else:
            body["declared_cost_micro_usd"] = request.declared_cost_micro_usd
        for name, cap in _DESCRIPTOR_CAPS.items():
            value = getattr(request, name)
            if value is None:
                continue
            # A token report is priced by its model, so an over-long one is sent and Cloud's length
            # rule says so, rather than dropped and reported as missing. Elsewhere a long
            # descriptor is only a label, and dropping it keeps one long name from losing the report.
            if len(value) <= cap or (name == "model" and "usage" in body):
                body[name] = value
        if request.correlation_id is not None:
            body["correlation_id"] = request.correlation_id

        self._reports.put(body)
        return ReportUsageResultSuccess(
            idempotency_key=idempotency_key,
            result_details=f"Queued a usage report for {_describe_call(request.model)}.",
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

    def _build_tags(
        self, node_type: str | None, node_id: str | None, workflow: WorkflowRuntime | None
    ) -> dict[str, Any]:
        """Build the `tags` body field Cloud reads attribution from, leaving out what is unknown."""
        tags: dict[str, Any] = {}
        project_chain = self._resolve_project_chain()
        if project_chain:
            tags["project"] = project_chain
        if workflow is not None:
            tags["workflow"] = self.engine.workflow_registry.get(workflow.workflow_id).name
        candidates = {
            "node_type": node_type,
            "node_id": node_id,
            "engine_id": self.engine.get_engine_id(),
            "session_id": self.engine.get_session_id(),
        }
        tags.update({key: value for key, value in candidates.items() if value is not None})
        return tags

    def _cloud_target(self, path: str) -> CloudTarget:
        """The Griptape Cloud URL for `path` and the headers to authenticate it. Raises `_NoCredentialError`."""
        secrets_manager = self.engine.secrets_manager
        credential = resolve_cloud_credential(secrets_manager)
        if credential is None:
            raise _NoCredentialError
        return CloudTarget(
            url=urljoin(resolve_cloud_base_url(secrets_manager), path),
            headers={"Authorization": f"Bearer {credential}"},
        )

    def _post_reports(self) -> None:
        """Post queued reports one at a time, for as long as the engine runs."""
        while True:
            body = self._reports.get()
            # One report failing in a way nothing anticipated must not end the thread and
            # silently drop every report after it.
            try:
                self._send_report(body)
            except Exception:
                logger.exception("A usage report failed unexpectedly and was dropped.")

    def _send_report(self, body: dict[str, Any]) -> None:
        """Post a usage report with bounded retries; log and drop it if Cloud cannot take it."""
        try:
            url, headers = self._cloud_target(SPEND_PATH)
            request_with_retry(
                "POST",
                url,
                max_attempts=REPORT_MAX_ATTEMPTS,
                should_retry=_is_retryable_report_error,
                json=body,
                headers=headers,
                timeout=REPORT_TIMEOUT_SECONDS,
            )
        except _NoCredentialError:
            logger.warning("Dropped a usage report: %s", MISSING_CREDENTIAL_MESSAGE)
        except httpx2.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == HTTPStatus.CONFLICT:
                # The same key with a different body: only the engine mints keys, so a bug here.
                logger.error(
                    "Griptape Cloud rejected usage report %s as a reused key: %s",
                    body["idempotency_key"],
                    exc.response.text,
                )
                return
            if status == HTTPStatus.BAD_REQUEST:
                # Cloud enforces the count rules, so this is usually the calling node's input.
                logger.warning(
                    "Griptape Cloud rejected usage report %s from the calling node or the engine: %s",
                    body["idempotency_key"],
                    _rejection_reason(exc.response),
                )
                return
            logger.warning(
                "Dropped usage report %s: Griptape Cloud answered HTTP %s: %s",
                body["idempotency_key"],
                status,
                exc.response.text,
            )
        except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
            logger.warning("Dropped usage report %s: %s", body["idempotency_key"], exc)

    def _unchecked(
        self, model: str, correlation_id: str, reason: str, *, level: int = logging.WARNING
    ) -> BudgetAccessResultSuccess:
        """Clear a call Cloud could not be asked about. WARNING by default, since the work goes ahead."""
        return BudgetAccessResultSuccess(
            correlation_id=correlation_id,
            checked=False,
            result_details=ResultDetails(
                message=(
                    f"Attempted to check a direct call to model '{model}' against your budgets. {reason} "
                    "The call will proceed without a budget check."
                ),
                level=level,
            ),
        )

    def _refused_over_http(
        self, model: str, correlation_id: str, response: httpx2.Response
    ) -> BudgetAccessResultSuccess:
        """Clear a call Cloud answered with an HTTP error rather than a decision."""
        if response.status_code == HTTPStatus.NOT_FOUND:
            # A Cloud without budget checks: the steady state of that deployment, not news.
            return self._unchecked(
                model,
                correlation_id,
                "Failed because this Griptape Cloud deployment does not offer budget checks.",
                level=logging.DEBUG,
            )
        return self._unchecked(
            model, correlation_id, f"Failed because Griptape Cloud refused the check (HTTP {response.status_code})."
        )

    def _report_not_sent(self, model: str | None, reason: str) -> ReportUsageResultFailure:
        return ReportUsageResultFailure(
            result_details=ResultDetails(
                message=f"Attempted to report the cost of {_describe_call(model)}. {reason} The node is unaffected.",
                level=logging.WARNING,
            )
        )


def _shape_problem(request: ReportUsageRequest) -> str | None:
    """Say why a report can't be built, or None when it can.

    Only what decides the body: a declared cost, or input and output token counts, never both.
    Every other rule on the counts is Cloud's to enforce; a report it rejects is logged and dropped.
    """
    has_tokens = request.input_tokens is not None or request.output_tokens is not None
    has_cost = request.declared_cost_micro_usd is not None
    if has_tokens == has_cost:
        return "a report needs either token counts or a declared cost, not both or neither."
    if has_tokens and (request.input_tokens is None or request.output_tokens is None):
        return "a token report needs both input and output token counts."
    return None


def _usage_body(request: ReportUsageRequest) -> dict[str, int | None]:
    """The `usage` field: the counts as the caller gave them, cache keys only when given."""
    usage: dict[str, int | None] = {
        "input_tokens": request.input_tokens,
        "output_tokens": request.output_tokens,
    }
    if request.cache_read_tokens is not None:
        usage["cache_read_tokens"] = request.cache_read_tokens
    if request.cache_write_tokens is not None:
        usage["cache_write_tokens"] = request.cache_write_tokens
    if request.input_audio_tokens is not None:
        usage["input_audio_tokens"] = request.input_audio_tokens
    if request.cache_audio_read_tokens is not None:
        usage["cache_audio_read_tokens"] = request.cache_audio_read_tokens
    return usage


def _decision(response: httpx2.Response) -> dict[str, Any] | None:
    """Return a budget check's answer, or None when it is not JSON or carries no boolean `allowed`."""
    try:
        result = response.json()
    except ValueError:
        return None
    if not isinstance(result, dict) or not isinstance(result.get("allowed"), bool):
        return None
    return result


def _describe_call(model: str | None) -> str:
    """Name a direct call by its model when the caller gave one."""
    if model is None:
        return "a direct model call"
    return f"a direct call to model '{model}'"


class CloudTarget(NamedTuple):
    """Where to send a Griptape Cloud request, and how to authenticate it."""

    url: str
    headers: dict[str, str]


class _NoCredentialError(Exception):
    """No Griptape Cloud credential is configured."""


def _rejection_reason(response: httpx2.Response) -> str:
    """Cloud's stated reasons for a 400, or its raw body when it gives none."""
    try:
        body = response.json()
    except ValueError:
        return response.text
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return response.text
    details = [str(error.get("detail")) for error in errors if isinstance(error, dict) and error.get("detail")]
    if not details:
        return response.text
    return "; ".join(details)


def _is_retryable_report_error(exc: BaseException) -> bool:
    """Retry transient failures, and 429 too: Cloud throttles reports per license."""
    if isinstance(exc, httpx2.HTTPStatusError) and exc.response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        return True
    return is_retryable_httpx_error(exc)
