from __future__ import annotations

from typing import Any, ClassVar

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

    # Resolve a saved trait_name back to its class. Walking __subclasses__
    # finds any trait whose module has been imported, which covers the traits a loaded
    # library brought in. Two libraries shipping the same class name are indistinguishable
    # here; keying registration by library would fix that.
    @classmethod
    def resolve(cls, trait_name: str) -> type[Trait] | None:
        def walk(klass: type[Trait]) -> type[Trait] | None:
            for subclass in klass.__subclasses__():
                if subclass.__name__ == trait_name:
                    return subclass
                found = walk(subclass)
                if found is not None:
                    return found
            return None

        return walk(Trait)

    @classmethod
    def traits_from_states(cls, states: list[dict[str, Any]]) -> list[Trait]:
        """Rebuild traits from serialized state, skipping any whose class cannot be found."""
        traits: list[Trait] = []
        for state in states:
            trait_name = state.get("trait_name")
            trait_class = cls.resolve(trait_name) if trait_name is not None else None
            if trait_class is None:
                continue
            traits.append(trait_class.from_state(state.get("trait_state", {})))
        return traits
