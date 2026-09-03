"""Tests for the redactor.

This is the security boundary of the whole diagnostics feature: a bundle is written to be
handed to someone else, and the redactor is the only thing standing between a user's API
keys and a support engineer's inbox. So these tests are written from the leak's point of
view — for each rule, does the secret still appear anywhere in the output?

The other half is the counts. A reader who cannot tell "this setting is empty" from "this
setting was hidden from you" will debug the wrong problem, so every removal has to be
counted under a reason.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from griptape_nodes.common.diagnostics.redaction import (
    MIN_SEARCHABLE_SECRET_LENGTH,
    REDACTED,
    REDACTED_USER,
    RedactionReason,
    Redactor,
)


class TestSensitiveConfigKeys:
    def test_removes_a_value_under_a_key_named_like_a_credential(self) -> None:
        config = {"openai_api_key": "sk-realvalue", "model": "gpt-4"}

        redacted = Redactor(normalize_identity=False).redact_config(config)

        assert redacted == {"openai_api_key": REDACTED, "model": "gpt-4"}

    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "API_KEY",
            "token",
            "refresh_token",
            "secret",
            "password",
            "passwd",
            "pwd",
            "credential",
            "Authorization",
            "basic_auth",
            "cookie",
            "session_cookie",
            "X-Amz-Signature",
            "bearer",
            "keys",
        ],
    )
    def test_matches_credential_key_names_as_substrings_and_ignores_case(self, key: str) -> None:
        redacted = Redactor(normalize_identity=False).redact_config({key: "value-to-hide"})

        assert redacted[key] == REDACTED

    @pytest.mark.parametrize("key", ["env", "headers", "ENV", "Headers"])
    def test_removes_the_mcp_fields_that_carry_credentials_by_construction(self, key: str) -> None:
        """`env` and `headers` do not read like secrets, and they are exactly where the leak was."""
        redacted = Redactor(normalize_identity=False).redact_config({key: {"ANTHROPIC_API_KEY": "sk-ant-realvalue"}})

        assert redacted[key] == {"ANTHROPIC_API_KEY": REDACTED}

    def test_keeps_the_names_inside_a_credential_mapping(self) -> None:
        """Which variables are set is the diagnostic signal; the names are not secret."""
        config = {"env": {"HF_TOKEN": "hf_realvalue", "PATH": "/usr/bin"}}

        redacted = Redactor(normalize_identity=False).redact_config(config)

        assert sorted(redacted["env"]) == ["HF_TOKEN", "PATH"]
        assert redacted["env"]["PATH"] == REDACTED

    def test_reaches_credentials_nested_in_lists_and_dicts(self) -> None:
        config = {"mcp_servers": [{"name": "local", "env": {"KEY": "value-to-hide"}}]}

        redacted = Redactor(normalize_identity=False).redact_config(config)

        assert redacted["mcp_servers"][0]["env"]["KEY"] == REDACTED
        assert redacted["mcp_servers"][0]["name"] == "local"

    def test_leaves_an_unset_value_visibly_unset(self) -> None:
        """Declared but never filled in is often the answer, so it must not read as hidden."""
        config = {"env": {"OPENAI_API_KEY": "", "OTHER_KEY": None, "SET_KEY": "realvalue"}}
        redactor = Redactor(normalize_identity=False)

        redacted = redactor.redact_config(config)

        assert redacted["env"]["OPENAI_API_KEY"] == ""
        assert redacted["env"]["OTHER_KEY"] is None
        assert redacted["env"]["SET_KEY"] == REDACTED
        assert redactor.counts() == {RedactionReason.CONFIG_KEY: 1}

    def test_leaves_ordinary_values_alone(self) -> None:
        config = {"workspace_directory": "/projects/thing", "max_nodes_in_parallel": 5, "enabled": True}
        redactor = Redactor(normalize_identity=False)

        assert redactor.redact_config(config) == config
        assert redactor.total_redactions() == 0

    def test_still_scrubs_a_credential_pasted_into_an_innocent_key(self) -> None:
        """The key name is a heuristic, so every string value is scanned as well."""
        config = {"notes": "the failing call used sk-ant-abcdefghijkl"}

        redacted = Redactor(normalize_identity=False).redact_config(config)

        assert "sk-ant-abcdefghijkl" not in redacted["notes"]

    def test_keeps_the_secret_names_a_library_asked_the_engine_to_look_for(self) -> None:
        """`secrets_to_register` holds names, not values, and the names are the whole signal."""
        config = {"secrets_to_register": {"OPENAI_API_KEY": None, "HF_TOKEN": None}}
        redactor = Redactor(normalize_identity=False)

        redacted = redactor.redact_config(config)

        assert sorted(redacted["secrets_to_register"]) == ["HF_TOKEN", "OPENAI_API_KEY"]
        assert redactor.total_redactions() == 0

    def test_removes_a_default_value_sitting_beside_a_declared_secret_name(self) -> None:
        """A default value beside a declared name is a real credential, not documentation.

        The mapping form of `secrets_to_register` carries values as well as names, and
        `SecretsManager.register_all_secrets` writes one as a secret. The names stay,
        because they are the setting's whole diagnostic signal; the values cannot.
        """
        config = {"secrets_to_register": {"OPENAI_API_KEY": "sk-realvalue", "HF_TOKEN": ""}}
        redactor = Redactor(normalize_identity=False)

        redacted = redactor.redact_config(config)

        assert redacted["secrets_to_register"]["OPENAI_API_KEY"] == REDACTED
        # Declared but never filled in, which is different from hidden.
        assert redacted["secrets_to_register"]["HF_TOKEN"] == ""
        assert redactor.counts() == {RedactionReason.CONFIG_KEY: 1}

    def test_keeps_the_secret_names_a_library_declared_as_a_list(self) -> None:
        """The list form holds names only, so nothing in it is removed."""
        config = {"secrets_to_register": ["OPENAI_API_KEY", "HF_TOKEN"]}
        redactor = Redactor(normalize_identity=False)

        redacted = redactor.redact_config(config)

        assert redacted["secrets_to_register"] == ["OPENAI_API_KEY", "HF_TOKEN"]
        assert redactor.total_redactions() == 0

    def test_normalizes_a_home_directory_hiding_in_a_dict_key(self) -> None:
        """A library is free to key a settings subtree by absolute path."""
        config = {str(Path.home() / "projects"): {"enabled": True}}
        redactor = Redactor()

        redacted = redactor.redact_config(config)

        # Only the home directory is replaced, so what follows keeps this platform's separator.
        assert list(redacted) == [f"~{os.sep}projects"]
        assert redactor.counts() == {RedactionReason.HOME_DIRECTORY: 1}

    def test_a_redacted_key_still_masks_its_own_value(self) -> None:
        """Sensitivity is decided on the original key, so rewriting it cannot unmask the value."""
        config = {f"{Path.home()}_api_key": "sk-realvalue"}

        redacted = Redactor().redact_config(config)

        assert redacted == {"~_api_key": REDACTED}

    def test_normalizes_a_home_directory_hiding_in_a_key_inside_a_masked_subtree(self) -> None:
        """Masking a subtree drops its values, which is no reason to stop reading its keys."""
        config = {"headers": {str(Path.home() / "cert.pem"): "realvalue"}}
        redactor = Redactor()

        redacted = redactor.redact_config(config)

        normalized_key = f"~{os.sep}cert.pem"
        assert list(redacted["headers"]) == [normalized_key]
        assert redacted["headers"][normalized_key] == REDACTED


class TestKnownSecretValues:
    def test_removes_a_known_secret_from_free_text(self) -> None:
        """The engine holds its own secrets, so it can find one a library logged verbatim."""
        redactor = Redactor(secret_values=["s3cr3t-value-12345"], normalize_identity=False)

        redacted = redactor.redact_text("request failed with token s3cr3t-value-12345 attached")

        assert "s3cr3t-value-12345" not in redacted
        assert redacted == f"request failed with token {REDACTED} attached"
        assert redactor.counts() == {RedactionReason.KNOWN_SECRET_VALUE: 1}

    def test_removes_every_occurrence(self) -> None:
        redactor = Redactor(secret_values=["s3cr3t-value-12345"], normalize_identity=False)

        redacted = redactor.redact_text("s3cr3t-value-12345 then s3cr3t-value-12345")

        assert "s3cr3t-value-12345" not in redacted
        assert redactor.counts() == {RedactionReason.KNOWN_SECRET_VALUE: 2}

    def test_removes_a_secret_that_contains_no_credential_shape_at_all(self) -> None:
        """The generic patterns would never catch this one; only the engine's own list can."""
        redactor = Redactor(secret_values=["correct horse battery staple"], normalize_identity=False)

        assert "correct horse" not in redactor.redact_text("password is correct horse battery staple")

    def test_ignores_a_secret_too_short_to_search_for(self) -> None:
        """Someone whose secret is `dev` would otherwise have their logs destroyed to protect nothing."""
        redactor = Redactor(secret_values=["dev"], normalize_identity=False)

        redacted = redactor.redact_text("running in dev mode on device dev0")

        assert redacted == "running in dev mode on device dev0"
        assert redactor.total_redactions() == 0

    def test_searches_for_a_secret_exactly_at_the_length_threshold(self) -> None:
        secret = "a" * MIN_SEARCHABLE_SECRET_LENGTH
        redactor = Redactor(secret_values=[secret], normalize_identity=False)

        assert secret not in redactor.redact_text(f"value {secret} here")

    def test_replaces_the_longer_secret_whole_when_one_contains_another(self) -> None:
        """Shortest-first would leave an unrecognizable remainder of the longer secret behind."""
        redactor = Redactor(secret_values=["abcdefghij", "abcdefghijklmnop"], normalize_identity=False)

        redacted = redactor.redact_text("the value is abcdefghijklmnop")

        assert redacted == f"the value is {REDACTED}"

    def test_escapes_regex_characters_in_a_secret(self) -> None:
        """A secret with `.` or `+` in it is a literal, not a pattern."""
        redactor = Redactor(secret_values=["a.b+c(d)e*f"], normalize_identity=False)

        assert "a.b+c(d)e*f" not in redactor.redact_text("key a.b+c(d)e*f used")
        assert redactor.redact_text("axbxcxdxexf") == "axbxcxdxexf"

    def test_removes_a_known_secret_used_as_a_bearer_token(self) -> None:
        """The bearer rule runs first, so the exact match must not be what saves this one."""
        known_value = "s3cr3t-value-12345"
        redactor = Redactor(secret_values=[known_value], normalize_identity=False)

        redacted = redactor.redact_text(f"Authorization: Bearer {known_value}")

        assert known_value not in redacted
        assert redacted == f"Authorization: Bearer {REDACTED}"
        # The reason, not just the outcome: either ordering removes the value, but only the
        # documented one counts it once, under the rule that actually consumed it.
        assert redactor.counts() == {RedactionReason.BEARER_TOKEN: 1}

    def test_removes_a_known_secret_used_as_a_url_query_parameter(self) -> None:
        known_value = "s3cr3t-value-12345"
        redactor = Redactor(secret_values=[known_value], normalize_identity=False)

        redacted = redactor.redact_text(f"GET https://api.example.com/v1/things?api_key={known_value}&limit=10")

        assert known_value not in redacted
        assert "limit=10" in redacted


