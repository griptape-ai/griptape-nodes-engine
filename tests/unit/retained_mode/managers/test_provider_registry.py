"""Tests for ProviderRegistry."""

import pytest

from griptape_nodes.retained_mode.managers.artifact_providers import (
    BaseArtifactProvider,
    ImageArtifactProvider,
    ProviderRegistry,
)


class TestProviderRegistry:
    """Test ProviderRegistry functionality."""

    def test_init_creates_empty_registry(self) -> None:
        """Test that initialization creates empty registry collections."""
        registry = ProviderRegistry()

        assert isinstance(registry._provider_classes, list)
        assert len(registry._provider_classes) == 0
        assert isinstance(registry._provider_instances, dict)
        assert len(registry._provider_instances) == 0
        assert isinstance(registry._file_format_to_provider_class, dict)
        assert len(registry._file_format_to_provider_class) == 0
        assert isinstance(registry._friendly_name_to_provider_class, dict)
        assert len(registry._friendly_name_to_provider_class) == 0

    def test_register_provider_success(self) -> None:
        """Test successful registration of a provider."""

        class TestProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Test"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"test", "tst"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"jpg"}

            @classmethod
            def get_default_preview_generator(cls) -> str:
                return "Default"

            @classmethod
            def get_default_preview_format(cls) -> str:
                return "jpg"

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        registry = ProviderRegistry()
        registry.register_provider(TestProvider)

        assert len(registry._provider_classes) == 1
        assert TestProvider in registry._provider_classes
        assert "test" in registry._file_format_to_provider_class
        assert "tst" in registry._file_format_to_provider_class
        assert TestProvider in registry._file_format_to_provider_class["test"]
        assert TestProvider in registry._file_format_to_provider_class["tst"]
        assert "test" in registry._friendly_name_to_provider_class
        assert registry._friendly_name_to_provider_class["test"] is TestProvider

    def test_register_provider_duplicate_friendly_name_raises(self) -> None:  # noqa: C901
        """Test that registering a provider with duplicate friendly name raises ValueError."""

        class Provider1(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Test"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"test1"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"jpg"}

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        class Provider2(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Test"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"test2"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"png"}

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        registry = ProviderRegistry()
        registry.register_provider(Provider1)

        with pytest.raises(ValueError, match="duplicate friendly name"):
            registry.register_provider(Provider2)

    def test_register_provider_case_insensitive_duplicate_raises(self) -> None:  # noqa: C901
        """Test that friendly name duplicate detection is case-insensitive."""

        class UpperProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "TEST"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"test1"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"jpg"}

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        class LowerProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "test"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"test2"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"png"}

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        registry = ProviderRegistry()
        registry.register_provider(UpperProvider)

        with pytest.raises(ValueError, match="duplicate friendly name"):
            registry.register_provider(LowerProvider)

    def test_get_provider_class_by_friendly_name_case_insensitive(self) -> None:
        """Test that get_provider_class_by_friendly_name is case-insensitive."""
        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)

        provider_class_lower = registry.get_provider_class_by_friendly_name("image")
        provider_class_title = registry.get_provider_class_by_friendly_name("Image")
        provider_class_upper = registry.get_provider_class_by_friendly_name("IMAGE")

        assert provider_class_lower is not None
        assert provider_class_title is not None
        assert provider_class_upper is not None
        assert provider_class_lower is provider_class_title
        assert provider_class_title is provider_class_upper
        assert provider_class_lower is ImageArtifactProvider

    def test_get_provider_class_by_friendly_name_not_found_returns_none(self) -> None:
        """Test that get_provider_class_by_friendly_name returns None if not found."""
        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)

        provider_class = registry.get_provider_class_by_friendly_name("Video")

        assert provider_class is None

    def test_get_provider_classes_by_format(self) -> None:
        """Test that get_provider_classes_by_format returns correct providers."""
        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)

        providers_jpg = registry.get_provider_classes_by_format("jpg")
        providers_png = registry.get_provider_classes_by_format("png")

        assert len(providers_jpg) == 1
        assert ImageArtifactProvider in providers_jpg
        assert len(providers_png) == 1
        assert ImageArtifactProvider in providers_png

    def test_get_provider_classes_by_format_not_found_returns_empty(self) -> None:
        """Test that get_provider_classes_by_format returns empty list if not found."""
        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)

        providers = registry.get_provider_classes_by_format("mp4")

        assert providers == []

    def test_multiple_providers_can_handle_same_format(self) -> None:
        """Test that multiple provider classes can be registered for the same format."""

        class AlternateImageProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "AlternateImage"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"jpg", "png"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"webp"}

            @classmethod
            def get_default_preview_generator(cls) -> str:
                return "Default"

            @classmethod
            def get_default_preview_format(cls) -> str:
                return "webp"

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)
        registry.register_provider(AlternateImageProvider)

        providers_jpg = registry.get_provider_classes_by_format("jpg")
        providers_png = registry.get_provider_classes_by_format("png")

        assert len(providers_jpg) == 2  # noqa: PLR2004
        assert ImageArtifactProvider in providers_jpg
        assert AlternateImageProvider in providers_jpg
        assert len(providers_png) == 2  # noqa: PLR2004
        assert ImageArtifactProvider in providers_png
        assert AlternateImageProvider in providers_png

    def test_lazy_instantiation_creates_singleton(self) -> None:
        """Test that get_or_create_provider_instance creates and caches singleton."""
        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)

        assert len(registry._provider_instances) == 0

        instance1 = registry.get_or_create_provider_instance(ImageArtifactProvider)
        assert isinstance(instance1, ImageArtifactProvider)
        assert len(registry._provider_instances) == 1

        instance2 = registry.get_or_create_provider_instance(ImageArtifactProvider)
        assert instance2 is instance1
        assert len(registry._provider_instances) == 1

    def test_lazy_instantiation_caches_instance(self) -> None:  # noqa: C901
        """Test that provider instances are cached per class."""

        class TestProvider1(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Test1"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"test1"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"jpg"}

            @classmethod
            def get_default_preview_generator(cls) -> str:
                return "Default"

            @classmethod
            def get_default_preview_format(cls) -> str:
                return "jpg"

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        class TestProvider2(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Test2"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"test2"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"png"}

            @classmethod
            def get_default_preview_generator(cls) -> str:
                return "Default"

            @classmethod
            def get_default_preview_format(cls) -> str:
                return "png"

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        registry = ProviderRegistry()
        registry.register_provider(TestProvider1)
        registry.register_provider(TestProvider2)

        instance1 = registry.get_or_create_provider_instance(TestProvider1)
        instance2 = registry.get_or_create_provider_instance(TestProvider2)

        assert instance1 is not instance2
        assert isinstance(instance1, TestProvider1)
        assert isinstance(instance2, TestProvider2)
        assert len(registry._provider_instances) == 2  # noqa: PLR2004

    def test_get_all_provider_classes(self) -> None:
        """Test that get_all_provider_classes returns all registered providers."""

        class TestProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Test"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"test"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"jpg"}

            @classmethod
            def get_default_preview_generator(cls) -> str:
                return "Default"

            @classmethod
            def get_default_preview_format(cls) -> str:
                return "jpg"

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)
        registry.register_provider(TestProvider)

        all_providers = registry.get_all_provider_classes()

        assert len(all_providers) == 2  # noqa: PLR2004
        assert ImageArtifactProvider in all_providers
        assert TestProvider in all_providers

    def test_get_provider_config_schema(self) -> None:
        """Test that get_provider_config_schema generates correct config keys."""
        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)

        config_schema = registry.get_provider_config_schema(ImageArtifactProvider)

        assert "artifacts.image.preview_generation.preview_format" in config_schema
        assert "artifacts.image.preview_generation.preview_generator" in config_schema
        assert config_schema["artifacts.image.preview_generation.preview_format"] == "webp"
        assert config_schema["artifacts.image.preview_generation.preview_generator"] == "Standard Thumbnail Generation"

    def test_unregister_provider_not_registered_is_ignored(self) -> None:
        """Test that unregistering a provider class that was never registered leaves the registry unchanged."""
        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)

        registry.unregister_provider(BaseArtifactProvider)

        assert registry.get_all_provider_classes() == [ImageArtifactProvider]
        assert registry.get_provider_classes_by_format("png") == [ImageArtifactProvider]
        assert registry.get_provider_class_by_friendly_name("Image") is ImageArtifactProvider

    def test_unregister_provider_keeps_other_providers_for_shared_format(self) -> None:
        """Test that unregistering a provider keeps other providers registered for the same format."""

        class AlternateImageProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "AlternateImage"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"png"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return set()

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

        registry = ProviderRegistry()
        registry.register_provider(ImageArtifactProvider)
        registry.register_provider(AlternateImageProvider)

        registry.unregister_provider(AlternateImageProvider)

        assert registry.get_all_provider_classes() == [ImageArtifactProvider]
        assert registry.get_provider_classes_by_format("png") == [ImageArtifactProvider]
        assert registry.get_provider_class_by_friendly_name("AlternateImage") is None
