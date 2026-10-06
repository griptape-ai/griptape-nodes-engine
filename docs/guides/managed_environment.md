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
- Each library's worker process can be started inside the environment that library needs.

## The three settings

| What                     | How you set it                                                | Purpose                                                                 |
| ------------------------ | ------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `GTN_LIBRARY_PATHS`      | Environment variable                                          | The libraries the environment provides.                                 |
| `library.provisioned_by` | Setting, or `GTN_CONFIG_LIBRARY__PROVISIONED_BY=environment`  | Tells the engine the environment provides libraries and their packages. |
| `worker.command_prefix`  | Setting, or `GTN_CONFIG_WORKER__COMMAND_PREFIX` (a JSON list) | Starts each library's worker inside that library's environment.         |

### `GTN_LIBRARY_PATHS`

A list of library manifests (`griptape_nodes_library.json` files), or folders to search for them,
separated the way your system separates `PATH` entries (`:` on macOS and Linux, `;` on Windows):

```bash
export GTN_LIBRARY_PATHS="/studio/libs/lib_foo/griptape_nodes_library.json:/studio/libs/lib_bar"
```

Each entry is treated like an entry in `libraries_to_register`, and these libraries load before
the ones in `libraries_to_register`. A relative path is read relative to the workspace. This works
whichever provisions libraries; only `environment` limits loading to this list. Set it in the
environment the engine starts in: a project's environment settings don't change it.

### `library.provisioned_by`

`engine` (the default) is the engine's usual behavior: it provisions libraries itself. Set it to
`environment` when the engine is started inside an environment that already holds every library
and package it needs:

```bash
export GTN_CONFIG_LIBRARY__PROVISIONED_BY=environment
```

Any value other than `engine` or `environment` is treated as `environment` and logged as an
error, so a misspelled value never downloads, builds, or installs anything. Fix the value to get
the engine's own provisioning back.

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

### `worker.command_prefix`

Libraries that run in their own worker process are started with the engine's own Python
interpreter, by its full path: `/path/to/python -m griptape_nodes_app engine --library-name "Foo Library" ...`.
`worker.command_prefix` is a list of words placed in front of that command, so your tool can
prepare the environment first and then run the worker inside it:

```bash
export GTN_CONFIG_WORKER__COMMAND_PREFIX='["env-tool", "run", "engine=={engine_version}", "python-{python_version}", "{library_request}", "--"]'
```

These placeholders are filled for each worker:

| Placeholder         | Filled with                                                       |
| ------------------- | ----------------------------------------------------------------- |
| `{library_request}` | The library's entry in `GTN_LIBRARY_WORKER_REQUESTS` (see below). |
| `{library_name}`    | The library's name, as written in its manifest.                   |
| `{engine_version}`  | The running engine's version, such as `0.103.0`.                  |
| `{python_version}`  | The Python the engine runs on, as major.minor, such as `3.12`.    |

A word that is exactly `{library_request}` becomes one word per space-separated part of the entry,
so one entry can name several packages. Inside a longer word it's replaced as text.

The prefix can't change which Python runs the worker: it is always the engine's own interpreter.
Prepare each worker's environment for that Python, for example by asking your tool for
`python-{python_version}`. Otherwise packages with compiled parts may be built for a different
Python and fail to import. Start the engine itself from the prepared environment rather than from
a virtual environment, so the worker doesn't pick up packages from that virtual environment.

`GTN_LIBRARY_WORKER_REQUESTS` says what each library's worker needs. Entries are
`<library name>=<request>`, separated like `PATH` entries, where the library name is the `name` in
the library's manifest:

```bash
export GTN_LIBRARY_WORKER_REQUESTS="Foo Library=lib_foo==1.4.2:Bar Library=lib_bar==2.0.1"
```

Because entries are separated like `PATH` entries, a request can't contain `:` on macOS and Linux,
or `;` on Windows. Like `GTN_LIBRARY_PATHS`, set it in the environment the engine starts in: a
project's environment settings don't change it.

If the prefix uses `{library_request}` and a library has no entry:

- With `library.provisioned_by` set to `environment`, that library's worker is not started. Its
    nodes stay editable, and running one says the environment does not say which packages its
    worker needs. The worker is never started without the prefix, because the engine's own
    environment was not prepared for it.
- With `engine`, the worker starts without the prefix, as if none were configured.

A prefix that is set but can't be used, such as a `GTN_CONFIG_WORKER__COMMAND_PREFIX` that isn't a
JSON list of words, is reported in the engine log. With `library.provisioned_by` set to
`environment`, every worker is then refused with that reason rather than started without the
prefix.

The worker receives the engine's environment as it was when the engine started, plus a few
variables the engine sets by name (`GTN_ENGINE_ID`, `GTN_ORCHESTRATOR_ENGINE_ID`,
`PYTHONUNBUFFERED`, and the static file server address). The engine copies no other variables and
changes none of your tool's, so your tool decides what the worker's environment contains.

Keep the prefix free of quoted arguments if Windows machines use it: some shells on Windows drop
double quotes inside arguments.

## Example launcher

A launcher only needs to set the variables and start the engine inside the prepared environment:

```bash
#!/bin/sh
export GTN_LIBRARY_PATHS="/studio/libs/lib_foo/griptape_nodes_library.json:/studio/libs/lib_bar/griptape_nodes_library.json"
export GTN_LIBRARY_WORKER_REQUESTS="Foo Library=lib_foo==1.4.2:Bar Library=lib_bar==2.0.1"
export GTN_CONFIG_LIBRARY__PROVISIONED_BY=environment
export GTN_CONFIG_WORKER__COMMAND_PREFIX='["env-tool", "run", "engine=={engine_version}", "{library_request}", "--"]'
exec gtn
```

Most package managers can set these variables for you: each library's package adds itself to
`GTN_LIBRARY_PATHS` and `GTN_LIBRARY_WORKER_REQUESTS`, and a launch package sets `library.provisioned_by`
and `worker.command_prefix`.

## What artists see

- The libraries in the editor are the ones the studio's environment provides. A library you added
    yourself under **Settings → Library** still appears, marked as not loaded, with a note that the
    environment doesn't provide it. Ask whoever manages your studio's setup to add it.
- Installing, updating, or switching a library's version from the editor doesn't work; those
    changes come from the studio's environment.
- If running a node says its worker couldn't start because the environment doesn't say which
    packages it needs, the environment is missing an entry for that library. Editing the node and
    saving the workflow still work.

For every setting and its environment variable, see the
[Configuration Reference](../reference/configuration_reference.md).