class TestCredentialPatterns:
    @pytest.mark.parametrize(
        ("text", "secret", "kept_prefix"),
        [
            ("key sk-ant-api03abcdefghij live", "sk-ant-api03abcdefghij", "sk-ant-"),
            ("key sk-abcdefghijklmnop live", "sk-abcdefghijklmnop", "sk-"),
            ("key gsk_abcdefghijklmnop live", "gsk_abcdefghijklmnop", "gsk_"),
            ("key ghp_abcdefghijklmnop live", "ghp_abcdefghijklmnop", "ghp_"),
            ("key github_pat_abcdefghijkl live", "github_pat_abcdefghijkl", "github_pat_"),
            ("key xoxb-abcdefghijklmnop live", "xoxb-abcdefghijklmnop", "xoxb-"),
            ("key AKIAIOSFODNN7EXAMPLE live", "AKIAIOSFODNN7EXAMPLE", "AKIA"),
            ("key hf_abcdefghijklmnop live", "hf_abcdefghijklmnop", "hf_"),
        ],
    )
    def test_removes_a_vendor_prefixed_key_while_keeping_the_prefix(
        self, text: str, secret: str, kept_prefix: str
    ) -> None:
        """The prefix says whose credential it was, which support needs and cannot abuse."""
        redactor = Redactor(normalize_identity=False)

        redacted = redactor.redact_text(text)

        assert secret not in redacted
        assert kept_prefix in redacted
        assert redacted.startswith("key ")
        assert redacted.endswith(" live")
        assert redactor.counts() == {RedactionReason.API_KEY_PATTERN: 1}

    def test_catches_an_unknown_vendors_key_it_was_never_taught(self) -> None:
        """A pattern list is a floor, not a ceiling: this one is covered by the sk- rule."""
        assert "sk-newvendor12345" not in Redactor(normalize_identity=False).redact_text("token sk-newvendor12345")

    @pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "Basic"])
    def test_removes_an_authorization_token_and_keeps_the_scheme(self, scheme: str) -> None:
        redactor = Redactor(normalize_identity=False)

        redacted = redactor.redact_text(f"Authorization: {scheme} abcdefghijklmnopqrst")

        assert "abcdefghijklmnopqrst" not in redacted
        assert scheme in redacted
        assert redactor.counts() == {RedactionReason.BEARER_TOKEN: 1}

    @pytest.mark.parametrize(
        "text",
        [
            "Bearer credentials required",
            "Basic authentication is not supported",
            "use bearer tokens here",
        ],
    )
    def test_leaves_prose_after_bearer_or_basic_alone(self, text: str) -> None:
        """The value class matches ordinary letters, so a low threshold redacts English."""
        redactor = Redactor(normalize_identity=False)

        assert redactor.redact_text(text) == text
        assert redactor.total_redactions() == 0

    def test_counts_one_credential_once(self) -> None:
        """A rule that reran over an already-redacted value inflated the count."""
        redactor = Redactor(normalize_identity=False)
        url = "https://bucket.s3.amazonaws.com/f.png?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE"

        redactor.redact_text(url)

        assert redactor.counts() == {RedactionReason.SIGNED_URL_PARAMETER: 1}

    def test_removes_the_signature_from_a_presigned_url(self) -> None:
        """A presigned URL in a log is a working credential until it expires."""
        redactor = Redactor(normalize_identity=False)
        url = "https://bucket.s3.amazonaws.com/f.png?X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature=deadbeefcafe"

        redacted = redactor.redact_text(url)

        assert "deadbeefcafe" not in redacted
        assert "AKIAEXAMPLE" not in redacted
        assert "X-Amz-Signature" in redacted
        assert redacted.startswith("https://bucket.s3.amazonaws.com/f.png?")

    def test_leaves_the_rest_of_a_url_readable(self) -> None:
        """The path and host are what make a URL useful for debugging."""
        redactor = Redactor(normalize_identity=False)

        redacted = redactor.redact_text("GET https://api.example.com/v1/things?limit=10&token=abcdefghijkl HTTP/1.1")

        assert "https://api.example.com/v1/things" in redacted
        assert "limit=10" in redacted
        assert "abcdefghijkl" not in redacted

    def test_leaves_text_with_no_credentials_untouched(self) -> None:
        redactor = Redactor(normalize_identity=False)
        text = "Node 'Load Image' finished in 1.2s, wrote 3 files, monkey keyboard turkey"

        assert redactor.redact_text(text) == text
        assert redactor.total_redactions() == 0


