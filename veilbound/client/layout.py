from __future__ import annotations

import pygame

from veilbound.shared.models import GameState


SLOT_GAP = 6
ROW_TOP = 128
ROW_GAP = 8
STATUS_GAP = 16
STATUS_CLEARANCE = 18
HAND_BOTTOM_CLEARANCE = 26
HAND_CARD_GAP = 10
HAND_CARD_HEIGHT_RATIO = 0.145
HAND_CARD_MIN_HEIGHT = 84
HAND_CARD_MAX_HEIGHT = 170
CARD_WIDTH_RATIO = 2
CARD_HEIGHT_RATIO = 3

# Debug Mode scrollable hand strip: all 50 roster cards in one horizontal row.
DEBUG_HAND_SIDE_MARGIN = 18
DEBUG_HAND_SCROLLBAR_HEIGHT = 8
DEBUG_HAND_SCROLLBAR_RESERVE = 16
DEBUG_HAND_MIN_THUMB_WIDTH = 30
# Hover magnification: 2× for inspection, selection overrides hover.
DEBUG_HOVER_SCALE = 2.0
DEBUG_HOVER_NEIGHBOR_PUSH = 1.0
DEBUG_HOVER_SECOND_PUSH = 0.5


def clamp_hand_scroll(value: float, maximum: float) -> float:
    """Clamp a horizontal hand-strip offset into its valid range."""
    return max(0.0, min(float(value), max(0.0, float(maximum))))


def debug_hover_index(rects: list[pygame.Rect], mouse_pos: tuple[int, int] | None) -> int | None:
    """Return the index of the hand card under the mouse, if any."""
    if mouse_pos is None:
        return None
    for index, rect in enumerate(rects):
        if rect.collidepoint(mouse_pos):
            return index
    return None


def debug_shifted_rects(
    rects: list[pygame.Rect],
    hover_index: int | None,
    scale: float = DEBUG_HOVER_SCALE,
) -> list[pygame.Rect]:
    """Apply dock-style magnification: the hovered card enlarges (anchored at
    its bottom edge) while its one or two nearest neighbors on each side move
    aside. Cards farther away keep their exact base rects."""
    if hover_index is None or not 0 <= hover_index < len(rects):
        return list(rects)
    base = rects[hover_index]
    growth = base.width * (scale - 1.0)
    shifted: list[pygame.Rect] = []
    for index, rect in enumerate(rects):
        if index == hover_index:
            enlarged = pygame.Rect(0, 0, round(base.width * scale), round(base.height * scale))
            enlarged.centerx = base.centerx
            enlarged.bottom = base.bottom
            shifted.append(enlarged)
            continue
        distance = abs(index - hover_index)
        if distance == 1:
            push = growth * 0.5 * DEBUG_HOVER_NEIGHBOR_PUSH
        elif distance == 2:
            push = growth * 0.5 * DEBUG_HOVER_SECOND_PUSH
        else:
            shifted.append(rect.copy())
            continue
        moved = rect.copy()
        moved.x += round(push) if index > hover_index else -round(push)
        shifted.append(moved)
    return shifted


