"""Image behaviour contracts specified alongside procedural sources."""

from collections.abc import Callable
from pathlib import Path

import pytest

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.artifact_events import (
    RegisterArtifactProviderRequest,
    RegisterArtifactProviderResultSuccess,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import ImageArtifactProvider

try:
    from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import (
        OpenImageIOThumbnailGenerator as ThumbnailGenerator,
    )
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO renderer tests require the optional dependency", allow_module_level=True)
from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_assertions import (
    assert_image,
)
from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_specifications import (
    ExpandedCase,
    expand_cases,
    load_thumbnail_specifications,
)

SPECIFICATION = load_thumbnail_specifications()
CASES = expand_cases(SPECIFICATION)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
async def test_thumbnail_contract(
    engine: Engine,
    tmp_path: Path,
    case: ExpandedCase,
    image_source: Callable[[str, str], Path],
) -> None:
    # Given a generated input with verified native storage and metadata.
    """Test thumbnail contract."""
    source = image_source(case.image.id, case.variant.id)
    result = engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
    assert isinstance(result, RegisterArtifactProviderResultSuccess)
    engine.config_manager.set_config_value("auto_inject_workflow_metadata", case.case.inject_metadata)
    engine.config_manager.set_config_value("preserve_source_metadata_in_thumbnails", case.case.preserve_source_metadata)
    destination = tmp_path / "thumbnails"
    generator = ThumbnailGenerator(
        str(source),
        case.output.format,
        str(destination),
        f"preview.{case.output.format}",
        case.case.parameters.model_dump(),
        engine=engine,
    )

    # When the public generator renders and publishes the thumbnail.
    if case.case.workflow_name is not None:
        entry = engine.workflow_registry.register_unsaved(
            case.case.workflow_name,
            working_directory=str(tmp_path),
            workflow_id="0" * 32,
        )
        workflow = engine.open_workflow(entry.workflow_id)
        with engine.bind_workflow(workflow):
            filename = await generator.attempt_generate_preview()
    else:
        filename = await generator.attempt_generate_preview()

    # Then its encoded representation meets explicit expectations.
    assert_image(destination / filename, case.output.expect, label=case.id)
