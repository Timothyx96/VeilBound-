from pathlib import Path

import pygame

from veilbound.client.assets import AssetLoader
from veilbound.shared.definitions import load_card_definitions


def test_card_image_path_is_resolved_from_project_root():
    definitions = load_card_definitions()
    loader = AssetLoader()
    definition = definitions["naruto_01"]
    assert loader.resolve(definition.image) == loader.root / definition.image


def test_missing_character_image_uses_fallback_without_crashing(tmp_path):
    loader = AssetLoader(tmp_path)
    image = loader.load_character("assets/art/characters/missing.png")
    assert image is loader.fallback_character
    assert image.get_size() == (256, 320)


def test_loaded_character_art_is_cached_and_aspect_ratio_is_preserved(tmp_path, monkeypatch):
    loader = AssetLoader(tmp_path)
    relative = "assets/art/characters/test.png"
    path = loader.resolve(relative)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"test image placeholder")
    source = pygame.Surface((100, 50), pygame.SRCALPHA)
    calls = []

    def fake_load(load_path):
        calls.append(Path(load_path))
        return source

    monkeypatch.setattr(pygame.image, "load", fake_load)
    loaded = loader.load_character(relative)
    again = loader.load_character(relative)
    fitted = loader.scale_to_fit(loaded, (40, 40))
    assert loaded.get_size() == (100, 50)
    assert again is loaded
    assert len(calls) == 1
    assert fitted.get_size() == (40, 20)

