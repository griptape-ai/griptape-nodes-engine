"""The validators are importable from the package itself."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

import griptape_nodes.exe_types.validators as validators_package
import griptape_nodes.exe_types.validators._checks as validators_checks
import griptape_nodes.exe_types.validators.base as validators_base
import griptape_nodes.exe_types.validators.common as validators_common
import griptape_nodes.exe_types.validators.context as validators_context
import griptape_nodes.exe_types.validators.media as validators_media
import griptape_nodes.exe_types.validators.media_validator as validators_media_validator

if TYPE_CHECKING:
    from types import ModuleType


def test_exports_every_public_validator_and_the_types_they_use() -> None:
    """Every `media` and `common` validator, the base types and `limit_error` are the same objects from the package."""
    source: dict[str, ModuleType] = dict.fromkeys(validators_media.__all__, validators_media)
    source.update(dict.fromkeys(validators_common.__all__, validators_common))
    source.update(
        dict.fromkeys(
            ("LIMITS_LINK_LABEL", "Condition", "IfParameter", "PreRunRule", "PreRunValidator"), validators_base
        )
    )
    source["CheckContext"] = validators_context
    source["MediaValidator"] = validators_media_validator
    source["limit_error"] = validators_checks
    assert set(validators_package.__all__) == set(source)
    for name, module in source.items():
        assert getattr(validators_package, name) is getattr(module, name)


@pytest.mark.parametrize(
    "first_import",
    [
        "from griptape_nodes.exe_types.validators import AllowedFormats, MaxFileSize, IfParameter",
        "from griptape_nodes.exe_types.validators import *",
        "import griptape_nodes.exe_types.core_types",
        "import griptape_nodes.utils.media_probe",
        "import griptape_nodes.exe_types.validators.runner",
    ],
    ids=["named", "star", "core-types-first", "input-checks-first", "runner-first"],
)
def test_imports_without_a_cycle_in_a_fresh_process(first_import: str) -> None:
    """The short imports work whichever module a process imports first."""
    # A circular import only shows when nothing else has been imported yet, so this needs its own process.
    code = f"{first_import}\nfrom griptape_nodes.exe_types.validators import AllowedFormats, IfParameter, MaxFileSize"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60)  # noqa: S603
    assert result.returncode == 0, result.stderr


def test_importing_the_package_does_not_load_the_runner() -> None:
    """The runner imports `core_types`, which imports the package, so the package must not import the runner."""
    code = (
        "import sys\n"
        "import griptape_nodes.exe_types.validators\n"
        "assert 'griptape_nodes.exe_types.validators.runner' not in sys.modules"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60)  # noqa: S603
    assert result.returncode == 0, result.stderr


def test_the_condition_alias_resolves_at_runtime() -> None:
    """Reading `Condition`'s value, as `get_type_hints` or a docs build does, finds every name it uses."""
    code = (
        "import typing\n"
        "from griptape_nodes.exe_types.validators import Condition, PreRunRule\n"
        "typing.get_type_hints(PreRunRule.when, include_extras=True)\n"
        "print(Condition.__value__)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60)  # noqa: S603
    assert result.returncode == 0, result.stderr
