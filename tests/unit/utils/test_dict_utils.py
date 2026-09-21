from griptape_nodes.utils.dict_utils import drop_blank_values, merge_dicts, normalize_secrets_to_register


class TestNormalizeSecretsToRegister:
    """Tests for normalize_secrets_to_register function."""

    def test_list_to_dict(self) -> None:
        """Test that list format is converted to dict with empty string defaults."""
        result = normalize_secrets_to_register(["KEY1", "KEY2", "KEY3"])
        assert result == {"KEY1": "", "KEY2": "", "KEY3": ""}

    def test_dict_unchanged(self) -> None:
        """Test that dict format is returned unchanged."""
        input_dict = {"KEY1": "default1", "KEY2": "", "KEY3": "default3"}
        result = normalize_secrets_to_register(input_dict)
        assert result == input_dict

    def test_none_returns_empty_dict(self) -> None:
        """Test that None returns empty dict."""
        result = normalize_secrets_to_register(None)
        assert result == {}

    def test_empty_list_returns_empty_dict(self) -> None:
        """Test that empty list returns empty dict."""
        result = normalize_secrets_to_register([])
        assert result == {}

    def test_empty_dict_returns_empty_dict(self) -> None:
        """Test that empty dict returns empty dict."""
        result = normalize_secrets_to_register({})
        assert result == {}


class TestDropBlankValues:
    """Tests for drop_blank_values function."""

    def test_drops_empty_and_whitespace_only_strings(self) -> None:
        """A value that is only whitespace configures nothing, same as an empty one."""
        assert drop_blank_values({"a": "", "b": "   ", "c": "\t\n", "d": "keep"}) == {"d": "keep"}

    def test_keeps_non_string_falsy_values(self) -> None:
        """The rule is about blank strings; False, 0, and empty containers are real values."""
        layer = {"a": False, "b": 0, "c": [], "d": {}, "e": None}

        assert drop_blank_values(layer) == layer

    def test_recurses_into_nested_dicts(self) -> None:
        """A blank nested setting drops without taking its siblings or its parent with it."""
        layer = {"agent": {"system_prompt": "  ", "model": "opus"}}

        assert drop_blank_values(layer) == {"agent": {"model": "opus"}}

    def test_leaves_list_entries_alone(self) -> None:
        """A blank inside a list is a different problem; the entry keeps its position."""
        layer = {"libraries_to_register": ["", "/path/to/lib"]}

        assert drop_blank_values(layer) == layer

    def test_does_not_mutate_the_input(self) -> None:
        """Callers hold the loaded layer; filtering hands back a copy."""
        layer = {"a": "", "nested": {"b": ""}}

        drop_blank_values(layer)

        assert layer == {"a": "", "nested": {"b": ""}}

    def test_preserves_surrounding_whitespace_on_a_real_value(self) -> None:
        """Only wholly blank values drop; a padded value is not trimmed."""
        assert drop_blank_values({"url": " https://example.com "}) == {"url": " https://example.com "}


class TestMergeDicts:
    """Tests for merge_dicts function."""

    def test_merge_dict_with_list_overwrites(self) -> None:
        """Test that when one side is dict and other is list, list overwrites (no recursive merge error)."""
        base = {"key": {"nested": "value"}}
        override = {"key": ["item1", "item2"]}

        # Should not raise TypeError, list should overwrite dict
        result = merge_dicts(base, override)
        assert result == {"key": ["item1", "item2"]}

    def test_merge_list_with_dict_overwrites(self) -> None:
        """Test that when one side is list and other is dict, dict overwrites."""
        base = {"key": ["item1", "item2"]}
        override = {"key": {"nested": "value"}}

        result = merge_dicts(base, override)
        assert result == {"key": {"nested": "value"}}

    def test_merge_two_dicts_recursively(self) -> None:
        """Test that two dicts are merged recursively."""
        base = {"key": {"a": 1, "b": 2}}
        override = {"key": {"b": 3, "c": 4}}

        result = merge_dicts(base, override)
        assert result == {"key": {"a": 1, "b": 3, "c": 4}}

    def test_merge_lists_when_enabled(self) -> None:
        """Test that lists are merged when merge_lists=True."""
        base = {"key": ["a", "b"]}
        override = {"key": ["b", "c"]}

        result = merge_dicts(base, override, merge_lists=True)
        # Sets deduplicate, order may vary
        assert set(result["key"]) == {"a", "b", "c"}

    def test_merge_lists_disabled_overwrites(self) -> None:
        """Test that lists overwrite when merge_lists=False."""
        base = {"key": ["a", "b"]}
        override = {"key": ["c", "d"]}

        result = merge_dicts(base, override, merge_lists=False)
        assert result == {"key": ["c", "d"]}
