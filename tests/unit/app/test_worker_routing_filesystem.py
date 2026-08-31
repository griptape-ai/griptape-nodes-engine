"""Filesystem requests must not cross the worker boundary.

Forwarding-by-default made "every request a node can issue survives a cattrs round trip" a
requirement, and the filesystem family does not meet it. Three separate ways:

- `content` is `str | bytes`. The wire form base64s bytes into a JSON string and cattrs resolves
  the union back to `str`, so a worker's write landed on disk as mojibake with no error raised
  anywhere. Silent data corruption.
- A path carrying macro variables is a `MacroPath` wrapping a `ParsedMacro`, which will not
  serialize at all. The worker blocked until the forward timed out.
- Four failure results declare `SequenceScanFailureReason | FileIOFailureReason`, which cattrs
  cannot disambiguate, so even the error could not travel.

None of that is a reason to make the wire smarter: the workspace is shared on disk, so a worker's
own answer was already the correct one. These tests pin the routing decision and the mechanism
behind it, so neither can be undone by accident.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.app.worker_routing import (
    _FORWARDING_FILESYSTEM_REQUESTS,
    LOCAL_ONLY_REQUEST_TYPES,
)
from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.retained_mode.events import os_events
from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.project_events import MacroPath

# Sanity floor for the derived list; os_events has 18 request types today.
_MINIMUM_EXPECTED_REQUESTS = 10

if TYPE_CHECKING:
    from types import ModuleType


def _requests_defined_in(module: ModuleType) -> list[type[RequestPayload]]:
    """Request types this module DEFINES.

    Filtered on `__module__` rather than namespace membership: these modules import request types
    from each other, and an imported one becoming local-only by accident would let a worker answer
    it against its own non-authoritative state.
    """
    return [
        payload
        for payload in vars(module).values()
        if isinstance(payload, type)
        and issubclass(payload, RequestPayload)
        and payload is not RequestPayload
        and payload.__module__ == module.__name__
    ]


def _filesystem_requests() -> list[type[RequestPayload]]:
    return _requests_defined_in(os_events)


def _macro_path_requests() -> list[type[RequestPayload]]:
    """Every registered request carrying a MacroPath, wherever it is defined.

    Derived from the whole payload registry on purpose. The first version of this rule was scoped
    to os_events, which missed the artifact preview requests entirely -- and a test built from the
    same scope agreed with the bug instead of failing.
    """
    carriers = []
    for payload in PayloadRegistry.get_registry().values():
        if not (isinstance(payload, type) and issubclass(payload, RequestPayload)):
            continue
        if not dataclasses.is_dataclass(payload):
            continue
        annotations = [f.type if isinstance(f.type, str) else str(f.type) for f in dataclasses.fields(payload)]
        if any("MacroPath" in annotation for annotation in annotations):
            carriers.append(payload)
    return carriers


class TestEveryFilesystemRequestHasARoutingDecision:
    def test_the_module_is_not_empty(self) -> None:
        """Guards the guard: a rename that empties this list would make the rest vacuous."""
        assert len(_filesystem_requests()) > _MINIMUM_EXPECTED_REQUESTS

    @pytest.mark.parametrize("request_type", _filesystem_requests(), ids=lambda cls: cls.__name__)
    def test_it_is_either_local_only_or_deliberately_forwarded(self, request_type: type[RequestPayload]) -> None:
        """A filesystem request added later must be routed on purpose, not by default.

        Defaulting to local is the safe direction here, so this fails only if someone adds a
        request AND puts it in the forwarding set without it being a user-facing side effect.
        """
        if request_type in _FORWARDING_FILESYSTEM_REQUESTS:
            assert request_type not in LOCAL_ONLY_REQUEST_TYPES
        else:
            assert request_type in LOCAL_ONLY_REQUEST_TYPES

    def test_opening_a_file_in_the_users_app_is_the_only_forwarded_one(self) -> None:
        """It is a side effect, not a filesystem read: it belongs where the user is.

        A headless worker subprocess launching a desktop application would be either invisible or
        wrong, so this one goes to the process sitting next to the person.
        """
        assert {os_events.OpenAssociatedFileRequest} == _FORWARDING_FILESYSTEM_REQUESTS


class TestNothingCarryingAMacroPathIsForwarded:
    """A MacroPath cannot be serialized, so forwarding one hangs the worker until it times out.

    Checked across the whole payload registry rather than one module: MacroPath is defined in
    project_events and used by both os_events and artifact_events, and the artifact preview
    requests are reachable from a handler a worker runs locally -- so a module-scoped rule let them
    through while looking complete.
    """

    def test_the_sweep_finds_the_ones_we_know_about(self) -> None:
        """Guards the guard: if the sweep silently found nothing, everything below is vacuous."""
        names = {payload.__name__ for payload in _macro_path_requests()}
        assert {"GetPreviewForArtifactRequest", "GetNextVersionIndexRequest"} <= names

    @pytest.mark.parametrize("request_type", _macro_path_requests(), ids=lambda cls: cls.__name__)
    def test_it_is_local_only(self, request_type: type[RequestPayload]) -> None:
        assert request_type in LOCAL_ONLY_REQUEST_TYPES


class TestTypeRegisteringRequestsAnswerLocally:
    """The two provider-registration payloads carry a bare `type` field.

    cattrs has no structure hook for `type`, and the point of the request is a
    process-local registry -- so these must answer locally. They fell out of the
    derivation once already: the bare builtin `type` annotation is an evaluated class,
    and a matcher reading `str(annotation)` sees "<class 'type'>" and misses it.
    """

    def test_register_artifact_provider_is_local(self) -> None:
        from griptape_nodes.retained_mode.events.artifact_events import RegisterArtifactProviderRequest

        assert RegisterArtifactProviderRequest in LOCAL_ONLY_REQUEST_TYPES

    def test_register_preview_generator_is_local(self) -> None:
        from griptape_nodes.retained_mode.events.artifact_events import RegisterPreviewGeneratorRequest

        assert RegisterPreviewGeneratorRequest in LOCAL_ONLY_REQUEST_TYPES


class TestTheWireCannotCarryThese:
    """Pin the mechanisms, so the exclusion is not "fixed" by routing these instead."""

    def test_bytes_come_back_as_a_corrupted_string(self) -> None:
        original = b"\x89PNG\r\n\x1a\n\x00\xff\xfe"
        wire = json.loads(
            json.dumps(converter.unstructure(os_events.WriteFileRequest(file_path="x.png", content=original)))
        )

        round_tripped = converter.structure(wire, os_events.WriteFileRequest).content

        assert round_tripped != original, "if bytes now survive, revisit whether writes may forward"
        assert isinstance(round_tripped, str)

    @pytest.mark.parametrize(
        "payload",
        [
            os_events.GetNextVersionIndexRequest(
                macro_path=MacroPath(parsed_macro=ParsedMacro("{outputs}/o.png"), variables={})
            ),
            os_events.ResolveMacroPathRequest(
                macro_path=MacroPath(parsed_macro=ParsedMacro("{outputs}/o.png"), variables={})
            ),
            os_events.GetNextUnusedFilenameRequest(
                file_path=MacroPath(parsed_macro=ParsedMacro("{outputs}/o.png"), variables={})
            ),
        ],
        ids=lambda p: type(p).__name__,
    )
    def test_a_macro_path_will_not_serialize(self, payload: RequestPayload) -> None:
        """`Directory("{outputs}/renders").with_versioning()` is the ordinary caller of these."""
        with pytest.raises(Exception):  # noqa: B017, PT011 - any failure to serialize is the point
            json.dumps(converter.unstructure(payload))

    def test_the_sequence_failure_union_cannot_be_structured(self) -> None:
        """So a worker could not even receive the error, let alone the result."""
        failure = os_events.ScanSequencesResultFailure(
            failure_reason=os_events.SequenceScanFailureReason.INVALID_TEMPLATE,
            result_details="no",
        )
        wire = json.loads(json.dumps(converter.unstructure(failure)))

        with pytest.raises(Exception):  # noqa: B017, PT011 - any failure to serialize is the point
            converter.structure(wire, os_events.ScanSequencesResultFailure)

    def test_the_ambiguous_union_is_still_declared_where_we_think(self) -> None:
        """If these are ever given a structure hook, the test above stops meaning anything."""
        annotated = [
            cls.__name__
            for cls in vars(os_events).values()
            if isinstance(cls, type)
            and dataclasses.is_dataclass(cls)
            and any(
                isinstance(f.type, str) and "SequenceScanFailureReason | FileIOFailureReason" in f.type
                for f in dataclasses.fields(cls)
            )
        ]
        assert sorted(annotated) == [
            "DeduceSequencesFromFileListResultFailure",
            "ListDirectoryResultFailure",
            "ListDirectorySequencesResultFailure",
            "ScanSequencesResultFailure",
        ]
