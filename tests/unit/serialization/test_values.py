"""Tests for the value codec: every value comes back with its exact type and content."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import json
import logging
import math
import sys
import types
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, NamedTuple, Self

import attrs
import pytest
from griptape.artifacts import ImageUrlArtifact, TextArtifact
from griptape.rules import Rule, Ruleset
from pydantic import BaseModel

import griptape_nodes
from griptape_nodes.serialization import values as values_module
from griptape_nodes.serialization.type_names import forget_stable_module_name, register_stable_module_name
from griptape_nodes.serialization.values import (
    TYPE_KEY,
    VALUE_KEY,
    UndecodedValue,
    Unencodable,
    ValueEncodeError,
    decode_value,
    encode_for_display,
    encode_value,
    register_value_codec,
    try_encode,
    untag,
)

if TYPE_CHECKING:
    from collections.abc import Generator


class Color(enum.Enum):
    RED = "red"


class Size(enum.IntEnum):
    SMALL = 1


class Span(NamedTuple):
    start: int
    end: int


class Point(BaseModel):
    x: int
    y: int


@dataclasses.dataclass
class Box:
    width: int
    label: str = "box"
    area: int = dataclasses.field(init=False, default=0)

    def __post_init__(self) -> None:
        self.area = self.width * self.width


@attrs.define
class Crate:
    _weight: int = attrs.field(alias="weight")
    contents: list[Any] = attrs.field(factory=list)


@register_value_codec
class Temperature:
    """Supplies its own state."""

    def __init__(self, celsius: float) -> None:
        self.celsius = celsius

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Temperature) and other.celsius == self.celsius

    def __hash__(self) -> int:
        return hash(self.celsius)

    def to_state(self) -> dict[str, float]:
        return {"celsius": self.celsius}

    @classmethod
    def from_state(cls, state: dict[str, float]) -> Self:
        return cls(state["celsius"])


class Kelvin(Temperature):
    """Covered by its decorated base class."""


class Opaque:
    """Has no plain-data form."""


class Label(str):
    __slots__ = ()


def _round_trip(value: Any) -> Any:
    encoded = encode_value(value)
    # Encoded values must be strict JSON, so browsers can parse them.
    return decode_value(json.loads(json.dumps(encoded, allow_nan=False)))


class TestPlainData:
    @pytest.mark.parametrize(
        "value",
        [None, True, False, 0, -7, 2**70, 1.5, "", "café 😀", [], [1, "a", None], {}, {"a": {"b": [1]}}],
    )
    def test_plain_data_encodes_as_itself(self, value: Any) -> None:
        assert encode_value(value) == value
        assert _round_trip(value) == value

    def test_bool_stays_bool(self) -> None:
        assert type(_round_trip(True)) is bool

    def test_plain_dict_holding_a_value_key_is_not_a_tag(self) -> None:
        assert _round_trip({VALUE_KEY: 1}) == {VALUE_KEY: 1}


class TestBuiltinContainers:
    @pytest.mark.parametrize(
        "value",
        [
            (1, "b"),
            (),
            {1, 2},
            frozenset({"a"}),
            b"\x00\x01\xff",
            bytearray(b"ab"),
            {1: "one", (2, 3): "pair"},
            {TYPE_KEY: "not a tag"},
        ],
    )
    def test_round_trip_keeps_exact_type(self, value: Any) -> None:
        restored = _round_trip(value)

        assert restored == value
        assert type(restored) is type(value)

    @pytest.mark.parametrize("value", [float("inf"), float("-inf")])
    def test_infinite_floats_round_trip(self, value: float) -> None:
        assert _round_trip(value) == value

    def test_nan_round_trips(self) -> None:
        assert math.isnan(_round_trip(float("nan")))

    def test_set_encoding_does_not_depend_on_insertion_order(self) -> None:
        assert encode_value({"b", "a", "c"}) == encode_value({"c", "a", "b"})

    def test_nested_containers_keep_their_types(self) -> None:
        value = [(1, {2}), {"k": (b"x",)}]

        assert _round_trip(value) == value
        assert type(_round_trip(value)[0]) is tuple

    def test_dict_holding_a_type_key_is_wrapped_not_split_into_pairs(self) -> None:
        value = {TYPE_KEY: "not a tag", "n": (1,)}

        assert encode_value(value) == {
            TYPE_KEY: "builtins:dict",
            VALUE_KEY: {TYPE_KEY: "not a tag", "n": {TYPE_KEY: "builtins:tuple", VALUE_KEY: [1]}},
        }

    def test_encoded_values_held_as_data_come_back_still_encoded(self) -> None:
        encoded = encode_value([(1, "b"), ImageUrlArtifact("https://example.com/a.png")])

        assert _round_trip(encoded) == encoded
        assert _round_trip(encode_value(encoded)) == encode_value(encoded)


class TestAdapters:
    @pytest.mark.parametrize(
        "value",
        [
            Color.RED,
            Size.SMALL,
            Path("/tmp/a.png"),  # noqa: S108
            PurePosixPath("a/b"),
            PureWindowsPath("C:\\a"),
            datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
            datetime.date(2024, 1, 2),
            datetime.time(3, 4),
            datetime.timedelta(days=2, seconds=3, microseconds=4),
            uuid.UUID("12345678-1234-5678-1234-567812345678"),
            decimal.Decimal("1.10"),
            Point(x=1, y=2),
            Span(1, 2),
            Temperature(21.5),
        ],
    )
    def test_round_trip_keeps_exact_type(self, value: Any) -> None:
        restored = _round_trip(value)

        assert restored == value
        assert type(restored) is type(value)

    def test_path_is_named_as_path_so_it_opens_on_any_os(self) -> None:
        encoded = encode_value(Path("a"))

        assert isinstance(encoded, dict)
        assert encoded[TYPE_KEY] == "pathlib:Path"

    def test_dataclass_rebuilds_fields_its_constructor_does_not_take(self) -> None:
        restored = _round_trip(Box(width=3, label="big"))

        assert restored == Box(width=3, label="big")
        assert restored.area == restored.width * restored.width

    def test_attrs_class_uses_constructor_argument_names(self) -> None:
        restored = _round_trip(Crate(weight=4, contents=[(1, 2)]))

        assert restored == Crate(weight=4, contents=[(1, 2)])

    @pytest.mark.parametrize(
        "value",
        [
            ImageUrlArtifact("https://example.com/a.png", name="a"),
            TextArtifact("hello"),
            Ruleset(name="style", rules=[Rule("Be concise")]),
        ],
    )
    def test_griptape_objects_round_trip(self, value: Any) -> None:
        restored = _round_trip(value)

        assert type(restored) is type(value)
        assert restored.to_dict() == value.to_dict()

    def test_dict_shaped_state_sits_beside_the_tag(self) -> None:
        encoded = encode_value(ImageUrlArtifact("https://example.com/a.png", name="a"))

        assert isinstance(encoded, dict)
        assert encoded[TYPE_KEY] == "griptape.artifacts.image_url_artifact:ImageUrlArtifact"
        assert encoded["type"] == "ImageUrlArtifact"
        assert encoded["value"] == "https://example.com/a.png"

    def test_values_inside_state_are_encoded_too(self) -> None:
        encoded = encode_value(Box(width=1, label="x"))

        assert encoded == {TYPE_KEY: f"{__name__}:Box", "width": 1, "label": "x"}

    def test_subclass_of_plain_type_encodes_as_the_base_type(self) -> None:
        assert type(_round_trip(Label("x"))) is str


class _Scale(enum.Enum):
    SMALL = "small"


class TestRegisteredCodecs:
    @pytest.fixture(autouse=True)
    def _restore_codecs(self) -> Generator[None, None, None]:
        saved = dict(values_module._registered)
        yield
        values_module._registered.clear()
        values_module._registered.update(saved)
        values_module._codec_cache.clear()

    def test_decorated_class_saves_through_its_own_methods(self) -> None:
        assert encode_value(Temperature(21.5)) == {TYPE_KEY: f"{__name__}:Temperature", "celsius": 21.5}

    def test_decorated_class_covers_its_subclasses(self) -> None:
        restored = _round_trip(Kelvin(3.0))

        assert type(restored) is Kelvin

    def test_methods_alone_do_not_make_a_class_savable(self) -> None:
        class Undecorated:
            def to_state(self) -> None:
                return None

            @classmethod
            def from_state(cls, state: None) -> Self:  # noqa: ARG003
                return cls()

        with pytest.raises(ValueEncodeError):
            encode_value(Undecorated())

    def test_decorating_a_class_without_the_methods_fails(self) -> None:
        with pytest.raises(ValueError, match="from_state"):
            register_value_codec(Opaque)  # pyright: ignore[reportArgumentType] the missing methods are the point

    def test_functions_cover_a_class_with_no_other_form(self) -> None:
        register_value_codec(Opaque, to_state=lambda _: None, from_state=lambda _: Opaque())

        assert type(_round_trip(Opaque())) is Opaque

    def test_functions_cover_only_the_exact_class(self) -> None:
        class OpaqueChild(Opaque):
            pass

        register_value_codec(Opaque, to_state=lambda _: None, from_state=lambda _: Opaque())

        with pytest.raises(ValueEncodeError):
            encode_value(OpaqueChild())

    @pytest.mark.parametrize("cls", [_Scale, Box, Point])
    def test_functions_cannot_replace_how_a_class_already_saves(self, cls: type) -> None:
        with pytest.raises(ValueError, match="already saves another way"):
            register_value_codec(cls, to_state=str, from_state=str)

    @pytest.mark.parametrize("cls", [str, dict, list, tuple, Path, ImageUrlArtifact])
    def test_griptape_and_plain_types_cannot_be_registered(self, cls: type) -> None:
        with pytest.raises(ValueError, match="Griptape already defines it"):
            register_value_codec(cls, to_state=str, from_state=str)

    def test_same_owner_registering_again_replaces_the_codec(self) -> None:
        """As when a library file reloads."""
        register_value_codec(Opaque, to_state=lambda _: "old", from_state=lambda _: Opaque())
        register_value_codec(Opaque, to_state=lambda _: "new", from_state=lambda _: Opaque())

        assert encode_value(Opaque()) == {TYPE_KEY: f"{__name__}:Opaque", VALUE_KEY: "new"}

    def test_another_owner_keeps_the_first_codec_with_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        register_value_codec(Opaque, to_state=lambda _: "first", from_state=lambda _: Opaque())
        other_library = {
            "__name__": "other_library.codecs",
            "register_value_codec": register_value_codec,
            "Opaque": Opaque,
        }

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            exec(  # noqa: S102 runs the call from another module's globals
                "register_value_codec(Opaque, to_state=lambda _: 'second', from_state=lambda _: Opaque())",
                other_library,
            )

        assert encode_value(Opaque()) == {TYPE_KEY: f"{__name__}:Opaque", VALUE_KEY: "first"}
        assert "other_library.codecs" in caplog.text


class TestEncodeFailures:
    def test_object_with_no_form_fails(self) -> None:
        with pytest.raises(ValueEncodeError, match="Opaque"):
            encode_value(Opaque())

    def test_object_nested_in_a_container_fails(self) -> None:
        with pytest.raises(ValueEncodeError, match="Opaque"):
            encode_value({"a": [Opaque()]})

    def test_value_that_contains_itself_fails(self) -> None:
        value: list[Any] = []
        value.append(value)

        with pytest.raises(ValueEncodeError, match="contains itself"):
            encode_value(value)

    def test_shared_value_is_not_mistaken_for_a_cycle(self) -> None:
        shared = [1]

        assert encode_value([shared, shared]) == [[1], [1]]

    def test_class_defined_in_a_function_fails(self) -> None:
        @dataclasses.dataclass
        class Local:
            x: int

        with pytest.raises(ValueEncodeError, match="inside a function"):
            encode_value(Local(1))

    def test_codec_error_becomes_an_encode_error(self) -> None:
        @register_value_codec
        class Broken:
            def to_state(self) -> dict:
                msg = "boom"
                raise RuntimeError(msg)

            @classmethod
            def from_state(cls, state: dict) -> Self:  # noqa: ARG003
                return cls()

        with pytest.raises(ValueEncodeError, match="boom"):
            encode_value(Broken())


class TestUndecodedValues:
    """Data this process cannot build is kept as-is, so passing it on loses nothing."""

    @pytest.mark.parametrize(
        ("data", "reason"),
        [
            ({TYPE_KEY: "no_such_module_xyz:Thing", "a": 1}, "has not loaded"),
            ({TYPE_KEY: "builtins:NoSuchThing"}, "has no"),
            ({TYPE_KEY: "os:getcwd"}, "does not name a class"),
            ({TYPE_KEY: "builtins:object"}, "no plain-data form"),
            ({TYPE_KEY: "not-a-type-name"}, "not a type name"),
            ({TYPE_KEY: 7}, "not text"),
            ({TYPE_KEY: "builtins:bytes", VALUE_KEY: "not base64!"}, "malformed"),
            ({TYPE_KEY: f"{__name__}:Point", "x": "not a number"}, "could not be rebuilt"),
        ],
    )
    def test_data_this_process_cannot_build_is_kept(self, data: dict, reason: str) -> None:
        decoded = decode_value(data)

        assert type(decoded) is UndecodedValue
        assert decoded == data
        assert reason in decoded.reason

    def test_module_unavailable_in_this_process_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """Expected wherever a library's classes live in another process."""
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            decoded = decode_value({TYPE_KEY: "no_such_module_xyz:Thing"})

        assert type(decoded) is UndecodedValue
        assert caplog.records == []

    def test_loaded_module_missing_the_name_logs_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """A renamed or removed class is unexpected, unlike a class this process never loaded."""
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            decoded = decode_value({TYPE_KEY: "builtins:NoSuchThing"})

        assert type(decoded) is UndecodedValue
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING

    def test_kept_value_encodes_back_to_the_same_data(self) -> None:
        data = {TYPE_KEY: "other_process_library:Artifact", "value": "https://example.com/a.png", "meta": {}}

        assert encode_value(decode_value(data)) == data

    def test_kept_value_inside_a_container_passes_through(self) -> None:
        data = [{TYPE_KEY: "other_process_library:Artifact", "pair": {TYPE_KEY: "builtins:tuple", VALUE_KEY: [1]}}]

        assert encode_value(decode_value(data)) == data

    @pytest.mark.parametrize(
        "data",
        [
            {TYPE_KEY: "builtins:set", VALUE_KEY: [{TYPE_KEY: "other_process_library:Color", VALUE_KEY: 1}]},
            {TYPE_KEY: "builtins:dict", VALUE_KEY: [[{TYPE_KEY: "other_process_library:Color", VALUE_KEY: 1}, "red"]]},
        ],
    )
    def test_kept_value_where_a_key_goes_passes_through_quietly(
        self, data: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            decoded = decode_value(data)

        assert type(decoded) is UndecodedValue
        assert encode_value(decoded) == data
        assert caplog.records == []

    def test_live_objects_pass_through(self) -> None:
        live = Opaque()

        assert decode_value(live) is live
        assert decode_value([live])[0] is live


class TestImportRestriction:
    """Decoding only imports a $type's module if it is under griptape or griptape_nodes."""

    def test_module_outside_allowed_packages_is_not_imported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = tmp_path / "marker.txt"
        module_path = tmp_path / "untrusted_module.py"
        module_path.write_text(f"open({marker!r}, 'w').write('ran')\nclass Thing:\n    pass\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop("untrusted_module", None)

        decoded = decode_value({TYPE_KEY: "untrusted_module:Thing"})

        assert type(decoded) is UndecodedValue
        assert "untrusted_module" not in sys.modules
        assert not marker.exists()

    def test_unloaded_griptape_nodes_module_still_imports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module_name = "griptape_nodes._test_import_probe"
        module_path = tmp_path / "_test_import_probe.py"
        module_path.write_text("import dataclasses\n\n\n@dataclasses.dataclass\nclass Thing:\n    x: int = 0\n")
        monkeypatch.setattr(griptape_nodes, "__path__", [*griptape_nodes.__path__, str(tmp_path)])
        sys.modules.pop(module_name, None)

        try:
            decoded = decode_value({TYPE_KEY: f"{module_name}:Thing", "x": 5})

            assert type(decoded).__name__ == "Thing"
            assert decoded.x == 5  # noqa: PLR2004 arbitrary probe value, not a meaningful constant
            assert module_name in sys.modules
        finally:
            sys.modules.pop(module_name, None)


class TestLibraryModules:
    """Classes from node library files are named by their library's stable module name."""

    _DYNAMIC = "gtn_dynamic_module_fixture_py_123"
    _STABLE = "griptape_nodes.node_libraries.fixture_library.fixture"

    @pytest.fixture
    def library_class(self) -> Generator[type, None, None]:
        module = types.ModuleType(self._DYNAMIC)
        sys.modules[self._DYNAMIC] = module
        sys.modules[self._STABLE] = module
        exec(  # noqa: S102
            "import dataclasses\n@dataclasses.dataclass\nclass Thing:\n    x: int\n",
            module.__dict__,
        )
        register_stable_module_name(self._DYNAMIC, self._STABLE)
        yield module.Thing
        forget_stable_module_name(self._DYNAMIC)
        del sys.modules[self._DYNAMIC]
        del sys.modules[self._STABLE]

    def test_value_is_tagged_with_the_stable_name(self, library_class: type) -> None:
        encoded = encode_value(library_class(1))

        assert encoded == {TYPE_KEY: f"{self._STABLE}:Thing", "x": 1}
        assert type(decode_value(encoded)) is library_class

    def test_library_file_without_a_stable_name_fails(self, library_class: type) -> None:
        forget_stable_module_name(self._DYNAMIC)

        with pytest.raises(ValueEncodeError, match="no stable module name"):
            encode_value(library_class(1))


class TestTryEncode:
    def test_returns_what_encode_value_returns(self) -> None:
        value = {"path": Path("/tmp/a"), "items": (1, 2)}  # noqa: S108

        assert try_encode(value) == encode_value(value)

    def test_returns_none_as_a_valid_encoding(self) -> None:
        assert try_encode(None) is None

    def test_returns_unencodable_with_the_reason(self) -> None:
        with pytest.raises(ValueEncodeError) as raised:
            encode_value(object())

        result = try_encode(object())

        assert result == Unencodable(reason=str(raised.value))

    def test_finds_an_unencodable_value_inside_a_container(self) -> None:
        assert isinstance(try_encode({"a": [object()]}), Unencodable)

    def test_display_encoding_falls_back_to_text(self) -> None:
        assert isinstance(encode_for_display(object()), str)


class TestUntag:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ((1, "b"), [1, "b"]),
            ({1, 2}, [1, 2]),
            (Color.RED, "red"),
            (Size.SMALL, 1),
            (Path("a/b.png"), "a/b.png"),
            (b"\x00\x01\xff", "AAH/"),
            (float("nan"), "nan"),
            (Span(1, 2), {"start": 1, "end": 2}),
            ({"pair": (1, (2, 3))}, {"pair": [1, [2, 3]]}),
        ],
    )
    def test_tagged_value_becomes_its_state(self, value: Any, expected: Any) -> None:
        assert untag(encode_value(value)) == expected

    def test_artifact_keeps_its_fields_without_the_tag(self) -> None:
        image = ImageUrlArtifact("https://example.com/a.png", name="a")

        untagged: Any = untag(encode_value(image))

        assert TYPE_KEY not in untagged
        assert untagged["type"] == "ImageUrlArtifact"
        assert untagged["value"] == "https://example.com/a.png"

    def test_keys_that_are_not_text_become_json_text(self) -> None:
        assert untag(encode_value({1: "one", (2, 3): "x"})) == {"1": "one", "[2,3]": "x"}

    def test_dict_with_its_own_type_key_keeps_it(self) -> None:
        value = {TYPE_KEY: "app.bsky.feed.post", "text": "hi", "at": (1, 2)}

        assert untag(encode_value(value)) == {TYPE_KEY: "app.bsky.feed.post", "text": "hi", "at": [1, 2]}

    def test_plain_data_is_unchanged(self) -> None:
        value = {"a": [1, {"b": None}], "c": "d"}

        assert untag(encode_value(value)) == value
