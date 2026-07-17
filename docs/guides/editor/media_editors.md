# Media Viewers and Editors

Whenever a parameter carries an image, video, audio clip, 3D model, or
Gaussian splat, the editor swaps in a viewer built for that media type
instead of a plain text field. Some of these viewers are also editors —
you can paint a mask, crop a frame, or composite layers without leaving
the canvas. This page is a tour of each one: where it appears, how you
open it, and what you can do once it's open.

Which viewer you get, and which of the extra buttons appear on top of
it, depends on options a node author set on that parameter. You don't
configure this yourself — it's baked into the node — but it explains
why the same image parameter might show a crop icon on one node and a
mask icon on another.

## Viewing an image and its right-click actions

Image parameters display through the standard image viewer: a
thumbnail with the image's name, dimensions, and file size underneath
it. Hovering reveals an expand button that opens the image full-screen
in a lightbox; press **Escape** or click outside it to close.

Right-click any image — on its thumbnail in a node, or on the enlarged
version in the lightbox — to get a context menu with:

- **Copy image** — copies the full-resolution image to your system
    clipboard as PNG (re-encoding it first if the source is a different
    format).
- **Copy image URL** — copies the image's URL as text.
- **Save image** — downloads the full-resolution image to disk.
- **Make thumbnail** — sets this image as the workflow's header
    thumbnail, the one shown in the workflow browser. This updates the
    workflow's saved metadata immediately.

<!-- screenshot: right-click context menu open on an image node, showing Copy image / Copy image URL / Save image / Make thumbnail -->

## Comparing two images with a slider

When a node exposes a pair of images through a single comparison
parameter, the editor renders them as one image with a draggable
vertical divider: everything left of the divider is the first image,
everything right of it is the second. Move your mouse across the
comparison area to slide the divider.

Hover to reveal an expand button that opens the same comparison
full-screen, with each side labeled by its filename. Escape closes it.

<!-- screenshot: image compare slider mid-drag, divider partway across the frame -->

## Cropping an image

Some image parameters carry a crop button (a crop icon over the
thumbnail) that opens the crop editor. Inside, drag the handles on the
overlay to resize the crop region, or use:

- **Reset All** — resets the crop to the full image.
- **Aspect ratio presets** — Square, 16:9, 3:2, 4:3, 9:16, 2:3, 3:4.
- **Zoom** — 10–300%, previewed as a pulsing dashed overlay.
- **Rotation** — -180° to 180°, with an arrow indicator showing the
    up direction.
- **Crop Details** — the current left/top/width/height in pixels.

**Accept & Save** writes the crop as parameter values on the node
(left, top, width, height, zoom, rotation) rather than baking it into
a new image file — the node itself is responsible for applying the
crop when it runs. **Cancel** discards your changes.

<!-- screenshot: crop modal with drag handles on an image and the aspect-ratio preset buttons visible in the sidebar -->

## Painting a mask

Image parameters with mask editing enabled show a mask icon; clicking
it opens the Paint Mask editor. You paint directly onto a canvas laid
over the source image:

- **Brush** paints white (reveal); **Erase** paints black (hide).
- **Brush Size** and **Blur** sliders control stroke width and soft
    edges. `[` and `]` also adjust brush size without touching the
    sliders.
- **Show Mask / Show Composite** toggles between the raw grayscale
    mask and the image with the mask applied as transparency.
- **Invert Mask** swaps black and white across the whole mask.
- **Reset Mask** restores the mask to the image's original alpha
    channel.
- **Flood Fill** fills the entire mask with the current tool's color.
- Hold **Alt** and drag to pan, or **Alt**+scroll to zoom; dedicated
    zoom buttons and a reset-view button sit above the canvas.

What **Apply** saves depends on what the parameter already holds: if
it's a plain image, painting edits that image's alpha channel and
saves a new image with transparency; if the parameter is already
paired with a separate mask, painting edits that mask instead and
saves it as its own grayscale file, keeping the source image
untouched.

<!-- screenshot: Paint Mask editor with a brush stroke mid-paint and the Invert/Reset/Flood Fill buttons visible in the sidebar -->

## Image Bash: compositing images and paint layers

Image Bash is the editor's layered compositing tool. Some image
parameters carry a pencil icon that opens it directly; it's also
reachable from parameters that store their state as structured layer
data, via an **Edit** button that opens the same tool. Griptape's own
nodes that pass images through Image Bash use it to build up a shot
from multiple source images and painted layers, rather than to edit
one image in place.

Inside, each layer is either an **image layer** (one of your source
images, which you can move, scale, and rotate with the on-canvas
transform handles) or a **brush layer** (a paintable canvas you draw
into). The sidebar lists every layer with a thumbnail, a visibility
toggle, opacity, and drag-to-reorder handles; you can rename, delete,
or duplicate any layer, and duplicate an image layer as a new
paintable brush layer.

