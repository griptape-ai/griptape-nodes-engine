# Running in a Managed Environment

Normally the engine looks after its own libraries: it downloads them, builds a separate Python
environment for each one, and installs the packages each library needs. Some studios would rather
do that themselves, with the same tools they use for every other application (a package manager
such as Rez or Conda, a container image, or an in-house launcher). This page is for the person
setting that up. If you're an artist, the short version is at the end:
[What artists see](#what-artists-see).

In a managed environment:

- The environment decides which libraries load, and at which versions.
- The engine never downloads, updates, or installs a library, and never builds a Python
    environment.

## The two settings

| What                        | How you set it                                                  | Purpose                                                                 |
| --------------------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `GTN_LIBRARY_PATHS`         | Environment variable                                            | The libraries the environment provides.                                 |
| `library.dependency_source` | Setting, or `GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE=environment` | Tells the engine the environment provides libraries and their packages. |

### `GTN_LIBRARY_PATHS`

A list of library manifests (`griptape_nodes_library.json` files), or folders to search for them,
separated the way your system separates `PATH` entries (`:` on macOS and Linux, `;` on Windows):

```bash
export GTN_LIBRARY_PATHS="/studio/libs/lib_foo/griptape_nodes_library.json:/studio/libs/lib_bar"
```

Each entry is treated like an entry in `libraries_to_register`, and these libraries load before
the ones in `libraries_to_register`. A relative path is read relative to the workspace. This works
in either dependency source; only the environment source limits loading to this list.

### `library.dependency_source`

`venv` (the default) is the engine's usual behavior. Set it to `environment` when the engine is
started inside an environment that already holds every library and package it needs:

```bash
export GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE=environment
```

With `environment`:

- Only the libraries in `GTN_LIBRARY_PATHS` load. Every other configured library (entries in
    `libraries_to_register`, and the Sandbox Library) is listed with the problem "This library was
    not loaded because the engine is running in an environment that provides its libraries, and
    this library is not one of them."
- `libraries_to_download` is ignored, and nothing is downloaded, updated, or synced. Requests to
    download, update, switch, or sync a library fail with a message saying the environment manages
    libraries.
- Checking a library for updates reports no update and says updates come from the environment,
    without contacting its git remote.
- The Sandbox Library is neither scanned nor loaded, and adding a sandbox node from a file fails
    with a message saying the environment manages libraries.
- No `.venv` or `.venv-exec` folder is created, and none left from an earlier run is used.
- A library that declares another library as a dependency is satisfied only by a library in
    `GTN_LIBRARY_PATHS`. If the environment doesn't provide it, the library reports the missing
    dependency instead of downloading it. The engine matches a dependency's repository name against
    the folders in each library's path, ignoring letter case and treating `-` and `_` alike, so
    keep a folder named after the repository in the path (for example
    `.../griptape_nodes_library_openexr/griptape_nodes_library.json` for
    `griptape-nodes-library-openexr`).
- The artist's own config file is left alone: entries for libraries that didn't load are not
    removed.

A misspelled value in `GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE` is reported in the engine log and
ignored, rather than quietly falling back to `venv`.

## Example launcher

A launcher only needs to set the variables and start the engine inside the prepared environment:

```bash
#!/bin/sh
export GTN_LIBRARY_PATHS="/studio/libs/lib_foo/griptape_nodes_library.json:/studio/libs/lib_bar/griptape_nodes_library.json"
export GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE=environment
exec gtn
```

Most package managers can set these variables for you: each library's package adds itself to
`GTN_LIBRARY_PATHS`, and a launch package sets `library.dependency_source`.

## What artists see

- The libraries in the editor are the ones the studio's environment provides. A library you added
    yourself under **Settings → Library** still appears, marked as not loaded, with a note that the
    environment doesn't provide it. Ask whoever manages your studio's setup to add it.
- Installing, updating, or switching a library's version from the editor doesn't work; those
    changes come from the studio's environment.

For every setting and its environment variable, see the
[Configuration Reference](../reference/configuration_reference.md).
