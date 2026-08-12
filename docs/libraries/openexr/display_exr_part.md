# Display EXR Part

**Renders an EXR part to an 8-bit sRGB (or RGBA) PNG with exposure and tone mapping or OCIO color management.**

Category: `EXR`

## TL;DR

- Accepts an `EXRPartArtifact` from [Load EXR](load_exr.md), auto-selects RGB display channels, and writes an `ImageUrlArtifact` you can preview in the canvas.
- Two color modes: `basic` (local exposure + filmic/linear tone mapping) and `ocio` (a connected `OCIOColorParamsArtifact` drives an OCIO display-view transform).
- Alpha is included automatically when an alpha channel (`A`) is found in the part.
- An **Open in external viewer** button opens the source EXR in a configured HDR viewer after the node has run (see [Library settings](index.md#library-settings)).

## Typical workflow position

```text
Load EXR → [Display EXR Part] → (image output)
```

## Node preview

<!-- TODO: add ../assets/display-exr-part.png screenshot -->

## Inputs

| Name           | Type                      | Required | Notes                                                                                                        |
| -------------- | ------------------------- | -------- | ------------------------------------------------------------------------------------------------------------ |
| `part`         | `EXRPartArtifact`         | Yes      | EXR part to render. Assumes scene-linear HDR data; no gamut conversion is applied.                           |
| `exposure`     | `float`                   | No       | Exposure in EV stops applied before tone mapping or the OCIO transform. Range −10 to +10, default `0.0`.     |
| `color_params` | `OCIOColorParamsArtifact` | No       | From [OCIO Color Parameters](../opencolorio/ocio_color_parameters.md). Required when `color_mode` is `ocio`. |

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

- **OCIO mode requires a connected `color_params`.** Without one, the node fails loudly — switch `color_mode` to `basic` if you don't have an OCIO config.
- **Data is assumed scene-linear.** No input gamut conversion is applied; if your EXR stores display-referred or log data, use OCIO mode with the correct source colorspace.
- **OCIO failures are surfaced loudly.** A broken config or invalid display/view fails the node rather than silently falling back to basic tone mapping.

## See also

- [Load EXR](load_exr.md) — provides the `part` input.
- [Display EXR Channel](display_exr_channel.md) — assemble a display image from individual channels instead.
- [OCIO Color Parameters](../opencolorio/ocio_color_parameters.md) — provides the `color_params` input.
