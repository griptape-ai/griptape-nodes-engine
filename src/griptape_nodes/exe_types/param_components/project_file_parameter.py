"""ProjectFileParameter - parameter component for project-aware file saving.

Wraps a MacroPath into a FileDestination with a baked-in write policy, deferring
path resolution to execution time via the situation-based macro system.
"""

from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.exe_types.core_types import ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.project_output_parameter import ProjectOutputParameter
from griptape_nodes.files.file import FileDestination
from griptape_nodes.files.project_file import ProjectFileDestination, file_destination_from_spec
from griptape_nodes.node_library.library_registry import LibraryRegistry, LibraryRegistryError
from griptape_nodes.retained_mode.events.destination_events import (
    GetConnectedFileDestinationRequest,
    GetConnectedFileDestinationResultSuccess,
)
from griptape_nodes.retained_mode.file_metadata.provenance_record import ProducingNodeIdentity
from griptape_nodes.traits.file_system_picker import FileSystemPicker


class ProjectFileParameter(ProjectOutputParameter):
    """Parameter component for project-aware file saving.

    Adds a file path parameter to a node that, when processed, returns a
    FileDestination containing a MacroPath and baked-in write policy for
    deferred path resolution.

    Usage:
        # In node __init__:
        self._file_param = ProjectFileParameter(
            node=self,
            name="output_file",
            default_filename="image.png",
        )
        self._file_param.add_parameter()

        # In node process():
        output_file = self._file_param.build_file(sub_dirs="renders")
    """

    DEFAULT_SITUATION = BuiltInSituation.SAVE_NODE_OUTPUT

    def __init__(  # noqa: PLR0913
        self,
        node: BaseNode,
        name: str,
        *,
        default_filename: str,
        situation: str = DEFAULT_SITUATION,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
    ) -> None:
        super().__init__(
            node,
            name,
            default_value=default_filename,
            situation=situation,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
        )

    @property
    def _settings_node_type(self) -> str:
        return "FileOutputSettings"

    @property
    def _settings_value_param_name(self) -> str:
        return "filename"

    @property
    def _settings_source_param_name(self) -> str:
        return "file_destination"

    @property
    def _parameter_output_type(self) -> str:
        return "str"

    def _make_parameter_traits(self) -> set:
        return {
            FileSystemPicker(
                allow_files=True,
                allow_directories=False,
                allow_create=True,
            )
        }

    def build_file(self, **extra_vars: str | int) -> FileDestination:
        """Build a FileDestination with a MacroPath from the parameter's current value.

        If a settings node (e.g. FileOutputSettings) is connected to this parameter, its
        ``FileDestination`` is used. A connected settings node with no destination set raises
        instead of silently falling back to the default situation, since that fallback hides
        user intent.

        Otherwise the parameter's string value is combined with this component's
        situation to build a ``FileDestination``.

        Either way the destination writes in the node's workflow, so it can be built and
        written from any thread.

        Args:
            **extra_vars: Additional variables for the macro (e.g., sub_dirs="renders")

        Returns:
            FileDestination with a MacroPath and baked-in write policy for deferred path resolution

        Raises:
            ValueError: If the connected settings node can't be read or has no destination set,
                or if the parameter's value is a URL that names no local file.
        """
        upstream = self._get_upstream_destination(
            GetConnectedFileDestinationRequest(node_name=self._node.name, parameter_name=self._name),
            GetConnectedFileDestinationResultSuccess,
            file_destination_from_spec,
        )
        if upstream is not None:
            return upstream

        value = self._node.get_parameter_value(self._name)
        filename = value if isinstance(value, str) and value else self._default_value

        if "node_name" not in extra_vars:
            extra_vars["node_name"] = self._node.name

        return ProjectFileDestination.from_situation_in_workflow(
            self._node.workflow,
            filename,
            self._situation_name,
            producing_node=self._producing_node_identity(),
            **extra_vars,
        )

    def _producing_node_identity(self) -> ProducingNodeIdentity:
        """Attested identity of the saving node, for the provenance record.

        This component KNOWS which node is saving, so the record never falls
        back to the engine's resolving-node inference (which picks the wrong
        node in parallel flows). The node's "library" metadata entry is only
        the disambiguation hint (multiple libraries can register the same node
        type); the registry is the authority for the library's name and version.
        """
        node_type = type(self._node).__name__
        library_name: str | None = self._node.metadata.get("library")
        library_version: str | None = None
        try:
            library = LibraryRegistry.get_library_for_node_type(node_type, library_name)
        except LibraryRegistryError:
            # Unregistered node type (e.g. a bare test node): keep the hint, no version.
            pass
        else:
            library_name = library.get_library_data().name
            library_version = library.get_metadata().library_version
        return ProducingNodeIdentity(
            name=self._node.name,
            node_type=node_type,
            library_name=library_name,
            library_version=library_version,
        )
