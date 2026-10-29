"""What a pre-run validator is, and what the engine reads for it.

A parameter declares `pre_run_validators=`; the engine runs them at the node's turn, just before
`validate_before_node_run`. Each validator says which readings it needs (`needs`). The engine reads
the union of those once per value and hands the same `MediaInfo` to every validator on the
parameter, so a validator never opens the file itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, override

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.input_checks import AudioProbe, ImageInfo, VideoProbe
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.utils.media_values import MediaKind

__all__ = [
    "EMPTY_MEDIA",
    "LIMITS_LINK_LABEL",
    "Condition",
    "MediaInfo",
    "MediaNeed",
    "PreRunValidator",
    "media_kind_of",
    "reads",
    "require_validators_fit",
]


# The label on the docs link every validator's error carries.
LIMITS_LINK_LABEL = "Provider limits"


class MediaNeed(StrEnum):
    """A reading of a media value that a validator needs."""

    SIZE = "size"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


# The artifact type names a parameter declares for each kind of media.
_KIND_FOR_TYPE: dict[str, MediaKind] = {
    "imageurlartifact": "image",
    "imageartifact": "image",
    "videourlartifact": "video",
    "videoartifact": "video",
    "audiourlartifact": "audio",
    "audioartifact": "audio",
}


@dataclass(frozen=True)
class MediaInfo:
    """What the engine read from one media value: only the readings its validators asked for.

    Each part is None when it wasn't asked for, or couldn't be read locally (a remote URL, a format
    nothing here opens, ffprobe missing or out of time). A validator skips a reading that is None.
    `image_format` is an image's format read from its file signature when `image` couldn't be read,
    such as "image/heif" for a HEIC photo Pillow can't open, so a format check still judges it.
    """

    size_bytes: int | None = None
    image: ImageInfo | None = None
    image_format: str | None = None
    video: VideoProbe | None = None
    audio: AudioProbe | None = None

    @property
    def frame(self) -> ImageInfo | None:
        """The image's pixel size, or the video's frame as stored (turned upright)."""
        if self.image is not None:
            return self.image
        if self.video is not None:
            return self.video.info
        return None

    @property
    def display_frame(self) -> ImageInfo | None:
        """A video frame's shape as it plays when its pixels aren't square, or None."""
        if self.video is None:
            return None
        return self.video.display_info

    @property
    def mime_type(self) -> str | None:
        """The format read from the file: the image's, the video's container or the audio's container."""
        if self.image is not None:
            return self.image.mime_type
        if self.image_format is not None:
            return self.image_format
        if self.video is not None:
            return self.video.mime_type
        if self.audio is not None:
            return self.audio.mime_type
        return None


# What a validator gets when nothing was read: the value holds no media, or no validator asked.
EMPTY_MEDIA = MediaInfo()


class Condition(Protocol):
    """Whether a validator applies, from what was read from the value's file.

    Pass one to `PreRunValidator.when` or `unless`. Write it as a function, and mark it with `reads`
    for the readings it uses, so the engine reads them along with the validator's. A limit that
    depends on another parameter's value is an `IfParameter`.
    """

    def __call__(self, media: MediaInfo, /) -> bool:
        """True when the validator should check this value."""
        raise NotImplementedError


def reads(*needs: MediaNeed) -> Callable[[Callable[[MediaInfo], bool]], Condition]:
    """Mark a condition with the readings it uses, so the engine reads them along with the validator's.

    ```python
    @reads(MediaNeed.IMAGE)
    def is_png(media: MediaInfo) -> bool:
        return media.mime_type == "image/png"
    ```

    A condition without it gets only the readings its validator asked for.
    """

    def mark(condition: Callable[[MediaInfo], bool]) -> Condition:
        condition.needs = frozenset(needs)  # pyright: ignore[reportFunctionMemberAccess]
        return condition

    return mark


