"""Classification, wording, and subprocess wire format for model download failures.

A download runs in a child process (`griptape_nodes.cli.commands.models download`),
so the exception is raised in one process and the message is shown by another. The
child classifies the exception it caught and reports the verdict; the parent turns
that verdict into the sentence the user reads. Both halves live here so a download
failure reads the same in the editor as it does in a terminal, and so the wire
format has its writer and its reader side by side.

The parent must never build a message out of the child's output streams. Those
streams also carry progress bars and library warnings, so text scavenged from them
surfaces animation frames where an explanation belongs.
"""

from __future__ import annotations

import errno
import json
from enum import StrEnum
from http import HTTPStatus
from typing import NamedTuple

import httpx
from huggingface_hub.errors import (
    GatedRepoError,
    HfHubHTTPError,
    HFValidationError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)

HF_TOKEN_PAGE = "https://huggingface.co/settings/tokens"  # noqa: S105  # a page about tokens, not a token

_EVENT_KIND_KEY = "error_type"
_EVENT_DETAIL_KEY = "error_message"


class DownloadErrorKind(StrEnum):
    """Why a download failed, at the granularity that changes what the user should do."""

    GATED_UNAUTHENTICATED = "gated_unauthenticated"
    GATED_NO_ACCESS = "gated_no_access"
    REPO_NOT_FOUND = "repo_not_found"
    REVISION_NOT_FOUND = "revision_not_found"
    INVALID_MODEL_ID = "invalid_model_id"
    RATE_LIMITED = "rate_limited"
    NO_DISK_SPACE = "no_disk_space"
    NETWORK_UNREACHABLE = "network_unreachable"
    UNKNOWN = "unknown"


class DownloadFailure(NamedTuple):
    """A child's verdict on a failed download.

    Attributes:
        kind: The classification the child assigned.
        detail: The underlying exception's own text, for the log and for the
            unclassified case. Never the contents of an output stream.
    """

    kind: DownloadErrorKind
    detail: str | None


def classify(exc: Exception) -> DownloadErrorKind:  # noqa: PLR0911
    """Assign a download exception the kind whose advice fits it.

    Args:
        exc: The exception that ended the download.

    Returns:
        DownloadErrorKind: The matching kind, or `UNKNOWN` when nothing fits.
    """
    # Order is load-bearing: GatedRepoError is a RepositoryNotFoundError, and every
    # HfHubHTTPError is also an httpx.HTTPError and an OSError.
    if isinstance(exc, GatedRepoError):
        if _status_code(exc) == HTTPStatus.FORBIDDEN:
            return DownloadErrorKind.GATED_NO_ACCESS
        # Hugging Face answers an anonymous or rejected request with 401 and a
        # gated-repo code, so anything that is not an explicit 403 is a credential
        # problem rather than a pending access request.
        return DownloadErrorKind.GATED_UNAUTHENTICATED
    if isinstance(exc, RevisionNotFoundError):
        return DownloadErrorKind.REVISION_NOT_FOUND
    if isinstance(exc, RepositoryNotFoundError):
        return DownloadErrorKind.REPO_NOT_FOUND
    if isinstance(exc, HFValidationError):
        return DownloadErrorKind.INVALID_MODEL_ID
    if isinstance(exc, HfHubHTTPError):
        if _status_code(exc) == HTTPStatus.TOO_MANY_REQUESTS:
            return DownloadErrorKind.RATE_LIMITED
        return DownloadErrorKind.UNKNOWN
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return DownloadErrorKind.NO_DISK_SPACE
    if isinstance(exc, httpx.TransportError):
        return DownloadErrorKind.NETWORK_UNREACHABLE
    return DownloadErrorKind.UNKNOWN


