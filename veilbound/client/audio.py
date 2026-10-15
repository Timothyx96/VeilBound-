from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pygame

from .settings import PROJECT_ROOT
from veilbound.shared.models import GameEvent


class AudioManager:
    """Loads configured sounds once and plays audio only for unique game events."""

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

        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init()
            self.mixer_ready = pygame.mixer.get_init() is not None
        except pygame.error:
            return
        if not self.mixer_ready:
            return

        self._start_background_music()

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

    def _start_background_music(self) -> None:
        music = self.settings.get("background_music", {})
        if not isinstance(music, dict) or not music.get("enabled", False):
            return
        relative_path = music.get("sound")
        if not isinstance(relative_path, str) or not relative_path.strip():
            return
        try:
            path = (self.root / relative_path).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                return
            volume = float(music.get("volume", 0.35))
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.set_volume(min(1.0, max(0.0, volume)))
            pygame.mixer.music.play(-1 if music.get("loop", True) else 0)
        except (OSError, pygame.error, TypeError, ValueError):
            pass

    def reset_match(self) -> None:
        """Allow identical row IDs to trigger sounds again in a restarted match."""
        self.played_events.clear()

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
                self._play_reveal_once(event_id)
            elif event.kind == "placement":
                event_id = f"{event.data.get('row_id', '')}:{event.data.get('slot_index', '')}"
                self.play_card_place(event_id)
            elif event.kind == "next_row":
                self._play_once(("next_row", str(event.data.get("row_id", ""))), self.next_row_sound)
            elif event.kind == "row_scored":
                self._play_once(("counter_sfx", str(event.data.get("row_id", ""))), self.counter_sound)

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
        self._play(self.reveal_slots[index])

    def play_card_place(self, event_id: str) -> None:
        """Play the configured sound once for this accepted card placement."""
        key = ("card_place", event_id)
        settings = self.settings.get("card_place", {})
        if isinstance(settings, dict) and settings.get("enabled", False):
            self._play_once(key, self.card_place_sound)

    def _play_once(self, key: tuple[str, str], sound: pygame.mixer.Sound | None) -> None:
        if key in self.played_events:
            return
        self.played_events.add(key)
        self._play(sound)

    @staticmethod
    def _play(sound: pygame.mixer.Sound | None) -> None:
        if sound is None:
            return
        try:
            sound.play()
        except pygame.error:
            pass