Painting tools include four brush types — Pen, Soft, Crayon, and
Spray — each with its own size, color, and opacity, plus extra
controls for Crayon (fleck size, fleck density, spread, square ratio,
density falloff) and Spray (spot size, spot density, spread radius).
Undo/redo covers your last 50 brush strokes. You can also resize the
canvas and change its background color from within the tool.

Default brush and canvas settings live in the editor's Settings panel
— search for "ImageBash" to find the card. Changing a default there
only affects new sessions of the tool going forward.

<!-- screenshot: Image Bash open with several image layers and a brush layer in the sidebar, transform handles visible on the selected layer -->

## Playing and comparing video

Video parameters use a frame-accurate player: step one frame at a
time, play forward or backward, and jump between marked frames. Keys
work the same as the on-screen controls: `←`/`→` step a frame,
`j`/`k`/`l` play backward, pause, and play forward. If the node also
exposes a frame-selection parameter, the timeline shows markers you
can add (`.` adds the current frame), drag, or clear, and `n`/`m` jump
to the previous/next marker.

The bottom bar shows playback FPS (editable, with a button to snap
back to the video's native rate), the current frame out of the total,
mute, and volume. An expandable **Video Details** panel reports
dimensions, file size, format, codec, aspect ratio, duration, and
frame rate. **Enlarge** opens the player in a larger dialog; the
**Fullscreen** button uses your browser's fullscreen mode.

Video comparison parameters show two videos side by side with the
same draggable divider as image comparison, kept in sync frame-for-frame,
with a switch to choose which side's audio track plays.

Right-click a video for **Copy video URL** and **Save video**.

<!-- screenshot: video player with the frame timeline and markers visible, one marker being dragged -->

## Playing audio

Audio parameters render as a waveform with playback controls
underneath: play/pause, elapsed/total time, mute, and a volume slider.
Click and drag anywhere on the waveform to scrub to that position.

## Capturing from a webcam or microphone

Image parameters with webcam capture enabled show a live camera
preview in place of the usual thumbnail once you grant camera access;
a camera button captures a still and uploads it, replacing the
preview with the captured image. A capture button lets you retake it.

Audio parameters with microphone capture enabled show a record button
next to the waveform. Click it to start recording — it turns into a
running timer — and click again to stop; the recording is encoded and
uploaded automatically. A trash icon clears a captured recording so
you can record again.

Both require you to grant the browser camera or microphone permission;
if access is denied, the component shows an error message instead of
the capture controls.

## Viewing 3D models

3D model parameters open in an interactive viewer: drag to orbit,
scroll to zoom, and right-click drag to pan. Supported formats are
glTF/GLB (including Draco-compressed meshes), OBJ, FBX, DAE, 3DS, STL,
and PLY; USDZ files load only if they're the plain-text USDA variant —
binary USDC files exported by tools like Rodin or Apple QuickLook
won't preview, though the file itself is still saved and downloadable.
Formats that don't carry their own materials (STL, PLY, plain OBJ) get
a neutral default material so they still read as solid geometry.

Hover the viewer and click the expand icon to open the same model
full-screen, with slow auto-rotation while it's open. Escape or the
close button exits.

<!-- screenshot: 3D model viewer with an orbited glTF model and the expand icon visible on hover -->

## Viewing Gaussian splats

Splat parameters (`.splat` and `.ksplat` files) use the same
orbit/zoom camera controls as the 3D model viewer, rendered through a
Gaussian splatting renderer instead of a triangle mesh. Click the
expand icon to open the splat full-screen; Escape or the close button
exits.

## Loading a workflow from an image

Griptape Nodes can embed a workflow's node graph inside the PNG it
exports as a thumbnail. Drop one of those PNG files onto the canvas, and
if
the editor detects embedded workflow metadata, it asks what you want
to do with it instead of just adding an image node:

- **Add Image** — treat it like any other image and create a regular
    image-loading node from it.
- **Add Nodes from Workflow** — add the nodes that generated the image
    to your current workflow, importing the referenced libraries,
    static files, and any workflows it depends on. Expand the node,
    library, and dependency lists in the dialog to preview what will be
    added before you commit.

Drop several PNG files at once and you get a batch version of the same
dialog: it tells you how many of the dropped images carry workflow
metadata and applies your choice (add as images, or add nodes from
workflow) to all of them at once. Images without embedded metadata are
always added as plain image nodes, regardless of which option you
pick.

<!-- screenshot: the "Workflow found in image" dialog with "Add Nodes from Workflow" selected and the node/library lists expanded -->
