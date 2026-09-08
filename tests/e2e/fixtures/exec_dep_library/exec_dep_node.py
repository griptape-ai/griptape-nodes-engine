"""Node that splits its dependencies the way the execution-environment design requires.

The two imports in this file are the whole point:

- ``fakeedit`` is imported at module scope, so it is an EDIT-TIME dependency. It has to resolve
  anywhere a node is merely defined or instantiated, which includes the orchestrator.
- ``fakeexec`` is imported inside ``process``, so it is an EXECUTION dependency. It only has to
  resolve where nodes actually execute.

Real heavy libraries have this shape with torch and diffusers standing in for ``fakeexec``.
"""

from __future__ import annotations

import fakeedit  # type: ignore[reportMissingImports]  # Edit-time dep: only importable from the library's edit venv.

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode


class ExecDepNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        # Reading the edit-time dep here proves it is available at instantiation, not just import.
        self.add_parameter(
            Parameter(
                name="edit_dep_version",
                tooltip="Version of the edit-time dependency seen at instantiation",
                type="str",
                default_value=fakeedit.__version__,
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="exec_dep_version",
                tooltip="Version of the execution dependency seen while running",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        # Deliberate deferred import, not a circular-import workaround: this dependency is
        # declared as execution-only, so it is absent from the orchestrator by design and
        # importing it at module scope would make this node impossible to instantiate there.
        import fakeexec  # type: ignore[reportMissingImports]

        self.parameter_output_values["edit_dep_version"] = fakeedit.__version__
        self.parameter_output_values["exec_dep_version"] = fakeexec.__version__
