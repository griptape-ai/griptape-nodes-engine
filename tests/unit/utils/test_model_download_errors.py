"""Unit tests for model_download_errors module."""

from __future__ import annotations

import errno

import httpx
import pytest
from huggingface_hub.errors import (
    GatedRepoError,
    HfHubHTTPError,
    HFValidationError,
    LocalEntryNotFoundError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)

from griptape_nodes.utils.model_download_errors import (
    DownloadErrorKind,
    DownloadFailure,
    classify,
    describe,
    format_error_event,
    parse_error_event,
)

MODEL_ID = "black-forest-labs/FLUX.1-dev"


def _hub_error(error_class: type[HfHubHTTPError], status_code: int) -> HfHubHTTPError:
    """Build a hub error the way `hf_raise_for_status` does, response and all."""
    request = httpx.Request("GET", f"https://huggingface.co/api/models/{MODEL_ID}")
    return error_class(f"{status_code} Client Error.", response=httpx.Response(status_code, request=request))


class TestClassify:
    def test_anonymous_gated_access_reads_as_a_credential_problem(self) -> None:
        """Hugging Face answers an unauthenticated request for a gated repo with 401."""
        assert classify(_hub_error(GatedRepoError, 401)) is DownloadErrorKind.GATED_UNAUTHENTICATED

    def test_authenticated_gated_access_reads_as_a_pending_access_request(self) -> None:
        assert classify(_hub_error(GatedRepoError, 403)) is DownloadErrorKind.GATED_NO_ACCESS

    def test_missing_repo(self) -> None:
        assert classify(_hub_error(RepositoryNotFoundError, 401)) is DownloadErrorKind.REPO_NOT_FOUND

    def test_missing_revision(self) -> None:
        assert classify(_hub_error(RevisionNotFoundError, 404)) is DownloadErrorKind.REVISION_NOT_FOUND

    def test_malformed_model_id(self) -> None:
        assert classify(HFValidationError("Repo id must be in the form 'repo_name'")) is (
            DownloadErrorKind.INVALID_MODEL_ID
        )

    def test_rate_limit(self) -> None:
        assert classify(_hub_error(HfHubHTTPError, 429)) is DownloadErrorKind.RATE_LIMITED

    def test_other_http_failures_stay_unclassified(self) -> None:
        assert classify(_hub_error(HfHubHTTPError, 500)) is DownloadErrorKind.UNKNOWN

    def test_full_disk(self) -> None:
        assert classify(OSError(errno.ENOSPC, "No space left on device")) is DownloadErrorKind.NO_DISK_SPACE

    def test_unreachable_host_mid_transfer(self) -> None:
        assert classify(httpx.ConnectError("nodename nor servname provided")) is (DownloadErrorKind.NETWORK_UNREACHABLE)

    def test_unreachable_host_before_any_transfer(self) -> None:
        """The shape an offline download actually fails with.

        `snapshot_download` catches the transport error while reading metadata and re-raises it as
        `LocalEntryNotFoundError`, which is a FileNotFoundError rather than an httpx error, so the
        transport branch alone left the common offline case unclassified.
        """
        offline = LocalEntryNotFoundError(
            "Got: ConnectError: [Errno 8] nodename nor servname provided, or not known\n"
            "An error happened while trying to locate the files on the Hub."
        )

        assert classify(offline) is DownloadErrorKind.NETWORK_UNREACHABLE

    def test_an_http_error_is_not_mistaken_for_a_transport_or_disk_error(self) -> None:
        """Every HfHubHTTPError is also an httpx.HTTPError and an OSError."""
        assert classify(_hub_error(HfHubHTTPError, 500)) is DownloadErrorKind.UNKNOWN

    def test_anything_else(self) -> None:
        assert classify(RuntimeError("boom")) is DownloadErrorKind.UNKNOWN


