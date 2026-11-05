# Offline and Proxy Installs

This page is for running Griptape Nodes on a network that reaches the internet only through an
HTTP(S) proxy, inspects TLS with its own certificate authority, or doesn't reach the internet at
all.

Griptape Nodes has no proxy or offline settings of its own. It works behind a proxy through the
standard environment variables that its tools already read: Python's HTTP clients, `uv` (which
installs Python packages), `git` (which downloads libraries), and the Hugging Face client (which
downloads models). There is no fully disconnected mode: signing in or activating a license always
needs a route to Griptape Cloud, directly or through the [Admin Server](enterprise/admin_server.md).

## What needs the network

| What                                                 | Reaches                                                    | When                                                                        |
| ---------------------------------------------------- | ---------------------------------------------------------- | --------------------------------------------------------------------------- |
| Sign-in, licensing, Griptape Cloud storage           | `cloud.griptape.ai`, or your Admin Server                  | Startup, then periodically                                                  |
| Engine events                                        | `api.nodes.griptape.ai` (WebSocket)                        | While the engine runs                                                       |
| Node libraries                                       | GitHub, or wherever the library's Git URL points           | First run (the standard library), and when you install or update a library  |
| Library Python dependencies                          | PyPI, or the library's own package index                   | When a library is registered                                                |
| Python interpreters for library environments         | GitHub (python-build-standalone, downloaded by `uv`)       | When a library environment is first created and no matching Python is found |
| Models                                               | Hugging Face                                               | When you download a model                                                   |
| Desktop app updates                                  | Griptape's update feed                                     | Unless [disabled by policy](enterprise/disabling_app_updates.md)            |
| Manual engine install (`install.sh` / `install.ps1`) | `astral.sh` (for `uv`), PyPI, and GitHub (for Python 3.12) | Install only                                                                |

The Desktop app bundles Python, the engine, the editor, and `git`. It doesn't bundle node
libraries, their dependencies, or models.

## Running behind a proxy

Set the standard proxy variables in the environment Griptape Nodes starts from:

```shell
HTTPS_PROXY=http://proxy.example.com:8080
HTTP_PROXY=http://proxy.example.com:8080
NO_PROXY=localhost,127.0.0.1
```

Keep `localhost` and `127.0.0.1` in `NO_PROXY`: the editor talks to the engine on your own machine.

The Desktop app passes its own environment through to the engine, but it doesn't read variables
from your shell profile. Set them where the app's process inherits them: as user or system
environment variables on Windows, with `launchctl setenv` on macOS (then restart the app), or in the
environment of your desktop session on Linux.

`git` reads the same variables. To give it its own proxy instead, use
`git config --global http.proxy http://proxy.example.com:8080`.

## TLS inspection and corporate certificates

If your proxy re-signs HTTPS traffic with your organization's certificate authority, each tool has
to trust that CA:

- **`uv` (library dependencies).** The Desktop app already tells `uv` to use the operating system's
    certificate store, so a CA your OS trusts works. For a manual engine install, set
    `UV_SYSTEM_CERTS=1` yourself.
- **`git` (library downloads).** On macOS and Windows, `git` uses the OS certificate store. On
    Linux, the Desktop app's bundled `git` uses its own CA bundle; point `GIT_SSL_CAINFO` at a PEM
    file that includes your CA.
- **The engine's own HTTPS and WebSocket connections** verify against the operating system's
    certificate store. Install your CA there.

A certificate error that mentions `UnknownIssuer` or `CERTIFICATE_VERIFY_FAILED` usually means one
of these is missing.

## Using an internal package index

Library dependencies are installed with `uv`, which reads its own environment variables. To install
from an internal mirror of PyPI instead of PyPI itself:

```shell
UV_DEFAULT_INDEX=https://pypi.internal.example.com/simple
```

To have `uv` fetch Python interpreters from an internal mirror of python-build-standalone, set
`UV_PYTHON_INSTALL_MIRROR`. See the
[uv environment variable reference](https://docs.astral.sh/uv/reference/environment/) for the full
list.

A library can also declare its own index in its manifest (`pip_install_flags`); that's set by the
library's author.

## Without internet access

Everything a workflow needs at run time can be made local, but it has to be put there first, from a
machine that has access:

- **Libraries.** Clone each library onto the machine (or onto an internal Git server) and register
    it from disk: in **Settings → Libraries**, the **Add Library** button under **Libraries To
    Register** accepts a `griptape_nodes_library.json` you already have.
    An internal Git URL also works with **Add Library** in the **Libraries** panel.
- **Library dependencies.** Point `uv` at an internal index (above), or install each library's
    dependencies once while connected. Set `UV_OFFLINE=1` to stop `uv` from trying the network
    afterwards; an install that needs a package it doesn't have then fails instead of hanging.
- **Models.** Download models while connected, or copy the Hugging Face cache onto the machine. Set
    `HF_HUB_OFFLINE=1` to keep the Hugging Face client from contacting the Hub, or `HF_ENDPOINT` to
    use an internal mirror.
- **Desktop app updates.** Turn them off with a [policy file](enterprise/disabling_app_updates.md)
    and distribute new versions yourself.

`library.dependency_install_behavior = "never"` doesn't make library registration offline. It stops
the engine from downloading other libraries that a library depends on; Python dependencies are
still installed with `uv`.

Licensing still needs Griptape Cloud. On a network with no direct internet access, run the
[Admin Server](enterprise/admin_server.md) as the single host that reaches it, and see
[Architecture](architecture.md) for exactly what crosses your network boundary.
