# Load EXR

**Loads an EXR file and exposes its full structure — parts, channels, and header attributes — as typed outputs, without reading pixels by default.**

Category: `EXR`

## TL;DR

- Header-only by default (`openexr.header_only` setting), so scanning is fast even on large multi-part renders.
- Populates dynamic **Parts** and **Channels** groups: one `EXRPartArtifact` per part and one `EXRChannelArtifact` per raw channel.
- Wire parts into [Display EXR Part](display_exr_part.md) and channels into [Display EXR Channel](display_exr_channel.md) or [Save EXR](save_exr.md).
- An **Open in external viewer** button opens the file in a configured HDR viewer (see [Library settings](index.md#library-settings)).

## Typical workflow position

```text
[Load EXR] → Display EXR Part → (image output)
           ↘ Display EXR Channel / Save EXR
```

## Node preview

<!-- TODO: add ../assets/load-exr.png screenshot -->

## Inputs

| Name        | Type  | Required | Notes                    |
| ----------- | ----- | -------- | ------------------------ |
| `file_path` | `str` | Yes      | Path to the `.exr` file. |

## Outputs

| Name                             | Type                    | Notes                                                       |
| -------------------------------- | ----------------------- | ----------------------------------------------------------- |
| `image_width` / `image_height`   | `int`                   | Dimensions from the data window.                            |
| `part_count` / `channel_count`   | `int`                   | Counts.                                                     |
| `compression`                    | `str`                   | e.g. `ZIP_COMPRESSION`, `DWAB_COMPRESSION`.                 |
| `storage_type`                   | `str`                   | `scanlineimage`, `tiledimage`, `deepscanline`, `deeptiled`. |
| `pixel_aspect_ratio`             | `float`                 | Pixel width/height ratio.                                   |
| `data_window` / `display_window` | `str`                   | `"xmin,ymin - xmax,ymax"`.                                  |
| `time_code`                      | `str`                   | `HH:MM:SS:FF`, empty if absent.                             |
| `software`                       | `str`                   | Authoring application, empty if absent.                     |
| `owner`                          | `str`                   | Asset owner, empty if absent.                               |
| `chromaticities`                 | `str`                   | JSON with `red_x/y`, `green_x/y`, `blue_x/y`, `white_x/y`.  |
| `custom_attributes`              | `str`                   | JSON of all non-standard header attributes.                 |
| `parts`                          | `list[EXRPartArtifact]` | Structured descriptor for every part in the file.           |

Dynamic groups are also populated after the scan:

- **Parts** — one `EXRPartArtifact` output per part, each with its own channel
    outputs (hidden for single-part files; channels appear directly in the
    Channels group instead).
- **Channels** — one `EXRChannelArtifact` per raw channel with name, pixel
    type, and sampling (single-part files only).

## Tips & pitfalls

- **Pixel types may read as approximate in header-only mode.** Set the `openexr.header_only` setting to `false` if you need accurate per-channel pixel types; the cost is loading pixel data into memory at scan time.
- **Multi-part files hide the flat Channels group.** For multi-part renders, per-channel outputs live inside each part's group instead.
- **Downstream nodes load pixels lazily.** The display and save nodes read pixel data themselves from the descriptor artifacts, so a header-only scan doesn't limit what you can do downstream.

## See also

- [Display EXR Part](display_exr_part.md) · [Display EXR Channel](display_exr_channel.md) — render parts/channels to viewable images.
- [Save EXR](save_exr.md) — write channels back out.
