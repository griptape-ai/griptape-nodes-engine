"""What a pre-run validator is.

A parameter declares `pre_run_validators=`; the engine runs them at the node's turn, just before
`validate_before_node_run`. `PreRunValidator` is a limit on any kind of value. `MediaValidator` is
the family for image, video and audio files: each says which readings it needs (`needs`), and the
engine reads the union of those once per value and hands the same `MediaInfo` to every media
validator on the parameter, so a validator never opens the file itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, override

from griptape_nodes.utils.media_probe import MediaInfo, MediaNeed

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.utils.media_values import MediaKind

__all__ = [
    "LIMITS_LINK_LABEL",
    "Condition",
    "MediaValidator",
    "PreRunValidator",
    "media_kind_of",
    "reads",
    "require_validators_fit",
]


# The label on the docs link every validator's error carries.
LIMITS_LINK_LABEL = "Provider limits"


# The artifact type names a parameter declares for each kind of media.
_KIND_FOR_TYPE: dict[str, MediaKind] = {
    "imageurlartifact": "image",
    "imageartifact": "image",
    "videourlartifact": "video",
    "videoartifact": "video",
    "audiourlartifact": "audio",
    "audioartifact": "audio",
}


class Condition(Protocol):
    """Whether a validator applies, from what was read from the value's file.

    Pass one to `MediaValidator.when` or `unless`. Write it as a function, and mark it with `reads`
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

    A validator on a list parameter checks each item, unless it sets `checks_whole_list`, in which
    case it gets the whole list. `first` narrows it to a list's first items:

    ```python
    SideRange(min=300).first(2)
    ```

    A limit on an image, video or audio file subclasses `MediaValidator`, which reads the file once.
    """

    # True for a validator that judges a list parameter as a whole, such as how many items it holds.
    checks_whole_list: ClassVar[bool] = False

    @abstractmethod
    def check(self, value: Any, *, subject: str, docs_url: str | None, position: int | None = None) -> NodeError | None:
        """An error when `value` breaks this limit, or None when it is within it.

        An input with no value is skipped before any validator runs, so `value` is never None; check
        that a required input is set in the node's `validate_before_node_run`.

        Args:
            value: The parameter's value; for a list, one item, or the whole list with `checks_whole_list`.
            subject: The start of the message naming the input, such as "Image 2 in 'reference images'".
                With `checks_whole_list`, the parameter's name as the UI shows it.
            docs_url: The node's page documenting its input limits, from `BaseNode.input_limits_url`.
            position: An item's 1-based position in a list parameter, or None for a single input or
                the whole list.
        """

    def selected(self, node: BaseNode) -> Sequence[PreRunValidator]:  # noqa: ARG002
        """The validators that apply on `node` now. A validator is itself; `IfParameter` picks a list."""
        return (self,)

    def candidates(self) -> Sequence[PreRunValidator]:
        """Every validator `selected` can return, so a parameter can refuse one that can't go on it."""
        return (self,)

    def first(self, count: int) -> PreRunValidator:
        """This validator, applied only to the first `count` items of a list parameter.

        On a single input, or on a validator that checks the whole list, it always applies.
        """
        return _FirstItems(self, count)


class MediaValidator(PreRunValidator):
    """A limit on an image, video or audio file, judged on readings the engine takes once.

    Subclasses declare `needs`, the readings `check_media` uses (`MediaNeed.SIZE`, `IMAGE`, `VIDEO`
    or `AUDIO`), and implement `check_media`. A media reading is taken for the kind the parameter
    holds; a validator that names none the parameter's kind matches can't go on it. `when` and
    `unless` narrow it by what was read from the file:

    ```python
    MaxFileSize(10 * MB).when(is_png)  # is_png: a condition on the file, marked with `reads`
    ```
    """

    # The readings `check_media` uses.
    needs: ClassVar[frozenset[MediaNeed]] = frozenset()

    @abstractmethod
    def check_media(
        self, value: Any, media: MediaInfo, *, subject: str, docs_url: str | None, position: int | None = None
    ) -> NodeError | None:
        """An error when `value` breaks this limit, or None when it is within it or couldn't be read.

        Args:
            value: The parameter's value, or one item of a list parameter.
            media: The readings `needs` asked for, read once for this value. A reading that couldn't
                be taken here (a remote URL, an unreadable file) is None, and the check skips it.
            subject: The start of the message naming the input.
            docs_url: The node's page documenting its input limits.
            position: An item's 1-based position in a list parameter, or None for a single input.
        """

    @override
    def check(self, value: Any, *, subject: str, docs_url: str | None, position: int | None = None) -> NodeError | None:
        """Refuse to judge a file without reading it: call `check_media` with what was read.

        Raises:
            TypeError: Always. Without readings every media check would pass.
        """
        msg = (
            f"Attempted to check {type(self).__name__} without reading the file. "
            "Failed because a media validator judges what was read from it; call check_media with a MediaInfo."
        )
        raise TypeError(msg)

    def when(self, condition: Condition) -> MediaValidator:
        """This validator, applied only to a value whose file `condition` holds for."""
        return _Conditional(self, condition, applies_when=True)

    def unless(self, condition: Condition) -> MediaValidator:
        """This validator, applied only to a value whose file `condition` doesn't hold for."""
        return _Conditional(self, condition, applies_when=False)

    @override
    def first(self, count: int) -> MediaValidator:
        return _FirstMediaItems(self, count)


