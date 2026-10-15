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


class BoardLayout:
    """Responsive four-row geometry with portrait-proportioned card slots."""

    def __init__(self, size: tuple[int, int], state: GameState):
        width, height = size
        self.width = width
        self.height = height
        self.row_rects: dict[str, pygame.Rect] = {}
        self.capacity_boxes: dict[str, pygame.Rect] = {}
        self.slot_rects: dict[tuple[str, int], pygame.Rect] = {}

        hand_player = state.player(state.viewer_id) if state.viewer_id else state.active_player
        hand_count = len(hand_player.hand)
        target_hand_height = min(
            HAND_CARD_MAX_HEIGHT,
            max(HAND_CARD_MIN_HEIGHT, round(height * HAND_CARD_HEIGHT_RATIO)),
        )
        hand_width = max(1, target_hand_height * CARD_WIDTH_RATIO // CARD_HEIGHT_RATIO)
        available_hand_width = max(1, width - 64)
        hand_gap = HAND_CARD_GAP
        if hand_count:
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
        hand_top = height - HAND_BOTTOM_CLEARANCE - hand_height
        status_y = hand_top - STATUS_GAP

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
        if hand_count:
            total_width = hand_width * hand_count + hand_gap * (hand_count - 1)
            start = max(0, (width - total_width) // 2)
            self.hand_cards = [
                pygame.Rect(start + index * (hand_width + hand_gap), hand_top, hand_width, hand_height)
                for index in range(hand_count)
            ]

        self.round_continue = pygame.Rect(width // 2 - 120, height // 2 + 95, 240, 48)
        self.match_continue = pygame.Rect(width // 2 - 110, height // 2 + 90, 220, 48)
