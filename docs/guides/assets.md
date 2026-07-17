# Assets and Outputs

When a node in your workflow generates an image, video, audio clip, or any
other file, that file has to land somewhere on disk and be reachable by the
editor so you can preview and download it. This page explains where those
files go by default, how to change that, and how files get into a workflow
in the first place (uploads, drag-and-drop).

## Where generated files go

Every workflow runs inside a **workspace** — the root folder Griptape Nodes
resolves relative paths against. Nodes don't write to arbitrary locations;
they save through a small set of named scenarios (the engine calls them
**situations**) that each point at a directory under the workspace:

- A node's rendered output (an image, a rendered video, generated audio)
    goes into the `outputs` directory.
- A file you drag into the editor or copy from outside the project goes
    into the `inputs` directory.
- Scratch files a node writes during processing and cleans up afterward go
    into `temp`.
- Thumbnails and preview images the editor generates for you go into
    hidden `.griptape-nodes-previews` / `.griptape-nodes-thumbnails`
    folders.

You rarely need to think about this mapping directly — a node that says
"save my output" just works — but if you're wondering "where did the file
this node just made actually go," it's one of these directories, under
your workspace. See [Directories](projects/directories.md) for the full
list and how to point any of them somewhere else (a shared drive, a
different subfolder, per-platform paths), and
[Situations](projects/situations.md) for the exact save rule each kind of
file follows, including what happens when a file with that name already
exists.

<!-- screenshot: the workspace folder in a file manager, with inputs/outputs/staticfiles visible -->

## The static files directory

Alongside `outputs` and `inputs` sits a `staticfiles` folder (the exact
name is configurable — see below). This is where the engine puts files it
needs to serve back to the editor over HTTP, rather than files a node
saved for your own use. Node preview thumbnails in the UI, files a node's
output parameter exposes for download, and other editor-facing artifacts
generally flow through here.

Concretely: when the engine needs to show you a file or let you download
it, it doesn't hand the browser a raw filesystem path — it creates a URL.
For files already living in `staticfiles`, that's usually a direct URL to
the local static file server (`http://localhost:8124/workspace/...` by
default). For anything else — an arbitrary path elsewhere in your
workspace, a `file://` path, or a macro path like `{outputs}/render.png` —
the engine mints a short-lived, presigned download URL on request. Either
way, the mechanism is the same: the editor asks for a URL, the engine
resolves it against wherever the file actually lives, and the browser uses
that URL to fetch or download the file. You don't manage these URLs
yourself; they're generated on demand and are not meant to be
long-lived or shared outside your session.

## Storage backends: local vs. Griptape Cloud

Where the underlying bytes actually live is controlled by the
`storage_backend` setting, which you set during `gtn init` or later in
your configuration:

- **`local`** (the default) — static files live on your local filesystem,
    under `<workspace_directory>/staticfiles`, served by a small local web
    server the engine starts alongside itself.
- **`gtc`** — static files live in a Griptape Cloud bucket instead of on
    your disk. Use this when you want assets to sync across machines, share
    a workflow's generated files with collaborators, or avoid filling up
    local disk with generated media. This requires a Griptape API key and a
    bucket (either set up during `gtn init`, or created later — see the
    [Griptape Cloud bucket configuration](../reference/command_line_interface.md#init)
    prompts in `gtn init`).

If you configure `gtc` but the bucket credentials aren't available when
the engine starts, it logs a warning and falls back to local storage
rather than failing outright — so a half-finished cloud setup won't
silently swallow your outputs.

Switching backends only changes where *new* static files are written; it
doesn't move files you already generated under the other backend.

See [Configuration Reference](../reference/configuration_reference.md#storage)
for the exact `storage_backend`, `static_files_directory`, and
`workspace_directory` settings, and
[Command Line Interface](../reference/command_line_interface.md#init) for
setting them from `gtn init`.

## Uploading files into a workflow

Getting an external file (a reference photo, a source video, an audio
clip) into a workflow works the same way in reverse: you drag a file onto
a node, or use a node's file picker, and the editor uploads it into the
project. The upload lands in the `inputs` directory by default, named
after the node and parameter that requested it so you can tell where it
came from later.

<!-- screenshot: dragging a file onto a node's file parameter, showing the drop target highlighted -->

Under the hood this is a two-step handoff rather than the browser talking
to your filesystem directly: the editor asks the engine for an upload URL,
then PUTs the file's bytes to that URL. This works the same way regardless
of storage backend — locally it's a PUT to the local static server;
against `gtc` it's a PUT to a presigned Griptape Cloud URL — so switching
backends doesn't change how uploading feels in the editor.

## Related pages

- [Directories](projects/directories.md) — the full list of named
    directories (`inputs`, `outputs`, `temp`, and more) and how to
    customize their paths.
- [Situations](projects/situations.md) — the save rules (where a file
    goes, what happens on a name collision) behind every kind of file a
    node writes.
- [Sequences](projects/sequences.md) — reading a directory of numbered
    output files (`render.0001.exr`, `render.0002.exr`, …) back in as a
    single ordered set.
- [Macros](projects/macros.md) — the path-template syntax (`{outputs}/{file_name_base}.{file_extension}`)
    that situations use to build file paths, if you want to customize where
    something is saved.
- [Configuration Reference](../reference/configuration_reference.md) — the
    exact settings (`storage_backend`, `static_files_directory`,
    `workspace_directory`, and friends) and their defaults and environment
    variable overrides.