class PreRunValidator(ABC):
    """A limit on one input, checked at the node's turn, before it runs.

    Subclasses declare `needs`, the readings `check` uses. A validator on a list parameter checks
    each item, unless it sets `checks_whole_list`, in which case it gets the whole list.

    `when`, `unless` and `first` wrap any validator to narrow where it applies:

    ```python
    MaxFileSize(10 * MB).when(is_png)  # is_png: a condition on the file, marked with `reads`
    SideRange(min=300).first(2)
    ```
    """

    # The readings `check` uses. A media reading (image, video or audio) is read for the kind the
    # parameter holds; a validator that names none the parameter's kind matches can't go on it.
    needs: ClassVar[frozenset[MediaNeed]] = frozenset()
    # True for a validator that judges a list parameter as a whole, such as how many items it holds.
    checks_whole_list: ClassVar[bool] = False

    @abstractmethod
    def check(
        self,
        value: Any,
        *,
        subject: str,
        docs_url: str | None,
        media: MediaInfo = EMPTY_MEDIA,
        position: int | None = None,
    ) -> NodeError | None:
        """An error when `value` breaks this limit, or None when it is within it or couldn't be read.

        Args:
            value: The parameter's value; for a list, one item, or the whole list with `checks_whole_list`.
            subject: The start of the message naming the input, such as "Image 2 in 'reference images'".
                With `checks_whole_list`, the parameter's name as the UI shows it.
            docs_url: The node's page documenting its input limits, from `BaseNode.input_limits_url`.
            media: The readings `needs` asked for, read once for this value. Empty for a validator
                that needs none.
            position: An item's 1-based position in a list parameter, or None for a single input or
                the whole list.
        """

    def selected(self, node: BaseNode) -> Sequence[PreRunValidator]:  # noqa: ARG002
        """The validators that apply on `node` now. A validator is itself; `IfParameter` picks a list."""
        return (self,)

    def candidates(self) -> Sequence[PreRunValidator]:
        """Every validator `selected` can return, so a parameter can refuse one that can't go on it."""
        return (self,)

    def when(self, condition: Condition) -> PreRunValidator:
        """This validator, applied only to a value whose file `condition` holds for."""
        return _Conditional(self, condition, applies_when=True)

    def unless(self, condition: Condition) -> PreRunValidator:
        """This validator, applied only to a value whose file `condition` doesn't hold for."""
        return _Conditional(self, condition, applies_when=False)

    def first(self, count: int) -> PreRunValidator:
        """This validator, applied only to the first `count` items of a list parameter.

        On a single input, or on a validator that checks the whole list, it always applies.
        """
        return _FirstItems(self, count)


@dataclass(frozen=True)
class _Wrapped(PreRunValidator):
    """A validator that runs `inner` in some cases. It reads what `inner` reads and checks what it checks."""

    inner: PreRunValidator

    def __post_init__(self) -> None:
        # Per instance, since they follow `inner`; the engine and the fit check read them from here.
        object.__setattr__(self, "needs", self.inner.needs)
        object.__setattr__(self, "checks_whole_list", self.inner.checks_whole_list)

    # A wrapped `IfParameter` picks, and each validator it picks or can pick is wrapped the same way.
    @override
    def selected(self, node: BaseNode) -> Sequence[PreRunValidator]:
        return [replace(self, inner=validator) for validator in self.inner.selected(node)]

    @override
    def candidates(self) -> Sequence[PreRunValidator]:
        return [replace(self, inner=validator) for validator in self.inner.candidates()]


@dataclass(frozen=True)
class _Conditional(_Wrapped):
    """`inner`, run only when `condition` holds (or, with `applies_when=False`, doesn't)."""

    condition: Condition
    applies_when: bool

    def __post_init__(self) -> None:
        super().__post_init__()
        condition_needs: frozenset[MediaNeed] = getattr(self.condition, "needs", frozenset())
        object.__setattr__(self, "needs", self.inner.needs | condition_needs)

    @override
    def check(
        self,
        value: Any,
        *,
        subject: str,
        docs_url: str | None,
        media: MediaInfo = EMPTY_MEDIA,
        position: int | None = None,
    ) -> NodeError | None:
        if self.condition(media) != self.applies_when:
            return None
        return self.inner.check(value, subject=subject, docs_url=docs_url, media=media, position=position)


