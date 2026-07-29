"""Project-defined variable definitions for project.yml `variables:` sections."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from griptape_nodes.retained_mode.variable_types import VariablePermission

# Value types a project variable may declare today. Only str and int participate
# in {VAR} macro substitution; richer types can be added once a consumer exists.
ProjectVariableType = Literal["str", "int"]


class ProjectVariableDef(BaseModel):
    """One user-defined variable declared by a project template.

    Declared in project.yml as:

        variables:
          shot_code:
            value: sc042
            type: str            # optional, default "str"
            permission: read_write  # optional, default read_write

    At template load these populate the project's stored variable layer in
    VariablesManager, where they participate in hierarchical variable lookup
    (FLOW > PROJECT > GLOBAL) and macro resolution. Unlike builtins and
    directories (the computed namespace), these are plain stored values: their
    names are NOT reserved, and READ_WRITE entries are writable at runtime.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Name of the variable (the {VAR} token text)")
    # Strict: no cross-type coercion. Plain `str | int` would let pydantic coerce a
    # YAML bool/float into the union ("True" or 1) before validators could see it.
    value: StrictStr | StrictInt = Field(description="The variable's value")
    type: ProjectVariableType = Field(default="str", description="Value type: 'str' or 'int'")
    permission: VariablePermission = Field(
        default=VariablePermission.READ_WRITE,
        description="What runtime writes are allowed: read_only, write_only, or read_write",
    )

    @model_validator(mode="after")
    def value_matches_declared_type(self) -> ProjectVariableDef:
        """The declared type and the actual value type must agree.

        A mismatch is a template-author error worth failing loudly at load: silently
        coercing (int 42 declared as str, or "42" declared as int) would make the
        variable's runtime behavior diverge from what the YAML says.
        """
        expected = str if self.type == "str" else int
        if not isinstance(self.value, expected):
            msg = (
                f"Project variable '{self.name}' declares type '{self.type}' but its value "
                f"{self.value!r} is {type(self.value).__name__}"
            )
            # Pydantic validators must raise ValueError to surface as a ValidationError;
            # TypeError would escape the validation machinery as a crash.
            raise ValueError(msg)  # noqa: TRY004
        return self
