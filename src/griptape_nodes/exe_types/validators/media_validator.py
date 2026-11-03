"""Limits on image, video and audio files: validators that read the value's file through `ctx.media`.

`MediaValidator` adds one thing to `PreRunValidator`: the kinds of media it can check (`kinds`), so a
validator on a parameter that holds none of them is refused when the node is built. Everything else
is a plain `check(value, ctx)`, reading what it needs from `ctx.media` (a `MediaFile`), which reads
each property of the file the first time a check asks for it.

The readers are in `griptape_nodes.utils.media_probe`. They read only files and data URIs on this
machine and skip any value they can't read here: a file costs a header read (a full read for WebP
and AVIF), each video or audio probe is time-limited, and an image passed as a data URI or in memory
is decoded in full. A node's own `validate_before_node_run` can call them (`local_video_probe` and
the rest) for a limit across several media inputs; during the node's checks they return what the
validators already read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from griptape_nodes.exe_types.validators.base import PreRunValidator
from griptape_nodes.exe_types.validators.context import media_kind_of

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.utils.media_values import MediaKind

__all__ = ["MediaValidator"]

ALL_MEDIA_KINDS: frozenset[MediaKind] = frozenset({"image", "video", "audio"})


class MediaValidator(PreRunValidator):
    """A limit on an image, video or audio file, judged on what `ctx.media` reads of it.

    Subclasses set `kinds`, the media they can check, and implement `check`:

    ```python
    class MaxWidth(MediaValidator):
        kinds = frozenset({"image"})

        def check(self, value, ctx):
            frame = ctx.media.frame
            if frame is None or frame.width <= 4096:
                return None
            return limit_error(f"{ctx.subject} is {frame.width} pixels wide and the limit is 4096.", ctx.docs_url)
    ```

    A property `ctx.media` can't read here is None; skip the check then, and leave it to the provider.
    """

    # The kinds of media `check` can judge. A parameter holding any other kind refuses the validator.
    kinds: ClassVar[frozenset[MediaKind]] = ALL_MEDIA_KINDS

    @override
    def require_fits(self, parameter: Parameter) -> None:
        """Refuse a parameter that holds no media, or a kind this validator can't check.

        Raises:
            TypeError: If a whole-list validator is on a single input, or the parameter holds no
                media or a kind not in `kinds`.
        """
        super().require_fits(parameter)
        kind = media_kind_of(parameter)
        name = type(self).__name__
        if kind is None:
            msg = (
                f"Attempted to add {name} to parameter '{parameter.name}'. Failed because it checks media, "
                f"and the parameter's type, {parameter.type}, isn't an image, video or audio artifact."
            )
            raise TypeError(msg)
        if kind not in self.kinds:
            readable = " or ".join(sorted(self.kinds))
            msg = (
                f"Attempted to add {name} to parameter '{parameter.name}'. "
                f"Failed because it checks {readable}, and the parameter holds {kind}."
            )
            raise TypeError(msg)
