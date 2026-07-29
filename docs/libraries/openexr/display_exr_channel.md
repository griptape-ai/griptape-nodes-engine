# Display EXR Channel

**Combines 1–4 individual EXR channels into an 8-bit sRGB or RGBA PNG, with optional alpha compositing over a background image.**

Category: `EXR`

## TL;DR

- Each slot (R, G, B, A) accepts an `EXRChannelArtifact` and is optional — channels can come from different EXR files or different parts.
- Missing RGB slots are zero-filled; connecting the A slot produces RGBA output with optional background compositing.
- At least one RGB slot must be connected, and all connected channels must share the same pixel dimensions.
- Same color management as [Display EXR Part](display_exr_part.md): `basic` tone mapping or `ocio` via a connected `OCIOColorParamsArtifact`.

## Typical workflow position

```text
Load EXR (channels) → [Display EXR Channel] → (image output)
```

## Node preview

<!-- TODO: add ../assets/display-exr-channel.png screenshot -->

## Inputs

| Name           | Type                                       | Required | Notes                                                                                                        |
| -------------- | ------------------------------------------ | -------- | ------------------------------------------------------------------------------------------------------------ |
| `channel_r`    | `EXRChannelArtifact`                       | No       | Channel mapped to the red plane.                                                                             |
| `channel_g`    | `EXRChannelArtifact`                       | No       | Channel mapped to the green plane.                                                                           |
| `channel_b`    | `EXRChannelArtifact`                       | No       | Channel mapped to the blue plane.                                                                            |
| `channel_a`    | `EXRChannelArtifact`                       | No       | Channel used as alpha. When connected, output is RGBA.                                                       |
| `exposure`     | `float`                                    | No       | Exposure in EV stops applied before tone mapping or the OCIO transform. Range −10 to +10, default `0.0`.     |
| `color_params` | `OCIOColorParamsArtifact`                  | No       | From [OCIO Color Parameters](../opencolorio/ocio_color_parameters.md). Required when `color_mode` is `ocio`. |
| `background`   | `ImageArtifact \| ImageUrlArtifact \| str` | No       | Background image for A-over-B compositing. Ignored when no alpha channel is connected.                       |

## Outputs

| Name          | Type               | Notes                                         |
| ------------- | ------------------ | --------------------------------------------- |
| `image`       | `ImageUrlArtifact` | 8-bit sRGB or RGBA PNG for in-canvas display. |
| `output_file` | `str`              | Path to the saved PNG file.                   |

## Parameters

| Name           | Type               | Default                                                      | Notes                                                                                                                                         |
| -------------- | ------------------ | ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `color_mode`   | `basic \| ocio`    | `ocio` if the OpenColorIO Library is installed, else `basic` | `basic` uses local tone mapping; `ocio` uses the connected `OCIOColorParamsArtifact`.                                                         |
| `tone_mapping` | `filmic \| linear` | `filmic`                                                     | Local tone mapping when `color_mode` is `basic`. `filmic` applies the Narkowicz 2015 curve; `linear` clamps to [0, 1]. Hidden in `ocio` mode. |

## Tips & pitfalls

- **Connect at least one RGB slot.** The node fails with only an alpha channel connected.
- **Dimensions must match across all connected channels.** Mixing channels from different resolutions fails — rescale upstream first.
- **Great for inspecting AOVs.** Wire a single AOV channel (e.g. `depth` or `normal.x`) into `channel_r` to visualize it in grayscale-red, or assemble `normal.x/y/z` into RGB.
- **Background compositing needs alpha.** The `background` input is silently ignored unless `channel_a` is connected.

## See also

- [Load EXR](load_exr.md) — provides the channel inputs.
- [Display EXR Part](display_exr_part.md) — render a whole part with automatic channel selection instead.
- [Save EXR](save_exr.md) — write assembled channels back to EXR.
