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
        self.renderer.sogeking_dialogue_duration = (
            max(0.0, float(audio.sogeking_dialogue_duration)) if audio is not None else 0.0
        )
        self.drag_id: str | None = None
        self.hover_index: int | None = None
        self.mouse_pos: tuple[int, int] | None = None
        self.animation_deadline: float | None = None
        self.animation_stage: str | None = None
        self.turn_confirmation_player_id: str | None = None
        self.remote = bool(getattr(engine, "is_remote", False))
        self._crazy_slots_completion_sent: str | None = None
        self._crazy_slots_spin_requested_for: str | None = None
        self._crazy_slots_landing_played_for: str | None = None
        self._crazy_slots_last_tick: tuple[str, int] | None = None
        self._sogeking_shot_completion_sent: str | None = None
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
        if self.engine.state.get("pending_sogeking_shot"):
            if event.type == pygame.MOUSEMOTION:
                self.mouse_pos = event.pos
            return True
        pending_crazy = self.engine.state.get("pending_crazy_slots")
        if pending_crazy:
            # Crazy Slots owns the interaction until its authoritative result
            # is applied. This prevents escape, placement, and duplicate spin
            # inputs from skipping or replaying the one-shot ability.
            if event.type == pygame.MOUSEMOTION:
                self.mouse_pos = event.pos
                return True
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_TAB:
                    self.renderer.crazy_slots_button_focused = not self.renderer.crazy_slots_button_focused
                    return True
                if event.key in (pygame.K_RETURN, pygame.K_SPACE):
                    if self.renderer.crazy_slots_button_focused:
                        status = str(pending_crazy.get("status", ""))
                        if status == "awaiting_spin" and self.renderer.crazy_slots_spin_ready:
                            self._request_crazy_slots_spin(pending_crazy)
                        elif (status == "spinning"
                              and self.renderer.crazy_slots_animation_complete
                              and not self.renderer.crazy_slots_continue_pending):
                            self._continue_crazy_slots(pending_crazy)
                    return True
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                owner_id = str(pending_crazy.get("owner_id", ""))
                viewer_id = str(getattr(self.engine, "player_id", "") or owner_id)
                if viewer_id == owner_id:
                    if (pending_crazy.get("status") == "awaiting_spin"
                            and self.renderer.crazy_slots_spin_ready
                            and self.renderer.crazy_slots_spin_rect is not None
                            and self.renderer.crazy_slots_spin_rect.collidepoint(event.pos)):
                        self._request_crazy_slots_spin(pending_crazy)
                    elif (pending_crazy.get("status") == "spinning"
                            and self.renderer.crazy_slots_animation_complete
                            and not self.renderer.crazy_slots_continue_pending
                            and self.renderer.crazy_slots_continue_rect is not None
                            and self.renderer.crazy_slots_continue_rect.collidepoint(event.pos)):
                        self._continue_crazy_slots(pending_crazy)
                return True
            return True
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
            # An armed elimination intercepts clicks: only highlighted targets
            # are interactive until the choice resolves (or the pause clears).
            if state is not None and state.get("pending_elimination"):
                pending = state.pending_elimination
                if not pending.get("selection_ready", True):
                    return True
                self._handle_elimination_click(event.pos, screen)
                return True
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

    def _request_crazy_slots_spin(self, pending: dict) -> None:
        owner_id = str(pending.get("owner_id", ""))
        viewer_id = str(getattr(self.engine, "player_id", "") or owner_id)
        if viewer_id != owner_id:
            return
        if self.remote:
            result = self.engine.spin_crazy_slots()
            if result.accepted:
                self._crazy_slots_spin_requested_for = str(pending.get("owner_card_id", ""))
                self.renderer.crazy_slots_spin_ready = False
        else:
            result = self.engine.spin_crazy_slots(owner_id)
        self._apply(result)

    def _continue_crazy_slots(self, pending: dict) -> None:
        if self.remote:
            result = self.engine.complete_crazy_slots()
            if result.accepted:
                self._crazy_slots_completion_sent = str(pending.get("spin_id", ""))
                self.renderer.crazy_slots_continue_pending = True
        else:
            result = self.engine.complete_crazy_slots(str(pending.get("owner_id", "")))
        self._apply(result)
        if not result.accepted:
            self.renderer.crazy_slots_continue_pending = False
            self._crazy_slots_completion_sent = None
            return
        self.renderer.crazy_slots_overlay_ready = False
        self.renderer.crazy_slots_animation_complete = False
        if not self.remote:
            if self.engine.state.get("pending_elimination"):
                self.animation_stage = None
                self.animation_deadline = None
            elif self.engine.state.phase == GamePhase.REVEAL:
                self.animation_stage = "reveal_gap"
                self.animation_deadline = time.monotonic() + self._seconds("delay_between_reveals_ms")
            else:
                self.animation_stage = None
                self.animation_deadline = None

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
                if "reconnect" in notice.lower() or self.renderer.crazy_slots_continue_pending:
                    self.renderer.crazy_slots_continue_pending = False
                    self._crazy_slots_completion_sent = None
            if self.animation_stage == "await_reveal":
                if self.engine.remote_locked:
                    return
                self.animation_stage = None
                self.animation_deadline = None
        sogeking_shot = self.engine.state.get("pending_sogeking_shot")
        if sogeking_shot:
            shot_id = str(sogeking_shot.get("shot_id", ""))
            if (shot_id and self.renderer.sogeking_shot_complete
                    and self._sogeking_shot_completion_sent != shot_id):
                self._sogeking_shot_completion_sent = shot_id
                result = self.engine.complete_sogeking_shot(shot_id)
                if not self.remote:
                    self._apply(result, now)
                    if result.accepted and self.engine.state.phase == GamePhase.ROW_RESULTS:
                        self.animation_stage = "score_count"
                        self.animation_deadline = now + self._seconds("score_count_ms")
            return
        self._sogeking_shot_completion_sent = None
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
            pending = self.engine.state.get("pending_elimination") or {}
            if not self.remote and pending and phase != GamePhase.REVEAL:
                pending["selection_ready"] = True
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

        pending_crazy = self.engine.state.get("pending_crazy_slots")
        if pending_crazy:
            status = str(pending_crazy.get("status", ""))
            if status == "awaiting_spin":
                if self.animation_stage == "reveal_gap":
                    if now < self._deadline():
                        return
                    if self.remote:
                        # A revealed card is acknowledged only after its flip,
                        # information, and reveal-gap presentation completes.
                        # The server keeps the room locked until both peers
                        # acknowledge, so do this before waiting for unlock.
                        self._reveal_next_card(now)
                        return
                    self.animation_stage = None
                    self.animation_deadline = None
                elif self.animation_stage is not None:
                    return
                if self.remote and self.engine.remote_locked:
                    return
                if (self.remote and self._crazy_slots_spin_requested_for
                        == str(pending_crazy.get("owner_card_id", ""))):
                    return
                self.renderer.crazy_slots_overlay_ready = True
                self.renderer.crazy_slots_spin_ready = True
                return

            if status == "spinning":
                self._crazy_slots_spin_requested_for = None
                spin_id = str(pending_crazy.get("spin_id", ""))
                if (self.remote
                        and self._crazy_slots_completion_sent == spin_id):
                    # Keep the overlay closed after the owner presses Continue
                    # while the authoritative result is in flight.
                    return
                self.renderer.crazy_slots_overlay_ready = True
                _distance, speed, _progress, done = self.renderer._crazy_slots_motion(pending_crazy, now)
                self.renderer.crazy_slots_animation_complete = done
                digit = self.renderer.crazy_slots_current_digit
                tick_key = (spin_id, self.renderer.crazy_slots_current_cell)
                if tick_key != self._crazy_slots_last_tick:
                    if self.audio is not None:
                        self.audio.play_crazy_slots_tick(spin_id, digit, speed)
                    self._crazy_slots_last_tick = tick_key
                if done and spin_id != self._crazy_slots_landing_played_for:
                    if self.audio is not None:
                        self.audio.play_crazy_slots_landing(spin_id)
                    self._crazy_slots_landing_played_for = spin_id
                return

        self.renderer.crazy_slots_spin_ready = False
        self.renderer.crazy_slots_overlay_ready = False
        self.renderer.crazy_slots_continue_rect = None
        self._crazy_slots_completion_sent = None
        self._crazy_slots_spin_requested_for = None
        self._crazy_slots_landing_played_for = None
        self._crazy_slots_last_tick = None

        # An armed elimination pauses reveals/scoring until a target is chosen.
        # Let the active reveal gap / duo animation finish before enabling any
        # target menu; remote clients continue the reveal-ack handshake too.
        if self.engine.state.get("pending_elimination"):
            pending = self.engine.state.pending_elimination
            if self.remote and pending.get("selection_ready"):
                # The reveal acknowledgement has released the server lock;
                # leave the target-selection state idle until the owner picks.
                self.animation_stage = None
                self.animation_deadline = None
                return
            allowed_stages = {"reveal_gap", "duo_energy"}
            if self.remote:
                allowed_stages.add("await_reveal")
            if self.animation_stage not in allowed_stages:
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
            pending = self.engine.state.get("pending_elimination") or {}
            if not self.remote and pending:
                pending["selection_ready"] = True
                self.animation_stage = None
                self.animation_deadline = None
                return
            self._reveal_next_card(now)
            return

        if self.animation_stage == "duo_energy":
            if now < self._deadline():
                return
            pending = self.engine.state.get("pending_elimination") or {}
            if not self.remote and pending:
                pending["selection_ready"] = True
                self.animation_stage = None
                self.animation_deadline = None
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

    def _handle_elimination_click(self, pos: tuple[int, int], screen) -> None:
        """Resolve an armed elimination from a click on a highlighted target."""
        state = self.engine.state
        pending = state.get("pending_elimination") or {}
        if self.remote and str(pending.get("attacker_owner_id", "")) != str(self.engine.player_id):
            self.renderer.notice = "The opponent chooses the ability target."
            self.renderer.notice_until = time.monotonic() + 3
            return
        layout = self.renderer.layout(state, screen.get_size())
        if pending.get("effect_type") == "kamui_exchange_rows":
            choosing_first_row = pending.get("stage") == "kamui_first_row"
            valid_rows = {
                str(item.get("row_id", ""))
                for item in (pending.get("valid_targets") or [])
                if item.get("target_kind") == "row"
            }
            selected_row_id = next((
                row_id for row_id in valid_rows
                if (rect := layout.row_rects.get(row_id)) is not None
                and rect.collidepoint(pos)
            ), None)
            if selected_row_id is None:
                self.renderer.notice = "Choose a highlighted row for Kamui."
                self.renderer.notice_until = time.monotonic() + 2.5
                return
            result = self.engine.select_elimination_target(selected_row_id, -1)
            self._apply(result)
            if result.accepted and choosing_first_row:
                self.renderer.notice = "Now choose the second highlighted row to exchange values."
                self.renderer.notice_until = time.monotonic() + 3
            return
        if (pending.get("effect_type") == "move_character"
                and pending.get("stage") == "destination_row"):
            valid_rows = {
                str(item.get("row_id", ""))
                for item in (pending.get("valid_targets") or [])
                if item.get("target_kind") == "row"
            }
            destination_row_id = next((
                row_id for row_id in valid_rows
                if (rect := layout.row_rects.get(row_id)) is not None
                and rect.collidepoint(pos)
            ), None)
            if destination_row_id is None:
                self.renderer.notice = "Click a highlighted row with an open slot."
                self.renderer.notice_until = time.monotonic() + 2.5
                return
            result = self.engine.select_destination_row(destination_row_id)
            self._apply(result)
            return
        valid = [
            (str(item.get("row_id", "")), int(item.get("slot_index", -1)))
            for item in (pending.get("valid_targets") or [])
            if item.get("target_kind", "character") == "character"
        ]
        hit = next(
            (
                key for key in valid
                if (rect := layout.slot_rects.get(key)) is not None and rect.collidepoint(pos)
            ),
            None,
        )
        if hit is None:
            if pending.get("effect_type") == "revive_target":
                self.renderer.notice = "Click a highlighted eliminated card to revive it."
            elif pending.get("effect_type") == "steal_target_bonus":
                self.renderer.notice = "Click a highlighted card to steal its bonus points."
            elif pending.get("effect_type") == "remove_target_bonus":
                self.renderer.notice = "Click a highlighted card to remove its bonus points."
            elif pending.get("effect_type") == "copy_target_bonus":
                if pending.get("stage") == "copy_transfer_target":
                    amount = max(0, int(pending.get("copied_bonus", 0)))
                    self.renderer.notice = f"Choose any highlighted character to receive +{amount} bonus points."
                else:
                    self.renderer.notice = "Choose an opposing character with bonus points to copy."
            elif pending.get("effect_type") == "eliminate_target":
                self.renderer.notice = "Click a highlighted card to eliminate it."
            elif pending.get("effect_type") == "move_character":
                self.renderer.notice = "Click a highlighted opposing character in this row."
            else:
                self.renderer.notice = "Click a highlighted card to resolve the ability."
            self.renderer.notice_until = time.monotonic() + 2.5
            return
        row_id, slot_index = hit
        result = self.engine.select_elimination_target(row_id, slot_index)
        # Local engines resolve inline (events flow through _apply); remote
        # engines send the request and the events arrive with the next update.
        self._apply(result)
        if (result.accepted and pending.get("kite_crazy_slots")
                and not self.engine.state.get("pending_elimination") and not self.remote
                and self.engine.state.phase == GamePhase.REVEAL):
            self.animation_stage = "reveal_gap"
            self.animation_deadline = time.monotonic() + self._seconds("delay_between_reveals_ms")
        if (result.accepted and pending.get("effect_type") == "move_character"):
            self.renderer.notice = "Now choose a highlighted row for the selected character."
            self.renderer.notice_until = time.monotonic() + 3
        if (result.accepted and pending.get("effect_type") == "copy_target_bonus"
                and self.engine.state.get("pending_elimination")
                and self.engine.state.pending_elimination.get("stage") == "copy_transfer_target"):
            amount = max(0, int(self.engine.state.pending_elimination.get("copied_bonus", 0)))
            self.renderer.notice = f"Now choose any character to receive +{amount} bonus points."
            self.renderer.notice_until = time.monotonic() + 3

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
            for event in result.events:
                if (event.kind == "target_selection_updated"
                        and str(event.data.get("ability_id", "")) == "hisoka_bungee_gum_copy_bonus"
                        and str(event.data.get("stage", "")) == "copy_transfer_target"):
                    amount = max(0, int(event.data.get("amount", 0)))
                    self.renderer.notice = f"Now choose any character to receive +{amount} bonus points."
                    self.renderer.notice_until = time.monotonic() + 3
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

