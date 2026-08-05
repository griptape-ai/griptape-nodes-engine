from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Trait


# This should probably register upon creation
class TraitRegistry:
    # I'm going to create a dictionary that stores all of the created traits we have so far?
    # Traits will be associated with certain key words
    key_to_trait: ClassVar[dict[str, list[Trait.__class__]]] = {}

    @classmethod
    def create_traits(cls, key_word: str) -> list[Trait] | None:
        if key_word not in cls.key_to_trait:
            return None
        values = cls.key_to_trait[key_word]
        return [trait() for trait in values]

    @classmethod
    def register_trait(cls, trait: Trait) -> None:
        key_words = trait.get_trait_keys()
        for key in key_words:
            if key in cls.key_to_trait:
                cls.key_to_trait[key].append(trait.__class__)
            else:
                cls.key_to_trait[key] = [trait.__class__]

    @classmethod
    def register_trait_from_json(cls) -> None:
        pass
