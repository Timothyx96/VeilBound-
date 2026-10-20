import json

import pygame

from veilbound.client.audio import AudioManager
from veilbound.client.settings import PROJECT_ROOT, load_settings
from veilbound.shared.models import GameEvent


def test_project_settings_use_real_assets_and_respect_preset_floors():
    settings = load_settings()
    assert settings.animation_preset in {"Slow", "Normal", "Fast"}
    assert settings.animation_ms["card_flip_ms"] >= 300
    assert settings.animation_ms["delay_between_reveals_ms"] >= 200
    assert settings.animation_ms["card_information_ms"] >= 600
    assert settings.animation_ms["row_result_ms"] >= 900
    assert settings.animation_ms["row_transition_ms"] >= 250
    assert settings.animation_preset == load_settings().animation_preset == "Normal"

    reveal = settings.audio["card_reveal"]
    assert len(reveal["sounds"]) == 7
    assert all((PROJECT_ROOT / path).is_file() for path in reveal["sounds"])
    assert (PROJECT_ROOT / settings.audio["next_row"]["sound"]).is_file()
    assert settings.audio["card_place"]["enabled"] is True
    assert (PROJECT_ROOT / settings.audio["card_place"]["sound"]).is_file()
    assert settings.background_path
    assert (PROJECT_ROOT / settings.background_path).is_file()


def test_animation_preset_and_values_can_be_changed_in_json(tmp_path):
    config = tmp_path / "settings.json"
    config.write_text(json.dumps({
        "animation": {
            "preset": "Fast",
            "presets": {"Fast": {"delay_between_reveals_ms": 123}},
        },
        "background": {"selected": "missing", "presets": {"safe": "assets/bg.png"}},
    }), encoding="utf-8")

    settings = load_settings(config)
    assert settings.animation_preset == "Fast"
    assert settings.animation_ms["delay_between_reveals_ms"] == 123
    assert settings.animation_ms["card_flip_ms"] == 300
    assert settings.background_name == "safe"
    assert settings.background_path == "assets/bg.png"


class FakeSound:
    def __init__(self, path):
        self.path = path
        self.play_count = 0

    def play(self):
        self.play_count += 1


class FixedIndex:
    def __init__(self, index):
        self.index = index
        self.seen_size = None

    def randrange(self, size):
        self.seen_size = size
        return self.index


def test_audio_events_play_one_uniform_reveal_and_one_next_row_sound(monkeypatch):
    pygame.mixer.quit()
    monkeypatch.setattr(pygame.mixer, "get_init", lambda: (44100, -16, 2))
    loaded = []

    def fake_sound(path):
        sound = FakeSound(path)
        loaded.append(sound)
        return sound

    monkeypatch.setattr(pygame.mixer, "Sound", fake_sound)
    settings = load_settings()
    audio = AudioManager(settings.audio)
    audio.rng = FixedIndex(6)

    sounds_by_path = {sound.path: sound for sound in loaded}
    next_row_sound = sounds_by_path[str(PROJECT_ROOT / settings.audio["next_row"]["sound"])]
    card_place_sound = sounds_by_path[str(PROJECT_ROOT / settings.audio["card_place"]["sound"])]

    reveal = GameEvent("card_revealed", "revealed", {"card_id": "card-1"})
    next_row = GameEvent("next_row", "next row", {"row_id": "row-2"})
    audio.on_events((reveal, reveal, next_row, next_row))

    assert audio.rng.seen_size == 7
    assert loaded[6].play_count == 1
    assert next_row_sound.play_count == 1
    assert sum(sound.play_count for sound in loaded) == 2
    audio.play_card_place("placement-1")
    assert card_place_sound.play_count == 1
    assert sum(sound.play_count for sound in loaded) == 3
    audio.reset_match()
    audio.on_events((next_row,))
    assert next_row_sound.play_count == 2


