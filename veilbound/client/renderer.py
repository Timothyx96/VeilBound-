from __future__ import annotations

import time

import pygame

from veilbound.client.assets import AssetLoader
from veilbound.client.layout import CARD_HEIGHT_RATIO, CARD_WIDTH_RATIO, BoardLayout
from veilbound.client.settings import AppSettings, load_settings
from veilbound.client.theme import (
    BG, CREAM, DEBUG, GOLD, P1, P2, PANEL, PANEL_LIGHT,
    SLOT_BORDER_WIDTH, SLOT_CARD_MARGIN, SLOT_CORNER_ORNAMENTS_ENABLED,
    SLOT_EMPTY_BORDER, SLOT_EMPTY_FILL, SLOT_GLOW_PADDING, SLOT_GLOW_STEPS,
    SLOT_HOVER_BORDER, SLOT_HOVER_GLOW, SLOT_INVALID_BORDER, SLOT_INVALID_FILL,
    SLOT_OCCUPIED_BORDER, SLOT_OCCUPIED_FILL,
    SLOT_RADIUS, SLOT_SHADOW, SLOT_GOLD, SLOT_VALID_BORDER,
    SLOT_VALID_FILL, SLOT_VALID_GLOW, TEXT_MUTED_GOLD, TEXT_SHADOW,
)
from veilbound.client.typography import (
    FONTS,
    get_font,
    preload_fonts,
    render_text,
    render_text_with_spacing,
    wrap_text,
)
from veilbound.shared.models import BattleRow, CardDefinition, GameEvent, GamePhase, GameState
from veilbound.shared.scoring import calculate_row_scores


