"""Small OpenCV-backed Pygame video player for in-game cutscenes."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pygame

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - optional at runtime, required by requirements.txt
    cv2 = None

logger = logging.getLogger("veilbound.video_player")


class VideoPlayer:
    """Decode a video incrementally and present it on a Pygame surface."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.capture = None
        self.frame: pygame.Surface | None = None
        self.finished = False
        self.frame_delay = 1 / 30
        self.next_frame_at = 0.0
        self._size: tuple[int, int] | None = None
        self._scaled_frame: pygame.Surface | None = None
        self.error: str | None = None
        if cv2 is None:
            self._fail("OpenCV is unavailable; the cutscene cannot be decoded.")
            return
        if not path.is_file():
            self._fail(f"Cutscene not found: {path}")
            return
        try:
            self.capture = cv2.VideoCapture(str(path))
            if not self.capture.isOpened():
                self._fail(f"Could not open cutscene: {path}")
                return
            fps = float(self.capture.get(cv2.CAP_PROP_FPS) or 0.0)
            if fps > 0:
                self.frame_delay = 1 / fps
            self._read_frame()
            self.next_frame_at = time.monotonic() + self.frame_delay
        except Exception as exc:  # pragma: no cover - codec dependent
            self._fail(f"Could not initialize cutscene: {exc}")

    def _fail(self, message: str) -> None:
        self.error = message
        self.finished = True
        logger.warning(message)
        self.close()

    def _read_frame(self) -> None:
        if self.capture is None or cv2 is None:
            self.finished = True
            return
        try:
            ok, frame = self.capture.read()
            if not ok:
                self.finished = True
                self.close()
                return
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            height, width = frame.shape[:2]
            self.frame = pygame.image.frombuffer(
                frame.tobytes(), (width, height), "RGB",
            ).copy()
            self._size = None
            self._scaled_frame = None
        except Exception as exc:  # pragma: no cover - codec dependent
            self._fail(f"Cutscene decode failed: {exc}")

    def update(self, now: float) -> None:
        if self.finished or now < self.next_frame_at:
            return
        self._read_frame()
        self.next_frame_at = now + self.frame_delay

    def draw(self, screen: pygame.Surface) -> None:
        if self.frame is None:
            screen.fill((7, 8, 18))
            return
        size = screen.get_size()
        if self._size != size:
            source_w, source_h = self.frame.get_size()
            scale = max(size[0] / source_w, size[1] / source_h)
            scaled_size = (
                max(size[0], round(source_w * scale)),
                max(size[1], round(source_h * scale)),
            )
            scaled = pygame.transform.smoothscale(self.frame, scaled_size)
            left = (scaled_size[0] - size[0]) // 2
            top = (scaled_size[1] - size[1]) // 2
            self._scaled_frame = scaled.subsurface(
                pygame.Rect(left, top, size[0], size[1]),
            ).copy()
            self._size = size
        if self._scaled_frame is not None:
            screen.blit(self._scaled_frame, (0, 0))

    def close(self) -> None:
        if self.capture is not None:
            try:
                self.capture.release()
            except Exception:  # pragma: no cover - codec dependent
                pass
            self.capture = None
