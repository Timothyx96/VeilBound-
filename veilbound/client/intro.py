"""Pre-match intro video playback for VeilBound.

Plays the "Game Found" cinematic video with a "GAME FOUND" text overlay
before transitioning into the match board. Uses OpenCV for video decoding;
if OpenCV is unavailable, the intro is skipped gracefully so the match
still starts.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pygame

from veilbound.client.theme import BG
from veilbound.client.typography import render_text

logger = logging.getLogger("veilbound.intro")

INTRO_VIDEO_PATH = "assets/backgrounds/PixVerse_V6_Fusion_720P_Create_a_cinematic_ani (1)-clean.mp4"
GAME_FOUND_TEXT = "GAME FOUND"
TEXT_FADE_IN_SECONDS = 1.0
TEXT_BOTTOM_PADDING = 60

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None


class VideoIntro:
    """Plays an intro video with a "GAME FOUND" overlay before entering a match."""

    def __init__(self, project_root: Path, screen_size: tuple[int, int]) -> None:
        self.screen_width, self.screen_height = screen_size
        self.project_root = project_root
        self.video_path = project_root / INTRO_VIDEO_PATH
        self.cap: cv2.VideoCapture | None = None
        self.current_frame: pygame.Surface | None = None
        self.video_rect: pygame.Rect | None = None
        self.done = False
        self.error: str | None = None
        self._start_time: float | None = None
        self._last_frame_time: float = 0.0
        self._frame_delay = 1.0 / 30.0
        self._text_alpha = 0
        self._current_surface: pygame.Surface | None = None
        self._init_video()

    def _init_video(self) -> None:
        try:
            if cv2 is None:
                self.error = "OpenCV (cv2) is not installed; skipping intro video."
                logger.warning(self.error)
                self.done = True
                return
            if not self.video_path.is_file():
                self.error = f"Intro video not found: {self.video_path}"
                logger.warning(self.error)
                self.done = True
                return
            self.cap = cv2.VideoCapture(str(self.video_path))
            if not self.cap.isOpened():
                self.error = f"Could not open intro video: {self.video_path}"
                logger.warning(self.error)
                self.done = True
                return
            fps = self.cap.get(cv2.CAP_PROP_FPS)
            if fps and fps > 0:
                self._frame_delay = 1.0 / fps
            self._start_time = time.monotonic()
            self._last_frame_time = self._start_time
            self._read_next_frame()
        except Exception as exc:
            self.error = f"Intro video error: {exc}"
            logger.warning(self.error)
            self.done = True

    def _read_next_frame(self) -> None:
        if self.cap is None:
            return
        ret, frame = self.cap.read()
        if not ret:
            self.done = True
            self._cleanup()
            return
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width = frame.shape[:2]
        surface = pygame.image.frombuffer(frame.tobytes(), (width, height), "RGB")
        self._current_surface = surface
        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        if self._current_surface is None:
            return
        source_w, source_h = self._current_surface.get_size()
        scale = min(self.screen_width / source_w, self.screen_height / source_h)
        new_w = max(1, int(source_w * scale))
        new_h = max(1, int(source_h * scale))
        scaled = pygame.transform.smoothscale(self._current_surface, (new_w, new_h))
        self.current_frame = scaled
        self.video_rect = scaled.get_rect(center=(self.screen_width // 2, self.screen_height // 2))

    def _cleanup(self) -> None:
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def update(self) -> None:
        if self.done or self.error:
            return
        now = time.monotonic()
        if self._start_time is not None:
            elapsed = now - self._start_time
            self._text_alpha = min(255, int(255 * elapsed / TEXT_FADE_IN_SECONDS))
        if now - self._last_frame_time >= self._frame_delay:
            self._read_next_frame()
            self._last_frame_time = now

    def draw(self, screen: pygame.Surface) -> None:
        if self.current_frame is not None and self.video_rect is not None:
            screen.blit(self.current_frame, self.video_rect)
        else:
            screen.fill(BG)
        if self._text_alpha > 0:
            self._draw_text_overlay(screen)

    def _draw_text_overlay(self, screen: pygame.Surface) -> None:
        text_surface = render_text(GAME_FOUND_TEXT, "title", shadow=True)
        if self._text_alpha < 255:
            text_surface = text_surface.convert_alpha()
            alpha_layer = pygame.Surface(text_surface.get_size(), pygame.SRCALPHA)
            alpha_layer.fill((255, 255, 255, self._text_alpha))
            text_surface.blit(alpha_layer, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        text_rect = text_surface.get_rect(centerx=self.screen_width // 2)
        text_rect.bottom = self.screen_height - TEXT_BOTTOM_PADDING

        backing = pygame.Surface((text_rect.width + 40, text_rect.height + 20), pygame.SRCALPHA)
        backing.fill((0, 0, 0, min(120, self._text_alpha // 2)))
        backing_rect = backing.get_rect(center=text_rect.center)
        screen.blit(backing, backing_rect)

        screen.blit(text_surface, text_rect)

    def is_done(self) -> bool:
        return self.done

    def cleanup(self) -> None:
        self._cleanup()


def run_intro(
    intro: VideoIntro,
    screen: pygame.Surface,
    clock: pygame.time.Clock,
) -> bool:
    """Run the intro video loop.

    Returns False if the user quit the application, True when the intro
    completed (or was skipped) and the match should proceed.
    """
    running = True
    while running and not intro.is_done():
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_SPACE, pygame.K_RETURN):
                    running = False
            display = _toggle_display_intro(event, screen)
            if display is not None:
                screen, _, _ = display
                intro.screen_width, intro.screen_height = screen.get_size()
        intro.update()
        screen = pygame.display.get_surface() or screen
        intro.draw(screen)
        pygame.display.flip()
        clock.tick(60)

    intro.cleanup()
    return True


def _toggle_display_intro(event, screen):
    from veilbound.client.app import toggle_display

    return toggle_display(event, screen, screen.get_size(), False)