class Renderer:
    def __init__(self, assets: AssetLoader | None = None, settings: AppSettings | None = None):
        preload_fonts()
        self.assets = assets or AssetLoader()
        self.settings = settings or load_settings()
        self.animation_ms = self.settings.animation_ms
        self.background = self.assets.load_image(self.settings.background_path)
        self.card_back_art = {
            "blue": self.assets.load_image("assets/art/card_backs/Card_Back_Blue.png"),
            "red": self.assets.load_image("assets/art/card_backs/Card_Back_Red.png"),
        }
        self.card_front_art = self.assets.load_image(
            "assets/art/card front/Antieke Perkamentachtergrond met Gouden Hoekornamenten.png"
        )
        self.card_frame_art = self._prepare_card_frame()
        self.options_background = self.assets.load_image("assets/backgrounds/Options Background.png")
        self._background_cache_size: tuple[int, int] | None = None
        self._background_cache: tuple[pygame.Surface, pygame.Surface] | None = None
        self._render_size: tuple[int, int] | None = None
        self._overlay_cache: dict[tuple[int, int, int, int, int, int], pygame.Surface] = {}
        self._row_backplates: dict[tuple[int, int], pygame.Surface] = {}
        self._slot_surface_cache: dict[tuple[str, tuple[int, int], bool], pygame.Surface] = {}
        self._card_frame_cache: dict[tuple[int, int], pygame.Surface] = {}
        self._card_front_cache: dict[tuple[int, int], pygame.Surface] = {}
        self._card_face_cache: dict[tuple[str, tuple[int, int]], pygame.Surface] = {}
        self._card_back_cache: dict[tuple[str, tuple[int, int]], pygame.Surface] = {}
        self._spotlight_portrait_cache: dict[tuple[str, tuple[int, int]], pygame.Surface] = {}
        self.flip_started: dict[tuple[str, int], float] = {}
        self.notice = "Drag a card into a valid empty slot."
        self.notice_until = 0.0
        self.room_label: str | None = None
        self.turn_confirmation_player_id: str | None = None
        self.last_card_reveal = ""
        self.reveal_card: dict | None = None
        self.reveal_card_visible_after = 0.0
        self.reveal_card_until = 0.0
        self.score_animation_row_id: str | None = None
        self.score_animation_started = 0.0

    def layout(self, state: GameState, size: tuple[int, int]) -> BoardLayout:
        return BoardLayout(size, state)

    def reset_animations(self) -> None:
        self.flip_started.clear()
        self.last_card_reveal = ""
        self.reveal_card = None
        self.reveal_card_visible_after = 0.0
        self.reveal_card_until = 0.0
        self.score_animation_row_id = None

    def note_events(self, events: tuple[GameEvent, ...]) -> None:
        for event in events:
            if event.kind == "card_revealed":
                key = (event.data["row_id"], event.data["slot_index"])
                if key in self.flip_started:
                    continue
                self.flip_started[key] = time.monotonic()
                self.last_card_reveal = event.message
                now = time.monotonic()
                self.reveal_card = dict(event.data)
                self.reveal_card_visible_after = now + self.animation_ms["card_flip_ms"] / 1000
                self.reveal_card_until = self.reveal_card_visible_after + self.animation_ms["card_information_ms"] / 1000
            elif event.kind == "row_scored":
                self.score_animation_row_id = event.data["row_id"]
                self.score_animation_started = time.monotonic()
            self.notice = event.message
            self.notice_until = time.monotonic() + 3.5

    def draw(self, screen, state: GameState, cards: dict[str, CardDefinition], legal_slots: list[tuple[str, int]], drag_id: str | None = None, mouse_pos: tuple[int, int] | None = None, fps: float = 0.0):
        if self._render_size != screen.get_size():
            self._render_size = screen.get_size()
            self._overlay_cache.clear()
            self._row_backplates.clear()
            self._slot_surface_cache.clear()
            self._card_frame_cache.clear()
            self._card_front_cache.clear()
            self._card_face_cache.clear()
            self._card_back_cache.clear()
            self._spotlight_portrait_cache.clear()
        screen.fill(BG)
        self._draw_background(screen)
        layout = self.layout(state, screen.get_size())
        self._draw_header(screen, state)
        if DEBUG:
            self._draw_fps(screen, fps)
        self._draw_rows(screen, state, cards, layout, set(legal_slots), drag_id, mouse_pos)
        reveal_view_active = self._reveal_view_active(state)
        self._draw_card_spotlight(screen, state, mouse_pos)
        self._draw_row_comparison(screen, state)
        self._draw_status(screen, state, layout)
        self._draw_hand(screen, state, cards, layout, drag_id, mouse_pos)
        self._draw_footer(screen, layout)
        if not reveal_view_active:
            self._draw_effect_tooltip(screen, state, cards, layout, mouse_pos)
        if state.phase == GamePhase.ROUND_RESULTS:
            self._draw_round_results(screen, state, layout)
        elif state.phase == GamePhase.MATCH_RESULTS:
            self._draw_match_results(screen, state, layout)
        return layout

    def _draw_background(self, screen) -> None:
        if self.background is None:
            return
        size = screen.get_size()
        if self._background_cache_size != size or self._background_cache is None:
            fit = self.assets.scale_to_fit(self.background, size)
            source_width, source_height = self.background.get_size()
            scale = max(size[0] / source_width, size[1] / source_height)
            cover_size = (
                max(size[0], round(source_width * scale)),
                max(size[1], round(source_height * scale)),
            )
            cover = pygame.transform.smoothscale(self.background, cover_size)
            cover_rect = cover.get_rect(center=(size[0] // 2, size[1] // 2))
            crop = pygame.Rect(-cover_rect.x, -cover_rect.y, size[0], size[1])
            cover = cover.subsurface(crop).copy()
            self._background_cache_size = size
            self._background_cache = (cover, fit)
        cover, fit = self._background_cache
        screen.blit(cover, (0, 0))
        screen.blit(fit, fit.get_rect(center=screen.get_rect().center))
        screen.blit(self._overlay(screen.get_size(), (8, 12, 22, 145)), (0, 0))

    def _overlay(self, size: tuple[int, int], color: tuple[int, int, int, int]) -> pygame.Surface:
        key = (*size, *color)
        overlay = self._overlay_cache.get(key)
        if overlay is None:
            overlay = pygame.Surface(size, pygame.SRCALPHA)
            overlay.fill(color)
            self._overlay_cache[key] = overlay
        return overlay

    def _reveal_view_active(self, state) -> bool:
        return (
            state.phase in {GamePhase.PLACEMENT, GamePhase.REVEAL, GamePhase.ROW_SCORING}
            and self.reveal_card is not None
            and time.monotonic() < self.reveal_card_until
        )

    def _draw_card_spotlight(self, screen, state, mouse_pos) -> None:
        if state.phase not in {GamePhase.PLACEMENT, GamePhase.REVEAL, GamePhase.ROW_SCORING} or self.reveal_card is None:
            return
        now = time.monotonic()
        if now < self.reveal_card_visible_after or now >= self.reveal_card_until:
            return

        width, height = screen.get_size()
        screen.blit(self._overlay(screen.get_size(), (4, 8, 17, 168)), (0, 0))

        full_card_art = str(self.reveal_card.get("full_card_art", ""))
        full_art = None
        if full_card_art:
            full_art = self.assets.load_image(full_card_art)
        has_full_art = full_art is not None

        card_max_w = 340
        card_max_h = 500
        card_w = min(card_max_w, width - 48)
        card_h = min(card_max_h, int(card_w * CARD_HEIGHT_RATIO / CARD_WIDTH_RATIO))
        card_rect = pygame.Rect(0, 0, card_w, card_h)

        effect_name = str(self.reveal_card.get("effect_name", "Effect"))
        description = str(self.reveal_card.get("effect_text", ""))
        name_font = get_font("ability_title")
        desc_font = get_font("ability_desc")

        name_lines = wrap_text(effect_name.upper(), "ability_title", 260)
        name_line_h = name_font.get_linesize()
        name_block_h = len(name_lines) * name_line_h

        desc_lines = wrap_text(description, "ability_desc", 260)
        desc_line_h = desc_font.get_linesize()
        desc_block_h = len(desc_lines) * desc_line_h

        panel_pad = 24
        panel_top_pad = 20
        panel_bottom_pad = 20
        name_max_w = max((name_font.size(line)[0] for line in name_lines), default=0)
        desc_max_w = max((desc_font.size(line)[0] for line in desc_lines), default=0)
        ability_w = min(320, max(260, panel_pad * 2 + max(name_max_w, desc_max_w)))
        ability_h = panel_top_pad + name_block_h + 20 + desc_block_h + panel_bottom_pad
        ability_h = min(420, max(170, ability_h))
        ability_rect = pygame.Rect(0, 0, ability_w, ability_h)

        gap = 24
        combined_w = ability_w + gap + card_w
        combined_h = max(ability_h, card_h)

        if combined_w > width - 48:
            combined_w = max(card_w, ability_w)
            combined_h = card_h + gap + ability_h
            side_by_side = False
        else:
            side_by_side = True

        group_rect = pygame.Rect(0, 0, combined_w, combined_h)
        group_rect.center = (width // 2, height // 2)

        card_rect.x = group_rect.x
        card_rect.y = group_rect.y + (combined_h - card_h) // 2
        if side_by_side:
            ability_rect.x = card_rect.right + gap
            ability_rect.y = group_rect.y + (combined_h - ability_h) // 2
        else:
            card_rect.y = group_rect.y
            ability_rect.x = group_rect.x
            ability_rect.y = card_rect.bottom + gap

        panel_surface = pygame.Surface((ability_w, ability_h), pygame.SRCALPHA)
        panel_surface.fill(PANEL)
        pygame.draw.rect(
            panel_surface, TEXT_MUTED_GOLD, panel_surface.get_rect(), width=2, border_radius=8,
        )
        screen.blit(panel_surface, ability_rect)

        name_y = ability_rect.y + 20
        for line in name_lines:
            name_surf = name_font.render(line.upper(), True, GOLD)
            screen.blit(name_surf, name_surf.get_rect(centerx=ability_rect.centerx, y=name_y))
            name_y += name_line_h

        divider_y = name_y + 10
        divider_rect = pygame.Rect(
            ability_rect.x + 24, divider_y, ability_w - 48, 1,
        )
        pygame.draw.rect(screen, TEXT_MUTED_GOLD, divider_rect)

        desc_y = divider_y + 16
        max_desc_lines = max(1, (ability_rect.bottom - desc_y - 10) // desc_line_h)
        if len(desc_lines) > max_desc_lines:
            desc_lines = desc_lines[:max_desc_lines]
            desc_lines[-1] = desc_lines[-1].rstrip(" .") + "..."
        for line in desc_lines:
            desc_surf = desc_font.render(line, True, CREAM)
            screen.blit(desc_surf, desc_surf.get_rect(centerx=ability_rect.centerx, y=desc_y))
            desc_y += desc_line_h

        if has_full_art:
            key = ("full_card", full_card_art, card_rect.size)
            cached_full_art = self._spotlight_portrait_cache.get(key)
            if cached_full_art is None:
                cached_full_art = self.assets.scale_to_fit(full_art, card_rect.size)
                self._spotlight_portrait_cache[key] = cached_full_art
            screen.blit(cached_full_art, card_rect)
        else:
            screen.blit(self._card_front_surface(card_rect.size), card_rect)
            art_rect = pygame.Rect(card_rect.x + 10, card_rect.y + 10, card_w - 20, int(card_h * 0.45))
            image_path = str(self.reveal_card.get("image", ""))
            portrait_key = (image_path, art_rect.size)
            portrait = self._spotlight_portrait_cache.get(portrait_key)
            if portrait is None:
                art = self.assets.load_character(image_path)
                portrait = self.assets.scale_to_fit(art, art_rect.size)
                self._spotlight_portrait_cache[portrait_key] = portrait
            screen.blit(portrait, portrait.get_rect(center=art_rect.center))
            name = str(self.reveal_card.get("name", "Unknown character"))
            portrait_bottom = art_rect.bottom
            name_y = portrait_bottom + 24
            name_lines = self._card_name_lines(name, card_w - 20)
            name_extra = self._draw_centered_name_lines(
                screen, name_lines, (card_rect.centerx, name_y), card_w - 20,
                variant="black",
            )
            base_points = int(self.reveal_card.get("base_points", 0))
            base_points_y = name_y + name_extra + 20
            self._text_center(screen, "BASE POINTS", (card_rect.centerx, base_points_y), "label_small")
            self._text_center_fit(
                screen, str(base_points), (card_rect.centerx, base_points_y + 22), "number_card", 96, shadow=True,
            )

    def _draw_effect_tooltip(self, screen, state, cards, layout, mouse_pos):
        if mouse_pos is None:
            return
        hovered = None
        hand_player = state.player(state.viewer_id) if state.viewer_id else state.active_player
        if state.phase == GamePhase.PLACEMENT and self.turn_confirmation_player_id is None:
            hovered = next((
                card for card, rect in zip(hand_player.hand, layout.hand_cards)
                if rect.collidepoint(mouse_pos)
            ), None)
        if hovered is None:
            for row in state.rows:
                for slot_index, card in enumerate(row.slots):
                    if (
                        card is not None
                        and card.revealed
                        and layout.slot_rects[(row.id, slot_index)].collidepoint(mouse_pos)
                    ):
                        hovered = card
                        break
                if hovered is not None:
                    break
        if hovered is None:
            return

        definition = cards[hovered.definition_id]
        width, height = screen.get_size()
        panel_width = min(400, width - 24)
        lines = wrap_text(definition.effect_text, "ability_desc", panel_width - 24)
        name_lines = self._card_name_lines(definition.name, panel_width - 20)
        name_extra = (len(name_lines) - 1) * get_font("card_name").get_linesize()
        line_height = get_font("ability_desc").get_linesize()
        max_lines = max(1, (height - 108) // line_height)
        visible_lines = lines[:max_lines]
        if len(lines) > max_lines:
            visible_lines[-1] = visible_lines[-1].rstrip(" .") + "..."
        panel_height = min(height - 24, 86 + name_extra + len(visible_lines) * line_height)
        x = mouse_pos[0] + 18
        if x + panel_width > width - 12:
            x = mouse_pos[0] - panel_width - 18
        x = min(max(12, x), width - panel_width - 12)
        y = mouse_pos[1] - panel_height - 18
        if y < 12:
            y = mouse_pos[1] + 18
        y = min(max(12, y), height - panel_height - 12)

        panel = pygame.Rect(x, y, panel_width, panel_height)
        pygame.draw.rect(screen, (20, 28, 44), panel, border_radius=9)
        pygame.draw.rect(screen, GOLD, panel, width=2, border_radius=9)
        self._draw_centered_name_lines(screen, name_lines, (panel.centerx, panel.y + 14), panel.width - 20)
        self._text_center_spaced(
            screen, definition.effect_name, (panel.centerx, panel.y + 35 + name_extra),
            "ability_title", 1, panel.width - 20,
        )
        self._text_center_fit(
            screen, f"BASE POINTS: {definition.base_points}", (panel.centerx, panel.y + 55 + name_extra),
            "label_small", panel.width - 20,
        )
        for index, line in enumerate(visible_lines):
            self._text(
                screen, line, (panel.x + 12, panel.y + 72 + name_extra + index * line_height),
                "ability_desc", max_width=panel.width - 24,
            )
    def _displayed_row_scores(self, row: BattleRow) -> dict[str, int]:
        if row.id != self.score_animation_row_id or not row.scored:
            return row.scores
        duration = self.animation_ms["score_count_ms"] / 1000
        if duration <= 0:
            progress = 1.0
        else:
            progress = min(1.0, max(0.0, (time.monotonic() - self.score_animation_started) / duration))
        return {player_id: int(score * progress) for player_id, score in row.scores.items()}

    def _draw_row_comparison(self, screen, state) -> None:
        if state.phase != GamePhase.ROW_RESULTS or state.current_row is None:
            return
        row = state.current_row
        scores = self._displayed_row_scores(row)
        width = min(600, screen.get_width() - 40)
        panel = pygame.Rect(0, 0, width, 196)
        panel.center = (screen.get_width() // 2, screen.get_height() // 2)
        pygame.draw.rect(screen, PANEL, panel, border_radius=16)
        pygame.draw.rect(screen, GOLD, panel, width=3, border_radius=16)
        self._text_center(screen, f"ROW {row.number} SCORE", (panel.centerx, panel.y + 24), "label_small")
        for index, player in enumerate(state.players):
            x = panel.x + panel.width // 4 + index * panel.width // 2
            self._text_center_fit(screen, player.name, (x, panel.y + 67), "ui", panel.width // 2 - 40)
            self._text_center(screen, str(scores[player.id]), (x, panel.y + 121), "number_row_score")
        if scores != row.scores:
            result_text = "Comparing scores..."
        elif row.winner_id is None:
            result_text = "Tie: no row points awarded"
        else:
            result_text = f"{state.player(row.winner_id).name} wins {row.awarded_points} row points"
        self._text_center_fit(screen, result_text, (panel.centerx, panel.y + 173), "label_small", panel.width - 28)

    def _draw_header(self, screen, state):
        width, _ = screen.get_size()
        for index, player in enumerate(state.players):
            x = 16 if index == 0 else width - 204
            rect = pygame.Rect(x, 10, 188, 66)
            pygame.draw.rect(screen, PANEL, rect, border_radius=11)
            accent = P1 if index == 0 else P2
            border_width = 3 if player.id == state.active_player.id and state.phase == GamePhase.PLACEMENT else 2
            pygame.draw.rect(screen, accent, rect, width=border_width, border_radius=11)
            self._text(screen, player.name, (rect.x + 12, rect.y + 7), "ui", max_width=rect.width - 24)
            self._text(screen, "MATCH PTS", (rect.x + 12, rect.y + 42), "label_small")
            self._text_right(
                screen, str(player.match_points), (rect.right - 12, rect.y + 37), "number_match", max_width=48,
            )

        title_width = max(100, width - 424)
        self._text_center_fit(screen, "VEILBOUND", (width // 2, 23), "title", title_width)
        self._text_center_fit(
            screen, f"ROUND {state.round_number} / {state.rounds_per_match}",
            (width // 2, 57), "label_small", title_width,
        )
    def _draw_fps(self, screen, fps: float) -> None:
        self._text_right(screen, f"FPS: {fps:.0f}", (screen.get_width() - 16, 84), "label_small")

    def _draw_rows(self, screen, state, cards, layout, legal_slots, drag_id, mouse_pos):
        now = time.monotonic()
        for row in state.rows:
            rect = layout.row_rects[row.id]
            box = layout.capacity_boxes[row.id]
            active_row = state.current_row is not None and state.current_row.id == row.id and state.phase in {
                GamePhase.REVEAL, GamePhase.ROW_SCORING, GamePhase.ROW_RESULTS,
            }
            if row.scored:
                if row.winner_id == state.players[0].id:
                    outline = P1
                elif row.winner_id == state.players[1].id:
                    outline = P2
                else:
                    outline = (153, 163, 177)
            else:
                outline = GOLD if active_row else (78, 91, 116)
            backplate = self._row_backplates.get(rect.size)
            if backplate is None:
                backplate = pygame.Surface(rect.size, pygame.SRCALPHA)
                pygame.draw.rect(backplate, (*PANEL, 112), backplate.get_rect(), border_radius=10)
                self._row_backplates[rect.size] = backplate
            screen.blit(backplate, rect)
            pygame.draw.rect(screen, outline, rect, width=3 if active_row or row.scored else 2, border_radius=10)
            self._draw_row_box(screen, row, box, state, cards)

            for slot_index, slot in enumerate(row.slots):
                slot_rect = layout.slot_rects[(row.id, slot_index)]
                target = (row.id, slot_index) in legal_slots
                hovered = mouse_pos is not None and slot_rect.collidepoint(mouse_pos)
                if slot is None:
                    if drag_id is not None:
                        slot_state = "valid" if target else "invalid" if hovered else "empty"
                    else:
                        slot_state = "hover" if hovered else "ready" if target else "empty"
                    self.draw_slot(screen, slot_rect, slot_state, marker=target)
                else:
                    self.draw_slot(screen, slot_rect, "occupied")
                    card_rect = self._portrait_card_rect(slot_rect)
                    self._draw_placed_card(screen, card_rect, row, slot_index, slot, cards, now)
                    if drag_id is not None and hovered:
                        self.draw_slot(screen, slot_rect, "invalid_overlay")

    def _prepare_card_frame(self) -> pygame.Surface | None:
        """Load the outline, key out its near-black background, and crop it."""
        image = self.assets.load_image("assets/art/ui/Card Outline .png")
        if image is None:
            return None
        image = image.copy()
        # The source has dark, partly opaque texture across its canvas. Exact
        # black colorkeying leaves that texture behind, so treat near-black as
        # transparent and keep the brighter gold/red line work.
        visible_mask = pygame.mask.from_threshold(
            image, (0, 0, 0, 255), (28, 28, 28, 255),
        )
        visible_mask.invert()
        key = visible_mask.to_surface(
            setcolor=(255, 255, 255, 255),
            unsetcolor=(0, 0, 0, 0),
        )
        bounds = key.get_bounding_rect(min_alpha=1)
        if bounds.width == 0 or bounds.height == 0:
            return None
        image.blit(key, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        image = image.subsurface(bounds).copy()
        return image

    def _draw_card_frame(self, screen, rect: pygame.Rect) -> None:
        if self.card_frame_art is None:
            return
        scale_x, scale_y = self.settings.card_frame_size
        frame_size = (
            max(1, round(rect.width * scale_x)),
            max(1, round(rect.height * scale_y)),
        )
        frame = self._card_frame_cache.get(frame_size)
        if frame is None:
            frame = pygame.transform.smoothscale(self.card_frame_art, frame_size)
            self._card_frame_cache[frame_size] = frame
        screen.blit(frame, frame.get_rect(center=rect.center))

    def _card_front_surface(self, size: tuple[int, int]) -> pygame.Surface:
        """Return the parchment front artwork scaled to a card/panel size."""
        cached = self._card_front_cache.get(size)
        if cached is not None:
            return cached
        if self.card_front_art is not None:
            # The source is square while cards are portrait; fill the card so
            # all four corner ornaments remain visible.
            front = pygame.transform.smoothscale(self.card_front_art, size)
        else:
            front = pygame.Surface(size, pygame.SRCALPHA)
            front.fill(CREAM)
        self._card_front_cache[size] = front
        return front

    def draw_slot(self, screen, rect: pygame.Rect, state: str, marker: bool = False) -> None:
        """Draw a cached slot skin for its visual state and size."""
        key = (state, rect.size, marker)
        slot_surface = self._slot_surface_cache.get(key)
        if slot_surface is None:
            slot_surface = self._make_slot_surface(state, rect.size, marker)
            self._slot_surface_cache[key] = slot_surface
        screen.blit(slot_surface, (rect.x - SLOT_GLOW_PADDING, rect.y - SLOT_GLOW_PADDING))

    def _make_slot_surface(self, state: str, size: tuple[int, int], marker: bool) -> pygame.Surface:
        width, height = size
        padding = SLOT_GLOW_PADDING
        surface = pygame.Surface((width + padding * 2, height + padding * 2), pygame.SRCALPHA)
        if pygame.display.get_surface() is not None:
            surface = surface.convert_alpha()
        slot_rect = pygame.Rect(padding, padding, width, height)

        styles = {
            "empty": (SLOT_EMPTY_FILL, SLOT_EMPTY_BORDER, None),
            "ready": (SLOT_EMPTY_FILL, SLOT_EMPTY_BORDER, None),
            "hover": (SLOT_EMPTY_FILL, SLOT_HOVER_BORDER, SLOT_HOVER_GLOW),
            "valid": (SLOT_VALID_FILL, SLOT_VALID_BORDER, SLOT_VALID_GLOW),
            "invalid": (SLOT_INVALID_FILL, SLOT_INVALID_BORDER, None),
            "invalid_overlay": (SLOT_INVALID_FILL, SLOT_INVALID_BORDER, None),
            "occupied": (SLOT_OCCUPIED_FILL, SLOT_OCCUPIED_BORDER, None),
        }
        fill, border, glow = styles.get(state, styles["empty"])

        if state != "invalid_overlay":
            shadow_rect = slot_rect.move(0, 2)
            pygame.draw.rect(surface, SLOT_SHADOW, shadow_rect, border_radius=SLOT_RADIUS)
        if glow is not None:
            self._draw_slot_glow(surface, slot_rect, glow)
        pygame.draw.rect(surface, fill, slot_rect, border_radius=SLOT_RADIUS)
        pygame.draw.rect(surface, border, slot_rect, width=SLOT_BORDER_WIDTH, border_radius=SLOT_RADIUS)

        if marker:
            marker_x = slot_rect.left - 4
            marker_y = slot_rect.centery
            marker = [
                (marker_x, marker_y - 3),
                (marker_x + 3, marker_y),
                (marker_x, marker_y + 3),
                (marker_x - 3, marker_y),
            ]
            pygame.draw.polygon(surface, SLOT_GOLD, marker)

        if SLOT_CORNER_ORNAMENTS_ENABLED:
            self._draw_slot_corner_ornaments(surface, slot_rect)
        return surface

    @staticmethod
    def _draw_slot_glow(surface: pygame.Surface, rect: pygame.Rect, color: tuple[int, int, int, int]) -> None:
        for spread, alpha in SLOT_GLOW_STEPS:
            glow_rect = rect.inflate(spread * 2, spread * 2)
            pygame.draw.rect(
                surface,
                (*color[:3], alpha),
                glow_rect,
                width=2,
                border_radius=SLOT_RADIUS + spread,
            )

    @staticmethod
    def _draw_slot_corner_ornaments(surface: pygame.Surface, rect: pygame.Rect) -> None:
        length = 5
        corners = (
            (rect.left + 2, rect.top + 2, 1, 1),
            (rect.right - 3, rect.top + 2, -1, 1),
            (rect.left + 2, rect.bottom - 3, 1, -1),
            (rect.right - 3, rect.bottom - 3, -1, -1),
        )
        for x, y, dx, dy in corners:
            pygame.draw.line(surface, SLOT_GOLD, (x, y), (x + dx * length, y), 1)
            pygame.draw.line(surface, SLOT_GOLD, (x, y), (x, y + dy * length), 1)

    def _draw_row_box(self, screen, row: BattleRow, rect: pygame.Rect, state: GameState, cards):
        fill = PANEL_LIGHT if not row.scored else (47, 59, 80)
        pygame.draw.rect(screen, fill, rect, border_radius=9)
        pygame.draw.rect(screen, GOLD if row.scored else (113, 128, 151), rect, width=2, border_radius=9)
        self._text_center_fit(screen, f"ROW {row.number}", (rect.centerx, rect.y + 10), "row_label", rect.width - 8)
        self._text_center(screen, str(row.point_value), (rect.centerx, rect.y + rect.height * 0.42), "number_capacity")
        display_scores = (
            self._displayed_row_scores(row)
            if row.scored
            else calculate_row_scores(row, tuple(player.id for player in state.players), cards)
        )
        p1_score = display_scores[state.players[0].id]
        p2_score = display_scores[state.players[1].id]
        self._text_center_fit(screen, f"P1 {p1_score}", (rect.centerx, rect.bottom - 25), "label_small", rect.width - 8)
        self._text_center_fit(screen, f"P2 {p2_score}", (rect.centerx, rect.bottom - 9), "label_small", rect.width - 8)

    def _draw_placed_card(self, screen, rect, row, slot_index, card, cards, now):
        definition = cards.get(card.definition_id) if card.revealed else None
        reveal_time = self.flip_started.get((row.id, slot_index))
        flip_duration = max(1, self.animation_ms["card_flip_ms"]) / 1000
        progress = 1.0 if reveal_time is None and card.revealed else 0.0
        if reveal_time is not None:
            progress = min(1.0, max(0.0, (now - reveal_time) / flip_duration))
        face_up = card.revealed and (reveal_time is None or progress >= 0.5)
        owner_accent = P1 if card.owner_id == "player-1" else P2
        if face_up:
            if definition is None:
                return
            surface = self._make_card_face(definition, rect.size)
        else:
            surface = self._make_card_back_surface(rect.size, card.owner_id)
        if reveal_time is not None and progress < 1.0:
            flip_scale = max(0.04, abs(1.0 - progress * 2.0))
            scaled_width = max(2, round(rect.width * flip_scale))
            surface = pygame.transform.smoothscale(surface, (scaled_width, rect.height))
            draw_rect = surface.get_rect(center=rect.center)
        else:
            draw_rect = rect
        screen.blit(surface, draw_rect)
        pygame.draw.rect(screen, owner_accent, rect, width=2, border_radius=6)
        if not card.revealed:
            self._draw_card_frame(screen, draw_rect)

    @staticmethod
    def _portrait_card_rect(slot_rect: pygame.Rect) -> pygame.Rect:
        """Fit a card inside its slot with a uniform margin and 2:3 proportions."""
        available_width = max(1, slot_rect.width - SLOT_CARD_MARGIN * 2)
        available_height = max(1, slot_rect.height - SLOT_CARD_MARGIN * 2)
        card_width = min(
            available_width,
            max(1, available_height * CARD_WIDTH_RATIO // CARD_HEIGHT_RATIO),
        )
        card_height = min(
            available_height,
            max(1, round(card_width * CARD_HEIGHT_RATIO / CARD_WIDTH_RATIO)),
        )
        card_width = min(
            available_width,
            max(1, round(card_height * CARD_WIDTH_RATIO / CARD_HEIGHT_RATIO)),
        )
        rect = pygame.Rect(0, 0, card_width, card_height)
        rect.center = slot_rect.center
        return rect

    def _make_card_face(self, definition: CardDefinition, size: tuple[int, int]) -> pygame.Surface:
        key = (definition.id, size)
        cached = self._card_face_cache.get(key)
        if cached is not None:
            return cached
        width, height = size

        # Full card artwork takes priority and replaces the entire card face.
        full_art = None
        if definition.full_card_art:
            full_art = self.assets.load_image(definition.full_card_art)
        if full_art is not None:
            face = pygame.Surface(size, pygame.SRCALPHA)
            fitted = self.assets.scale_to_fit(full_art, size)
            face.blit(fitted, fitted.get_rect(center=(width // 2, height // 2)))
            self._card_face_cache[key] = face
            return face

        # Fallback: existing card front + anime image system.
        face = self._card_front_surface(size).copy()
        pygame.draw.rect(face, (151, 139, 114), face.get_rect(), width=2, border_radius=6)
        compact = height < 100
        name_lines = self._card_name_lines(definition.name, width - 8)
        wrapped_name = len(name_lines) > 1 and height >= 110
        art_height = max(
            8 if compact else 18,
            round(height * (0.22 if compact else 0.18 if wrapped_name else 0.29)),
        )
        art_area = pygame.Rect(3, 3, max(2, width - 6), max(2, art_height - 3))
        art = self.assets.load_character(definition.image)
        scaled = self.assets.scale_to_fit(art, art_area.size)
        face.blit(scaled, scaled.get_rect(center=art_area.center))
        pygame.draw.line(face, (153, 145, 127), (3, art_height), (width - 4, art_height), 1)

        if compact:
            name_y = art_height + max(7, get_font("card_name").get_linesize() // 2) + 2
            self._text_center_fit(
                face, definition.name, (width // 2, name_y), "card_name", width - 6,
                variant="parchment",
            )
            point_font_size = max(12, min(FONTS["number_card"]["size"], round(height * 0.24)))
            point_font = get_font("number_card", point_font_size)
            point_text = str(definition.base_points)
            point = point_font.render(point_text, True, FONTS["number_card"]["color"])
            shadow = point_font.render(point_text, True, TEXT_SHADOW)
            outlined = pygame.Surface((point.get_width() + 1, point.get_height() + 1), pygame.SRCALPHA)
            outlined.blit(shadow, (1, 1))
            outlined.blit(point, (0, 0))
            point_rect = outlined.get_rect(center=(width // 2, height - outlined.get_height() // 2 - 2))
            face.blit(outlined, point_rect)
            self._card_face_cache[key] = face
            return face

        if wrapped_name:
            self._draw_centered_name_lines(
                face, name_lines, (width // 2, 0), width - 8,
                variant="parchment", top=art_height + 1,
            )
            label_y = height - 45
        else:
            self._text_center_fit(
                face, definition.name, (width // 2, art_height + 10), "card_name", width - 6,
                variant="parchment",
            )
            label_y = height - 45
        self._text_center_fit(
            face, "BASE POINTS", (width // 2, label_y), "label_small", width - 8, variant="parchment",
        )
        self._text_center_fit(
            face, str(definition.base_points), (width // 2, height - 19), "number_card", width - 10, shadow=True,
        )
        self._card_face_cache[key] = face
        return face

    @staticmethod
    def _card_back_variant(player_id: str) -> str:
        return "blue" if player_id == "player-1" else "red"

    def _make_card_back_surface(self, size: tuple[int, int], player_id: str) -> pygame.Surface:
        variant = self._card_back_variant(player_id)
        key = (variant, size)
        cached = self._card_back_cache.get(key)
        if cached is not None:
            return cached
        artwork = self.card_back_art.get(variant)
        if artwork is not None:
            back = pygame.Surface(size, pygame.SRCALPHA)
            back.fill(BG)
            fitted_art = self.assets.scale_to_fit(artwork, size)
            back.blit(fitted_art, fitted_art.get_rect(center=back.get_rect().center))
        else:
            # Keep cards readable if the configured artwork is missing.
            back = pygame.Surface(size, pygame.SRCALPHA)
            accent = P1 if variant == "blue" else P2
            back.fill(PANEL)
            pygame.draw.rect(back, accent, back.get_rect(), width=2, border_radius=6)
        self._card_back_cache[key] = back
        return back

    def _draw_hand(self, screen, state, cards, layout, drag_id, mouse_pos):
        if state.phase != GamePhase.PLACEMENT or self.turn_confirmation_player_id is not None:
            return
        hand_player = state.player(state.viewer_id) if state.viewer_id else state.active_player
        for card, rect in zip(hand_player.hand, layout.hand_cards):
            if card.id == drag_id:
                continue
            screen.blit(self._make_card_face(cards[card.definition_id], rect.size), rect)
        if drag_id and mouse_pos and hand_player.id == state.active_player.id:
            card = next((item for item in hand_player.hand if item.id == drag_id), None)
            if card:
                drag_height = max(1, round(layout.hand_cards[0].height * 1.1))
                drag_width = max(1, round(drag_height * CARD_WIDTH_RATIO / CARD_HEIGHT_RATIO))
                surface = self._make_card_face(cards[card.definition_id], (drag_width, drag_height))
                rect = surface.get_rect(center=mouse_pos)
                screen.blit(surface, rect)
    def _draw_status(self, screen, state, layout):
        if state.phase == GamePhase.PLACEMENT:
            if self.turn_confirmation_player_id is not None and not self._reveal_view_active(state):
                player = state.player(self.turn_confirmation_player_id)
                text = f"{player.name}, your turn. Press Enter or click to show your hand."
            else:
                empty = sum(row.capacity - row.placed_count for row in state.rows)
                text = f"Round {state.round_number}: {state.active_player.name} to place - {empty} slots remain"
                if (
                    state.viewer_id is not None
                    and state.active_player.id != state.viewer_id
                    and self.turn_confirmation_player_id is None
                ):
                    text = f"Waiting for {state.active_player.name} to place - {empty} slots remain"
        elif state.phase == GamePhase.REVEAL:
            text = self.last_card_reveal or f"Revealing Row {state.current_row.number}"
        elif state.phase == GamePhase.ROW_SCORING:
            text = f"Comparing Player 1 and Player 2 in Row {state.current_row.number}"
        elif state.phase == GamePhase.ROW_RESULTS:
            row = state.current_row
            scores = self._displayed_row_scores(row)
            text = f"Row {row.number}: P1 {scores[state.players[0].id]} - P2 {scores[state.players[1].id]}"
            if row.winner_id:
                text += f" - {state.player(row.winner_id).name} earns {row.awarded_points}"
            else:
                text += " - tie, no points awarded"
        elif state.phase == GamePhase.ROUND_RESULTS:
            text = f"Round {state.round_number} complete. Match points: {state.players[0].match_points} - {state.players[1].match_points}"
        else:
            text = "Match results"
        self._text_center_fit(
            screen, text, (screen.get_width() // 2, layout.status_y), "status_banner", screen.get_width() - 40,
        )

    def _draw_footer(self, screen, layout):
        footer = "Drag cards to slots  -  F11: fullscreen  -  Esc: Menu"
        room_label = getattr(self, "room_label", None)
        if room_label:
            footer += f"  -  {room_label}"
        self._text_center_fit(
            screen, footer, (screen.get_width() // 2, screen.get_height() - 12), "label_small", screen.get_width() - 24,
        )

    def _draw_round_results(self, screen, state, layout):
        self._draw_overlay_backdrop(screen)
        width = min(680, screen.get_width() - 36)
        panel = pygame.Rect(screen.get_width() // 2 - width // 2, screen.get_height() // 2 - 205, width, 410)
        pygame.draw.rect(screen, PANEL, panel, border_radius=18)
        pygame.draw.rect(screen, GOLD, panel, width=2, border_radius=18)
        self._text_center_fit(
            screen, f"ROUND {state.round_number} RESULTS", (panel.centerx, panel.y + 38), "title", panel.width - 28,
        )
        gap = 18
        column_width = (panel.width - 3 * gap) // 2
        for index, player in enumerate(state.players):
            col = pygame.Rect(panel.x + gap + index * (column_width + gap), panel.y + 76, column_width, 132)
            accent = P1 if index == 0 else P2
            pygame.draw.rect(screen, PANEL_LIGHT, col, border_radius=12)
            pygame.draw.rect(screen, accent, col, width=2, border_radius=12)
            self._text_center_fit(screen, player.name, (col.centerx, col.y + 24), "ui", col.width - 16)
            self._text_center(screen, f"{player.match_points}", (col.centerx, col.y + 66), "number_match")
            self._text_center_fit(
                screen, f"ROUND POINTS: {player.round_points}", (col.centerx, col.y + 108),
                "label_small", col.width - 16,
            )
        if state.round_winner_id is None:
            result = "Round tied"
        else:
            result = f"{state.player(state.round_winner_id).name} wins this round"
        self._text_center_fit(screen, result, (panel.centerx, panel.y + 240), "ui", panel.width - 32)
        self._text_center_fit(
            screen, "Cumulative match points carry into the next round.",
            (panel.centerx, panel.y + 275), "label_small", panel.width - 32,
        )
        label = f"Continue to Round {state.round_number + 1}" if state.round_number < state.rounds_per_match else "Show Match Result"
        pygame.draw.rect(screen, (66, 94, 127), layout.round_continue, border_radius=9)
        self._text_center_fit(screen, label, layout.round_continue.center, "ui", layout.round_continue.width - 14)

    def _draw_match_results(self, screen, state, layout):
        self._draw_overlay_backdrop(screen)
        panel = pygame.Rect(screen.get_width() // 2 - 300, screen.get_height() // 2 - 188, 600, 376)
        pygame.draw.rect(screen, PANEL, panel, border_radius=18)
        pygame.draw.rect(screen, GOLD, panel, width=2, border_radius=18)
        if state.winner_id is None:
            headline = "MATCH DRAW"
        else:
            headline = f"{state.player(state.winner_id).name.upper()} WINS"
        self._text_center_fit(screen, headline, (panel.centerx, panel.y + 48), "title", panel.width - 28)
        for index, player in enumerate(state.players):
            x = panel.x + panel.width // 4 + index * panel.width // 2
            self._text_center_fit(screen, player.name, (x, panel.y + 124), "ui", panel.width // 2 - 32)
            self._text_center(screen, str(player.match_points), (x, panel.y + 179), "number_match")
            self._text_center(screen, "MATCH POINTS", (x, panel.y + 211), "label_small")
        pygame.draw.rect(screen, (66, 94, 127), layout.match_continue, border_radius=9)
        self._text_center_fit(
            screen, "Play Again", layout.match_continue.center, "ui", layout.match_continue.width - 16,
        )

    def _draw_overlay_backdrop(self, screen):
        screen.blit(self._overlay(screen.get_size(), (4, 8, 17, 220)), (0, 0))

    @staticmethod
    def _text(screen, text, position, role, max_width=None, *, variant=None, shadow=False):
        screen.blit(render_text(text, role, max_width, variant=variant, shadow=shadow), position)

    @staticmethod
    def _text_right(screen, text, right_top, role, max_width=None, *, variant=None, shadow=False):
        rendered = render_text(text, role, max_width, variant=variant, shadow=shadow)
        screen.blit(rendered, (right_top[0] - rendered.get_width(), right_top[1]))

    @staticmethod
    def _text_center(screen, text, position, role, *, variant=None, shadow=False):
        rendered = render_text(text, role, variant=variant, shadow=shadow)
        screen.blit(rendered, rendered.get_rect(center=position))

    @staticmethod
    def _text_center_fit(screen, text, position, role, max_width, *, variant=None, shadow=False):
        rendered = render_text(text, role, max_width, variant=variant, shadow=shadow)
        screen.blit(rendered, rendered.get_rect(center=position))

    @staticmethod
    def _card_name_lines(name, max_width):
        lines = wrap_text(name, "card_name", max_width)
        if len(lines) > 2:
            lines = lines[:2]
            lines[1] = lines[1].rstrip(" .") + "..."
        return lines

    @staticmethod
    def _draw_centered_name_lines(surface, lines, center, max_width, *, variant=None, top=None):
        line_height = get_font("card_name").get_linesize()
        if top is None:
            first_center_y = center[1] - ((len(lines) - 1) * line_height) // 2
        else:
            first_center_y = top + line_height // 2
        for index, line in enumerate(lines):
            rendered = render_text(line, "card_name", max_width, variant=variant)
            rect = rendered.get_rect(center=(center[0], first_center_y + index * line_height))
            surface.blit(rendered, rect)
        return (len(lines) - 1) * line_height

    @staticmethod
    def _text_center_spaced(screen, text, position, role, spacing, max_width):
        rendered = render_text_with_spacing(text, role, spacing, max_width)
        screen.blit(rendered, rendered.get_rect(center=position))

