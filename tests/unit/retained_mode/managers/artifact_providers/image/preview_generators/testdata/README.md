# Thumbnail contract fixtures

Declarative thumbnail tests: procedural inputs and expected encoded outputs.
Pydantic validates each YAML file using [`image_specifications.py`](../image_fixtures/image_specifications.py).
Unknown fields are rejected. Optional fields can be omitted; unlisted expectations
are unchecked. `&name` defines a reusable YAML value; `*name` references that value.

## Example

```yaml
schema_version: 1                     # Required schema version.
images:
  - id: translucent_red               # Unique image id: letters, digits, '_' and '-'.
    description: Check alpha resizing # Optional explanation of the behaviour.
    source:
      dimensions: {width: 64, height: 32} # Positive data-window dimensions in pixels.
      # EXR sources may also set data_origin and display_window.
      # data_origin: {x: -8, y: 4}
      # display_window: {x: 0, y: 0, width: 128, height: 64}
      channels: [R, G, B, A]           # Ordered, unique names; also supports Y and extras.
      colour_space: srgb              # Optional intent only; does not write a colour tag.
      alpha_association: straight     # Optional: straight or premultiplied; requires A.
      metadata:                       # Optional encoded attributes: string/int/float values.
        gtn_workflow_name: test_workflow
      pixels:
        pattern: solid                # Pattern-specific fields are listed below.
        colour: [1, 0, 0, 0.5]         # One value per channel; integer sources use [0, 1].
    variants:
      - id: png8                      # Unique input variant id within this image.
        format: png                   # Input format; allowed depths are listed below.
        sample_type: uint8            # Stored sample type, verified after encoding.
        metadata: {artist_note: keep} # Optional additions/overrides to source metadata.
        encoding: {CompressionLevel: 6} # Optional format-specific OIIO writer attributes.
        expect:                       # Optional read-back checks on the generated input.
          metadata:
            equals: {gtn_workflow_name: test_workflow}
    cases:
      - id: resize                    # Unique case id within this image.
        operation: generate_thumbnail # Currently the only operation.
        inputs: [png8]                # Nonempty list of declared variant ids to test.
        parameters:                   # Optional; each bound defaults to 1024.
          max_width: 16               # Integer from 1 to 8192.
          max_height: 16              # Integer from 1 to 8192.
        inject_metadata: false        # Optional; enable engine publication metadata.
        preserve_source_metadata: false # Optional; retain PNG source text in PNG output.
        workflow_name: test_workflow  # Optional bound workflow; see metadata case below.
        outputs:
          - format: png               # Output: png, jpg, jpeg or webp.
            expect:                   # Required; at least one assertion. Also used above.
              dimensions: {width: 16, height: 8} # Optional literal expected dimensions.
              sample_type: uint8      # Optional expected stored depth/type.
              channels: [R, G, B, A]   # Optional ordered decoded channel names.
              metadata:               # Optional checks; equality includes value type.
                equals: {gtn_workflow_name: test_workflow}
                present: [artist_note] # Keys that must exist, regardless of value.
                absent: [source_capture_id] # Keys that must be missing.
              pixels:
                checks:               # Nonempty list; every check is evaluated.
                  - kind: all_pixels   # all_pixels, sample or region; see examples below.
                    colour: [1, 0, 0, 0.5] # Expected values in decoded channel order.
                    max_absolute_error: 0.003921668627450981 # Required finite tolerance >= 0.
```

## Source pixel patterns

Use one mapping under `source.pixels`:

```yaml
{pattern: gradient, axis: x, start: [0, 0, 0], end: [1, 1, 1]}
# The colours are at the first and last pixels along the axis (x or y).

{pattern: colour_bars, axis: x, colours: [[1, 0, 0], [0, 0, 1]]}
# Equal bands along the axis, in spatial order.

{pattern: checkerboard, cell_dimensions: {width: 8, height: 8},
 colours: [[0, 0, 0], [1, 1, 1]]}
# Positive cell dimensions and exactly two alternating colours, first at top-left.

{pattern: quadrants, colours: [[1, 0, 0], [0, 0, 1], [0, 1, 0], [1, 1, 1]]}
# Four colours: top-left, top-right, bottom-left, bottom-right.
```

Floating-point sources allow HDR values outside [0, 1]. Every colour matches channels.
Integer source values use round-half-up quantisation. For odd quadrant dimensions,
the right and bottom halves receive the extra pixel. Colour bars select the band with
`floor(coordinate * colour_count / axis_length)`.
Pixel checks read straight alpha for PNG/TIFF. EXR can model premultiplied values.
Decoded grayscale channel R is normalised to the declared Y for comparisons.

## Pixel and window checks

Both `colour` and `max_absolute_error` are required:

```yaml
{kind: sample, position: {x: 4, y: 2}, colour: [1, 0, 0], max_absolute_error: 0}
{kind: region, region: {x: 0, y: 0, width: 4, height: 2},
 colour: [1, 0, 0], max_absolute_error: 0}
```

Coordinates are zero-based from the stored data window's top-left, regardless of origin.
Region width and height must be positive. `expect.data_window` and `expect.display_window`
accept `{x, y, width, height}` to check native signed origins and positive dimensions.

## Input formats

- PNG: `uint1`, `uint8`, `uint16`. Supports one to four channels. One-bit input requires solid grayscale.
- JPEG: `uint8`. Supports grayscale or RGB.
- WebP: `uint8`. Supports RGB or RGBA.
- TIFF: `uint8`, `uint16`, `float32`. Supports extra channels.
- EXR: `float16`, `float32`. Supports extra channels. The origin defaults to zero and the display window defaults to the data window.
- HDR: `float32`. Requires RGB.
- DPX: `uint8`, `uint16`. Requires RGB.

## JPEG encoding expectations

JPEG outputs can specify `expect.encoding`: `quantization` holds the exact 64-entry brightness
and colour tables, `sampling` uses Pillow's codes (0=4:4:4, 1=4:2:2, 2=4:2:0), and
`progressive` checks the encoding mode. `variant.encoding` configures the input writer.
See [`thumbnail_square_bounds_and_encoding.yaml`](thumbnail_square_bounds_and_encoding.yaml)
for a complete example. YAML aliases refer to anchors in the same file.

## Adding a test

1. Define or reuse an image and encoded variant. Keep expectations close to the source.
1. Add a case selecting input IDs, bounds, output formats and explicit expected results. If you add
    a file, list it in `load_thumbnail_specifications()`.
1. Justify pixel values and tolerances independently of the production thumbnail generator.
1. Run [`test_openimageio_thumbnail_contracts.py`](../test_openimageio_thumbnail_contracts.py)
    with `pytest -k 'image/variant/case/format'`.

Inputs are generated lazily once per worker in pytest temporary directories.
Collection generates no files. Stored dimensions, depth and channel count are verified.
Failure injection, logging and internal call-order assertions remain in Python.
Provenance and coverage notes live beside the relevant cases.
