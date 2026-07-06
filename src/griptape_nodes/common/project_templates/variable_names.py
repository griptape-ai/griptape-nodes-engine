"""Canonical macro-variable names bound by the write path.

These constants name variables that this codebase's writers bind into macro
variables bags — either from ``FilenameParts`` (the first three below) or from
caller-supplied context (the rest). Situation macros in ``default_project_template.py``
reference them literally (``{file_name_base}``, ``{node_name}``, etc.); the writer
emits them with these exact keys.

They live under ``common/project_templates`` — not the macro parser — because the
macro parser is *general* and has no opinion on what a template author names their
variables. This is a convention that the shipping project templates and the writer
happen to agree on.

Downstream code that *reads* a variables bag produced by a writer (sequence
scanners, reverse-match helpers, retention pruners) should stay bag-opaque: it
receives whatever variables the writer's macro actually bound and forwards them
verbatim. These constants are for the *write* side — the code that mechanically
translates a filename or context into named macro variables.
"""

# Filename-derived (from ``FilenameParts`` splitting).
FILE_NAME_BASE_VARIABLE_NAME = "file_name_base"
FILE_EXTENSION_VARIABLE_NAME = "file_extension"
SUB_DIRS_VARIABLE_NAME = "sub_dirs"

# Caller-context-derived (bound by node/artifact writers from their own state).
NODE_NAME_VARIABLE_NAME = "node_name"
SOURCE_FILE_NAME_VARIABLE_NAME = "source_file_name"
SOURCE_RELATIVE_PATH_VARIABLE_NAME = "source_relative_path"
DRIVE_VOLUME_MOUNT_VARIABLE_NAME = "drive_volume_mount"
PREVIEW_FORMAT_VARIABLE_NAME = "preview_format"
