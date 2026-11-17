# Display EXR Part

**Renders an EXR part to an 8-bit RGB (or RGBA) PNG with exposure and a display transform or OCIO color management.**

Category: `EXR`

## TL;DR

- Accepts an `EXRPartArtifact` from [Load EXR](load_exr.md) or [Read EXR](read_exr.md), auto-selects RGB display channels, and writes an `ImageUrlArtifact` you can preview in the canvas.
- Two color modes: `basic` (exposure plus an `sRGB`, `rec709`, `rec1886` or `raw` display transform) and `ocio` (a connected `OCIOColorParamsArtifact` drives an OCIO display-view transform).
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

| Name    | Type               | Notes                                                                                      |
| ------- | ------------------ | ------------------------------------------------------------------------------------------ |
| `image` | `ImageUrlArtifact` | 8-bit RGB or RGBA PNG, encoded with the selected display transform, for in-canvas display. |

## Parameters

| Name                  | Type                               | Default                                                      | Notes                                                                                                                                       |
| --------------------- | ---------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `color_mode`          | `basic \| ocio`                    | `ocio` if the OpenColorIO Library is installed, else `basic` | `basic` uses a local display transform; `ocio` uses the connected `OCIOColorParamsArtifact`.                                                |
| `tone_mapping`        | `sRGB \| rec709 \| rec1886 \| raw` | `sRGB`                                                       | Display transform when `color_mode` is `basic`. See [OpenColorIO integration](index.md#opencolorio-integration). Hidden in `ocio` mode.     |
| `embed_color_profile` | `bool`                             | `false`                                                      | Embed an ICC profile naming the display transform, so color-managed viewers show `rec709`/`rec1886`/`raw` correctly. Hidden in `ocio` mode. |
| `output_file`         | `str`                              | `display_exr.png`                                            | File name to save to, resolved into the project's output location. The saved location is carried by `image`.                                |

## Tips & pitfalls

- **OCIO mode requires a connected `color_params`.** Without one, the node fails loudly — switch `color_mode` to `basic` if you don't have an OCIO config.
- **Data is assumed scene-linear.** No input gamut conversion is applied; if your EXR stores display-referred or log data, use OCIO mode with the correct source colorspace.
- **OCIO failures are surfaced loudly.** A broken config or invalid display/view fails the node rather than silently falling back to basic mode.
- **Basic mode clips values above 1.0.** The display transforms don't tone-map highlights; lower `exposure` or use OCIO mode for a tone-mapped view.

## See also

- [Load EXR](load_exr.md) · [Read EXR](read_exr.md) — provide the `part` input.
- [Display EXR Channel](display_exr_channel.md) — assemble a display image from individual channels instead.
- [OCIO Color Parameters](../opencolorio/ocio_color_parameters.md) — provides the `color_params` input.
