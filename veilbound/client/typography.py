"""Central font registry and text layout helpers for the Pygame client."""

from __future__ import annotations

import logging
from pathlib import Path

import pygame

from .theme import (
    TEXT_ABILITY_DARK,
    TEXT_ABILITY_PARCHMENT,
    TEXT_FLAVOR,
    TEXT_IVORY,
    TEXT_MUTED_GOLD,
    TEXT_PARCHMENT_INK,
    TEXT_SHADOW,
    TEXT_TITLE_GOLD,
    TEXT_WHITE,
)


MIN_FONT_SIZE = 12
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FONT_DIRECTORY = PROJECT_ROOT / "assets" / "fonts"

FONTS = {
    "title": {"file": "Cinzel-Bold.otf", "size": 44, "color": TEXT_TITLE_GOLD},
    "card_name": {
        "file": "Cinzel-Bold.otf",
        "size": 16,
        "color": TEXT_IVORY,
        "variants": {"parchment": TEXT_PARCHMENT_INK, "black": (0, 0, 0)},
    },
    "number_card": {"file": "OSWALD.otf", "size": 28, "color": TEXT_WHITE},
    "number_row_score": {"file": "OSWALD.otf", "size": 56, "color": TEXT_TITLE_GOLD},
    "number_match": {"file": "OSWALD.otf", "size": 20, "color": TEXT_IVORY},
    # Row capacity is a compact label number rather than a score.
    "number_capacity": {"file": "OSWALD.otf", "size": 32, "color": TEXT_TITLE_GOLD},
    "ability_title": {
        "file": "BarlowCondensed-Bold.ttf",
        "size": 14,
        "color": TEXT_TITLE_GOLD,
        "uppercase": True,
    },
    "ability_desc": {
        "file": "BarlowCondensed-Medium.ttf",
        "size": 20,
        "color": TEXT_ABILITY_DARK,
        "variants": {"parchment": TEXT_ABILITY_PARCHMENT},
    },
    "ui": {"file": "BarlowCondensed-Medium.ttf", "size": 22, "color": TEXT_IVORY},
    "status_banner": {"file": "BarlowCondensed-Medium.ttf", "size": 26, "color": TEXT_IVORY},
    "label_small": {
        "file": "BarlowCondensed-Regular.ttf",
        "size": 13,
        "color": TEXT_MUTED_GOLD,
        "uppercase": True,
    },
    "row_label": {
        "file": "BarlowCondensed-Regular.ttf",
        "size": 16,
        "color": TEXT_MUTED_GOLD,
        "uppercase": True,
    },
    "flavor": {"file": "CormorantGaramond-Italic.ttf", "size": 15, "color": TEXT_FLAVOR},
}

_FONT_CACHE: dict[tuple[str, int], pygame.font.Font] = {}
_WARNED_FONT_FILES: set[str] = set()
_LOGGER = logging.getLogger(__name__)