def describe(failure: DownloadFailure, *, model_id: str, revision: str | None = None) -> str:  # noqa: PLR0911
    """Word a download failure for the person who started it.

    Bare URLs are deliberate: the editor renders these messages as markdown, which
    autolinks them, and a terminal shows them as-is. Markdown link syntax would only
    read as punctuation in the terminal.

    Args:
        failure: The child's verdict.
        model_id: The model the user asked for.
        revision: The revision the user asked for, when one was pinned.

    Returns:
        str: A sentence naming the model, what went wrong, and the next step.
    """
    model_url = f"https://huggingface.co/{model_id}"
    attempted = f"Attempted to download '{model_id}'."

    if failure.kind is DownloadErrorKind.GATED_UNAUTHENTICATED:
        return (
            f"{attempted} Hugging Face restricts this model to accounts it can identify, and it did not "
            f"accept this request. Add your Hugging Face token as HF_TOKEN under Settings -> API Keys & "
            f"Secrets, then start the download again. Create a token with 'Read' access at {HF_TOKEN_PAGE}."
        )
    if failure.kind is DownloadErrorKind.GATED_NO_ACCESS:
        return (
            f"{attempted} Your Hugging Face account has not been granted access to it. Request access at "
            f"{model_url}, then start the download again."
        )
    if failure.kind is DownloadErrorKind.REPO_NOT_FOUND:
        return f"{attempted} Hugging Face has no model with that id. Check the id at {model_url}."
    if failure.kind is DownloadErrorKind.REVISION_NOT_FOUND:
        pinned = f"revision '{revision}'" if revision else "the requested revision"
        return (
            f"Attempted to download '{model_id}' at {pinned}. That revision does not exist. The model's "
            f"available revisions are listed at {model_url}."
        )
    if failure.kind is DownloadErrorKind.INVALID_MODEL_ID:
        return (
            f"{attempted} That is not a valid Hugging Face model id. An id looks like "
            f"'black-forest-labs/FLUX.1-dev': an owner, a slash, then the model name."
        )
    if failure.kind is DownloadErrorKind.RATE_LIMITED:
        return (
            f"{attempted} Hugging Face is limiting how many requests this machine may make. Wait a few "
            f"minutes and start the download again. Adding your Hugging Face token as HF_TOKEN under "
            f"Settings -> API Keys & Secrets raises the limit; create one at {HF_TOKEN_PAGE}."
        )
    if failure.kind is DownloadErrorKind.NO_DISK_SPACE:
        return (
            f"{attempted} The drive holding the model cache is out of space. Free up space, or delete "
            f"models you no longer need in Model Management, then start the download again."
        )
    if failure.kind is DownloadErrorKind.NETWORK_UNREACHABLE:
        return (
            f"{attempted} Could not reach huggingface.co. Check this machine's internet connection, then "
            f"start the download again."
        )
    if failure.detail:
        return f"{attempted} Failed due to: {failure.detail}"
    return f"{attempted} Failed for an unexpected reason. The engine log has the details."


def format_error_event(failure: DownloadFailure) -> str:
    """Serialize a verdict for the child to write to stderr.

    Leads with a newline because tqdm ends a progress frame with a bare carriage
    return: an event written straight after one shares a line with it, and a reader
    splitting on newlines alone would never see the event.

    Args:
        failure: The verdict to report.

    Returns:
        str: The bytes to write, newline-delimited on both sides.
    """
    event = {_EVENT_KIND_KEY: failure.kind.value, _EVENT_DETAIL_KEY: failure.detail}
    return "\n" + json.dumps(event) + "\n"


def parse_error_event(stderr: str) -> DownloadFailure | None:
    """Recover the child's verdict from what it wrote to stderr.

    Splits on carriage returns as well as newlines, so a frame the child's own
    leading newline did not separate cannot hide the event behind it.

    Args:
        stderr: Everything the child wrote to stderr.

    Returns:
        DownloadFailure | None: The reported verdict, or None when the child died
            without reporting one.
    """
    for fragment in stderr.replace("\r", "\n").splitlines():
        candidate = fragment.strip()
        if not candidate.startswith("{"):
            continue
        try:
            event = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or _EVENT_KIND_KEY not in event:
            continue
        detail = event.get(_EVENT_DETAIL_KEY)
        return DownloadFailure(
            kind=_to_kind(event[_EVENT_KIND_KEY]),
            detail=detail if isinstance(detail, str) else None,
        )
    return None


def _status_code(exc: HfHubHTTPError) -> int | None:
    """Read the HTTP status off a hub error, which carries its response."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    return None


def _to_kind(value: object) -> DownloadErrorKind:
    """Map a kind off the wire, tolerating one this build does not know."""
    if isinstance(value, str):
        try:
            return DownloadErrorKind(value)
        except ValueError:
            return DownloadErrorKind.UNKNOWN
    return DownloadErrorKind.UNKNOWN