class TestDescribe:
    @pytest.mark.parametrize("kind", [kind for kind in DownloadErrorKind if kind is not DownloadErrorKind.UNKNOWN])
    def test_every_classified_kind_is_worded(self, kind: DownloadErrorKind) -> None:
        """A kind added to the enum and to `classify` but not to `describe` falls through to UNKNOWN.

        Comparing against the UNKNOWN wording is what makes that visible: asserting only that the
        message names the model passes on the fallback, which names it too.
        """
        detail = "401 Client Error. Cannot access gated repo"
        unworded = describe(DownloadFailure(kind=DownloadErrorKind.UNKNOWN, detail=detail), model_id=MODEL_ID)

        message = describe(DownloadFailure(kind=kind, detail=detail), model_id=MODEL_ID)

        assert MODEL_ID in message
        assert message != unworded
        assert detail not in message

    def test_unauthenticated_gate_asks_for_a_token_not_for_access_approval(self) -> None:
        message = describe(
            DownloadFailure(kind=DownloadErrorKind.GATED_UNAUTHENTICATED, detail="401 Client Error."),
            model_id=MODEL_ID,
        )

        assert "HF_TOKEN" in message
        assert "Settings -> API Keys & Secrets" in message
        assert "Request access" not in message

    def test_granted_access_gate_asks_for_access_approval_not_for_a_token(self) -> None:
        message = describe(
            DownloadFailure(kind=DownloadErrorKind.GATED_NO_ACCESS, detail="403 Client Error."),
            model_id=MODEL_ID,
        )

        assert f"https://huggingface.co/{MODEL_ID}" in message
        assert "HF_TOKEN" not in message

    def test_a_pinned_revision_is_named(self) -> None:
        message = describe(
            DownloadFailure(kind=DownloadErrorKind.REVISION_NOT_FOUND, detail=None),
            model_id=MODEL_ID,
            revision="refs/pr/1",
        )

        assert "refs/pr/1" in message

    def test_an_unpinned_revision_reads_as_a_sentence(self) -> None:
        message = describe(
            DownloadFailure(kind=DownloadErrorKind.REVISION_NOT_FOUND, detail=None),
            model_id=MODEL_ID,
            revision=None,
        )

        assert "the requested revision" in message
        assert "None" not in message

    def test_an_unclassified_failure_carries_the_exception_text(self) -> None:
        message = describe(
            DownloadFailure(kind=DownloadErrorKind.UNKNOWN, detail="[Errno 1] Operation not permitted"),
            model_id=MODEL_ID,
        )

        assert "[Errno 1] Operation not permitted" in message

    def test_an_unclassified_failure_with_nothing_to_say_points_at_the_log(self) -> None:
        message = describe(DownloadFailure(kind=DownloadErrorKind.UNKNOWN, detail=None), model_id=MODEL_ID)

        assert "engine log" in message
        assert "None" not in message


class TestErrorEventRoundTrip:
    def test_a_written_event_reads_back(self) -> None:
        failure = DownloadFailure(kind=DownloadErrorKind.GATED_NO_ACCESS, detail="403 Client Error.")

        assert parse_error_event(format_error_event(failure)) == failure

    def test_an_event_behind_progress_frames_is_still_found(self) -> None:
        """Tqdm separates frames with a bare carriage return, not a newline."""
        stderr = "\rFetching 29 files:   0%|          | 0/29 [00:00<?, ?it/s]" + format_error_event(
            DownloadFailure(kind=DownloadErrorKind.GATED_UNAUTHENTICATED, detail="401")
        )

        failure = parse_error_event(stderr)

        assert failure is not None
        assert failure.kind is DownloadErrorKind.GATED_UNAUTHENTICATED

    def test_an_event_sharing_a_line_with_a_frame_is_still_found(self) -> None:
        """Guards the reader against a writer that does not lead with a newline."""
        stderr = '\rFetching 29 files:   0%|  | 0/29\r{"error_type": "repo_not_found", "error_message": "404"}\n'

        failure = parse_error_event(stderr)

        assert failure is not None
        assert failure.kind is DownloadErrorKind.REPO_NOT_FOUND

    def test_a_kind_this_build_does_not_know_reads_as_unclassified(self) -> None:
        failure = parse_error_event('{"error_type": "invented_later", "error_message": "details"}\n')

        assert failure is not None
        assert failure.kind is DownloadErrorKind.UNKNOWN
        assert failure.detail == "details"

    def test_progress_frames_alone_are_not_a_verdict(self) -> None:
        assert parse_error_event("\rFetching 29 files:   7%|  | 2/29 [00:00<00:02, 12.14it/s]\n") is None

    def test_silence_is_not_a_verdict(self) -> None:
        assert parse_error_event("") is None

    def test_unrelated_json_is_not_a_verdict(self) -> None:
        assert parse_error_event('{"downloaded_bytes": 0, "total_bytes": 0}\n') is None
