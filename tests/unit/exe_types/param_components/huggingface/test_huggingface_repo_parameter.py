"""Tests for HuggingFaceRepoParameter download-name handling.

These tests cover how `::<subname>` postfixes (used by providers like the LTX-2 pipeline
to encode a sub-model selector within a single HuggingFace repo, e.g.
`Lightricks/LTX-2::ltx-2-19b-dev`) are stripped before model names reach the model
manager UI (button link, help text) and the download path. See
griptape-ai/griptape-nodes#4553.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter import HuggingFaceRepoParameter
from tests.unit.exe_types.mocks import MockNode


class TestGetDownloadModels:
    @pytest.fixture(autouse=True)
    def _stub_cache_scan(self) -> Iterator[None]:
        # HuggingFaceRepoParameter.__init__ calls refresh_parameters() -> get_choices() -> fetch_repo_revisions(),
        # which reads the real HuggingFace cache. Stub it so tests don't depend on local cache state.
        with patch(
            "griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter.list_repo_revisions_in_cache",
            return_value=[],
        ):
            yield

    def test_strips_double_colon_subname_postfix(self) -> None:
        node = MockNode()
        param = HuggingFaceRepoParameter(
            node,
            repo_ids=[
                "Lightricks/LTX-2::ltx-2-19b-dev",
                "Lightricks/LTX-2::ltx-2-19b-dev-fp8",
                "Lightricks/LTX-2::ltx-2-19b-dev-fp4",
            ],
        )

        assert param.get_download_models() == ["Lightricks/LTX-2"]

    def test_preserves_names_without_postfix(self) -> None:
        node = MockNode()
        param = HuggingFaceRepoParameter(
            node,
            repo_ids=["dg845/LTX-2.3-Diffusers", "microsoft/DialoGPT-medium"],
        )

        assert param.get_download_models() == ["dg845/LTX-2.3-Diffusers", "microsoft/DialoGPT-medium"]

    def test_dedupes_variants_sharing_a_base_repo(self) -> None:
        node = MockNode()
        param = HuggingFaceRepoParameter(
            node,
            repo_ids=[
                "Lightricks/LTX-2::ltx-2-19b-dev",
                "Lightricks/LTX-2::ltx-2-19b-dev-fp8",
                "dg845/LTX-2.3-Diffusers",
            ],
        )

        # The two `Lightricks/LTX-2::*` entries resolve to the same base repo and must collapse.
        assert param.get_download_models() == ["Lightricks/LTX-2", "dg845/LTX-2.3-Diffusers"]

    def test_deprecated_only_yields_empty_list(self) -> None:
        node = MockNode()
        param = HuggingFaceRepoParameter(
            node,
            repo_ids=[],
            deprecated_repo_ids=["Lightricks/LTX-2::ltx-2-19b-dev"],
        )

        assert param.get_download_models() == []


class TestGetDownloadCommands:
    @pytest.fixture(autouse=True)
    def _stub_cache_scan(self) -> Iterator[None]:
        with patch(
            "griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter.list_repo_revisions_in_cache",
            return_value=[],
        ):
            yield

    def test_commands_use_base_repo_not_postfixed_name(self) -> None:
        node = MockNode()
        param = HuggingFaceRepoParameter(
            node,
            repo_ids=[
                "Lightricks/LTX-2::ltx-2-19b-dev",
                "Lightricks/LTX-2::ltx-2-19b-dev-fp8",
            ],
        )

        # A `huggingface-cli download` call with `Lightricks/LTX-2::ltx-2-19b-dev` would fail —
        # the postfix is not a valid HF repo ID. Commands must reference the base repo only.
        assert param.get_download_commands() == ['huggingface-cli download "Lightricks/LTX-2"']

    def test_commands_omit_deprecated_repos(self) -> None:
        node = MockNode()
        param = HuggingFaceRepoParameter(
            node,
            repo_ids=["microsoft/DialoGPT-medium"],
            deprecated_repo_ids=["microsoft/DialoGPT-small"],
        )

        assert param.get_download_commands() == ['huggingface-cli download "microsoft/DialoGPT-medium"']


class TestDeprecation:
    @pytest.fixture(autouse=True)
    def _stub_cache_scan(self) -> Iterator[None]:
        with patch(
            "griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter.list_repo_revisions_in_cache",
            return_value=[],
        ):
            yield

    def test_is_deprecated_matches_by_exact_id(self) -> None:
        node = MockNode()
        param = HuggingFaceRepoParameter(
            node,
            repo_ids=["microsoft/DialoGPT-medium"],
            deprecated_repo_ids=["microsoft/DialoGPT-small"],
        )

        assert param._is_deprecated("microsoft/DialoGPT-small") is True
        assert param._is_deprecated("microsoft/DialoGPT-medium") is False
        assert param._is_deprecated("unknown/repo") is False

    def test_deprecated_repos_are_appended_to_repo_ids(self) -> None:
        # Deprecated repos still live in `_repo_ids` so a workflow that already selected one
        # can continue to show it in the dropdown via refresh_parameters filtering logic.
        node = MockNode()
        param = HuggingFaceRepoParameter(
            node,
            repo_ids=["microsoft/DialoGPT-medium"],
            deprecated_repo_ids=["microsoft/DialoGPT-small"],
        )

        assert param._repo_ids == ["microsoft/DialoGPT-medium", "microsoft/DialoGPT-small"]

    def test_deprecated_postfixed_repo_is_filtered_before_dedup(self) -> None:
        # Regression guard: if deprecation filtering ran after `::` stripping, a deprecated
        # `Lightricks/LTX-2::foo` would mark the base `Lightricks/LTX-2` as "seen" and hide
        # live variants. The implementation must filter deprecation first.
        node = MockNode()
        param = HuggingFaceRepoParameter(
            node,
            repo_ids=["Lightricks/LTX-2::ltx-2-19b-dev"],
            deprecated_repo_ids=["Lightricks/LTX-2::ltx-2-19b-dev-fp4"],
        )

        assert param.get_download_models() == ["Lightricks/LTX-2"]

    def test_empty_deprecated_list_defaults_to_no_deprecations(self) -> None:
        node = MockNode()
        param = HuggingFaceRepoParameter(node, repo_ids=["microsoft/DialoGPT-medium"])

        assert param._deprecated_repos == []
        assert param._is_deprecated("microsoft/DialoGPT-medium") is False
