"""Sound effects plus a centralized background-music state manager.

Only one music track plays at a time; see ``AudioManager.play_music``.
"""

from __future__ import annotations

import logging
import io
import math
import random
import struct
import time
import wave
from array import array
from pathlib import Path
from typing import Any

import pygame

from .settings import PROJECT_ROOT
from veilbound.shared.models import GameEvent

logger = logging.getLogger("veilbound.audio")

# Music track keys referenced by the game's screen/state flow.
MUSIC_LOBBY = "lobby"  # game startup + main lobby
MUSIC_GAME = "game"  # active match gameplay
MUSIC_LOADING = "loading"  # match loading / intro-video transition
MUSIC_OPTIONS = "options"  # in-game options (pause) menu
MUSIC_EXIT = "exit"  # shutdown

# Project-relative file for each music track (existing files, never renamed).
MUSIC_TRACKS: dict[str, str] = {
    MUSIC_LOBBY: "assets/sounds/Background_music/Intro_Lobby_New.mp3",
    MUSIC_GAME: "assets/sounds/Background_music/Game_Music_New.mp3",
    MUSIC_LOADING: "assets/sounds/Background_music/Loading_into_match_music.mp3",
    MUSIC_OPTIONS: "assets/sounds/Background_music/Options_Menu_Music.mp3",
    MUSIC_EXIT: "assets/sounds/Background_music/Bye_Bye.mp3",
}

# Crossfade time in milliseconds when switching or stopping tracks.
FADE_MS = 300