class TestIdentityNormalization:
    def test_replaces_the_home_directory_with_a_tilde(self) -> None:
        redactor = Redactor()

        redacted = redactor.redact_text(f"saved to {Path.home()}/workspace/thing.py")

        assert str(Path.home()) not in redacted
        assert redacted.endswith("/workspace/thing.py")
        assert redacted.startswith("saved to ~")

    def test_replaces_a_home_directory_written_with_forward_slashes(self) -> None:
        """A path built by string concatenation reaches a log with the other separator."""
        redactor = Redactor()
        home = str(Path.home()).replace("\\", "/")

        assert home not in redactor.redact_text(f"saved to {home}/workspace")

    def test_leaves_another_users_home_directory_recognizable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`/Users/sam` matching inside `/Users/samantha` rewrote a colleague's path to `~antha`.

        Nothing leaks either way, but the result reads as this user's own home when it is
        somebody else's, and a shared-machine path is exactly what support is looking at.

        Both paths are built under `tmp_path` rather than written out, so the separator is
        this platform's. Spelled `/Users/sam`, the home directory would not appear in the
        text at all on Windows and nothing would be replaced either way.
        """
        home = tmp_path / "sam"
        # A colleague's home, sharing the first three characters of this user's.
        neighbor_file = tmp_path / "samantha" / "workspace" / "thing.py"
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setattr("getpass.getuser", lambda: "unrelated-name")
        redactor = Redactor()

        redacted = redactor.redact_text(f"read {neighbor_file} and {home / 'notes.txt'}")

        assert str(neighbor_file) in redacted
        assert f"~{os.sep}notes.txt" in redacted
        assert redactor.counts() == {RedactionReason.HOME_DIRECTORY: 1}

    def test_redact_path_accepts_a_path_object(self) -> None:
        redacted = Redactor().redact_path(Path.home() / "workspace")

        assert redacted.startswith("~")

    def test_leaves_paths_alone_when_normalization_is_off(self) -> None:
        """`--show-identity` is for a user who would rather share the real paths."""
        redactor = Redactor(normalize_identity=False)
        text = f"saved to {Path.home()}/workspace"

        assert redactor.redact_text(text) == text

    def test_normalizes_the_username_on_its_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("getpass.getuser", lambda: "alexandra")
        redactor = Redactor()

        redacted = redactor.redact_text("engine started by alexandra")

        assert redacted == f"engine started by {REDACTED_USER}"
        assert redactor.counts()[RedactionReason.USERNAME] == 1

    def test_ignores_a_username_too_short_to_search_for(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A two-character username is left alone even where it stands as its own word.

        The username appears in the text as a whole word on purpose. Word-boundary matching
        would spare `alpha` whatever the length threshold did, so a text of only `alpha`
        would pass with the threshold removed and prove nothing about it.
        """
        monkeypatch.setattr("getpass.getuser", lambda: "al")
        redactor = Redactor()

        assert redactor.redact_text("al ran the alpha value") == "al ran the alpha value"
        assert RedactionReason.USERNAME not in redactor.counts()

    def test_matches_a_username_only_on_word_boundaries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("getpass.getuser", lambda: "sam")

        redacted = Redactor().redact_text("sam ran the same sample")

        assert redacted == f"{REDACTED_USER} ran the same sample"

    def test_still_produces_output_when_the_username_cannot_be_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not knowing the username is not a failure; a report still has to be produced."""

        def raise_os_error() -> str:
            msg = "no password database"
            raise OSError(msg)

        monkeypatch.setattr("getpass.getuser", raise_os_error)

        assert Redactor().redact_text("engine started") == "engine started"

    def test_still_produces_output_when_there_is_no_home_directory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Some service accounts and containers have none."""

        def raise_runtime_error() -> Path:
            msg = "could not resolve home"
            raise RuntimeError(msg)

        monkeypatch.setattr(Path, "home", raise_runtime_error)

        assert Redactor().redact_text("engine started") == "engine started"


