import logging
import os
from typing import TYPE_CHECKING, Any, Literal

from griptape_nodes.retained_mode.managers.resource_components.capability_field import (
    CapabilityField,
    validate_capabilities,
)
from griptape_nodes.retained_mode.managers.resource_components.resource_instance import ResourceInstance
from griptape_nodes.retained_mode.managers.resource_components.resource_type import ResourceType
from griptape_nodes.retained_mode.managers.resource_types.os_resource import current_architecture

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.resource_components.resource_instance import Requirements

logger = logging.getLogger("griptape_nodes")


# CPU capability field names
CPUCapability = Literal[
    "cores",
    "threads",
    "architecture",
    "clock_speed_ghz",
]


class CPUInstance(ResourceInstance):
    """Resource instance representing CPU compute resources."""

    def can_be_freed(self) -> bool:
        """CPU resources can typically be freed when not in use."""
        return True

    def free(self) -> None:
        """Free CPU resource instance."""
        logger.debug("Freeing CPU resource instance %s", self.get_instance_id())

    def get_capability_typed(self, key: CPUCapability) -> Any:
        """Type-safe capability getter using Literal types."""
        return self.get_capability_value(key)


class CPUResourceType(ResourceType):
    """Resource type for CPU compute resources."""

    def get_capability_schema(self) -> list[CapabilityField]:
        """Get the capability schema for CPU resources."""
        return [
            CapabilityField(
                name="cores",
                type_hint=int,
                description="Number of CPU cores",
                required=True,
            ),
            CapabilityField(
                name="threads",
                type_hint=int,
                description="Number of threads (with hyperthreading)",
                required=False,
            ),
            CapabilityField(
                name="architecture",
                type_hint=str,
                description="CPU architecture: 'x86_64', 'arm64', 'aarch64'",
                required=True,
            ),
            CapabilityField(
                name="clock_speed_ghz",
                type_hint=float,
                description="Base clock speed in GHz",
                required=False,
            ),
        ]

    def create_instance(self, capabilities: dict[str, Any]) -> ResourceInstance:
        """Create a new CPU resource instance."""
        # Validate capabilities against schema
        validation_errors = validate_capabilities(self.get_capability_schema(), capabilities)
        if validation_errors:
            error_msg = f"Invalid CPU capabilities: {', '.join(validation_errors)}"
            raise ValueError(error_msg)

        return CPUInstance(resource_type=self, instance_id_prefix="cpu", capabilities=capabilities)

    def select_best_compatible_instance(
        self, compatible_instances: list[ResourceInstance], _requirements: "Requirements | None" = None
    ) -> ResourceInstance | None:
        """Select the best CPU instance from compatible ones.

        Prioritizes CPUs with:
        1. More cores
        2. Higher clock speed
        """
        if not compatible_instances:
            return None

        def sort_key(instance: ResourceInstance) -> tuple[int, float]:
            # More cores is better
            cores = instance.get_capability_value("cores") if instance.has_capability("cores") else 1

            # Higher clock speed is better
            clock_speed = (
                instance.get_capability_value("clock_speed_ghz") if instance.has_capability("clock_speed_ghz") else 0
            )

            return (int(cores), float(clock_speed))

        sorted_instances = sorted(compatible_instances, key=sort_key, reverse=True)
        return sorted_instances[0]


def detect_cpu_capabilities() -> dict[str, Any]:
    """Describe this machine's CPU."""
    return {
        "cores": os.cpu_count() or 1,
        "architecture": current_architecture(),
    }
