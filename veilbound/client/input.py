from __future__ import annotations

import time

import pygame

from .layout import debug_hover_index
from veilbound.shared.models import ActionResult, GamePhase
from .audio import AudioManager
from .renderer import Renderer

# Pygame wheel buttons: 4 = up, 5 = down. Older Pygame also emits
# MOUSEWHEEL with x/y deltas; both are handled for reliability.
_WHEEL_UP_BUTTONS = (4,)
_WHEEL_DOWN_BUTTONS = (5,)
_DEBUG_SCROLL_WHEEL_STEP = 120
_DEBUG_SCROLL_KEY_STEP = 160
_DEBUG_SCROLL_PAGE_STEP = 480


class InputController:
    def __init__(self, engine: GameEngine, renderer: Renderer, audio: AudioManager | None = None):
        self.engine = engine
        self.renderer = renderer
        self.audio = audio
        self.drag_id: str | None = None
        self.hover_index: int | None = None
        self.mouse_pos: tuple[int, int] | None = None
        self.animation_deadline: float | None = None
        self.animation_stage: str | None = None
        self.turn_confirmation_player_id: str | None = None
        self.remote = bool(getattr(engine, "is_remote", False))
        # Debug Mode scrollbar drag state.
        self._debug_scrollbar_drag: bool = False
        self._debug_scrollbar_grab_offset: float = 0.0
        if not self.remote and engine.state.phase == GamePhase.PLACEMENT:
            self._require_turn_confirmation(engine.state.active_player.id)

    @property
    def _debug_active(self) -> bool:
        return bool(getattr(self.renderer, "debug_mode", False)) and bool(
            getattr(self.engine, "debug_mode", False)
        )

    def _debug_layout(self, state, screen):
        layout = self.renderer.layout(state, screen.get_size())
        from veilbound.client.layout import clamp_hand_scroll

        self.renderer.debug_scroll = clamp_hand_scroll(
            self.renderer.debug_scroll, layout.debug_max_scroll
        )
        return layout

    def _scroll_debug_hand(self, delta: float, state, screen) -> None:
        from veilbound.client.layout import clamp_hand_scroll

        layout = self.renderer.layout(state, screen.get_size())
        self.renderer.debug_scroll = clamp_hand_scroll(
            self.renderer.debug_scroll + delta, layout.debug_max_scroll
        )

    def _press_debug_scrollbar(self, pos: tuple[int, int], layout) -> bool:
        """Start dragging the debug scrollbar track or thumb."""
        if not self._debug_active:
            return False
        track = layout.debug_track
        if track.width <= 0:
            return False
        thumb = layout.debug_thumb_rect(self.renderer.debug_scroll)
        if thumb.collidepoint(pos):
            self._debug_scrollbar_drag = True
            self._debug_scrollbar_grab_offset = pos[0] - thumb.x
            self.mouse_pos = pos
            return True
        if track.collidepoint(pos):
            fraction = max(0.0, min(1.0, (pos[0] - track.x) / max(1, track.width)))
            self.renderer.debug_scroll = fraction * layout.debug_max_scroll
            self.mouse_pos = pos
            self._debug_scrollbar_drag = True
            self._debug_scrollbar_grab_offset = 0.0
            return True
        return False

    def _drag_debug_thumb(self, pos: tuple[int, int], screen) -> None:
        """Continue scrolling the debug hand by dragging the thumb."""
        if not self._debug_scrollbar_drag or not self._debug_active:
            return
        layout = self.renderer.layout(self.engine.state, screen.get_size())
        track = layout.debug_track
        if track.width <= 0:
            return
        thumb = layout.debug_thumb_rect(self.renderer.debug_scroll)
        fraction = max(0.0, min(1.0, (pos[0] - thumb.x + self._debug_scrollbar_grab_offset) / max(1, track.width)))
        self.renderer.debug_scroll = fraction * layout.debug_max_scroll
        self.mouse_pos = pos

    def handle(self, event: pygame.event.Event, screen) -> bool | str:
        if event.type == pygame.QUIT:
            return False
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                return "pause"
            if event.key in (pygame.K_RETURN, pygame.K_SPACE):
                if not self._confirm_turn():
                    self._continue_results()
            elif event.key == pygame.K_r and self.engine.state.phase == GamePhase.MATCH_RESULTS:
                self._restart()
            elif self._debug_active and self.engine.state.phase == GamePhase.PLACEMENT:
                if event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                    step = -_DEBUG_SCROLL_KEY_STEP if event.key == pygame.K_LEFT else _DEBUG_SCROLL_KEY_STEP
                    self._scroll_debug_hand(step, self.engine.state, screen)
                elif event.key in (pygame.K_HOME, pygame.K_END):
                    from veilbound.client.layout import clamp_hand_scroll

                    layout = self.renderer.layout(self.engine.state, screen.get_size())
                    target = 0.0 if event.key == pygame.K_HOME else layout.debug_max_scroll
                    self.renderer.debug_scroll = clamp_hand_scroll(target, layout.debug_max_scroll)
            return True
        if event.type == pygame.MOUSEMOTION:
            self.mouse_pos = event.pos
            if self._debug_scrollbar_drag and self._debug_active:
                self._drag_debug_thumb(event.pos, screen)
            elif self.engine.state.phase == GamePhase.PLACEMENT:
                layout = self.renderer.layout(self.engine.state, screen.get_size())
                if getattr(layout, "debug_mode", False) and self.renderer.debug_mode:
                    _, self.hover_index = layout.debug_visible_cards(self.renderer.debug_scroll, event.pos)
                else:
                    self.hover_index = debug_hover_index(layout.hand_cards, event.pos)
            return True
        if event.type == pygame.MOUSEWHEEL:
            # Modern Pygame: horizontal cards scroll via the vertical wheel,
            # Shift + wheel, or a horizontal wheel/touchpad gesture.
            if self._debug_active and self.engine.state.phase == GamePhase.PLACEMENT:
                delta = event.y + event.x
                if delta:
                    self._scroll_debug_hand(-delta * _DEBUG_SCROLL_WHEEL_STEP, self.engine.state, screen)
                    self.mouse_pos = getattr(event, "pos", self.mouse_pos) or pygame.mouse.get_pos()
                    return True
            return True
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            state = self.engine.state
            if state.phase == GamePhase.PLACEMENT and self.turn_confirmation_player_id is not None:
                self._confirm_turn()
                return True
            layout = self.renderer.layout(state, screen.get_size())
            if state.phase == GamePhase.ROUND_RESULTS and layout.round_continue.collidepoint(event.pos):
                self._continue_results()
                return True
            if state.phase == GamePhase.MATCH_RESULTS and layout.match_continue.collidepoint(event.pos):
                self._restart()
                return True
            if state.phase == GamePhase.PLACEMENT and self.animation_stage is None and not getattr(self.engine, "remote_locked", False):
                if self._debug_active:
                    if self._press_debug_scrollbar(event.pos, layout):
                        return True
                    _, hover_index = layout.debug_visible_cards(self.renderer.debug_scroll, event.pos)
                    if hover_index is not None and 0 <= hover_index < len(state.active_player.hand):
                        self.drag_id = state.active_player.hand[hover_index].id
                        self.hover_index = None
                        self.mouse_pos = event.pos
                    return True
                for card, rect in zip(state.active_player.hand, layout.hand_cards):
                    if rect.collidepoint(event.pos):
                        self.drag_id = card.id
                        self.hover_index = None
                        self.mouse_pos = event.pos
                        break
            return True
        if event.type == pygame.MOUSEBUTTONDOWN and event.button in _WHEEL_UP_BUTTONS + _WHEEL_DOWN_BUTTONS:
            # Legacy Pygame wheel-as-buttons: vertical wheel (or Shift + wheel,
            # which SDL reports as vertical) scrolls the horizontal strip.
            if self._debug_active and self.engine.state.phase == GamePhase.PLACEMENT:
                step = _DEBUG_SCROLL_WHEEL_STEP if event.button in _WHEEL_DOWN_BUTTONS else -_DEBUG_SCROLL_WHEEL_STEP
                self._scroll_debug_hand(step, self.engine.state, screen)
                return True
            return True
        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if self.drag_id and self.animation_stage is None and self.engine.state.phase == GamePhase.PLACEMENT and not getattr(self.engine, "remote_locked", False):
                layout = self.renderer.layout(self.engine.state, screen.get_size())
                target = next((slot for slot, rect in layout.slot_rects.items() if rect.collidepoint(event.pos)), None)
                if target:
                    row_id, slot_index = target
                    result = self.engine.play_card(
                        self.engine.state.active_player.id, self.drag_id, row_id, slot_index,
                    )
                    self._apply(result)
                else:
                    self.renderer.notice = "Drop the card into a valid empty slot."
                    self.renderer.notice_until = time.monotonic() + 2.5
            self.drag_id = None
            self.mouse_pos = None
            if self._debug_scrollbar_drag and self._debug_active:
                self._debug_scrollbar_drag = False
            return True
        return True

    def update(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.remote:
            self.engine.poll()
            events = self.engine.drain_events()
            if events:
                self._apply(ActionResult(True, events=events), now)
            notice = self.engine.drain_notice()
            if notice:
                self.renderer.notice = notice
                self.renderer.notice_until = now + 4
            if self.animation_stage == "await_reveal":
                if self.engine.remote_locked:
                    return
                self.animation_stage = None
                self.animation_deadline = None
        phase = self.engine.state.phase
        if phase == GamePhase.MATCH_RESULTS:
            self.animation_deadline = None
            self.animation_stage = None
            return
        if phase == GamePhase.ROUND_RESULTS:
            if self.animation_stage != "round_results":
                self.animation_stage = "round_results"
                self.animation_deadline = now + self._seconds("final_round_result_ms")
            if self.animation_deadline is not None and now >= self.animation_deadline:
                self._continue_results()
            return
        if phase not in {GamePhase.PLACEMENT, GamePhase.REVEAL, GamePhase.ROW_SCORING, GamePhase.ROW_RESULTS}:
            self.animation_deadline = None
            self.animation_stage = None
            return

        if self.animation_stage == "card_flip":
            if now < self._deadline():
                return
            self.animation_stage = "card_info"
            self.animation_deadline = now + self._seconds("card_information_ms")
            return

        if self.animation_stage == "card_info":
            if now < self._deadline():
                return
            if phase == GamePhase.REVEAL:
                self.animation_stage = "reveal_gap"
                self.animation_deadline = now + self._seconds("delay_between_reveals_ms")
            elif self.remote:
                self._reveal_next_card(now)
                self.animation_stage = "await_reveal"
                self.animation_deadline = None
            else:
                self.animation_stage = None
                self.animation_deadline = None
            return

        if phase == GamePhase.PLACEMENT:
            # Fresh round: drop presentation stages left over from an earlier
            # results screen (they block drag input and are no longer valid).
            if self.animation_stage in {"round_results", "row_result", "row_transition", "score_count"}:
                self.animation_stage = None
                self.animation_deadline = None
            return

        if self.animation_stage == "reveal_gap":
            if now < self._deadline():
                return
            if self.renderer.duo_animation_pending:
                self.animation_stage = "duo_energy"
                self.animation_deadline = self.renderer.duo_animation_deadline
                return
            self._reveal_next_card(now)
            return

        if self.animation_stage == "duo_energy":
            if now < self._deadline():
                return
            self._reveal_next_card(now)
            return

        if self.animation_stage == "score_count":
            if now < self._deadline():
                return
            self.animation_stage = "row_result"
            self.animation_deadline = now + self._seconds("row_result_ms")
            return

        if self.animation_stage == "row_result":
            if now < self._deadline():
                return
            result = self.engine.advance_after_row_result()
            self._apply(result)
            if result.accepted and self.engine.state.phase in {GamePhase.REVEAL, GamePhase.ROW_SCORING}:
                self.animation_stage = "row_transition"
                self.animation_deadline = now + self._seconds("row_transition_ms")
            elif result.accepted and self.engine.state.phase == GamePhase.ROUND_RESULTS:
                self.animation_stage = "round_results"
                self.animation_deadline = now + self._seconds("final_round_result_ms")
            else:
                self.animation_stage = None
                self.animation_deadline = None
            return

        if self.animation_stage == "row_transition":
            if now < self._deadline():
                return
            self.animation_stage = None
            self.animation_deadline = None
            return

        if phase == GamePhase.REVEAL:
            self._reveal_next_card(now)
        elif phase == GamePhase.ROW_SCORING:
            result = self.engine.score_current_row()
            self._apply(result)
            if result.accepted:
                self.animation_stage = "score_count"
                self.animation_deadline = now + self._seconds("score_count_ms")
        elif phase == GamePhase.ROW_RESULTS:
            self.animation_stage = "row_result"
            self.animation_deadline = now + self._seconds("row_result_ms")

    def _reveal_next_card(self, now: float) -> None:
        result = self.engine.reveal_next_card()
        self._apply(result, now)
        if self.remote and result.accepted and not result.events:
            self.animation_stage = "await_reveal"
            self.animation_deadline = None

    def _seconds(self, setting: str) -> float:
        milliseconds = self.renderer.animation_ms.get(setting, 0)
        return max(0, milliseconds) / 1000

    def _deadline(self) -> float:
        return self.animation_deadline if self.animation_deadline is not None else 0.0

    def _continue_results(self) -> None:
        phase = self.engine.state.phase
        if phase == GamePhase.ROUND_RESULTS:
            result = self.engine.continue_after_round_results()
            self._apply(result)
            self.animation_stage = None
            self.animation_deadline = None
            if not self.remote and self.engine.state.phase == GamePhase.PLACEMENT:
                self.renderer.reset_animations()
                self._require_turn_confirmation(self.engine.state.active_player.id)
        elif phase == GamePhase.MATCH_RESULTS:
            self._restart()

    def _restart(self) -> None:
        self.engine.start_new_match()
        self.renderer.reset_animations()
        if self.audio is not None:
            self.audio.reset_match()
        self.animation_deadline = None
        self.animation_stage = None
        self.drag_id = None
        if not self.remote and self.engine.state.phase == GamePhase.PLACEMENT:
            self._require_turn_confirmation(self.engine.state.active_player.id)

    def _require_turn_confirmation(self, player_id: str | None) -> None:
        self.turn_confirmation_player_id = player_id
        self.renderer.turn_confirmation_player_id = player_id

    def _confirm_turn(self) -> bool:
        if (
            self.turn_confirmation_player_id is None
            or self.engine.state.phase != GamePhase.PLACEMENT
            or self.animation_stage is not None
        ):
            return False
        self._require_turn_confirmation(None)
        return True

    def _apply(self, result, now: float | None = None) -> None:
        if result.accepted:
            self.renderer.note_events(result.events)
            if self.audio is not None:
                self.audio.on_events(result.events)
            if any(event.kind == "card_revealed" for event in result.events):
                animation_now = time.monotonic() if now is None else now
                self.animation_stage = "card_flip"
                self.animation_deadline = animation_now + self._seconds("card_flip_ms")
            if not self.remote and any(event.kind == "turn" for event in result.events) and self.engine.state.phase == GamePhase.PLACEMENT:
                self._require_turn_confirmation(self.engine.state.active_player.id)
            elif self.engine.state.phase != GamePhase.PLACEMENT:
                self._require_turn_confirmation(None)
        elif result.reason:
            self.renderer.notice = result.reason
            self.renderer.notice_until = time.monotonic() + 3

