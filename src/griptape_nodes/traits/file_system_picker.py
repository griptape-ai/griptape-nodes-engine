from collections.abc import Callable
from typing import Any

import attrs

from griptape_nodes.exe_types.core_types import Parameter, Trait


def _pattern_list(patterns: list[str] | None) -> list[str]:
    if patterns is None:
        return []
    return patterns


class FileSystemPicker(Trait):
    allow_files: bool = attrs.field(default=False)
    allow_directories: bool = attrs.field(default=True)
    allow_sequences: bool = attrs.field(default=False)
    multiple: bool = attrs.field(default=False)
    file_types: list[str] = attrs.field(default=None, converter=_pattern_list)
    file_extensions: list[str] = attrs.field(default=None, converter=_pattern_list)
    exclude_patterns: list[str] = attrs.field(default=None, converter=_pattern_list)
    include_patterns: list[str] = attrs.field(default=None, converter=_pattern_list)
    max_file_size: int | None = attrs.field(default=None)
    min_file_size: int | None = attrs.field(default=None)
    workspace_only: bool = attrs.field(default=False)
    initial_path: str | None = attrs.field(default=None)
    allow_create: bool = attrs.field(default=False)
    allow_rename: bool = attrs.field(default=False)

    def ui_options_for_trait(self) -> dict[str, Any]:
        """Generate the fileSystemPicker UI options dictionary."""
        options: dict[str, Any] = {
            "allowFiles": self.allow_files,
            "allowDirectories": self.allow_directories,
            "allowSequences": self.allow_sequences,
            "multiple": self.multiple,
            "workspaceOnly": self.workspace_only,
            "allowCreate": self.allow_create,
            "allowRename": self.allow_rename,
        }

        # Add file types/extensions
        if self.file_types:
            options["fileTypes"] = self.file_types
        elif self.file_extensions:
            options["fileExtensions"] = self.file_extensions

        # Add patterns
        if self.exclude_patterns:
            options["excludePatterns"] = self.exclude_patterns
        if self.include_patterns:
            options["includePatterns"] = self.include_patterns

        # Add size limits
        if self.max_file_size is not None:
            options["maxFileSize"] = self.max_file_size
        if self.min_file_size is not None:
            options["minFileSize"] = self.min_file_size

        # Add initial path
        if self.initial_path:
            options["initialPath"] = self.initial_path

        return {"fileSystemPicker": options}

    def validators_for_trait(self) -> list[Callable[[Parameter, Any], Any]]:
        """Validate file system picker configuration."""

        def validate(param: Parameter, value: Any) -> None:  # noqa: ARG001
            # Validate that at least one selection type is enabled
            if not self.allow_files and not self.allow_directories and not self.allow_sequences:
                msg = "At least one of allow_files, allow_directories, or allow_sequences must be True"
                raise ValueError(msg)

            # Validate that creation is only allowed when appropriate selection types are enabled
            if self.allow_create and not self.allow_files and not self.allow_directories and not self.allow_sequences:
                msg = "allow_create requires at least one of allow_files, allow_directories, or allow_sequences to be True"
                raise ValueError(msg)

            # Validate that rename is only allowed when appropriate selection types are enabled
            if self.allow_rename and not self.allow_files and not self.allow_directories and not self.allow_sequences:
                msg = "allow_rename requires at least one of allow_files, allow_directories, or allow_sequences to be True"
                raise ValueError(msg)

            # Validate file size limits
            if (
                self.max_file_size is not None
                and self.min_file_size is not None
                and self.max_file_size < self.min_file_size
            ):
                msg = "max_file_size cannot be less than min_file_size"
                raise ValueError(msg)

            # Validate that file types/extensions are valid
            all_file_types = self.file_types + self.file_extensions
            for file_type in all_file_types:
                if not file_type.startswith("."):
                    msg = f"File type '{file_type}' must start with a dot (e.g., '.py')"
                    raise ValueError(msg)

        return [validate]

    def converters_for_trait(self) -> list[Callable]:
        """Convert file system picker values if needed."""

        def converter(value: Any) -> Any:
            # If value is a string and we expect a list, convert it
            if isinstance(value, str) and self.multiple:
                return [value] if value else []
            return value

        return [converter]


# These Traits get added to a list on the parameter. When they are added they apply their functions to the parameter.
