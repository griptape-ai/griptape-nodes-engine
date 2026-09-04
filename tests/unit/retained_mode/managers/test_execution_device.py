"""Choosing a compute device without importing a framework to find out.

Every model-wrapping library currently imports torch purely to call `torch.cuda.is_available()`.
That pulls an execution-time dependency into whichever process asks the question -- including one
that only edits workflows, where the import fails outright. The engine already detects the
machine's backends (nvidia-smi for CUDA, a platform check for MPS) and registers them as a
compute resource, so the answer is available without the import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.resource_events import (
    CreateResourceInstanceRequest,
    CreateResourceInstanceResultSuccess,
    GetExecutionDeviceRequest,
    GetExecutionDeviceResultFailure,
    GetExecutionDeviceResultSuccess,
)
from griptape_nodes.retained_mode.managers.resource_types.compute_resource import ComputeInstance

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


@pytest.fixture(autouse=True)
def _no_system_compute(engine: Engine) -> None:
    """Drop the real machine's compute instance so these tests describe a machine, not this one.

    OSManager registers one system compute instance at boot advertising whatever this host has.
    A test that adds a second is not describing a second GPU -- there is one system instance, and
    standing in for it is the only way to assert the choice for a machine we do not have.
    """
    manager = engine.resource_manager
    for instance_id, instance in list(manager._instances.items()):
        if isinstance(instance, ComputeInstance):
            del manager._instances[instance_id]


def _register_compute(engine: Engine, backends: list[str]) -> None:
    """Stand in for the system compute instance, advertising exactly these backends."""
    result = engine.handle_request(
        CreateResourceInstanceRequest(
            resource_type_name="ComputeResourceType",
            capabilities={"compute": backends},
        )
    )
    assert isinstance(result, CreateResourceInstanceResultSuccess), getattr(result, "result_details", result)


class TestDevicePreference:
    def test_cuda_wins_when_present(self, engine: Engine) -> None:
        _register_compute(engine, ["cpu", "cuda"])

        result = engine.handle_request(GetExecutionDeviceRequest())

        assert isinstance(result, GetExecutionDeviceResultSuccess)
        assert result.device == "cuda"
        assert result.available == ["cpu", "cuda"]

    def test_mps_wins_over_cpu(self, engine: Engine) -> None:
        _register_compute(engine, ["cpu", "mps"])

        result = engine.handle_request(GetExecutionDeviceRequest())

        assert isinstance(result, GetExecutionDeviceResultSuccess)
        assert result.device == "mps"

    def test_cpu_when_it_is_all_there_is(self, engine: Engine) -> None:
        _register_compute(engine, ["cpu"])

        result = engine.handle_request(GetExecutionDeviceRequest())

        assert isinstance(result, GetExecutionDeviceResultSuccess)
        assert result.device == "cpu"


class TestPreferredDevice:
    def test_a_preference_this_machine_has_is_honored(self, engine: Engine) -> None:
        """A user or config pin outranks the general order."""
        _register_compute(engine, ["cpu", "cuda"])

        result = engine.handle_request(GetExecutionDeviceRequest(preferred="cpu"))

        assert isinstance(result, GetExecutionDeviceResultSuccess)
        assert result.device == "cpu"
        assert result.honored_preference is True

    def test_a_preference_this_machine_lacks_falls_back_rather_than_failing(self, engine: Engine) -> None:
        """A workflow authored on a CUDA box must still run on a laptop.

        Failing here would make the device pin a portability barrier: the file would open and then
        refuse to run for a reason the author never intended to encode.
        """
        _register_compute(engine, ["cpu", "mps"])

        result = engine.handle_request(GetExecutionDeviceRequest(preferred="cuda"))

        assert isinstance(result, GetExecutionDeviceResultSuccess)
        assert result.device == "mps"
        assert result.honored_preference is False
        assert "cuda" in str(result.result_details), "the answer should say the preference was unavailable"


class TestNoComputeResource:
    def test_it_fails_rather_than_guessing(self, engine: Engine) -> None:
        """With nothing registered the machine's backends are genuinely unknown."""
        result = engine.handle_request(GetExecutionDeviceRequest())

        assert isinstance(result, GetExecutionDeviceResultFailure)
        assert "no compute resource instance" in str(result.result_details)


class TestTheNodeAccessor:
    def test_a_node_reads_the_device_without_importing_anything(self, engine: Engine) -> None:
        from tests.unit.exe_types.mocks import MockNode

        _register_compute(engine, ["cpu", "cuda"])
        node = MockNode(name="device_reader")

        assert node.execution_device == "cuda"
        assert node.available_compute == ["cpu", "cuda"]

    def test_a_node_falls_back_to_cpu_when_the_engine_cannot_say(self, engine: Engine) -> None:  # noqa: ARG002
        """A node that cannot pick a device is worse than one running slowly."""
        from tests.unit.exe_types.mocks import MockNode

        node = MockNode(name="device_reader_no_resource")

        assert node.execution_device == "cpu"
        assert node.available_compute == ["cpu"]
