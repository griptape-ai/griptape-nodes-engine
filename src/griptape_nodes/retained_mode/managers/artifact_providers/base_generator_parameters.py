"""Base Pydantic model and custom Field for generator parameters."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField


def Field(
    *,
    description: str,  # REQUIRED - no default allowed
    editor_schema_type: str,  # REQUIRED - no default allowed
    **kwargs: Any,
) -> Any:
    """Custom Field wrapper that enforces description and editor_schema_type.

    This wrapper ensures all generator parameters have human-readable descriptions
    and explicit schema types for editor UI compatibility.

    Args:
        description: Human-readable description (REQUIRED)
        editor_schema_type: Schema type for editor UI - 'integer', 'number', 'string', 'boolean' (REQUIRED)
        **kwargs: Standard Pydantic Field arguments (default, ge, le, gt, lt, etc.)

    Returns:
        Pydantic Field with enhanced metadata

    Example:
        max_width: int = Field(
            default=1024,
            description="Maximum width in pixels for generated preview",
            editor_schema_type="integer",
            ge=1,
        )
    """
    json_schema_extra = kwargs.pop("json_schema_extra", {})
    json_schema_extra["editor_schema_type"] = editor_schema_type

    return PydanticField(
        description=description,
        json_schema_extra=json_schema_extra,
        **kwargs,
    )


class BaseGeneratorParameters(BaseModel):
    """Base class for all generator parameter models.

    Enforces:
    - All fields must have descriptions (via custom Field())
    - All fields must specify editor_schema_type (via custom Field())
    - Extra fields are ignored (allows backward compatibility with old config)
    - Supports dict[str, Any] input/output for config compatibility

    Usage:
        class MyGeneratorParams(BaseGeneratorParameters):
            max_width: int = Field(
                default=1024,
                description="Maximum width in pixels",
                editor_schema_type="integer",
                ge=1,
            )
    """

    model_config = ConfigDict(
        extra="ignore",  # TODO: Change to "forbid" after https://github.com/griptape-ai/griptape-nodes-engine/issues/3980
        validate_assignment=True,  # Validate on attribute assignment
        str_strip_whitespace=True,  # Clean string inputs
    )

    @classmethod
    def get_json_schema_type(cls, field_name: str) -> str:
        """Get editor schema type for a field.

        Args:
            field_name: Name of the field

        Returns:
            Editor schema type string ('integer', 'number', 'string', 'boolean', etc.)

        Note:
            All fields must specify editor_schema_type via Field() - no fallback inference.
        """
        field_info = cls.model_fields[field_name]

        # Extract editor_schema_type from field metadata
        if field_info.json_schema_extra and isinstance(field_info.json_schema_extra, dict):
            editor_type = field_info.json_schema_extra.get("editor_schema_type")
            if editor_type and isinstance(editor_type, str):
                return editor_type

        # This should never happen if Field() is used correctly
        msg = f"Field '{field_name}' missing required editor_schema_type"
        raise ValueError(msg)
