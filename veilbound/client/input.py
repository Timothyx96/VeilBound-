from __future__ import annotations

import time

import pygame

from veilbound.shared.engine import GameEngine
from veilbound.shared.models import GamePhase
from .audio import AudioManager
from .renderer import Renderer


class InputController:
    def __init__(self, engine: GameEngine, renderer: Renderer, audio: AudioManager | None = None):
        self.engine = engine
        self.renderer = renderer
        self.audio = audio
        self.drag_id: str | None = None
        self.mouse_pos: tuple[int, int] | None = None
        self.animation_deadline: float | None = None
        self.animation_stage: str | None = None
        self.turn_confirmation_player_id: str | None = None
        if engine.state.phase == GamePhase.PLACEMENT:
            self._require_turn_confirmation(engine.state.active_player.id)

    def handle(self, event: pygame.event.Event, screen) -> bool:
        if event.type == pygame.QUIT:
            return False
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                return False
            if event.key in (pygame.K_RETURN, pygame.K_SPACE):
                if not self._confirm_turn():
                    self._continue_results()
            elif event.key == pygame.K_r and self.engine.state.phase == GamePhase.MATCH_RESULTS:
                self._restart()
            return True
        if event.type == pygame.MOUSEMOTION:
            self.mouse_pos = event.pos
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
            if state.phase == GamePhase.PLACEMENT and self.animation_stage is None:
                for card, rect in zip(state.active_player.hand, layout.hand_cards):
                    if rect.collidepoint(event.pos):
                        self.drag_id = card.id
                        self.mouse_pos = event.pos
                        break
            return True
        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if self.drag_id and self.animation_stage is None and self.engine.state.phase == GamePhase.PLACEMENT:
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
            return True
        return True

    def update(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
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
            else:
                self.animation_stage = None
                self.animation_deadline = None
            return

        if phase == GamePhase.PLACEMENT:
            return

        if self.animation_stage == "reveal_gap":
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
            if self.engine.state.phase == GamePhase.PLACEMENT:
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
        if self.engine.state.phase == GamePhase.PLACEMENT:
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
            if any(event.kind == "turn" for event in result.events) and self.engine.state.phase == GamePhase.PLACEMENT:
                self._require_turn_confirmation(self.engine.state.active_player.id)
            elif self.engine.state.phase != GamePhase.PLACEMENT:
                self._require_turn_confirmation(None)
        elif result.reason:
            self.renderer.notice = result.reason
            self.renderer.notice_until = time.monotonic() + 3