def needs_of(validator: PreRunValidator) -> frozenset[MediaNeed]:
    """The readings a validator needs: a media validator's `needs`, or none."""
    if isinstance(validator, MediaValidator):
        return validator.needs
    return frozenset()


def run_check(  # noqa: PLR0913
    validator: PreRunValidator,
    value: Any,
    media: MediaInfo,
    *,
    subject: str,
    docs_url: str | None,
    position: int | None = None,
) -> NodeError | None:
    """Run one validator on one value, handing a media validator what was read for it."""
    if isinstance(validator, MediaValidator):
        return validator.check_media(value, media, subject=subject, docs_url=docs_url, position=position)
    return validator.check(value, subject=subject, docs_url=docs_url, position=position)


@dataclass(frozen=True)
class _FirstItems(PreRunValidator):
    """`inner`, run only on a list's first `count` items."""

    inner: PreRunValidator
    count: int

    def __post_init__(self) -> None:
        # Per instance, since it follows `inner`; the engine and the fit check read it from here.
        object.__setattr__(self, "checks_whole_list", self.inner.checks_whole_list)
        if self.count < 1:
            msg = (
                f"Attempted to apply {_shown_name(self)} to the first {self.count} items. "
                "Failed because the count must be at least 1."
            )
            raise ValueError(msg)

    # A wrapped `IfParameter` picks, and each validator it picks or can pick is wrapped the same way.
    @override
    def selected(self, node: BaseNode) -> Sequence[PreRunValidator]:
        return [validator.first(self.count) for validator in self.inner.selected(node)]

    @override
    def candidates(self) -> Sequence[PreRunValidator]:
        return [validator.first(self.count) for validator in self.inner.candidates()]

    @override
    def check(self, value: Any, *, subject: str, docs_url: str | None, position: int | None = None) -> NodeError | None:
        if position is not None and position > self.count:
            return None
        return self.inner.check(value, subject=subject, docs_url=docs_url, position=position)


@dataclass(frozen=True)
class _MediaWrapped(MediaValidator):
    """A media validator that runs `inner` in some cases. It reads what `inner` reads."""

    inner: MediaValidator

    def __post_init__(self) -> None:
        # Per instance, since they follow `inner`; the engine and the fit check read them from here.
        object.__setattr__(self, "needs", self.inner.needs)
        object.__setattr__(self, "checks_whole_list", self.inner.checks_whole_list)

    @override
    def selected(self, node: BaseNode) -> Sequence[PreRunValidator]:
        return [replace(self, inner=validator) for validator in self.inner.selected(node)]

    @override
    def candidates(self) -> Sequence[PreRunValidator]:
        return [replace(self, inner=validator) for validator in self.inner.candidates()]


@dataclass(frozen=True)
class _Conditional(_MediaWrapped):
    """`inner`, run only when `condition` holds for the file (or, with `applies_when=False`, doesn't)."""

    condition: Condition
    applies_when: bool

    @override
    def __post_init__(self) -> None:
        super().__post_init__()
        condition_needs: frozenset[MediaNeed] = getattr(self.condition, "needs", frozenset())
        object.__setattr__(self, "needs", self.inner.needs | condition_needs)

    @override
    def check_media(
        self, value: Any, media: MediaInfo, *, subject: str, docs_url: str | None, position: int | None = None
    ) -> NodeError | None:
        if self.condition(media) != self.applies_when:
            return None
        return self.inner.check_media(value, media, subject=subject, docs_url=docs_url, position=position)


@dataclass(frozen=True)
class _FirstMediaItems(_MediaWrapped):
    """A media validator run only on a list's first `count` items."""

    count: int

    @override
    def __post_init__(self) -> None:
        super().__post_init__()
        if self.count < 1:
            msg = (
                f"Attempted to apply {_shown_name(self)} to the first {self.count} items. "
                "Failed because the count must be at least 1."
            )
            raise ValueError(msg)

    @override
    def check_media(
        self, value: Any, media: MediaInfo, *, subject: str, docs_url: str | None, position: int | None = None
    ) -> NodeError | None:
        if position is not None and position > self.count:
            return None
        return self.inner.check_media(value, media, subject=subject, docs_url=docs_url, position=position)


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
        validator_needs = needs_of(validator)
        if MediaNeed.SIZE in validator_needs:
            needs.add(MediaNeed.SIZE)
        # A media kind's own reading shares its name, such as MediaNeed("video").
        if kind is not None and MediaNeed(kind) in validator_needs:
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
            validator_needs = needs_of(validator)
            if not validator_needs:
                continue
            if kind is None:
                msg = (
                    f"Attempted to add {name} to parameter '{parameter.name}'. Failed because it checks media, "
                    f"and the parameter's type, {parameter.type}, isn't an image, video or audio artifact."
                )
                raise TypeError(msg)
            media_needs = validator_needs - {MediaNeed.SIZE}
            if media_needs and MediaNeed(kind) not in media_needs:
                readable = " or ".join(sorted(need.value for need in media_needs))
                msg = (
                    f"Attempted to add {name} to parameter '{parameter.name}'. "
                    f"Failed because it checks {readable}, and the parameter holds {kind}."
                )
                raise TypeError(msg)


def _shown_name(validator: PreRunValidator) -> str:
    """The validator's class name for an error, naming the validator a `when`, `unless` or `first` wraps."""
    while isinstance(validator, (_FirstItems, _MediaWrapped)):
        validator = validator.inner
    return type(validator).__name__