class TestCounts:
    def test_counts_are_grouped_by_reason(self) -> None:
        redactor = Redactor(secret_values=["s3cr3t-value-12345"], normalize_identity=False)

        redactor.redact_config({"api_key": "hidden", "note": "s3cr3t-value-12345 and sk-abcdefghijkl"})

        assert redactor.counts() == {
            RedactionReason.CONFIG_KEY: 1,
            RedactionReason.KNOWN_SECRET_VALUE: 1,
            RedactionReason.API_KEY_PATTERN: 1,
        }
        assert redactor.total_redactions() == 3  # noqa: PLR2004

    def test_a_reason_that_never_fired_is_omitted(self) -> None:
        """A count of zero would read as "checked and found nothing", which is noise."""
        redactor = Redactor(normalize_identity=False)

        redactor.redact_text("nothing to see here")

        assert redactor.counts() == {}

    def test_counts_accumulate_across_every_call(self) -> None:
        """One redactor covers a whole bundle, so its counts have to cover every file in it."""
        redactor = Redactor(normalize_identity=False)

        redactor.redact_text("sk-abcdefghijkl")
        redactor.redact_text("sk-mnopqrstuvwx")

        assert redactor.total_redactions() == 2  # noqa: PLR2004

    def test_an_empty_string_is_returned_unchanged(self) -> None:
        redactor = Redactor()

        assert redactor.redact_text("") == ""
        assert redactor.total_redactions() == 0
