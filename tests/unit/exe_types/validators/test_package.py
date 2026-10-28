"""The validators are importable from the package itself."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

import griptape_nodes.exe_types.validators as validators_package
from griptape_nodes.exe_types.validators import base, common, media

if TYPE_CHECKING:
    from types import ModuleType


def test_exports_every_public_validator_and_the_types_they_use() -> None:
    """Every `media` and `common` validator, and the base types, are the same objects from the package."""
    source: dict[str, ModuleType] = dict.fromkeys(media.__all__, media)
    source.update(dict.fromkeys(common.__all__, common))
    source.update(
        dict.fromkeys(("EMPTY_MEDIA", "Condition", "MediaInfo", "MediaNeed", "PreRunValidator", "reads"), base)
    )
    assert set(validators_package.__all__) == set(source)
    for name, module in source.items():
        assert getattr(validators_package, name) is getattr(module, name)


def test_refuses_a_name_it_does_not_export() -> None:
    """A name the package doesn't export raises AttributeError, as for any module."""
    with pytest.raises(AttributeError, match="NotAValidator"):
        validators_package.NotAValidator  # noqa: B018  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize(
    "first_import",
    [
        "from griptape_nodes.exe_types.validators import AllowedFormats, MaxFileSize, DependsOn",
        "from griptape_nodes.exe_types.validators import *",
        "import griptape_nodes.exe_types.core_types",
        "import griptape_nodes.exe_types.input_checks",
    ],
    ids=["named", "star", "core-types-first", "input-checks-first"],
)
def test_imports_without_a_cycle_in_a_fresh_process(first_import: str) -> None:
    """The short imports work whichever module a process imports first."""
    # A circular import only shows when nothing else has been imported yet, so this needs its own process.
    code = f"{first_import}\nfrom griptape_nodes.exe_types.validators import AllowedFormats, DependsOn, MaxFileSize"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60)  # noqa: S603
    assert result.returncode == 0, result.stderr