class BoardLayout:
    """Responsive four-row geometry with portrait-proportioned card slots."""

    def __init__(self, size: tuple[int, int], state: GameState, debug_mode: bool = False):
        width, height = size
        self.width = width
        self.height = height
        self.row_rects: dict[str, pygame.Rect] = {}
        self.capacity_boxes: dict[str, pygame.Rect] = {}
        self.slot_rects: dict[tuple[str, int], pygame.Rect] = {}
        # Debug Mode shows the active player's full 50-card hand in one
        # horizontally scrollable row. Same card art/styling, same placement.
        self.debug_mode = debug_mode

        hand_player = state.player(state.viewer_id) if state.viewer_id else state.active_player
        hand_count = len(hand_player.hand)
        # Debug Mode needs headroom: the hovered card enlarges upward from the
        # hand strip, so reserve that growth above the strip (below the status
        # line) instead of overlapping the board rows.
        debug_reserve = 0
        target_hand_height = min(
            HAND_CARD_MAX_HEIGHT,
            max(HAND_CARD_MIN_HEIGHT, round(height * HAND_CARD_HEIGHT_RATIO)),
        )
        hand_width = max(1, target_hand_height * CARD_WIDTH_RATIO // CARD_HEIGHT_RATIO)
        if debug_mode:
            debug_card_w = max(1, round(hand_width * 0.52))
            debug_card_h = max(1, round(debug_card_w * CARD_HEIGHT_RATIO / CARD_WIDTH_RATIO))
            debug_reserve = max(0, round(debug_card_h * (DEBUG_HOVER_SCALE - 1.0)))
        available_hand_width = max(1, width - 64)
        hand_gap = HAND_CARD_GAP
        if debug_mode:
            # One true-size card per roster entry in a single horizontal row.
            # The strip is intentionally wider than the screen; the input
            # controller owns the scroll offset and the renderer clips it.
            hand_gap = HAND_CARD_GAP
            hand_width = debug_card_w
            hand_height = debug_card_h
        elif hand_count:
            if hand_count > 1:
                hand_gap = min(
                    HAND_CARD_GAP,
                    max(0, (available_hand_width - hand_count) // (hand_count - 1)),
                )
            width_for_each_hand_card = max(
                1,
                (available_hand_width - hand_gap * (hand_count - 1)) // hand_count,
            )
            hand_width = min(hand_width, width_for_each_hand_card)
            hand_height = max(1, round(hand_width * CARD_HEIGHT_RATIO / CARD_WIDTH_RATIO))
        else:
            hand_height = max(1, round(hand_width * CARD_HEIGHT_RATIO / CARD_WIDTH_RATIO))
        if debug_mode:
            hand_height_with_bar = hand_height + DEBUG_HAND_SCROLLBAR_RESERVE
        else:
            hand_height_with_bar = hand_height
        hand_top = height - HAND_BOTTOM_CLEARANCE - hand_height_with_bar - debug_reserve
        status_y = hand_top - debug_reserve - STATUS_GAP

        row_count = max(1, len(state.rows))
        row_gap = ROW_GAP
        rows_bottom = status_y - STATUS_CLEARANCE
        row_height = max(
            1,
            (rows_bottom - ROW_TOP - row_gap * (row_count - 1)) // row_count,
        )
        max_capacity = max((row.capacity for row in state.rows), default=1)
        self.status_y = status_y
        self.hand_top = hand_top

        row_left = 18
        row_width = max(1, width - 36)
        for row_index, row in enumerate(state.rows):
            y = ROW_TOP + row_index * (row_height + row_gap)
            row_rect = pygame.Rect(row_left, y, row_width, row_height)
            box = pygame.Rect(row_left, y, row_height, row_height)
            self.row_rects[row.id] = row_rect
            self.capacity_boxes[row.id] = box

            slot_left = box.right + 12
            slot_right = row_rect.right - 12
            available = max(1, slot_right - slot_left)
            slot_gap = min(
                SLOT_GAP,
                max(0, (available - max_capacity) // max(1, max_capacity - 1)),
            )
            width_for_each_slot = max(
                1,
                (available - slot_gap * (max_capacity - 1)) // max_capacity,
            )
            max_slot_height = max(1, row_height - 10)
            width_for_row_height = max(1, max_slot_height * CARD_WIDTH_RATIO // CARD_HEIGHT_RATIO)
            slot_width = min(width_for_each_slot, width_for_row_height)
            slot_height = min(
                max_slot_height,
                max(1, round(slot_width * CARD_HEIGHT_RATIO / CARD_WIDTH_RATIO)),
            )

            total_width = row.capacity * slot_width + (row.capacity - 1) * slot_gap
            start_x = slot_left + max(0, (available - total_width) // 2)
            slot_y = y + (row_height - slot_height) // 2
            for slot_index in range(row.capacity):
                x = start_x + slot_index * (slot_width + slot_gap)
                self.slot_rects[(row.id, slot_index)] = pygame.Rect(x, slot_y, slot_width, slot_height)

        self.hand_cards: list[pygame.Rect] = []
        self.hand_gap = hand_gap
        self.hand_card_size = (hand_width, hand_height)
        # Debug Mode scroll metrics; the controller stores the live offset.
        self.debug_viewport = pygame.Rect(0, 0, 0, 0)
        self.debug_track = pygame.Rect(0, 0, 0, 0)
        self.debug_content_width = 0
        self.debug_max_scroll = 0.0
        if debug_mode and hand_count:
            viewport = pygame.Rect(
                DEBUG_HAND_SIDE_MARGIN,
                hand_top,
                max(1, width - DEBUG_HAND_SIDE_MARGIN * 2),
                hand_height,
            )
            content_width = hand_width * hand_count + hand_gap * (hand_count - 1)
            self.debug_viewport = viewport
            self.debug_track = pygame.Rect(
                viewport.x,
                viewport.bottom + (DEBUG_HAND_SCROLLBAR_RESERVE - DEBUG_HAND_SCROLLBAR_HEIGHT) // 2,
                viewport.width,
                DEBUG_HAND_SCROLLBAR_HEIGHT,
            )
            self.debug_content_width = content_width
            self.debug_max_scroll = max(0.0, float(content_width - viewport.width))
            self.hand_cards = [
                pygame.Rect(
                    viewport.x + index * (hand_width + hand_gap),
                    hand_top,
                    hand_width,
                    hand_height,
                )
                for index in range(hand_count)
            ]
        elif hand_count:
            total_width = hand_width * hand_count + hand_gap * (hand_count - 1)
            start = max(0, (width - total_width) // 2)
            self.hand_cards = [
                pygame.Rect(start + index * (hand_width + hand_gap), hand_top, hand_width, hand_height)
                for index in range(hand_count)
            ]

        self.round_continue = pygame.Rect(width // 2 - 120, height // 2 + 95, 240, 48)
        self.match_continue = pygame.Rect(width // 2 - 110, height // 2 + 90, 220, 48)

    def debug_thumb_rect(self, scroll: float) -> pygame.Rect:
        """Scrollbar thumb rect for a scroll offset inside ``debug_track``."""
        track = self.debug_track
        if track.width <= 0:
            return pygame.Rect(0, 0, 0, 0)
        if self.debug_max_scroll <= 0 or self.debug_content_width <= 0:
            return track.copy()
        visible_ratio = min(1.0, track.width / float(self.debug_content_width))
        thumb_width = max(DEBUG_HAND_MIN_THUMB_WIDTH, round(track.width * visible_ratio))
        thumb_width = min(thumb_width, track.width)
        travel = track.width - thumb_width
        fraction = clamp_hand_scroll(scroll, self.debug_max_scroll) / self.debug_max_scroll
        return pygame.Rect(
            track.x + round(travel * fraction),
            track.y,
            thumb_width,
            track.height,
        )

    def debug_visible_cards(
        self, scroll: float, mouse_pos: tuple[int, int] | None = None
    ) -> tuple[list[pygame.Rect], int | None]:
        """On-screen hand rects at ``scroll`` plus the hovered card index.

        Base rects are shifted left by the scroll offset and clipped to the
        viewport; hover magnification is computed from what is actually
        visible so hit-testing, tooltips, and drawing always agree.
        """
        offset = clamp_hand_scroll(scroll, self.debug_max_scroll)
        shifted_origin = [
            pygame.Rect(
                rect.x - round(offset),
                rect.y,
                rect.width,
                rect.height,
            )
            for rect in self.hand_cards
        ]
        viewport = self.debug_viewport
        start = next(
            (index for index, rect in enumerate(shifted_origin) if rect.right > viewport.x),
            0,
        )
        end = next(
            (index for index, rect in enumerate(shifted_origin) if rect.x >= viewport.right),
            len(shifted_origin),
        )
        window = shifted_origin[start:end]
        hover_local = debug_hover_index(window, mouse_pos)
        hover_index = start + hover_local if hover_local is not None else None
        if hover_index is not None:
            full = debug_shifted_rects(shifted_origin, hover_index)
        else:
            full = shifted_origin
        visible = [
            rect
            for rect in full
            if rect.right > viewport.x and rect.x < viewport.right
        ]
        return visible, hover_index