@dataclass(frozen=True)
class _FirstItems(_Wrapped):
    """`inner`, run only on a list's first `count` items."""

    count: int

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.count < 1:
            msg = (
                f"Attempted to apply {_shown_name(self)} to the first {self.count} items. "
                "Failed because the count must be at least 1."
            )
            raise ValueError(msg)

    @override
    def check(
        self,
        value: Any,
        *,
        subject: str,
        docs_url: str | None,
        media: MediaInfo = EMPTY_MEDIA,
        position: int | None = None,
    ) -> NodeError | None:
        if position is not None and position > self.count:
            return None
        return self.inner.check(value, subject=subject, docs_url=docs_url, media=media, position=position)


def media_kind_of(parameter: Parameter) -> MediaKind | None:
    """The kind of media a parameter holds, or a list parameter's items hold, from its declared type."""
    type_name = parameter.type.strip().lower()
    if type_name.startswith("list[") and type_name.endswith("]"):
        type_name = type_name.removeprefix("list[").removesuffix("]")
    return _KIND_FOR_TYPE.get(type_name)


def holds_list(parameter: Parameter) -> bool:
    """Whether a parameter's value is a list, whose items its validators check one by one."""
    return parameter.type.strip().lower().startswith("list[")


def needs_for_kind(validators: Iterable[PreRunValidator], kind: MediaKind | None) -> frozenset[MediaNeed]:
    """The readings to take of one value for these validators, on a parameter holding `kind`."""
    needs: set[MediaNeed] = set()
    for validator in validators:
        if MediaNeed.SIZE in validator.needs:
            needs.add(MediaNeed.SIZE)
        # A media kind's own reading shares its name, such as MediaNeed("video").
        if kind is not None and MediaNeed(kind) in validator.needs:
            needs.add(MediaNeed(kind))
    return frozenset(needs)


def require_validators_fit(parameter: Parameter) -> None:
    """Raise when a parameter declares a pre-run validator its type can't be checked with.

    Raises:
        TypeError: If a media validator is on a parameter that holds no media, or none of the media
            it reads is the kind the parameter holds; or a whole-list validator is on a single input.
    """
    kind = media_kind_of(parameter)
    is_list = holds_list(parameter)
    for declared in parameter.pre_run_validators:
        for validator in declared.candidates():
            name = _shown_name(validator)
            if validator.checks_whole_list and not is_list:
                msg = (
                    f"Attempted to add {name} to parameter '{parameter.name}'. "
                    "Failed because it checks a list, and the parameter holds a single value."
                )
                raise TypeError(msg)
            if not validator.needs:
                continue
            if kind is None:
                msg = (
                    f"Attempted to add {name} to parameter '{parameter.name}'. Failed because it checks media, "
                    f"and the parameter's type, {parameter.type}, isn't an image, video or audio artifact."
                )
                raise TypeError(msg)
            media_needs = validator.needs - {MediaNeed.SIZE}
            if media_needs and MediaNeed(kind) not in media_needs:
                readable = " or ".join(sorted(need.value for need in media_needs))
                msg = (
                    f"Attempted to add {name} to parameter '{parameter.name}'. "
                    f"Failed because it checks {readable}, and the parameter holds {kind}."
                )
                raise TypeError(msg)


def _shown_name(validator: PreRunValidator) -> str:
    """The validator's class name for an error, naming the validator a `when`, `unless` or `first` wraps."""
    while isinstance(validator, _Wrapped):
        validator = validator.inner
    return type(validator).__name__
