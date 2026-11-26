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

## Thumbnail precision

The engine selects its standard thumbnail backend at startup and logs the image provider variant
at INFO level when it first creates the provider.
It uses OpenImageIO when installed and Pillow otherwise; selection remains fixed until restart.
Install `griptape-nodes-engine[openimageio]` to enable OpenImageIO. App packaging is updated
separately, so the app's dependency declaration determines whether the extra is installed.

With OpenImageIO, the standard thumbnail generator preserves 16-bit source precision in PNG
previews. JPEG and WebP previews use 8-bit channels. A preview's format determines whether
it can retain the source's bit depth. PNG workflow metadata is added without re-encoding
the pixels, including for ordinary PNG saves. The original file is unchanged.

Standard thumbnails read source bytes through the engine's normal file-read operation.
OpenImageIO copies them into a private subdirectory of the project's `temp` directory for
decoding and encoding; Pillow decodes and encodes in memory. Source and preview scratch files are removed after rendering, and the preview bytes
are saved through the engine's normal file-write operation.

Standard thumbnails check the stored source dimensions before decoding pixels.
`max_thumbnail_source_pixels` defaults to `178956970`, matching the previous Pillow refusal
threshold. The setting can lower the limit for either backend. Pillow also enforces its own
pixel ceiling, so raising this setting above that ceiling requires OpenImageIO. Install
`griptape-nodes-engine[openimageio]` and restart the engine to select that backend. Increase
the setting only if your machine has enough memory.
The limit counts data-window pixels; it does not bound encoded source bytes, channel count,
bit depth or total processing memory. The source bytes are already buffered before this check.

PNG thumbnails include workflow metadata when `auto_inject_workflow_metadata` is enabled.
Source PNG text is discarded by default, matching the previous thumbnail behaviour.
Set `preserve_source_metadata_in_thumbnails` to `true` to keep source text, including artist
notes and source workflow fields. Retained text uses compression for long values within the
PNG text-size limit, and Unicode is preserved. Current workflow metadata replaces matching
keys; source-only fields remain, so a preview can contain both source and current provenance.
This setting applies only to PNG source text in PNG thumbnails and is independent of
`auto_inject_workflow_metadata`. Ordinary PNG saves retain their existing metadata policy. If the
metadata cannot be added, the thumbnail is still saved and the engine logs a warning.
Thumbnail writes replace the previous preview only after encoding and codec verification
succeed, so a failed write leaves the previous preview available.

Transparent images are resized with colour weighted by alpha, so hidden colours in fully
transparent pixels do not add dark or coloured fringes to visible edges.

Grayscale images also get WebP and JPEG previews. WebP retains grayscale transparency;
JPEG removes transparency after restoring straight colour, without compositing onto a new
background. PNG retains grayscale channels and transparency. For images
with more than four channels, PNG and WebP previews use the first four as red, green, blue
and alpha. JPEG previews use the first three as red, green and blue. Extra channels remain
in the original file.

### Standard thumbnail source formats

With OpenImageIO installed, `GeneratePreviewRequest` and `GeneratePreviewFromDefaultsRequest`
can generate standard thumbnails from formats such as OpenEXR, Radiance HDR and DPX when the
corresponding readers are installed. The built-in `Image` provider and direct calls to
`OpenImageIOThumbnailGenerator` use the same list of supported source extensions. Pillow standard
thumbnails continue to support PNG, JPEG, GIF, BMP, WebP, TIFF and TGA.

The OpenImageIO provider reads source metadata from the first image header. Preview metadata
reports a cropped EXR's display window dimensions and accounts for image orientation. It reports
OpenImageIO's colour-space tag, or `Unknown` when the tag is absent. Project directory routing
and Rounded Image Preview Generation keep their existing format support. Follow-up work is
tracked in [#6180](https://github.com/griptape-ai/griptape-nodes-engine/issues/6180).

For OpenImageIO standard thumbnails, EXR output shows the display window, preserving cropped-render
placement and proportions. Overscan is excluded, and missing pixels appear black or transparent
according to the source channels. Scene-linear sources such as EXR and HDR are converted to
sRGB using the built-in colour configuration. Unsupported colour spaces, such as camera-log
DPX, retain their pixel values and produce a warning. Original files are unchanged.

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
