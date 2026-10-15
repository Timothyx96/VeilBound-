from __future__ import annotations

from pathlib import Path

import pygame


class AssetLoader:
    """Centralized image loading with a generated character-art fallback."""

    def __init__(self, project_root: Path | None = None):
        self.root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        self.fallback_character = self._make_fallback()
        self._cache: dict[str, pygame.Surface | None] = {}

    def resolve(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Asset paths must stay within the project root")
        return path

    def load_character(self, relative_path: str) -> pygame.Surface:
        if relative_path in self._cache:
            return self._cache[relative_path] or self.fallback_character
        image = self.load_image(relative_path)
        return image or self.fallback_character

    def load_image(self, relative_path: str | None) -> pygame.Surface | None:
        if not isinstance(relative_path, str) or not relative_path.strip():
            return None
        if relative_path in self._cache:
            return self._cache[relative_path]
        try:
            path = self.resolve(relative_path)
            if not path.is_file():
                self._cache[relative_path] = None
                return None
            image = pygame.image.load(str(path))
            if pygame.display.get_surface() is not None:
                image = image.convert_alpha()
            else:
                image = image.copy()
        except (OSError, pygame.error, ValueError):
            image = None
        self._cache[relative_path] = image
        return image

    @staticmethod
    def scale_to_fit(image: pygame.Surface, bounds: tuple[int, int]) -> pygame.Surface:
        max_width, max_height = bounds
        if max_width < 1 or max_height < 1:
            raise ValueError("Image bounds must be positive")
        source_width, source_height = image.get_size()
        scale = min(max_width / source_width, max_height / source_height)
        width = max(1, round(source_width * scale))
        height = max(1, round(source_height * scale))
        return pygame.transform.smoothscale(image, (width, height))

    @staticmethod
    def _make_fallback() -> pygame.Surface:
        image = pygame.Surface((256, 320), pygame.SRCALPHA)
        image.fill((31, 42, 66))
        pygame.draw.rect(image, (68, 83, 111), (12, 12, 232, 296), border_radius=18)
        pygame.draw.rect(image, (193, 165, 117), (21, 21, 214, 278), width=3, border_radius=15)
        pygame.draw.circle(image, (222, 205, 168), (128, 112), 48)
        pygame.draw.polygon(image, (105, 117, 145), [(48, 268), (69, 184), (128, 155), (187, 184), (208, 268)])
        pygame.draw.polygon(image, (31, 42, 66), [(87, 101), (100, 62), (141, 69), (170, 106), (143, 88), (113, 91)])
        pygame.draw.circle(image, (31, 42, 66), (112, 116), 4)
        pygame.draw.circle(image, (31, 42, 66), (145, 116), 4)
        return image

