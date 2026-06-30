from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, Trait


@dataclass(eq=False)
class FileSystemPicker(Trait):
    allow_files: bool = False
    allow_directories: bool = True
    allow_sequences: bool = False
    multiple: bool = False
    file_types: list[str] = field(default_factory=list)
    file_extensions: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    include_patterns: list[str] = field(default_factory=list)
    max_file_size: int | None = None
    min_file_size: int | None = None
    workspace_only: bool = False
    initial_path: str | None = None
    allow_create: bool = False
    allow_rename: bool = False
    element_id: str = field(default_factory=lambda: "FileSystemPicker")

    def __init__(  # noqa: PLR0913
        self,
        *,
        allow_files: bool = False,
        allow_directories: bool = True,
        allow_sequences: bool = False,
        multiple: bool = False,
        file_types: list[str] | None = None,
        file_extensions: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        include_patterns: list[str] | None = None,
        max_file_size: int | None = None,
        min_file_size: int | None = None,
        workspace_only: bool = False,
        initial_path: str | None = None,
        allow_create: bool = False,
        allow_rename: bool = False,
    ) -> None:
        super().__init__()
        self.allow_files = allow_files
        self.allow_directories = allow_directories
        self.allow_sequences = allow_sequences
        self.multiple = multiple
        self.file_types = file_types or []
        self.file_extensions = file_extensions or []
        self.exclude_patterns = exclude_patterns or []
        self.include_patterns = include_patterns or []
        self.max_file_size = max_file_size
        self.min_file_size = min_file_size
        self.workspace_only = workspace_only
        self.initial_path = initial_path
        self.allow_create = allow_create
        self.allow_rename = allow_rename

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["fileSystemPicker", "file_picker", "folder_picker"]

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
