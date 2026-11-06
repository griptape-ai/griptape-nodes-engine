"""Detection of this machine's OS, CPU, and compute resources.

Platform, architecture, and tool lookups are patched so the tests run identically on any host.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.resource_events import ListRegisteredResourceTypesRequest
from griptape_nodes.retained_mode.managers.resource_types import compute_resource, os_resource
from griptape_nodes.retained_mode.managers.resource_types.compute_resource import (
    ComputeBackend,
    ComputeInstance,
    detect_compute_backends,
)
from griptape_nodes.retained_mode.managers.resource_types.cpu_resource import CPUInstance, detect_cpu_capabilities
from griptape_nodes.retained_mode.managers.resource_types.os_resource import (
    Architecture,
    OSInstance,
    current_architecture,
    current_platform_version,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


def _set_platform(monkeypatch: pytest.MonkeyPatch, sys_platform: str) -> None:
    monkeypatch.setattr("griptape_nodes.files.os_utils.sys.platform", sys_platform)


def _set_uname(monkeypatch: pytest.MonkeyPatch, *, machine: str = "x86_64", release: str = "6.1.0") -> None:
    monkeypatch.setattr(
        os_resource.os, "uname", lambda: SimpleNamespace(machine=machine, release=release), raising=False
    )


class TestCurrentArchitecture:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            ("x86_64", Architecture.X86_64),
            ("AMD64", Architecture.X86_64),
            ("arm64", Architecture.ARM64),
            ("aarch64", Architecture.AARCH64),
            ("riscv64", "riscv64"),
        ],
    )
    def test_normalizes_uname_machine(self, monkeypatch: pytest.MonkeyPatch, machine: str, expected: str) -> None:
        _set_platform(monkeypatch, "linux")
        _set_uname(monkeypatch, machine=machine)
        assert current_architecture() == expected

    def test_windows_reads_processor_architecture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "win32")
        monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
        assert current_architecture() == Architecture.X86_64


class TestCurrentPlatformVersion:
    def test_reads_uname_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_uname(monkeypatch, release="24.1.0")
        assert current_platform_version() == "24.1.0"

    def test_falls_back_to_sys_platform_without_uname(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Windows has no os.uname().
        monkeypatch.delattr(os_resource.os, "uname", raising=False)
        monkeypatch.setattr(os_resource.sys, "platform", "win32")
        assert current_platform_version() == "win32"


class TestDetectCpuCapabilities:
    def test_reports_cores_and_architecture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "linux")
        _set_uname(monkeypatch, machine="aarch64")
        monkeypatch.setattr("griptape_nodes.retained_mode.managers.resource_types.cpu_resource.os.cpu_count", lambda: 8)
        assert detect_cpu_capabilities() == {"cores": 8, "architecture": Architecture.AARCH64}

    def test_unknown_core_count_reports_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "griptape_nodes.retained_mode.managers.resource_types.cpu_resource.os.cpu_count", lambda: None
        )
        assert detect_cpu_capabilities()["cores"] == 1


class TestDetectComputeBackends:
    @pytest.fixture(autouse=True)
    def _linux_x86(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "linux")
        _set_uname(monkeypatch, machine="x86_64")

    def _nvidia_smi(self, monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stdout: str = "") -> None:
        monkeypatch.setattr(compute_resource.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(
            compute_resource.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode, stdout=stdout),
        )

    def test_cpu_only_without_nvidia_smi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(compute_resource.shutil, "which", lambda _name: None)
        assert detect_compute_backends() == [ComputeBackend.CPU]

    def test_cuda_when_nvidia_smi_lists_a_gpu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._nvidia_smi(monkeypatch, stdout="NVIDIA RTX 4090\n")
        assert detect_compute_backends() == [ComputeBackend.CPU, ComputeBackend.CUDA]

    @pytest.mark.parametrize(("returncode", "stdout"), [(1, "NVIDIA RTX 4090"), (0, "  \n")])
    def test_no_cuda_when_nvidia_smi_finds_nothing(
        self, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
    ) -> None:
        self._nvidia_smi(monkeypatch, returncode=returncode, stdout=stdout)
        assert detect_compute_backends() == [ComputeBackend.CPU]

    def test_no_cuda_when_nvidia_smi_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(compute_resource.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")

        def time_out(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5)

        monkeypatch.setattr(compute_resource.subprocess, "run", time_out)
        assert detect_compute_backends() == [ComputeBackend.CPU]

    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            ("arm64", [ComputeBackend.CPU, ComputeBackend.MPS]),
            ("x86_64", [ComputeBackend.CPU]),
        ],
    )
    def test_mps_only_on_apple_silicon(self, monkeypatch: pytest.MonkeyPatch, machine: str, expected: list) -> None:
        _set_platform(monkeypatch, "darwin")
        _set_uname(monkeypatch, machine=machine)
        monkeypatch.setattr(compute_resource.shutil, "which", lambda _name: None)
        assert detect_compute_backends() == expected


class TestResourceManagerRegistersSystemResources:
    def test_registers_one_instance_per_system_resource(self, engine: Engine) -> None:
        result = engine.handle_request(ListRegisteredResourceTypesRequest())
        assert set(result.resource_type_names) >= {"OSResourceType", "CPUResourceType", "ComputeResourceType"}  # type: ignore[attr-defined]

        instance_types = [type(instance) for instance in engine.resource_manager._capability_instances.values()]
        for instance_type in (OSInstance, CPUInstance, ComputeInstance):
            assert instance_types.count(instance_type) == 1
