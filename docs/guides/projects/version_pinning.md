# Pinning engine and library versions

This guide is for administrators who distribute a project and need it to run
against a known-good engine version and a known-good set of library versions.
Pinning makes the project the source of truth: when a user activates it, the
engine refuses to run under an incompatible version and provisions each pinned
library to the version the project declares.

Both pins live in the **project-adjacent config** — a `griptape_nodes_config.json`
placed next to the project's `griptape-nodes-project.yml`. The project YAML itself
carries no version data; the adjacent config does. See
[Workspace](workspace.md#config-files) for how this file layers on top of user
config.

```
/MyProject/
  griptape-nodes-project.yml      <- the project (no version pins here)
  griptape_nodes_config.json      <- engine + library pins live here
```

Distribute both files together. Because the adjacent config layers above the
user's global config but below their workspace config, your pins apply to every
user who activates the project without being baked into their machine-wide
settings.

## Pin the engine version

Set `requires_engine` to a [PEP 440 version specifier](https://peps.python.org/pep-0440/#version-specifiers).
When the project is activated, the running engine's version must satisfy the
specifier or activation is blocked.

```json
{
  "app_events": {
    "on_app_initialization_complete": {
      "requires_engine": ">=0.80,<1.0"
    }
  }
}
```

- The specifier is matched against the running engine's version.
- A mismatch **blocks activation**: the project does not load and the user is
    told which version is required versus which is running.
- Omit the key (or set it to `null`) to skip the engine check entirely.

Use a bounded range (`>=0.80,<1.0`) rather than an open lower bound so the
project does not silently activate under a future major engine release that may
have changed behavior.

## Pin library versions

`libraries_to_download` lists the libraries the engine provisions on the
project's behalf. Each entry can be either a bare git URL string (today's
behavior — clone from source, no version enforcement) or an object that adds a
version pin:

```json
{
  "app_events": {
    "on_app_initialization_complete": {
      "libraries_to_download": [
        {
          "name": "Griptape Nodes Library",
          "version": "==0.79.0",
          "git_url": "griptape-ai/griptape-nodes-library-standard@v0.79.0"
        }
      ]
    }
  }
}
```

| Field     | Required | Meaning                                                                                                                                                            |
| --------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `git_url` | Yes      | Git source in `url@ref` form: a full URL or `user/repo` shorthand, with an optional `@branch\|tag\|commit` suffix. No `@ref` uses the repository's default branch. |
| `version` | No       | PEP 440 specifier the installed library must satisfy (e.g. `==0.79.0`, `>=1.2,<2`). Omit to pin by source only.                                                    |
| `name`    | No       | The library's manifest `name`. When set, the installed copy is matched by name to decide whether a re-download is needed.                                          |

Pin both the `git_url` ref **and** the `version` to the same release (e.g.
`@v0.79.0` and `==0.79.0`) so the source you clone and the version you enforce
cannot drift apart.

### Only downloaded libraries are overwritten

A library listed in `libraries_to_download` is the only kind the engine will
**overwrite** to satisfy a pin. A library that is merely registered (listed in
`libraries_to_register` by path) is loaded as-is and is **never** overwritten by
project activation. If you want a project to be able to enforce a library
version, the library must be in the download list, not just the register list.

You do not need to also add the library to `libraries_to_register`: after a
successful download the engine appends the resolved manifest path to the
register list automatically, so the download-then-load chain works on its own.

### Where the downloads land

`libraries_to_download` says **what** to install and at which version;
[`libraries_dir`](projects.md#libraries-directory) (a field in the project YAML)
says **where** those libraries install and resolve. The two compose: each pinned
download is provisioned into the project's resolved libraries directory. When a
project declares no `libraries_dir` (and inherits none from a parent), downloads
land in the workspace-relative `libraries` directory, preserving today's behavior.
A shared `libraries_dir` across a project tree means a pinned library downloaded
once by the parent is reused by every child rather than re-downloaded.

## What happens on activation

When a user activates a pinned project, the engine compares each
`libraries_to_download` entry against what is installed and plans one of:

| Plan          | When                                             | Effect                                                                      |
| ------------- | ------------------------------------------------ | --------------------------------------------------------------------------- |
| **SKIP**      | The installed version already satisfies the pin  | Nothing changes.                                                            |
| **INSTALL**   | The library is not installed yet                 | Clones the pinned source. Non-destructive.                                  |
| **OVERWRITE** | A different, non-satisfying version is installed | Deletes the local library directory and re-clones the pin. **Destructive.** |

Before any destructive OVERWRITE runs, the editor shows a read-only **preview**
of the full plan and waits for the user to approve it. A denial is a clean
no-op: the prior project stays active and no library files change. The preview
also reports an engine-version mismatch (`requires_engine`) and blocks approval
when the running engine cannot satisfy the pin.

## Worked example

A project that requires engine `>=0.80,<1.0` and pins the standard library to
`0.79.0`:

`/MyProject/griptape-nodes-project.yml`

```yaml
"project_template_schema_version": "1.0.0"
"name": "my-pinned-project"
"description": "Runs on engine 0.80-0.x with the standard library pinned to 0.79.0."
```

`/MyProject/griptape_nodes_config.json`

```json
{
  "app_events": {
    "on_app_initialization_complete": {
      "requires_engine": ">=0.80,<1.0",
      "libraries_to_download": [
        {
          "name": "Griptape Nodes Library",
          "version": "==0.79.0",
          "git_url": "griptape-ai/griptape-nodes-library-standard@v0.79.0"
        }
      ]
    }
  }
}
```

Activation behavior:

1. **Engine check** — if the running engine is outside `>=0.80,<1.0`, activation
    is blocked with a version-mismatch message.
1. **First activation (clean machine)** — the standard library is absent, so the
    plan is INSTALL: `v0.79.0` is cloned and registered.
1. **Re-activation, `0.79.0` already present** — the pin is satisfied, so the
    plan is SKIP.
1. **A different version is installed** (say a prior project left `0.78.0`) —
    `0.78.0` does not satisfy `==0.79.0`, so the plan is the destructive
    OVERWRITE: the preview modal shows it, and on approval the local library
    directory is deleted and `v0.79.0` is re-cloned.

## Notes and gotchas

- **Bare strings still work.** An existing `"libraries_to_download": ["user/repo"]`
    list keeps cloning from source with no version enforcement. Only the object
    form enforces a `version`.
- **Per-user overrides win.** A user's workspace config layers above the
    project-adjacent config (see [Workspace](workspace.md#config-resolution-order)).
    A user can override your pins locally; the pins are defaults distributed with
    the project, not a lock.
- **CLI alternative.** Administrators automating headless engines can clone a
    library with `griptape-nodes libraries download <git_url>` and update with
    `griptape-nodes libraries sync`. See [Libraries](../libraries.md#cli-alternatives)
    and the [Command Line Interface](../../reference/command_line_interface.md) reference. The
    declarative config above is the portable way to ship the same pins with a
    project.
