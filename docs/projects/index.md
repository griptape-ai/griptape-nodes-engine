# Project

The project system controls how files are organized, named, and saved when you work in Griptape Nodes. Every time a node saves an image, copies an uploaded file, or downloads a URL, the project system decides where that file goes and what it's called.

## Why it exists

Without a project system, every file save operation would require a hard-coded path. The project system replaces hard-coded paths with named templates called **macros** and named file-saving scenarios called **situations**. This lets you customize your entire project's file layout in one place, and every node that saves files automatically follows that layout.

## How the pieces fit together

```
workspace/                         ← workspace directory (your root)
  griptape-nodes-project.yml       ← optional: your customizations
  my_workflow/                     ← workflow directory
    inputs/                        ← default inputs directory
    outputs/                       ← default outputs directory
    temp/                          ← default temp directory
    .griptape-nodes-previews/      ← default previews directory
```

The conceptual hierarchy is:

```
workspace
  └── project template
        ├── situations    (where and how to save files in each scenario)
        ├── directories   (logical name → relative path mappings)
        └── environment   (custom key-value variables)
```

**Workspace** is the root directory for all work in this project. It is configured in your settings.

**Project template** is the configuration file that governs this project. It is loaded at startup — first the system defaults, then any [parent projects](projects.md#parent-projects) declared via `parent_project_path`, and finally the overrides in this project's `griptape-nodes-project.yml`. The project template is what lets you control where files land and how they're named.

**Situations** are named scenarios for when a file is read or written. Each has a macro template that determines the file path and a policy that determines what happens when a file already exists.

**Directories** are logical name-to-path mappings. Each directory has a short name (like `outputs`) that you use as a variable in macros. That name maps to a real path on disk — by default `outputs` maps to an `outputs` subfolder. Directory paths can be relative or absolute, and can themselves contain macro variables or environment variable references.

**Environment** is a bag of custom key-value pairs that can be referenced in macros.

**Macros** are the template strings used in situations and directories to generate concrete file paths.

## Pages in this section

- [Workspace](workspace.md) — the root working context and how relative paths resolve
- [Projects](projects.md) — the project file format and the merge model
- [Version Pinning](version_pinning.md) — pin a project to an engine version and to specific library versions
- [Macros](macros.md) — template syntax reference for generating file paths
- [Sequences](sequences.md) — image-sequence patterns and how missing frames are handled
- [Directories](directories.md) — logical name-to-path mappings
- [Situations](situations.md) — named file-saving scenarios with policies
- [Environment & Builtin Variables](environment.md) — custom variables and system-provided values
- [File Extension Directories](file_extension_directories.md) — route files to folders by extension
- [Customization Guide](customization.md) — practical examples for common customizations