class AudioManager:
    """Loads configured sounds once and plays audio only for unique game events.

    Also acts as the centralized music manager: one background track plays at
    a time, and ``play_music`` only restarts playback when the track actually
    changes, so screen updates never re-trigger the current song.
    """

    def __init__(self, settings: dict[str, Any], project_root: Path | None = None):
        self.settings = settings if isinstance(settings, dict) else {}
        self.root = (project_root or PROJECT_ROOT).resolve()
        self.rng = random.Random()
        self.played_events: set[tuple[str, str]] = set()
        self.mixer_ready = False
        self.reveal_slots: list[pygame.mixer.Sound | None] = []
        self.next_row_sound: pygame.mixer.Sound | None = None
        self.card_place_sound: pygame.mixer.Sound | None = None
        self.counter_sound: pygame.mixer.Sound | None = None
        self.crazy_slots_spin_sound: pygame.mixer.Sound | None = None
        self.crazy_slots_tick_sounds: list[pygame.mixer.Sound] = []
        self.crazy_slots_chime_sound: pygame.mixer.Sound | None = None
        self._crazy_slots_last_tick: tuple[str, float] | None = None
        self.ability_sounds: dict[str, pygame.mixer.Sound | None] = {}
        self.character_reveal_sounds: dict[str, pygame.mixer.Sound | None] = {}
        self.luffy_haki_sounds: tuple[tuple[pygame.mixer.Sound | None, float], ...] = ()
        self.luffy_laugh_sound: pygame.mixer.Sound | None = None
        self.all_might_reveal_sound: pygame.mixer.Sound | None = None
        self.sfx_volume = 0.8
        # Music state: the current track, the track to restore after the
        # options screen, and whether exit music has started (blocks repeated
        # exit inputs from restarting the track).
        self.music_enabled = False
        self.music_volume = 0.35
        self.current_music: str | None = None
        self.previous_music: str | None = None
        self.exit_music_active = False
        music_cfg = self.settings.get("background_music", {})
        if isinstance(music_cfg, dict):
            self.music_enabled = bool(music_cfg.get("enabled", False))
            try:
                self.music_volume = min(1.0, max(0.0, float(music_cfg.get("volume", 0.35))))
            except (TypeError, ValueError):
                self.music_volume = 0.35

        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init()
            self.mixer_ready = pygame.mixer.get_init() is not None
        except pygame.error:
            return
        if not self.mixer_ready:
            return

        # Purpose-built native PCM layers: there is no Conqueror's Haki asset
        # in the project. The only Luffy file is a laugh, so it is not reused
        # as a misleading combat impact.
        self.luffy_haki_sounds = (
            (self._synthesize_haki_layer("impact", 0.58, 1701), 0.95),
            (self._synthesize_haki_layer("burst", 0.48, 1702), 0.72),
            (self._synthesize_haki_layer("rumble", 1.12, 1703), 0.42),
        )
        self.luffy_laugh_sound = self._amplify_sound(
            self._load_sound("assets/sounds/Abillity_Sfx/Ability_Resolution/Luffy_laugh.mp3"),
            gain=2.0,
        )
        self.all_might_reveal_sound = self._load_sound(
            "assets/sounds/Abillity_Sfx/Ability_Resolution/Allmight_sfx.mp3"
        )
        self.character_reveal_sounds = {
            "nico robin": self._load_sound(
                "assets/sounds/Abillity_Sfx/Ability_Resolution/Robin_Reveal.mp3"
            ),
            "trafalgar d. water law": self._load_sound(
                "assets/sounds/Abillity_Sfx/Ability_Resolution/Law_on_reveal.mp3"
            ),
        }

        self._start_initial_music()

        reveal = self.settings.get("card_reveal", {})
        if isinstance(reveal, dict) and reveal.get("enabled", False):
            paths = reveal.get("sounds", [])
            if isinstance(paths, list):
                self.reveal_slots = [self._load_sound(item) for item in paths]

        next_row = self.settings.get("next_row", {})
        if isinstance(next_row, dict) and next_row.get("enabled", False):
            self.next_row_sound = self._load_sound(next_row.get("sound"))

        card_place = self.settings.get("card_place", {})
        if isinstance(card_place, dict) and card_place.get("enabled", False):
            self.card_place_sound = self._load_sound(card_place.get("sound"))

        counter_sfx = self.settings.get("counter_sfx", {})
        if isinstance(counter_sfx, dict) and counter_sfx.get("enabled", False):
            self.counter_sound = self._load_sound(counter_sfx.get("sound"))

        # Kite's signature spin voice/SFX plays once when the authoritative
        # result begins animating; the existing counter sound marks the landing.
        self.crazy_slots_spin_sound = self._load_sound(
            "assets/sounds/Abillity_Sfx/Ability_Resolution/Kite_Spin.mp3"
        )
        self.crazy_slots_tick_sounds = [
            self._synthesize_tone((frequency,), 0.024)
            for frequency in (300, 360, 430, 510, 600, 700, 800, 900)
        ]
        self.crazy_slots_chime_sound = self._synthesize_tone(
            (523, 659, 784, 1047), 0.62, arpeggio=True,
        )

    def _start_initial_music(self) -> None:
        """Begin the startup/lobby music the moment the game launches."""
        self.play_music(MUSIC_LOBBY)

    def _music_cfg(self) -> dict[str, Any]:
        music = self.settings.get("background_music", {})
        return music if isinstance(music, dict) else {}

    def _resolve_path(self, relative_path: str) -> Path | None:
        """Resolve a project-relative path, or None when the file is missing."""
        try:
            path = (self.root / relative_path).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                return None
            return path
        except (OSError, ValueError):
            return None

    def _resolve_music_path(self, key: str) -> Path | None:
        relative_path = MUSIC_TRACKS.get(key)
        if key == MUSIC_LOBBY:
            # Honor the configured lobby path when it points at a real file.
            configured = self._music_cfg().get("sound")
            if isinstance(configured, str) and configured.strip():
                candidate = self._resolve_path(configured)
                if candidate is not None:
                    return candidate
        if not isinstance(relative_path, str):
            return None
        return self._resolve_path(relative_path)

    def _fade_out_current(self) -> None:
        """Fade the current track out; only waits while a track is audible."""
        try:
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.fadeout(FADE_MS)
                pygame.time.wait(FADE_MS)
        except pygame.error:
            pass

    def play_music(self, key: str) -> None:
        """Play a background music track; a no-op while it is already playing.

        Only one track plays at a time: the current track fades out before the
        new one starts, so screen changes never overlap music.
        """
        if not self.mixer_ready or not self.music_enabled:
            return
        if key == self.current_music:
            return
        self._fade_out_current()
        path = self._resolve_music_path(key)
        if path is None:
            logger.warning("Music track not found for %s", key)
            self.current_music = None
            return
        try:
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.set_volume(self.music_volume)
            pygame.mixer.music.play(-1)
            self.current_music = key
        except (OSError, pygame.error, TypeError, ValueError) as exc:
            logger.warning("Failed to play music track %s: %s", key, exc)
            self.current_music = None

    def stop_music(self) -> None:
        """Fade out and stop the current track, if any."""
        if not self.mixer_ready:
            return
        self._fade_out_current()
        try:
            pygame.mixer.music.stop()
        except pygame.error:
            pass
        self.current_music = None

    def enter_options(self) -> None:
        """Play options music, remembering the track to restore on exit."""
        if not self.mixer_ready or self.current_music == MUSIC_OPTIONS:
            return
        self.previous_music = self.current_music
        self.play_music(MUSIC_OPTIONS)

    def leave_options(self) -> None:
        """Restore the track that was playing before the options screen."""
        if self.current_music != MUSIC_OPTIONS:
            return
        previous, self.previous_music = self.previous_music, None
        if previous is not None and previous != MUSIC_OPTIONS:
            self.play_music(previous)
        else:
            self.stop_music()

    def play_exit_music(self) -> None:
        """Play exit music once, keeping the app alive until it finishes.

        ``exit_music_active`` blocks re-entry so repeated exit inputs cannot
        restart the track. Window events are pumped during playback so the
        application stays responsive; missing files or playback failures are
        logged and the caller shuts down safely.
        """
        if not self.mixer_ready or not self.music_enabled or self.exit_music_active:
            return
        self.exit_music_active = True
        self.stop_music()
        path = self._resolve_music_path(MUSIC_EXIT)
        if path is None:
            logger.warning("Exit music not found; shutting down without it.")
            return
        try:
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.set_volume(self.music_volume)
            pygame.mixer.music.play(0)
        except (OSError, pygame.error, TypeError, ValueError) as exc:
            logger.warning("Failed to play exit music: %s", exc)
            return
        try:
            if pygame.mixer.music.get_busy():
                clock = pygame.time.Clock()
                while pygame.mixer.music.get_busy():
                    pygame.event.pump()
                    clock.tick(30)
        except pygame.error as exc:
            logger.warning("Exit music playback interrupted: %s", exc)

    def reset_match(self) -> None:
        """Allow identical row IDs to trigger sounds again in a restarted match."""
        self.played_events.clear()
        self._crazy_slots_last_tick = None

    def set_music_volume(self, volume: float) -> None:
        """Set background music volume (0.0 to 1.0) and apply it immediately."""
        self.music_volume = min(1.0, max(0.0, float(volume)))
        try:
            pygame.mixer.music.set_volume(self.music_volume)
        except pygame.error:
            pass

    def set_sfx_volume(self, volume: float) -> None:
        """Set SFX volume (0.0 to 1.0); applied to each sound when it plays."""
        self.sfx_volume = min(1.0, max(0.0, float(volume)))

    def _load_sound(self, relative_path: Any) -> pygame.mixer.Sound | None:
        if not isinstance(relative_path, str) or not relative_path.strip():
            return None
        try:
            path = (self.root / relative_path).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                return None
            return pygame.mixer.Sound(str(path))
        except (OSError, pygame.error, ValueError):
            return None

    def on_events(self, events: tuple[GameEvent, ...]) -> None:
        for event in events:
            if event.kind == "card_revealed":
                event_id = str(event.data.get("card_id", ""))
                character_name = str(event.data.get("name", "")).strip().casefold()
                character_sound = self.character_reveal_sounds.get(character_name)
                if event_id and character_sound is not None:
                    self._play_once(("character_reveal", event_id), character_sound)
                self._play_reveal_once(event_id)
            elif event.kind == "placement":
                event_id = f"{event.data.get('row_id', '')}:{event.data.get('slot_index', '')}"
                self.play_card_place(event_id)
            elif event.kind == "next_row":
                self._play_once(("next_row", str(event.data.get("row_id", ""))), self.next_row_sound)
            elif event.kind == "row_scored":
                self._play_once(("counter_sfx", str(event.data.get("row_id", ""))), self.counter_sound)
            elif event.kind == "crazy_slots_spin_started":
                spin_id = str(event.data.get("spin_id", ""))
                if spin_id:
                    self._play_once(("crazy_slots_spin", spin_id), self.crazy_slots_spin_sound)
            elif (event.kind == "ability_activated"
                    and str(event.data.get("ability_id", "")) == "luffy_warrior_of_liberation_eliminate"):
                owner_id = str(event.data.get("owner_card_id", ""))
                if owner_id:
                    self._play_luffy_haki_once(owner_id)
            elif (event.kind == "ability_activated"
                    and str(event.data.get("ability_id", "")) == "mha_04_symbol_of_peace"):
                owner_id = str(event.data.get("owner_card_id", ""))
                if owner_id:
                    self._play_once(("all_might_reveal", owner_id), self.all_might_reveal_sound)
            elif event.kind == "ability_resolved":
                sound_path = event.data.get("sound")
                if not isinstance(sound_path, str) or not sound_path.strip():
                    continue
                resolution_id = str(event.data.get("resolution_id", ""))
                if not resolution_id:
                    resolution_id = ":".join((
                        str(event.data.get("ability_id", "")),
                        str(event.data.get("owner_card_id", "")),
                        str(event.data.get("row_id", "")),
                        str(event.data.get("slot_index", "")),
                    ))
                if sound_path not in self.ability_sounds:
                    self.ability_sounds[sound_path] = self._load_sound(sound_path)
                self._play_once(
                    ("ability_sfx", resolution_id),
                    self.ability_sounds[sound_path],
                    volume_multiplier=(
                        0.5 if sound_path.replace("\\", "/").casefold().endswith("/roronoa_zoro.mp3")
                        else 1.0
                    ),
                )

    def play_crazy_slots_tick(self, spin_id: str, digit: int, speed: float) -> None:
        """Play one throttled reel tick, lowering its pitch as the reel slows."""
        now = time.monotonic()
        if self._crazy_slots_last_tick is not None:
            last_spin, last_time = self._crazy_slots_last_tick
            if last_spin == spin_id and now - last_time < 0.028:
                return
        self._crazy_slots_last_tick = (spin_id, now)
        if not self.mixer_ready or self.sfx_volume <= 0:
            return
        if not self.crazy_slots_tick_sounds:
            return
        speed_ratio = min(1.0, max(0.0, float(speed) / 26.0))
        pitch_index = min(
            len(self.crazy_slots_tick_sounds) - 1,
            max(0, round(speed_ratio * (len(self.crazy_slots_tick_sounds) - 1))),
        )
        digit_tweak = (max(1, int(digit)) - 1) % 3
        sound = self.crazy_slots_tick_sounds[min(
            len(self.crazy_slots_tick_sounds) - 1, pitch_index + (digit_tweak if speed_ratio > 0.45 else 0),
        )]
        self._play(sound, self.sfx_volume * 0.62)

    def play_crazy_slots_landing(self, spin_id: str) -> None:
        if spin_id:
            self._play_once(("crazy_slots_chime", spin_id), self.crazy_slots_chime_sound)

    def _play_luffy_haki_once(self, owner_card_id: str) -> None:
        key = ("luffy_haki_reveal", owner_card_id)
        if key in self.played_events:
            return
        self.played_events.add(key)
        if not self.mixer_ready or self.sfx_volume <= 0:
            return
        self._play(self.luffy_laugh_sound, self.sfx_volume * 0.78)
        for sound, gain in self.luffy_haki_sounds:
            self._play(sound, self.sfx_volume * gain)

    @staticmethod
    def _synthesize_haki_layer(kind: str, duration: float, seed: int) -> pygame.mixer.Sound | None:
        """Synthesize one short, distinct impact/energy/rumble layer in PCM."""
        if pygame.mixer.get_init() is None:
            return None
        sample_rate = 22050
        sample_count = max(1, int(sample_rate * duration))
        rng = random.Random(seed)
        buffer = io.BytesIO()
        phase = 0.0
        filtered_noise = 0.0
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            pcm = bytearray()
            for index in range(sample_count):
                t = index / sample_rate
                progress = index / sample_count
                noise = rng.uniform(-1.0, 1.0)
                if kind == "impact":
                    frequency = 39.0 + 77.0 * math.exp(-t * 5.4)
                    phase += math.tau * frequency / sample_rate
                    body = (math.sin(phase) * math.exp(-t * 5.1)
                            + 0.24 * math.sin(phase * 2.03) * math.exp(-t * 8.5))
                    transient = noise * math.exp(-t * 76.0) * 0.72
                    value = math.tanh((body * 0.9 + transient) * 1.55) * 0.82
                elif kind == "burst":
                    filtered_noise = filtered_noise * 0.72 + noise * 0.28
                    frequency = 175.0 + 475.0 * math.exp(-t * 4.0)
                    phase += math.tau * frequency / sample_rate
                    envelope = max(0.0, 1.0 - progress) ** 1.9
                    value = (0.62 * filtered_noise + 0.35 * math.sin(phase)) * envelope * 0.72
                else:  # restrained sub-bass rumble under the transient
                    frequency = 38.0 + 4.0 * math.sin(math.tau * 2.8 * t)
                    phase += math.tau * frequency / sample_rate
                    attack = min(1.0, t / 0.045)
                    envelope = attack * math.exp(-t * 2.7)
                    modulation = 0.78 + 0.22 * math.sin(math.tau * 3.4 * t)
                    value = (math.sin(phase) + 0.20 * math.sin(phase * 1.51)) * envelope * modulation * 0.52
                sample = int(max(-1.0, min(1.0, value)) * 32767)
                pcm.extend(struct.pack("<h", sample))
            output.writeframes(pcm)
        buffer.seek(0)
        try:
            return pygame.mixer.Sound(file=buffer)
        except pygame.error:
            return None

    @staticmethod
    def _amplify_sound(sound: pygame.mixer.Sound | None, gain: float) -> pygame.mixer.Sound | None:
        """Raise a clip's average level with a soft limiter for loud peaks."""
        mixer_format = pygame.mixer.get_init()
        if sound is None or mixer_format is None or mixer_format[1] != -16:
            return sound
        samples = array("h")
        try:
            samples.frombytes(sound.get_raw())
            limiter = math.tanh(gain)
            for index, sample in enumerate(samples):
                normalized = sample / 32768.0
                amplified = math.tanh(normalized * gain) / limiter
                samples[index] = max(-32768, min(32767, round(amplified * 32768)))
            return pygame.mixer.Sound(buffer=samples.tobytes())
        except (pygame.error, OverflowError, ValueError):
            return sound

    @staticmethod
    def _synthesize_tone(
        frequencies: tuple[int, ...], duration: float, *, arpeggio: bool = False,
    ) -> pygame.mixer.Sound | None:
        """Build a short PCM sound in memory for Crazy Slots reel feedback."""
        if pygame.mixer.get_init() is None:
            return None
        sample_rate = 22050
        sample_count = max(1, int(sample_rate * duration))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            pcm = bytearray()
            for sample_index in range(sample_count):
                t = sample_index / sample_rate
                envelope = (1 - sample_index / sample_count) ** (2.2 if not arpeggio else 1.15)
                if arpeggio:
                    note_duration = duration / len(frequencies)
                    note_index = min(len(frequencies) - 1, int(t / note_duration))
                    active_frequency = frequencies[note_index]
                    local_t = t - note_index * note_duration
                    tone = math.sin(2 * math.pi * active_frequency * local_t)
                else:
                    tone = math.sin(2 * math.pi * frequencies[0] * t)
                value = int(max(-1.0, min(1.0, tone * envelope * 0.30)) * 32767)
                pcm.extend(struct.pack("<h", value))
            output.writeframes(pcm)
        buffer.seek(0)
        try:
            return pygame.mixer.Sound(file=buffer)
        except pygame.error:
            return None

    def _play_reveal_once(self, event_id: str) -> None:
        settings = self.settings.get("card_reveal", {})
        if not isinstance(settings, dict) or not settings.get("enabled", False) or not self.reveal_slots:
            return
        key = ("card_revealed", event_id)
        if key in self.played_events:
            return
        self.played_events.add(key)

        if settings.get("random_selection", True):
            index = self.rng.randrange(len(self.reveal_slots))
        else:
            index = 0
        self._play(self.reveal_slots[index], self.sfx_volume)

    def play_card_place(self, event_id: str) -> None:
        """Play the configured sound once for this accepted card placement."""
        key = ("card_place", event_id)
        settings = self.settings.get("card_place", {})
        if isinstance(settings, dict) and settings.get("enabled", False):
            self._play_once(key, self.card_place_sound)

    def _play_once(
        self,
        key: tuple[str, str],
        sound: pygame.mixer.Sound | None,
        *,
        volume_multiplier: float = 1.0,
    ) -> None:
        if key in self.played_events:
            return
        self.played_events.add(key)
        self._play(sound, self.sfx_volume * volume_multiplier)

    @staticmethod
    def _play(sound: pygame.mixer.Sound | None, volume: float = 1.0) -> None:
        if sound is None:
            return
        try:
            sound.set_volume(min(1.0, max(0.0, volume)))
        except (pygame.error, AttributeError):
            pass
        try:
            sound.play()
        except pygame.error:
            pass
