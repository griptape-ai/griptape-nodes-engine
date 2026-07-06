"""Canonical macro-variable names bound by ``ProjectFileDestination.from_situation``.

These constants are *project-template conventions*: they name the three variables
that this codebase's ``FilenameParts``-driven writer binds when it splits a filename
into a base + extension (+ optional relative sub-directory). Situation macros in
``default_project_template.py`` reference them literally (``{file_name_base}``,
``{file_extension}``, ``{sub_dirs?:/}``); the writer emits them with these exact keys.

They live under ``common/project_templates`` — not the macro parser — because the
macro parser is *general* and has no opinion on what a template author names their
variables. This is a convention that the shipping project templates and the writer
happen to agree on.

Downstream code that *reads* a variables bag produced by the writer (sequence
scanners, reverse-match helpers, retention pruners) should stay bag-opaque: they
receive whatever variables the writer's macro actually bound, and forward them
verbatim. These constants are for the *write* side — the one place that
mechanically translates ``FilenameParts`` into named macro variables.
"""

FILE_NAME_BASE_VARIABLE_NAME = "file_name_base"
FILE_EXTENSION_VARIABLE_NAME = "file_extension"
SUB_DIRS_VARIABLE_NAME = "sub_dirs"
