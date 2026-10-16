"""Unit tests for the centralized background-music state machine."""

import pygame
import pytest

from veilbound.client.audio import (
    MUSIC_EXIT,
    MUSIC_GAME,
    MUSIC_LOADING,
    MUSIC_LOBBY,
    MUSIC_OPTIONS,
    AudioManager,
)

MUSIC_FILES = (
    "Intro_Lobby_music.mp3",
    "Game_Music.mp3",
    "Loading_into_match_music.mp3",
    "Options_Menu_Music.mp3",
    "Bye_Bye.mp3",
)


class FakeMusic:
    """Records pygame.mixer.music calls without touching real audio."""

    def __init__(self):
        self.loaded = []
        self.plays = []
        self.fadeouts = []
        self.stops = 0
        self.volume = 1.0

    def load(self, path):
        self.loaded.append(path)

    def play(self, loops=-1):
        self.plays.append(loops)

    def fadeout(self, ms):
        self.fadeouts.append(ms)

    def stop(self):
        self.stops += 1

    def set_volume(self, volume):
        self.volume = volume

    def get_busy(self):
        # Nothing is audible in the fake, so fades never sleep in tests.
        return False


@pytest.fixture()
def fake_music(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(pygame.mixer.music, "load", fake.load)
    monkeypatch.setattr(pygame.mixer.music, "play", fake.play)
    monkeypatch.setattr(pygame.mixer.music, "fadeout", fake.fadeout)
    monkeypatch.setattr(pygame.mixer.music, "stop", fake.stop)
    monkeypatch.setattr(pygame.mixer.music, "set_volume", fake.set_volume)
    monkeypatch.setattr(pygame.mixer.music, "get_busy", fake.get_busy)
    monkeypatch.setattr(pygame.mixer, "get_init", lambda: (44100, -16, 2))
    return fake


def make_project(tmp_path):
    music_dir = tmp_path / "assets" / "sounds" / "Background_music"
    music_dir.mkdir(parents=True)
    for name in MUSIC_FILES:
        (music_dir / name).write_bytes(b"mp3")
    return tmp_path


def make_settings(**overrides):
    config = {"enabled": True, "volume": 0.4, "loop": True}
    config.update(overrides)
    return {"background_music": config}


def test_startup_and_lobby_share_intro_track_without_restarts(fake_music, tmp_path):
    audio = AudioManager(make_settings(), make_project(tmp_path))
    assert audio.current_music == MUSIC_LOBBY
    assert audio.music_volume == 0.4
    assert fake_music.loaded[0].endswith("Intro_Lobby_music.mp3")

    # Lobby navigation re-affirms the same track and must never restart it.
    audio.play_music(MUSIC_LOBBY)
    audio.play_music(MUSIC_LOBBY)
    assert len(fake_music.loaded) == 1


def test_game_music_replaces_lobby_and_stops_cleanly(fake_music, tmp_path):
    audio = AudioManager(make_settings(), make_project(tmp_path))
    assert audio.current_music == MUSIC_LOBBY

    audio.play_music(MUSIC_GAME)
    assert audio.current_music == MUSIC_GAME
    assert fake_music.loaded[-1].endswith("Game_Music.mp3")

    # Re-issuing the same key during gameplay must not restart the song.
    audio.play_music(MUSIC_GAME)
    assert len(fake_music.loaded) == 2

    audio.stop_music()
    assert audio.current_music is None
    assert fake_music.stops >= 1


def test_lobby_settings_sound_overrides_and_falls_back_when_missing(fake_music, tmp_path):
    root = make_project(tmp_path)
    configured = "assets/sounds/Background_music/Loading_into_match_music.mp3"
    audio = AudioManager(make_settings(sound=configured), root)
    assert audio.current_music == MUSIC_LOBBY
    assert fake_music.loaded[0].endswith("Loading_into_match_music.mp3")

    fallback = AudioManager(make_settings(sound="missing_track.mp3"), root)
    assert fallback.current_music == MUSIC_LOBBY
    assert fake_music.loaded[1].endswith("Intro_Lobby_music.mp3")


def test_missing_track_keeps_game_running(fake_music, tmp_path):
    # tmp_path contains no music files at all.
    audio = AudioManager(make_settings(), tmp_path)
    assert audio.current_music is None

    audio.play_music(MUSIC_LOADING)
    assert audio.current_music is None

    # Missing exit file: must not raise, block, or claim playback started.
    loads_before = len(fake_music.loaded)
    audio.play_exit_music()
    assert len(fake_music.loaded) == loads_before
    assert audio.exit_music_active is True


def test_options_music_during_gameplay_restores_game_music(fake_music, tmp_path):
    audio = AudioManager(make_settings(), make_project(tmp_path))
    audio.play_music(MUSIC_GAME)
    loads_before = len(fake_music.loaded)

    audio.enter_options()
    assert audio.current_music == MUSIC_OPTIONS
    assert len(fake_music.loaded) == loads_before + 1

    # Repeated per-frame calls must not restart the options track.
    audio.enter_options()
    assert len(fake_music.loaded) == loads_before + 1

    # Closing options resumes game music cleanly (restart, never underneath).
    audio.leave_options()
    assert audio.current_music == MUSIC_GAME
    assert fake_music.loaded[-1].endswith("Game_Music.mp3")


def test_leaving_options_from_silence_stops_music(fake_music, tmp_path):
    audio = AudioManager(make_settings(), make_project(tmp_path))
    audio.stop_music()

    audio.enter_options()
    assert audio.current_music == MUSIC_OPTIONS
    audio.leave_options()
    assert audio.current_music is None


def test_exit_music_plays_once_even_with_repeated_inputs(fake_music, tmp_path):
    audio = AudioManager(make_settings(), make_project(tmp_path))
    audio.play_music(MUSIC_LOBBY)

    audio.play_exit_music()
    assert fake_music.loaded[-1].endswith("Bye_Bye.mp3")
    assert fake_music.plays[-1] == 0  # exit track must not loop
    loads = len(fake_music.loaded)

    audio.play_exit_music()  # repeated exit input must not restart the track
    assert len(fake_music.loaded) == loads


def test_disabled_music_never_plays(fake_music, tmp_path):
    audio = AudioManager(make_settings(enabled=False), make_project(tmp_path))
    assert audio.current_music is None
    assert fake_music.loaded == []

    audio.play_music(MUSIC_LOBBY)
    audio.play_exit_music()
    assert fake_music.loaded == []
