import logging
import shutil
import subprocess
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from griptape_nodes.files import os_utils
from griptape_nodes.retained_mode.managers.resource_components.capability_field import (
    CapabilityField,
    validate_capabilities,
)
from griptape_nodes.retained_mode.managers.resource_components.resource_instance import ResourceInstance
from griptape_nodes.retained_mode.managers.resource_components.resource_type import ResourceType
from griptape_nodes.retained_mode.managers.resource_types.os_resource import Architecture, current_architecture

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.resource_components.resource_instance import Requirements

logger = logging.getLogger("griptape_nodes")


class ComputeBackend(StrEnum):
    """Supported compute backends."""

    CPU = "cpu"
    CUDA = "cuda"  # NVIDIA GPU
    MPS = "mps"  # Apple Metal Performance Shaders


# Compute capability field names
ComputeCapability = Literal[
    "compute",  # List of available compute backends: ["cpu", "cuda", "mps"]
]


class ComputeInstance(ResourceInstance):
    """Resource instance representing available compute backends."""

    def can_be_freed(self) -> bool:
        """Compute resources can be freed when no longer needed."""
        return True

    def free(self) -> None:
        """Free compute resource instance."""
        logger.debug("Freeing compute resource instance %s", self.get_instance_id())

    def get_capability_typed(self, key: ComputeCapability) -> Any:
        """Type-safe capability getter using Literal types."""
        return self.get_capability_value(key)


class ComputeResourceType(ResourceType):
    """Resource type for compute backend availability."""

    def get_capability_schema(self) -> list[CapabilityField]:
        """Get the capability schema for compute resources."""
        return [
            CapabilityField(
                name="compute",
                type_hint=list,
                description="List of available compute backends: 'cpu', 'cuda', 'mps'",
                required=True,
            ),
        ]

    def create_instance(self, capabilities: dict[str, Any]) -> ResourceInstance:
        """Create a new compute resource instance."""
        # Validate capabilities against schema
        validation_errors = validate_capabilities(self.get_capability_schema(), capabilities)
        if validation_errors:
            error_msg = f"Invalid compute capabilities: {', '.join(validation_errors)}"
            raise ValueError(error_msg)

        return ComputeInstance(resource_type=self, instance_id_prefix="compute", capabilities=capabilities)

    def select_best_compatible_instance(
        self, compatible_instances: list[ResourceInstance], _requirements: "Requirements | None" = None
    ) -> ResourceInstance | None:
        """Select the best compute instance from compatible ones.

        Returns the first compatible instance (no special selection criteria).
        """
        if not compatible_instances:
            return None

        return compatible_instances[0]


def detect_compute_capabilities() -> dict[str, Any]:
    """Describe this machine's compute backends."""
    return {"compute": detect_compute_backends()}


def detect_compute_backends() -> list[str]:
    """Detect available compute backends: always 'cpu', plus 'cuda' or 'mps' if available."""
    backends: list[str] = [ComputeBackend.CPU]
    if _is_cuda_available():
        backends.append(ComputeBackend.CUDA)
    if _is_mps_available():
        backends.append(ComputeBackend.MPS)

    logger.debug("Detected compute backends: %s", backends)
    return backends


def _is_cuda_available() -> bool:
    """Check if CUDA is available by detecting the NVIDIA driver.

    Uses nvidia-smi, which is lightweight and doesn't require torch.
    """
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0 or not result.stdout.strip():
        return False
    logger.debug("CUDA detected via nvidia-smi: %s", result.stdout.strip().split("\n")[0])
    return True


def _is_mps_available() -> bool:
    """Check if MPS (Metal Performance Shaders) is available.

    MPS requires macOS 12.3+ on Apple Silicon. Every arm64 Mac can run 12.3+, so an arm64 Mac
    counts as having MPS.
    """
    if not os_utils.is_mac():
        return False
    if current_architecture() not in (Architecture.ARM64, Architecture.AARCH64):
        return False
    logger.debug("MPS detected: Apple Silicon Mac")
    return True