def get_font(role: str, size: int | None = None) -> pygame.font.Font:
    """Return the cached font for a role and size, falling back safely if needed."""
    spec = FONTS[role]
    font_size = max(MIN_FONT_SIZE, int(size if size is not None else spec["size"]))
    filename = spec["file"]
    key = (filename, font_size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    if not pygame.font.get_init():
        pygame.font.init()
    path = (FONT_DIRECTORY / filename).resolve()
    try:
        font = pygame.font.Font(str(path), font_size)
    except (OSError, pygame.error) as exc:
        if filename not in _WARNED_FONT_FILES:
            _LOGGER.warning(
                "Could not load font file '%s' at '%s' (%s); using Pygame's default font.",
                filename,
                path,
                exc,
            )
            _WARNED_FONT_FILES.add(filename)
        font = pygame.font.Font(None, font_size)
    _FONT_CACHE[key] = font
    return font


def preload_fonts() -> None:
    """Prime every size used by fitting before the game enters its draw loop."""
    for role, spec in FONTS.items():
        for size in range(MIN_FONT_SIZE, spec["size"] + 1):
            get_font(role, size)


def _format_text(text: str, role: str) -> str:
    value = str(text)
    return value.upper() if FONTS[role].get("uppercase", False) else value


def _measure(font: pygame.font.Font, text: str, spacing: int = 0) -> int:
    if spacing > 0:
        return sum(font.size(character)[0] for character in text) + max(0, len(text) - 1) * spacing
    return font.size(text)[0]


def _truncate(text: str, font: pygame.font.Font, max_width: int, spacing: int = 0) -> str:
    ellipsis = "..."
    if _measure(font, ellipsis, spacing) > max_width:
        return ""
    prefix = text
    while prefix and _measure(font, prefix + ellipsis, spacing) > max_width:
        prefix = prefix[:-1]
    return prefix.rstrip() + ellipsis


def _fit_text(text: str, role: str, max_width: int | None, spacing: int = 0) -> tuple[str, pygame.font.Font]:
    spec = FONTS[role]
    base_size = spec["size"]
    if max_width is None:
        return text, get_font(role, base_size)

    available = max(0, int(max_width))
    for size in range(base_size, MIN_FONT_SIZE - 1, -1):
        font = get_font(role, size)
        if _measure(font, text, spacing) <= available:
            return text, font

    font = get_font(role, MIN_FONT_SIZE)
    return _truncate(text, font, available, spacing), font


def _text_color(role: str, variant: str | None) -> tuple[int, int, int]:
    spec = FONTS[role]
    if variant is not None:
        return spec.get("variants", {}).get(variant, spec["color"])
    return spec["color"]


def _make_surface(size: tuple[int, int]) -> pygame.Surface:
    surface = pygame.Surface(size, pygame.SRCALPHA)
    if pygame.display.get_surface() is not None:
        return surface.convert_alpha()
    return surface


def render_text(
    text: str,
    role: str,
    max_width: int | None = None,
    *,
    variant: str | None = None,
    shadow: bool = False,
) -> pygame.Surface:
    """Render a role-styled line, shrinking and then ellipsizing it if needed."""
    fit_width = None if max_width is None else max_width - (1 if shadow else 0)
    value, font = _fit_text(_format_text(text, role), role, fit_width)
    color = _text_color(role, variant)
    rendered = font.render(value, True, color)
    if not shadow:
        return rendered

    shadow_surface = font.render(value, True, TEXT_SHADOW)
    result = _make_surface((rendered.get_width() + 1, rendered.get_height() + 1))
    result.blit(shadow_surface, (1, 1))
    result.blit(rendered, (0, 0))
    return result


def render_text_with_spacing(
    text: str,
    role: str,
    spacing: int = 1,
    max_width: int | None = None,
    *,
    variant: str | None = None,
    shadow: bool = False,
) -> pygame.Surface:
    """Render individually spaced glyphs, used for uppercase ability titles."""
    fit_width = None if max_width is None else max_width - (1 if shadow else 0)
    value, font = _fit_text(_format_text(text, role), role, fit_width, spacing)
    color = _text_color(role, variant)
    glyphs = [font.render(character, True, color) for character in value]
    width = sum(glyph.get_width() for glyph in glyphs) + max(0, len(glyphs) - 1) * spacing
    height = font.get_linesize()
    extra = 1 if shadow else 0
    result = _make_surface((max(1, width + extra), max(1, height + extra)))
    x = 0
    for character, glyph in zip(value, glyphs):
        y = max(0, (height - glyph.get_height()) // 2)
        if shadow:
            shadow_glyph = font.render(character, True, TEXT_SHADOW)
            result.blit(shadow_glyph, (x + 1, y + 1))
        result.blit(glyph, (x, y))
        x += glyph.get_width() + spacing
    return result


def wrap_text(
    text: str,
    role: str,
    max_width: int,
    *,
    variant: str | None = None,
) -> list[str]:
    """Word-wrap text and split oversized words so lines stay within the width."""
    del variant  # Wrapping only needs metrics; color is applied when each line is rendered.
    font = get_font(role)
    width = max(1, int(max_width))
    paragraphs = str(text).splitlines() or [""]
    lines: list[str] = []

    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if font.size(candidate)[0] <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            if font.size(word)[0] <= width:
                current = word
                continue

            fragment = ""
            for character in word:
                if fragment and font.size(fragment + character)[0] > width:
                    lines.append(fragment)
                    fragment = character
                else:
                    fragment += character
            current = fragment
        if current:
            lines.append(current)
    return lines
