"""Tests for reading user-facing messages off exceptions."""

from __future__ import annotations

from griptape_nodes.utils.exception_utils import readable_exception_message


class _MissingSettingError(KeyError):
    def __str__(self) -> str:
        return f"Setting {self.args[0]!r} is missing"


class _PlainKeySubclassError(KeyError):
    pass


class TestReadableExceptionMessage:
    def test_key_error_message_drops_its_repr_quoting(self) -> None:
        # str() on a KeyError reprs its argument, which would show a quoted sentence.
        message = readable_exception_message(KeyError("Node type 'Agent' not found in library 'Lib'"))

        assert message == "Node type 'Agent' not found in library 'Lib'"

    def test_key_error_carrying_a_bare_key_reads_as_that_key(self) -> None:
        assert readable_exception_message(KeyError("model_provider")) == "model_provider"

    def test_key_error_with_a_non_string_key_keeps_its_repr(self) -> None:
        assert readable_exception_message(KeyError(42)) == "42"

    def test_key_error_with_several_args_keeps_its_repr(self) -> None:
        assert readable_exception_message(KeyError("library", "node")) == "('library', 'node')"

    def test_key_error_subclass_with_its_own_str_keeps_it(self) -> None:
        assert readable_exception_message(_MissingSettingError("strength")) == "Setting 'strength' is missing"

    def test_key_error_subclass_without_its_own_str_drops_the_quoting(self) -> None:
        assert (
            readable_exception_message(_PlainKeySubclassError("Library L has no node N")) == "Library L has no node N"
        )

    def test_other_exceptions_keep_their_message(self) -> None:
        assert readable_exception_message(ValueError("bad value")) == "bad value"
