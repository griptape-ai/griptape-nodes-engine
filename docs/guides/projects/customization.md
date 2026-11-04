# Customization Guide

All customizations go in `griptape-nodes-project.yml` in your workspace directory. You only need to include the things you want to change — everything else inherits from the system defaults.

## Changing the outputs directory path

Move all node outputs to a folder called `renders`:

```yaml
project_template_schema_version: "0.1.0"
name: "My Project"

directories:
  outputs:
    path_macro: "renders"
```

All situations that use `{outputs}` will now save to the `renders` folder instead of `outputs`.

## Changing the inputs directory path

```yaml
project_template_schema_version: "0.1.0"
name: "My Project"

directories:
  inputs:
    path_macro: "source_files"
```

## Adding a custom directory

Add a directory for client deliverables:

```yaml
project_template_schema_version: "0.1.0"
name: "My Project"

directories:
  deliverables:
    path_macro: "client/final"
```

Now `{deliverables}` is available as a variable in any macro.

## Adding a custom situation

Add a situation that saves final renders to the deliverables directory with overwrite policy:

```yaml
project_template_schema_version: "0.1.0"
name: "My Project"

directories:
  deliverables:
    path_macro: "client/final"

situations:
  save_deliverable:
    macro: "{deliverables}/{workflow_name?:_}{file_name_base}.{file_extension}"
    policy:
      on_collision: overwrite
      create_dirs: true
    description: "Final deliverable for client"
```

## Modifying an existing situation

Change `save_node_output` to always include the workflow name in the filename:

```yaml
project_template_schema_version: "0.1.0"
name: "My Project"

situations:
  save_node_output:
    macro: "{outputs}/{workflow_name:_}{file_name_base}{_index?:03}.{file_extension}"
```

Only the `macro` field is replaced. The policy, fallback, and description are inherited from the default.

## Adding environment variables

Define custom values for use in macros:

```yaml
project_template_schema_version: "0.1.0"
name: "My Project"

environment:
  CLIENT_CODE: "ACME"
  SEASON: "S03"
```

Reference them in a directory path or situation macro:

```yaml
directories:
  outputs:
    path_macro: "{CLIENT_CODE}/{SEASON}/renders"
```

## Routing files by extension

Use `file_extension_directories` to land files of different types in different subfolders without writing a separate situation for each type. See [File Extension Directories](file_extension_directories.md) for the full reference.

```yaml
project_template_schema_version: "0.3.0"
name: "My Project"

file_extension_directories:
  png: "images"
  jpg: "images"
  mp4: "videos"
  wav: "audio"

situations:
  save_node_output:
    macro: "{outputs}/{file_extension_directory?:/}{node_name?:_}{file_name_base}{_index?:03}.{file_extension}"
```

Extensions can also resolve to a macro. To route videos to a share drive while everything else stays under `outputs`, use the situation macro that lets the routing value dictate the root:

```yaml
project_template_schema_version: "0.3.0"
name: "My Project"

file_extension_directories:
  png: "{outputs}/images"
  mp4: "{workspace_dir}/shared/videos"

situations:
  save_node_output:
    macro: "{file_extension_directory?:/}{node_name?:_}{file_name_base}{_index?:03}.{file_extension}"
```

## Referencing OS environment variables

Pull values from the operating system environment:

```yaml
project_template_schema_version: "0.1.0"
name: "My Project"

environment:
  SHARED_DRIVE: "$STUDIO_SHARED_STORAGE"

directories:
  outputs:
    path_macro: "{SHARED_DRIVE}/renders"
```

If the OS environment variable `STUDIO_SHARED_STORAGE` is set to `/mnt/studio`, then `{outputs}` resolves to `/mnt/studio/renders`.

## Putting it all together

A complete customized project file for a visual effects project:

```yaml
project_template_schema_version: "0.1.0"
name: "VFX Pipeline"
description: "Customized layout for VFX production"

environment:
  SHOW_CODE: "AURORA"

directories:
  inputs:
    path_macro: "source"
  outputs:
    path_macro: "renders"
  plates:
    path_macro: "source/plates"
  deliverables:
    path_macro: "deliverables/{SHOW_CODE}"

situations:
  save_node_output:
    macro: "{outputs}/{workflow_name?:_}{file_name_base}{_index?:03}.{file_extension}"

  save_deliverable:
    macro: "{deliverables}/{workflow_name?:_}{file_name_base}.{file_extension}"
    policy:
      on_collision: overwrite
      create_dirs: true
    fallback: save_file
    description: "Final deliverable"
```
