# Assets and Outputs

When a node in your workflow generates an image, video, audio clip, or any
other file, that file has to land somewhere on disk and be reachable by the
editor so you can preview and download it. This page explains where those
files go by default, how to change that, and how files get into a workflow
in the first place (uploads, drag-and-drop).

## Where generated files go

Nodes don't write to hard-coded locations. Every save goes through your
**project**: the project defines a set of named directories (`inputs`,
`outputs`, `temp`, and more) and a set of named file-saving scenarios
called **situations** that decide which directory a given kind of file
lands in and what it gets called. With the default project, that works
out to:

- A node's rendered output (an image, a rendered video, generated audio)
    is saved through the [`save_node_output`](projects/situations.md#save_node_output)
    situation, into the project's `outputs` directory.
- A file you drag into the editor or copy from outside the project is
    saved through the [`copy_external_file`](projects/situations.md#copy_external_file)
    situation, into the `inputs` directory.
- Scratch files a node writes during processing and cleans up afterward go
    into `temp`.
- Thumbnails and preview images the editor generates for you go into
    hidden `.griptape-nodes-previews` / `.griptape-nodes-thumbnails`
    folders.

None of these paths are baked into the engine — they come from the
project, and a project file can point any of them somewhere else (a
shared drive, a different subfolder, per-platform paths) or change what
happens when a file with that name already exists. See
[Directories](projects/directories.md) for the full list of named
directories, [Situations](projects/situations.md) for the save rule each
kind of file follows, and [Project](projects/index.md) for how to
customize all of it.

<!-- screenshot (#5166): the default project's folder layout in a file manager, with inputs and outputs visible -->

## How the editor previews and downloads files

When the engine needs to show you a file or let you download it, it
doesn't hand the browser a raw filesystem path — it creates a URL. For
files inside your workspace, that's usually a direct URL to the local
static file server (`http://localhost:8124/workspace/...` by default).
For anything else — a path outside the workspace, a `file://` path, or a
macro path like `{outputs}/render.png` — the engine mints a short-lived,
presigned download URL on request. Either way, the mechanism is the same:
the editor asks for a URL, the engine resolves it against wherever the
file actually lives, and the browser uses that URL to fetch or download
the file. You don't manage these URLs yourself; they're generated on
demand and are not meant to be long-lived or shared outside your session.

You may also see a `staticfiles` folder in your workspace (the exact name
is set by the `static_files_directory` setting). It belongs to an older
save path that most nodes no longer use — they save through the project
system's directories described above instead — but files saved through
the engine's static-file API still land there.

## Uploading files into a workflow

Getting an external file (a reference photo, a source video, an audio
clip) into a workflow works the same way in reverse: you drag a file onto
a node, or use a node's file picker, and the editor uploads it into the
project. Where it lands is decided by the project's
[`copy_external_file`](projects/situations.md#copy_external_file)
situation. With the default project, that's the `inputs` directory,
grouped into a per-type subfolder (`images`, `videos`, `audio`, `text`)
when the file's extension is recognized; if a file with the same name is
already there, the new upload gets a numbered suffix (`photo_001.png`)
instead of overwriting it.

<!-- screenshot (#5166): dragging a file onto a node's file parameter, showing the drop target highlighted -->

Under the hood this is a two-step handoff rather than the browser talking
to your filesystem directly: the editor asks the engine for an upload URL,
then PUTs the file's bytes to that URL.

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
    exact settings (`static_files_directory`, `workspace_directory`, and
    friends) and their defaults and environment variable overrides.
