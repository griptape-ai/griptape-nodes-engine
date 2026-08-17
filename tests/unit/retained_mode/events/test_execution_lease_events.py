"""Tests for the execution lease protocol events.

These five events are the public contract between an engine and its admission
authority (the Load Balancer, which lives in a separate repository and imports
them through the published engine package). The tests here pin the contract
properties that external implementations rely on: schema versioning, the
skip-the-line cancel, registry membership, and additive wire evolution.
"""

import semver

from griptape_nodes.retained_mode.events.base_events import SkipTheLineMixin
from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.retained_mode.events.execution_lease_events import (
    AcquireExecutionLeaseRequest,
    AcquireExecutionLeaseResultFailure,
    AcquireExecutionLeaseResultSuccess,
    CancelExecutionLeaseRequest,
    CancelExecutionLeaseResultFailure,
    CancelExecutionLeaseResultSuccess,
    ExecutionAdmissionStatusEntry,
    ExecutionAdmissionStatusEvent,
    ReleaseExecutionLeaseRequest,
    ReleaseExecutionLeaseResultFailure,
    ReleaseExecutionLeaseResultSuccess,
    RenewExecutionLeaseRequest,
    RenewExecutionLeaseResultFailure,
    RenewExecutionLeaseResultSuccess,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


class TestAcquireExecutionLease:
    def test_schema_version_defaults_to_latest(self) -> None:
        request = AcquireExecutionLeaseRequest(engine_id="eng-1", lease_id="lease-1")

        assert request.schema_version == AcquireExecutionLeaseRequest.LATEST_SCHEMA_VERSION

    def test_latest_schema_version_is_valid_semver(self) -> None:
        """The version comparison rule (caret under 0.x) requires parseable semver."""
        parsed = semver.VersionInfo.parse(AcquireExecutionLeaseRequest.LATEST_SCHEMA_VERSION)

        assert parsed.major == 0

    def test_requirements_and_machine_id_default_to_none(self) -> None:
        """Both are optional: requirements are advisory, machine_id is a v1 constant."""
        request = AcquireExecutionLeaseRequest(engine_id="eng-1", lease_id="lease-1")

        assert request.requirements is None
        assert request.machine_id is None

    def test_result_success_stores_lease_id(self) -> None:
        result = AcquireExecutionLeaseResultSuccess(lease_id="lease-1", result_details="granted")

        assert result.lease_id == "lease-1"

    def test_result_failure_can_be_created(self) -> None:
        result = AcquireExecutionLeaseResultFailure(result_details="already waiting")

        assert result is not None


class TestLeaseLifecycleEvents:
    def test_release_stores_lease_id(self) -> None:
        request = ReleaseExecutionLeaseRequest(lease_id="lease-1")

        assert request.lease_id == "lease-1"

    def test_renew_stores_lease_id(self) -> None:
        request = RenewExecutionLeaseRequest(lease_id="lease-1")

        assert request.lease_id == "lease-1"

    def test_cancel_stores_lease_id(self) -> None:
        request = CancelExecutionLeaseRequest(lease_id="lease-1")

        assert request.lease_id == "lease-1"

    def test_cancel_is_skip_the_line(self) -> None:
        """Cancel must never queue behind the acquire it is cancelling."""
        request = CancelExecutionLeaseRequest(lease_id="lease-1")

        assert isinstance(request, SkipTheLineMixin)

    def test_acquire_release_renew_are_not_skip_the_line(self) -> None:
        """Only cancel skips the line; the rest take the normal path."""
        assert not isinstance(AcquireExecutionLeaseRequest(engine_id="e", lease_id="l"), SkipTheLineMixin)
        assert not isinstance(ReleaseExecutionLeaseRequest(lease_id="l"), SkipTheLineMixin)
        assert not isinstance(RenewExecutionLeaseRequest(lease_id="l"), SkipTheLineMixin)

    def test_lifecycle_results_can_be_created(self) -> None:
        assert ReleaseExecutionLeaseResultSuccess(result_details="ok") is not None
        assert ReleaseExecutionLeaseResultFailure(result_details="unknown") is not None
        assert RenewExecutionLeaseResultSuccess(result_details="ok") is not None
        assert RenewExecutionLeaseResultFailure(result_details="expired") is not None
        assert CancelExecutionLeaseResultSuccess(result_details="ok") is not None
        assert CancelExecutionLeaseResultFailure(result_details="unknown") is not None


class TestPayloadRegistryMembership:
    def test_all_lease_payloads_are_registered(self) -> None:
        """A payload missing from the registry cannot be deserialized off the wire."""
        for payload_type in (
            AcquireExecutionLeaseRequest,
            AcquireExecutionLeaseResultSuccess,
            AcquireExecutionLeaseResultFailure,
            ReleaseExecutionLeaseRequest,
            ReleaseExecutionLeaseResultSuccess,
            ReleaseExecutionLeaseResultFailure,
            RenewExecutionLeaseRequest,
            RenewExecutionLeaseResultSuccess,
            RenewExecutionLeaseResultFailure,
            CancelExecutionLeaseRequest,
            CancelExecutionLeaseResultSuccess,
            CancelExecutionLeaseResultFailure,
            ExecutionAdmissionStatusEvent,
        ):
            assert PayloadRegistry.get_type(payload_type.__name__) is payload_type


class TestWireRoundTrip:
    def test_acquire_round_trips_through_converter(self) -> None:
        request = AcquireExecutionLeaseRequest(
            engine_id="eng-1",
            lease_id="lease-42",
            session_id="sess-1",
            scope="single_node",
            requirements={"compute": ("cuda", "present"), "min_vram_gb": (24, ">=")},
            machine_id="gpu-box-1",
        )

        rebuilt = converter.structure(converter.unstructure(request), AcquireExecutionLeaseRequest)

        assert rebuilt.engine_id == "eng-1"
        assert rebuilt.lease_id == "lease-42"
        assert rebuilt.session_id == "sess-1"
        assert rebuilt.scope == "single_node"
        assert rebuilt.machine_id == "gpu-box-1"
        assert rebuilt.schema_version == AcquireExecutionLeaseRequest.LATEST_SCHEMA_VERSION

    def test_unknown_fields_are_ignored(self) -> None:
        """Additive evolution: an older peer must tolerate fields it does not know."""
        payload = converter.unstructure(AcquireExecutionLeaseRequest(engine_id="eng-1", lease_id="lease-1"))
        payload["field_from_the_future"] = "ignored"

        rebuilt = converter.structure(payload, AcquireExecutionLeaseRequest)

        assert rebuilt.engine_id == "eng-1"

    def test_status_event_round_trips_with_entries(self) -> None:
        event = ExecutionAdmissionStatusEvent(
            entries=[
                ExecutionAdmissionStatusEntry(engine_id="eng-2", position=1, scope="workflow"),
                ExecutionAdmissionStatusEntry(engine_id="eng-3", position=None),
            ]
        )

        rebuilt = converter.structure(converter.unstructure(event), ExecutionAdmissionStatusEvent)

        assert [entry.engine_id for entry in rebuilt.entries] == ["eng-2", "eng-3"]
        assert rebuilt.entries[0].position == 1
        assert rebuilt.entries[1].position is None

    def test_position_less_entry_is_representable(self) -> None:
        """A non-FIFO policy omits position; the entry shape must allow it."""
        entry = ExecutionAdmissionStatusEntry(engine_id="eng-1")

        assert entry.position is None