def test_reveal_audio_plays_once_per_distinct_card_reveal(monkeypatch, tmp_path):
    pygame.mixer.quit()
    monkeypatch.setattr(pygame.mixer, "get_init", lambda: (44100, -16, 2))
    loaded = []

    def fake_sound(path):
        sound = FakeSound(path)
        loaded.append(sound)
        return sound

    monkeypatch.setattr(pygame.mixer, "Sound", fake_sound)
    sound_paths = ["reveal-a.mp3", "reveal-b.mp3"]
    for path in sound_paths:
        (tmp_path / path).write_bytes(b"sound")
    audio = AudioManager({
        "card_reveal": {"enabled": True, "random_selection": True, "sounds": sound_paths},
    }, tmp_path)
    audio.rng = FixedIndex(1)
    first = GameEvent("card_revealed", "revealed", {"card_id": "card-1"})
    second = GameEvent("card_revealed", "revealed", {"card_id": "card-2"})

    audio.on_events((first, second, first))

    assert audio.rng.seen_size == 2
    assert [sound.play_count for sound in loaded] == [0, 2]


def test_ability_sfx_plays_only_on_successful_resolution_once(monkeypatch):
    pygame.mixer.quit()
    monkeypatch.setattr(pygame.mixer, "get_init", lambda: (44100, -16, 2))
    loaded = []

    def fake_sound(path):
        sound = FakeSound(path)
        loaded.append(sound)
        return sound

    monkeypatch.setattr(pygame.mixer, "Sound", fake_sound)
    audio = AudioManager(load_settings().audio)
    preloaded_count = len(loaded)
    sound_path = "assets/sounds/Abillity_Sfx/Ability_Resolution/Killua.mp3"
    activated = GameEvent("ability_activated", "started", {
        "ability_id": "killua_godspeed_eliminate",
        "owner_card_id": "killua-card",
        "sound": sound_path,
    })
    failed = GameEvent("elimination_failed", "no valid target", {})
    resolved = GameEvent("ability_resolved", "eliminated", {
        "resolution_id": "42",
        "ability_id": "killua_godspeed_eliminate",
        "owner_card_id": "killua-card",
        "sound": sound_path,
    })

    audio.on_events((activated, failed))
    assert len(loaded) == preloaded_count
    audio.on_events((resolved, resolved))
    ability_sounds = [sound for sound in loaded if sound.path.endswith("Killua.mp3")]
    assert len(ability_sounds) == 1
    ability_sound = ability_sounds[0]
    assert ability_sound.play_count == 1


def test_gon_ability_sfx_is_silent_on_failure_and_plays_on_resolution(monkeypatch):
    pygame.mixer.quit()
    monkeypatch.setattr(pygame.mixer, "get_init", lambda: (44100, -16, 2))
    loaded = []

    def fake_sound(path):
        sound = FakeSound(path)
        loaded.append(sound)
        return sound

    monkeypatch.setattr(pygame.mixer, "Sound", fake_sound)
    audio = AudioManager(load_settings().audio)
    sound_path = "assets/sounds/Abillity_Sfx/Ability_Resolution/Gon.mp3"
    failed = GameEvent("ability_failed", "effect did not resolve", {
        "resolution_id": "gon-failed",
        "ability_id": "gon_jajanken_rock_ultimate_transformation",
        "owner_card_id": "gon-card",
        "sound": sound_path,
    })
    resolved = GameEvent("ability_resolved", "effects applied", {
        "resolution_id": "gon-success",
        "ability_id": "gon_jajanken_rock_ultimate_transformation",
        "owner_card_id": "gon-card",
        "sound": sound_path,
    })

    audio.on_events((failed,))
    assert not [sound for sound in loaded if sound.path.endswith("Gon.mp3")]

    audio.on_events((resolved, resolved))
    gon_sounds = [sound for sound in loaded if sound.path.endswith("Gon.mp3")]
    assert len(gon_sounds) == 1
    assert gon_sounds[0].play_count == 1


def test_missing_sound_or_unavailable_mixer_is_safe(monkeypatch, tmp_path):
    pygame.mixer.quit()
    monkeypatch.setattr(pygame.mixer, "get_init", lambda: None)
    monkeypatch.setattr(pygame.mixer, "init", lambda: (_ for _ in ()).throw(pygame.error("no audio device")))
    audio = AudioManager({"card_reveal": {"enabled": True, "sounds": ["missing.mp3"]}}, tmp_path)
    audio.on_events((GameEvent("card_revealed", "revealed", {"card_id": "card-2"}),))
    assert audio.reveal_slots == []
