"""Reusable animated ability inspection panel for hovered cards."""

from __future__ import annotations

import time

import pygame

from .assets import AssetLoader
from .theme import CREAM, GOLD, TEXT_WHITE
from .typography import get_font


class AbilityInspectionPanel:
    """Draw one shared, animated card inspection panel over the game board."""

    IMAGE = "assets/art/ui/card_panel/Card_Panel_Empty.png"
    FADE_SECONDS = 0.16
    # Lower this normalized Y value to move the character type higher.
    TYPE_AREA_TOP = 0.185
    TYPE_COLORS = {
        "GOAT": (202, 116, 255),
        "Dynamic Duo": (94, 207, 255),
        "Fighter": (255, 133, 104),
        "Trickster": (255, 205, 101),
        "Regular": (174, 205, 224),
    }

    def __init__(self, assets: AssetLoader):
        self.frame = assets.load_image(self.IMAGE)
        self.card = None
        self.definition = None
        self.anchor: pygame.Rect | None = None
        self.clip: pygame.Rect | None = None
        self.alpha = 0.0
        self.last_time = time.monotonic()
        self.frame_cache: dict[tuple[int, int], pygame.Surface] = {}

    def draw(
        self,
        screen: pygame.Surface,
        definition,
        card,
        anchor: pygame.Rect | None,
        clip: pygame.Rect | None = None,
        avoid: list[pygame.Rect] | None = None,
    ) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self.last_time)
        self.last_time = now
        if definition is not None and card is not None and anchor is not None:
            self.definition = definition
            self.card = card
            self.anchor = anchor.copy()
            self.clip = (clip or screen.get_rect()).copy()
            self.alpha = min(1.0, self.alpha + elapsed / self.FADE_SECONDS)
        else:
            self.alpha = max(0.0, self.alpha - elapsed / self.FADE_SECONDS)
        if self.alpha <= 0.0 or self.definition is None or self.anchor is None:
            return

        bounds = self.clip or screen.get_rect()
        panel_w = min(520, max(260, bounds.width - 24))
        panel_h = min(360, max(230, round(panel_w * 0.54)))
        if panel_h > bounds.height - 16:
            panel_h = max(190, bounds.height - 16)
        rect = self._place(self.anchor, panel_w, panel_h, bounds, avoid or [])
        if rect is None:
            return

        progress = min(1.0, self.alpha)
        eased = 1.0 - (1.0 - progress) ** 3
        rise = round((1.0 - eased) * 12)
        rect.y += rise
        alpha = round(eased * 255)
        panel = self._frame_surface(rect.size).copy()
        panel.set_alpha(alpha)
        screen.blit(panel, rect)
        self._draw_content(screen, rect, alpha)

    def _frame_surface(self, size: tuple[int, int]) -> pygame.Surface:
        cached = self.frame_cache.get(size)
        if cached is not None:
            return cached
        if self.frame is None:
            surface = pygame.Surface(size, pygame.SRCALPHA)
            pygame.draw.rect(surface, (15, 13, 33, 242), surface.get_rect(), border_radius=18)
            pygame.draw.rect(surface, GOLD, surface.get_rect(), width=3, border_radius=18)
        else:
            surface = pygame.transform.smoothscale(self.frame, size)
        self.frame_cache[size] = surface
        return surface

    def _draw_content(self, screen: pygame.Surface, rect: pygame.Rect, alpha: int) -> None:
        definition = self.definition
        card = self.card
        if definition is None or card is None:
            return
        ink = pygame.Surface(rect.size, pygame.SRCALPHA)
        scale_x, scale_y = rect.width / 1664, rect.height / 896
        name_area = pygame.Rect(round(0.22 * rect.width), round(0.205 * rect.height), round(0.38 * rect.width), round(0.105 * rect.height))
        type_area = pygame.Rect(
            round(0.63 * rect.width), round(self.TYPE_AREA_TOP * rect.height),
            round(0.27 * rect.width), round(0.08 * rect.height),
        )
        title_area = pygame.Rect(round(0.25 * rect.width), round(0.40 * rect.height), round(0.62 * rect.width), round(0.10 * rect.height))
        body_area = pygame.Rect(round(0.20 * rect.width), round(0.53 * rect.height), round(0.68 * rect.width), round(0.29 * rect.height))
        type_color = self.TYPE_COLORS.get(str(definition.type), (190, 164, 255))

        name_font = get_font("card_name", max(16, round(26 * scale_y)))
        name_text = self._fit_text(name_font, definition.name, name_area.width)
        self._blit_text(ink, name_text, name_font, CREAM, name_area, align="left")

        type_label = str(definition.type or "Character")
        type_font = get_font("ability_title", max(13, round(19 * scale_y)))
        type_text = self._fit_text(type_font, type_label, type_area.width - 14)
        self._blit_text(ink, type_text, type_font, type_color, type_area, align="center")

        icon_center = (round(0.17 * rect.width), round(0.44 * rect.height))
        icon_radius = max(14, round(27 * scale_y))
        pygame.draw.circle(ink, (*type_color, 58), icon_center, icon_radius + 5)
        pygame.draw.circle(ink, (*type_color, 205), icon_center, icon_radius, width=max(2, round(2 * scale_y)))
        bolt = [
            (icon_center[0] + round(2 * scale_x), icon_center[1] - round(15 * scale_y)),
            (icon_center[0] - round(9 * scale_x), icon_center[1] + round(1 * scale_y)),
            (icon_center[0] - round(1 * scale_x), icon_center[1] + round(1 * scale_y)),
            (icon_center[0] - round(4 * scale_x), icon_center[1] + round(14 * scale_y)),
            (icon_center[0] + round(10 * scale_x), icon_center[1] - round(4 * scale_y)),
            (icon_center[0] + round(2 * scale_x), icon_center[1] - round(4 * scale_y)),
        ]
        pygame.draw.polygon(ink, (*type_color, 255), bolt)

        ability_font = get_font("ability_title", max(16, round(24 * scale_y)))
        ability_text = self._fit_text(ability_font, definition.effect_name or "No Ability", title_area.width)
        self._blit_text(ink, ability_text, ability_font, type_color, title_area, align="left")

        desc = str(definition.effect_text or "No ability description.")
        desc_size = max(13, round(19 * scale_y))
        desc_font = get_font("ability_desc", desc_size)
        lines = self._wrap(desc, desc_font, body_area.width)
        line_height = desc_font.get_linesize() + max(1, round(2 * scale_y))
        max_lines = max(1, body_area.height // line_height)
        while len(lines) > max_lines and desc_size > 12:
            desc_size -= 1
            desc_font = get_font("ability_desc", desc_size)
            lines = self._wrap(desc, desc_font, body_area.width)
            line_height = desc_font.get_linesize() + max(1, round(2 * scale_y))
            max_lines = max(1, body_area.height // line_height)
        # Descriptions in the active roster fit at this size. Continue to use a
        # smaller readable size on unusually small screens before clipping.
        if len(lines) > max_lines:
            desc_font = get_font("ability_desc", 12)
            lines = self._wrap(desc, desc_font, body_area.width)
            line_height = desc_font.get_linesize()
            max_lines = max(1, body_area.height // line_height)
        text_y = body_area.y
        for line in lines[:max_lines]:
            text_surface = desc_font.render(line, True, TEXT_WHITE)
            ink.blit(text_surface, (body_area.x, text_y))
            text_y += line_height
        ink.set_alpha(alpha)
        screen.blit(ink, rect.topleft)

    @staticmethod
    def _fit_text(font: pygame.font.Font, text: str, max_width: int) -> str:
        if font.size(text)[0] <= max_width:
            return text
        suffix = "..."
        while text and font.size(text + suffix)[0] > max_width:
            text = text[:-1]
        return text + suffix

    @staticmethod
    def _wrap(text: str, font: pygame.font.Font, max_width: int) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [""]:
            words = paragraph.split()
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and font.size(candidate)[0] > max_width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)
        return lines or [""]

    @staticmethod
    def _blit_text(target, text, font, color, area, align="left") -> None:
        rendered = font.render(text, True, color)
        if align == "center":
            pos = rendered.get_rect(center=area.center)
        elif align == "right":
            pos = rendered.get_rect(midright=area.midright)
        else:
            pos = rendered.get_rect(midleft=area.midleft)
        rendered.set_alpha(255)
        target.blit(rendered, pos)

    @staticmethod
    def _place(
        anchor: pygame.Rect,
        width: int,
        height: int,
        bounds: pygame.Rect,
        avoid: list[pygame.Rect],
    ) -> pygame.Rect | None:
        gap = 14
        candidates: list[pygame.Rect] = []
        for x in (anchor.right + gap, anchor.left - width - gap):
            candidate = pygame.Rect(x, anchor.centery - height // 2, width, height)
            if bounds.contains(candidate) and not candidate.colliderect(anchor):
                candidates.append(candidate)
        for y in (anchor.top - height - gap, anchor.bottom + gap):
            candidate = pygame.Rect(anchor.centerx - width // 2, y, width, height)
            if bounds.contains(candidate) and not candidate.colliderect(anchor):
                candidates.append(candidate)
        if candidates:
            return min(
                candidates,
                key=lambda candidate: sum(candidate.colliderect(item) for item in avoid),
            )
        # Tight layouts can still fit the panel beside the card with a clamped
        # vertical position. Prefer the side with more room.
        left_space = anchor.left - bounds.left
        right_space = bounds.right - anchor.right
        x = anchor.right + gap if right_space >= left_space else anchor.left - width - gap
        x = max(bounds.left + 8, min(x, bounds.right - width - 8))
        y = max(bounds.top + 8, min(anchor.centery - height // 2, bounds.bottom - height - 8))
        candidate = pygame.Rect(x, y, width, height)
        if bounds.contains(candidate) and not candidate.colliderect(anchor):
            return candidate
        return None
