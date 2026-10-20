from __future__ import annotations

import logging
import random
from collections.abc import Iterable, Mapping

from .abilities import AbilityActivation, AbilityManager
from .config import MATCH_ROUNDS, PLAYER_POOL_SIZE, GameConfig
from .definitions import load_card_definitions
from .elimination import EliminationManager, EliminationTargeting
from .models import (
    ActionResult, BattleRow, CardInstance, GameEvent, GamePhase, GameState,
    PlayerState,
)


ROW_COUNT = 4
logger = logging.getLogger(__name__)


def deal_player_pool(roster: Mapping[str, object] | Iterable[str], pool_size: int = PLAYER_POOL_SIZE, seed: int | None = None) -> list[str]:
    """Draw a unique character pool for a player."""
    character_ids = list(roster.keys() if isinstance(roster, Mapping) else roster)
    if pool_size < 0:
        raise ValueError("Pool size cannot be negative")
    if pool_size > len(character_ids):
        raise ValueError(
            f"Cannot deal a {pool_size}-card pool from a {len(character_ids)}-character roster"
        )
    chooser = random if seed is None else random.Random(seed)
    return chooser.sample(character_ids, pool_size)


def calculate_row_scores(row, player_ids: tuple[str, str], cards) -> dict[str, int]:
    """Score revealed cards only, using each instance's current points.

    ``current_points`` starts at the character's immutable ``base_points`` and
    is the only value ability effects modify, so row scoring always reflects
    bonuses granted during reveals. Instances without runtime points (masked
    proxies built by remote views) fall back to the definition's base value.
    """
    scores = {player_id: 0 for player_id in player_ids}
    for card in row.slots:
        if card is not None and card.revealed:
            definition = cards[card.definition_id]
            current = card.get("current_points")
            scores[card.owner_id] += definition.base_points if current is None else int(current)
    return scores


class GameEngine:
    """Pygame-independent authority for card placement and round scoring."""

    def __init__(self, config: GameConfig | None = None, seed: int | None = None, debug_mode: bool = False):
        self.config = config or GameConfig()
        self.cards = load_card_definitions()
        # Debug Mode behaves exactly like Local Mode, except both players can
        # access all 50 roster cards instead of the normal 5-card hand.
        self.debug_mode = debug_mode
        # Single authority for ability registration and resolution.
        self.abilities = AbilityManager(self.cards)
        # Single authority for elimination targeting and resolution.
        self.elimination = EliminationManager(self.cards)
        if self.config.deck_size != PLAYER_POOL_SIZE:
            raise ValueError(f"Each player must have a {PLAYER_POOL_SIZE}-card pool")
        if self.config.initial_hand_size < 0 or self.config.draw_per_turn < 0:
            raise ValueError("Hand and draw sizes cannot be negative")
        if not 1 <= self.config.capacity_min <= self.config.capacity_max <= 10:
            raise ValueError("Row capacities must be between 1 and 10")
        if self.config.rounds_per_match != MATCH_ROUNDS:
            raise ValueError(f"A match must contain exactly {MATCH_ROUNDS} rounds")
        self._rng = random.Random(seed)
        self._card_serial = 0
        self._empty_draw_piles_warned: set[str] = set()
        self.state: GameState
        self.start_new_match(seed=seed)

    def _new_card(self, definition_id: str, owner_id: str) -> CardInstance:
        self._card_serial += 1
        definition = self.cards[definition_id]
        # Fresh instances per match: runtime points start at the base value.
        # base_points is snapshotted on the instance so dynamic bonuses can be
        # added and removed without ever corrupting the original value.
        return CardInstance(
            f"card-{self._card_serial:05d}",
            definition_id,
            owner_id,
            current_points=definition.base_points,
            base_points=definition.base_points,
            applied_bonuses={},
        )

    def start_new_match(self, seed: int | None = None) -> GameState:
        if seed is not None:
            self._rng = random.Random(seed)
        players = [PlayerState("player-1", "Player 1"), PlayerState("player-2", "Player 2")]
        self.state = GameState(players=players, rows=[], rounds_per_match=self.config.rounds_per_match)
        self._empty_draw_piles_warned.clear()
        # Pending ability registrations never carry across matches.
        self.abilities.reset()
        for player in players:
            # Debug Mode: both players get all 50 roster cards directly in the
            # hand (sorted roster order) with an empty draw pile. Standard
            # Local Mode keeps its normal 5-card hand untouched.
            if self.debug_mode:
                player.draw_pile = []
                player.hand = [
                    self._new_card(definition_id, player.id)
                    for definition_id in sorted(self.cards.keys())
                ]
                continue
            pool = deal_player_pool(
                self.cards,
                pool_size=self.config.deck_size,
                seed=self._rng.getrandbits(64),
            )
            player.draw_pile = [self._new_card(definition_id, player.id) for definition_id in pool]
            for _ in range(min(self.config.initial_hand_size, len(player.draw_pile))):
                player.hand.append(player.draw_pile.pop())
        self._prepare_round(starter_index=0)
        return self.state

    def _prepare_round(self, starter_index: int) -> None:
        state = self.state
        # A new round rebuilds the board, so pending ability registrations tied
        # to the previous round's card positions are discarded. Per-instance
        # activation records live on the cards themselves and are unaffected.
        self.abilities.reset()
        state.rows = [
            BattleRow(
                id=f"round-{state.round_number}-row-{number}",
                number=number,
                capacity=self._rng.randint(self.config.capacity_min, self.config.capacity_max),
                slots=[],
            )
            for number in range(1, ROW_COUNT + 1)
        ]
        for row in state.rows:
            row.slots = [None] * row.capacity
        state.active_player_index = starter_index
        state.turn_number = 1
        state.row_index = 0
        state.reveal_card_index = 0
        state.final_reveal_active = False
        state.pending_elimination = None
        state.phase = GamePhase.PLACEMENT
        state.winner_id = None
        state.round_winner_id = None
        state.scored_row_order = []
        state.events = []

        for player in state.players:
            player.round_points = 0
        self._emit("round_started", f"Round {state.round_number} begins. Fill every slot in the four rows.", {"round": state.round_number})

    def legal_slots(self, player_id: str | None = None) -> list[tuple[str, int]]:
        state = self.state
        if state.phase != GamePhase.PLACEMENT:
            return []
        # A pending elimination pauses all placement until a target is chosen.
        if state.get("pending_elimination"):
            return []
        player = state.active_player if player_id is None else state.player(player_id)
        if player.id != state.active_player.id or not player.hand:
            return []
        legal: list[tuple[str, int]] = []
        for row in state.rows:
            next_index = next((index for index, card in enumerate(row.slots) if card is None), None)
            if next_index is not None:
                legal.append((row.id, next_index))
        return legal

    def play_card(self, player_id: str, card_id: str, row_id: str, slot_index: int) -> ActionResult:
        """Place into one empty slot; rejection paths do not mutate the state."""
        state = self.state
        if state.phase != GamePhase.PLACEMENT:
            return ActionResult(False, "Card placement is closed for this round.")
        if state.get("pending_elimination"):
            return ActionResult(False, "Select an elimination target before continuing.")
        try:
            player = state.player(player_id)
        except KeyError:
            return ActionResult(False, "Unknown player.")
        if player.id != state.active_player.id:
            return ActionResult(False, "It is not that player's turn.")
        card = next((item for item in player.hand if item.id == card_id), None)
        if card is None:
            return ActionResult(False, "That card is not in the player's hand.")
        row = next((item for item in state.rows if item.id == row_id), None)
        if row is None:
            return ActionResult(False, "Unknown row.")
        if not isinstance(slot_index, int) or not 0 <= slot_index < row.capacity:
            return ActionResult(False, "Unknown slot.")
        if row.slots[slot_index] is not None:
            return ActionResult(False, "That slot is already occupied.")
        next_slot = next((index for index, placed in enumerate(row.slots) if placed is None), None)
        if slot_index != next_slot:
            return ActionResult(False, "Place the card in the next empty slot from left to right.")

        event_start = len(state.events)
        player.hand.remove(card)
        player.used.append(card)
        row.slots[slot_index] = card
        self._emit("placement", f"{player.name} places a face-down card in Row {row.number}.", {
            "player_id": player.id, "row_id": row.id, "slot_index": slot_index,
        })
        if slot_index > 0:
            previous = row.slots[slot_index - 1]
            if previous is not None:
                self._reveal_card(row, slot_index - 1, previous)
        # A placement reveal may trigger an elimination ability: arm the pause
        # before turn bookkeeping so the choice resolves with this turn's
        # events. Subsequent placements are blocked until a target is chosen.
        self._begin_pending_elimination(event_start)
        self._draw_for_active_player()
        if all(item.is_full for item in state.rows):
            state.row_index = 0
            state.reveal_card_index = 0
            state.final_reveal_active = True
            hidden_remains = any(
                card is not None and not card.revealed
                for full_row in state.rows
                for card in full_row.slots
            )
            if hidden_remains:
                state.phase = GamePhase.REVEAL
                message = "All slots are filled. Final reveals begin with Row 1."
            else:
                state.phase = GamePhase.ROW_SCORING
                state.final_reveal_active = False
                message = "All slots are filled and revealed. Scoring begins with Row 1."
                self._emit("final_reveal_complete", message, {})
            self._emit("placement_complete", message, {})
        else:
            self._advance_turn()
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def reveal_next_card(self) -> ActionResult:
        state = self.state
        if state.phase != GamePhase.REVEAL or not state.final_reveal_active:
            return ActionResult(False, "The game is not revealing a row.")
        if state.get("pending_elimination"):
            return ActionResult(False, "Select an elimination target before continuing the reveal.")
        event_start = len(state.events)
        next_card = next((
            (row, index, card)
            for row in state.rows
            for index, card in enumerate(row.slots)
            if card is not None and not card.revealed
        ), None)
        if next_card is None:
            return ActionResult(False, "Every card is already revealed.")
        row, hidden, card = next_card
        self._reveal_card(row, hidden, card)
        state.reveal_card_index += 1

        # A reveal may trigger an elimination ability: pause the reveal flow
        # for player target selection (fails through when no target exists).
        if self._begin_pending_elimination(event_start):
            # Do not advance to the next card; wait for target selection.
            return ActionResult(True, events=tuple(state.events[event_start:]))

        if all(placed is not None and placed.revealed for full_row in state.rows for placed in full_row.slots):
            state.final_reveal_active = False
            state.phase = GamePhase.ROW_SCORING
            self._emit("final_reveal_complete", "All cards are revealed. Compare scores starting with Row 1.", {})
        return ActionResult(True, events=tuple(state.events[event_start:]))

    # ---------------------------------------------------------------- elimination

    def _begin_pending_elimination(self, event_start: int) -> bool:
        """Arm the pending-elimination pause when a reveal activated one.

        Scans the events appended since ``event_start`` for an elimination
        ability activation. When found, arms ``state.pending_elimination``
        with a JSON-safe targeting payload and the current valid targets.
        If no valid targets exist the attempt fails immediately (emitting
        ``elimination_failed``) and False is returned so the caller continues
        the normal flow. Returns True when the flow must pause for player
        target selection.
        """
        state = self.state
        elimination_activation = next((
            event for event in state.events[event_start:]
            if event.kind == "ability_activated"
            and event.data.get("is_elimination")
        ), None)
        if elimination_activation is None:
            return False
        owner_card_id = str(elimination_activation.data.get("owner_card_id", ""))
        owner_card = next((
            slot for r in state.rows for slot in r.slots
            if slot is not None and slot.id == owner_card_id
        ), None)
        if owner_card is None:
            return False
        definition = self.cards[owner_card.definition_id]
        ability = getattr(definition, "ability", None)
        if ability is None:
            return False
        targeting = EliminationTargeting(
            target_scope=ability.target_scope,
            target_side=ability.target_side,
            allowed_types=ability.allowed_types,
            max_base_points=ability.max_base_points,
            min_base_points=ability.min_base_points,
        )
        valid_targets = self.elimination.get_valid_targets(state, owner_card, targeting)
        # JSON-safe pending payload: plain scalars only, so the authority can
        # serialize it into network views.
        state.pending_elimination = {
            "attacker_card_id": owner_card.id,
            "attacker_owner_id": owner_card.owner_id,
            "attacker_name": str(definition.name),
            "targeting": {
                "target_scope": targeting.target_scope,
                "target_side": targeting.target_side,
                "allowed_types": list(targeting.allowed_types),
                "max_base_points": targeting.max_base_points,
                "min_base_points": targeting.min_base_points,
            },
            "valid_targets": [
                {"row_id": r.id, "slot_index": i, "card_id": c.id}
                for r, i, c in valid_targets
            ],
        }
        if not valid_targets:
            # No legal targets: fail the elimination attempt, then continue
            # so a fully revealed board still scores.
            self.fail_elimination()
            return False
        return True

    def select_elimination_target(
        self, target_row_id: str, target_slot_index: int
    ) -> ActionResult:
        """Resolve a player's elimination target selection.

        Validates that the chosen slot is still a legal target, eliminates it,
        clears the pending-elimination state, and returns the resolution event.
        Returns a rejected ActionResult if the target is no longer valid or no
        elimination is pending.
        """
        state = self.state
        pending = state.get("pending_elimination")
        if not pending:
            return ActionResult(False, "No elimination is awaiting target selection.")

        # Locate the chosen card.
        row = next((r for r in state.rows if r.id == target_row_id), None)
        if row is None or not 0 <= target_slot_index < len(row.slots):
            return ActionResult(False, "Unknown target slot.")
        target_card = row.slots[target_slot_index]
        if target_card is None:
            return ActionResult(False, "That slot is empty.")

        # Re-validate against live state: the card must still be a legal target.
        attacker_card = next((
            slot for r in state.rows for slot in r.slots
            if slot is not None and slot.id == str(pending["attacker_card_id"])
        ), None)
        if attacker_card is None:
            state.pending_elimination = None
            return ActionResult(False, "The ability's owner card left the board.")

        targeting = EliminationTargeting(
            target_scope=str(pending["targeting"].get("target_scope", "any_row")),
            target_side=str(pending["targeting"].get("target_side", "opposing")),
            allowed_types=tuple(pending["targeting"].get("allowed_types") or ()),
            max_base_points=pending["targeting"].get("max_base_points"),
            min_base_points=pending["targeting"].get("min_base_points"),
        )
        valid_targets = self.elimination.get_valid_targets(state, attacker_card, targeting)
        valid_keys = {(r.id, i) for r, i, _ in valid_targets}
        if (target_row_id, target_slot_index) not in valid_keys:
            return ActionResult(False, "That card is no longer a valid elimination target.")

        # Resolve the elimination through the centralized elimination function.
        attacker_name = str(pending.get("attacker_name", "Unknown character"))
        target_definition = self.cards.get(target_card.definition_id)
        target_name = str(getattr(target_definition, "name", "Unknown character"))
        self.elimination.eliminate_target(state, target_card, attacker_card)
        # Dynamic Duo bonuses that depended on the eliminated partner are
        # removed now; every card keeps its own persistent elimination state.
        self.abilities.recalculate_after_elimination(state, target_card)

        event_start = len(state.events)
        self._emit(
            "elimination",
            f"{target_name} has been eliminated by {attacker_name}.",
            {
                "attacker_card_id": str(pending["attacker_card_id"]),
                "attacker_name": attacker_name,
                "target_card_id": target_card.id,
                "target_name": target_name,
                "row_id": target_row_id,
                "slot_index": target_slot_index,
                "definition_id": target_card.definition_id,
            },
        )
        state.pending_elimination = None
        # The pause skipped the reveal-completion check; re-run it so a fully
        # revealed board proceeds to row scoring instead of stalling.
        if state.phase == GamePhase.REVEAL and all(
            placed is not None and placed.revealed
            for full_row in state.rows for placed in full_row.slots
        ):
            state.final_reveal_active = False
            state.phase = GamePhase.ROW_SCORING
            self._emit("final_reveal_complete", "All cards are revealed. Compare scores starting with Row 1.", {})
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def fail_elimination(self) -> ActionResult:
        """Call when an elimination ability has no valid targets.

        Clears pending elimination state and returns a failed-elimination event
        so the client can show the ``ELIMINATION ATTEMPT FAILED`` message.
        """
        state = self.state
        pending = state.get("pending_elimination")
        if not pending:
            return ActionResult(False, "No elimination is awaiting target selection.")
        attacker_name = str(pending.get("attacker_name", "Unknown character"))
        event_start = len(state.events)
        self._emit(
            "elimination_failed",
            f"{attacker_name}'s elimination attempt failed: no valid targets.",
            {
                "attacker_card_id": str(pending["attacker_card_id"]),
                "attacker_name": attacker_name,
            },
        )
        state.pending_elimination = None
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def score_current_row(self) -> ActionResult:
        state = self.state
        if state.phase != GamePhase.ROW_SCORING:
            return ActionResult(False, "A row can be scored only after all its cards are revealed.")
        if state.get("pending_elimination"):
            return ActionResult(False, "Select an elimination target before scoring.")
        if not all(
            row.is_full and all(card is not None and card.revealed for card in row.slots)
            for row in state.rows
        ):
            return ActionResult(False, "The final reveal sequence must finish before scoring.")
        row = state.current_row
        if row is None or row.scored:
            return ActionResult(False, "This row has already been scored.")
        scores = calculate_row_scores(row, tuple(player.id for player in state.players), self.cards)
        first, second = state.players
        if scores[first.id] == scores[second.id]:
            row.winner_id = None
            row.awarded_points = 0
            result_text = f"Row {row.number} is tied. No points are awarded."
        else:
            winner = first if scores[first.id] > scores[second.id] else second
            row.winner_id = winner.id
            row.awarded_points = row.point_value
            winner.match_points += row.point_value
            winner.round_points += row.point_value
            result_text = f"{winner.name} wins Row {row.number} and gains {row.point_value} match points."
        row.scores = scores
        row.scored = True
        state.scored_row_order.append(row.id)
        state.phase = GamePhase.ROW_RESULTS
        event_start = len(state.events)
        self._emit("row_scored", result_text, {
            "row_id": row.id,
            "row_number": row.number,
            "scores": dict(scores),
            "winner_id": row.winner_id,
            "row_points": row.awarded_points,
            "tie_break_applied": False,
            "match_points": {player.id: player.match_points for player in state.players},
            "round_points": {player.id: player.round_points for player in state.players},
        })
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def advance_after_row_result(self) -> ActionResult:
        state = self.state
        if state.phase != GamePhase.ROW_RESULTS:
            return ActionResult(False, "There is no row result to advance from.")
        if state.row_index < len(state.rows) - 1:
            next_row = state.rows[state.row_index + 1]
            if not next_row.is_full or not all(card is not None and card.revealed for card in next_row.slots):
                return ActionResult(False, "The final reveal sequence must finish before scoring advances.")
        event_start = len(state.events)
        if state.row_index == len(state.rows) - 1:
            state.phase = GamePhase.ROUND_RESULTS
            first, second = state.players
            if first.round_points == second.round_points:
                state.round_winner_id = None
            else:
                state.round_winner_id = first.id if first.round_points > second.round_points else second.id
            self._emit("round_results", f"Round {state.round_number} is complete.", {
                "round": state.round_number,
                "match_points": {player.id: player.match_points for player in state.players},
                "round_points": {player.id: player.round_points for player in state.players},
                "winner_id": state.round_winner_id,
            })
        else:
            state.row_index += 1
            state.reveal_card_index = 0
            row = state.current_row
            state.phase = GamePhase.ROW_SCORING
            self._emit("next_row", f"Proceeding to Row {row.number}.", {"row_id": row.id, "row_number": row.number})
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def continue_after_round_results(self) -> ActionResult:
        state = self.state
        if state.phase != GamePhase.ROUND_RESULTS:
            return ActionResult(False, "Round results are not ready to continue.")
        if state.round_number < state.rounds_per_match:
            state.round_number += 1
            starter_index = (state.round_number - 1) % len(state.players)
            self._prepare_round(starter_index)
            events = tuple(state.events)
        else:
            event_start = len(state.events)
            points = [player.match_points for player in state.players]
            if points[0] == points[1]:
                state.winner_id = None
                message = f"The match ends in a draw at {points[0]} points."
                kind = "match_draw"
            else:
                winner = max(state.players, key=lambda player: player.match_points)
                state.winner_id = winner.id
                message = f"{winner.name} wins the match with {winner.match_points} points."
                kind = "match_win"
            state.phase = GamePhase.MATCH_RESULTS
            self._emit(kind, message, {"winner_id": state.winner_id, "match_points": points})
            events = tuple(state.events[event_start:])
        return ActionResult(True, events=events)

    def view_for(self, player_id: str) -> dict:
        """Return a JSON-compatible view with all unrevealed card identities masked."""
        try:
            viewer = self.state.player(player_id)
        except KeyError as error:
            raise ValueError(f"Unknown player: {player_id}") from error
        player_views = []
        for player in self.state.players:
            own = player.id == viewer.id
            player_views.append({
                "id": player.id,
                "name": player.name,
                "match_points": player.match_points,
                "round_points": player.round_points,
                "hand_count": len(player.hand),
                # Private hand cards are identified by local catalog IDs only. Ability
                # descriptions are sent to peers only with public reveal events.
                "hand": [{"id": card.id, "definition_id": card.definition_id} for card in player.hand] if own else None,
                "deck_count": len(player.draw_pile),
                "draw_pile_count": len(player.draw_pile),
                "used_count": len(player.used),
            })
        rows = []
        for row in self.state.rows:
            rows.append({
                "id": row.id,
                "number": row.number,
                "capacity": row.capacity,
                "point_value": row.point_value,
                "slots": [
                    {"occupied": False} if card is None else {
                        "occupied": True,
                        "owner_id": card.owner_id,
                        "card": self._card_view(card) if card.revealed else None,
                    }
                    for card in row.slots
                ],
                "scores": dict(row.scores) if row.scored else None,
                "winner_id": row.winner_id if row.scored else None,
                "scored": row.scored,
                "awarded_points": row.awarded_points,
                "eliminated_slots": list(row.get("eliminated_slots") or []),
            })
        return {
            "viewer_id": viewer.id,
            "players": player_views,
            "rows": rows,
            "active_player_id": self.state.active_player.id,
            "turn_number": self.state.turn_number,
            "round_number": self.state.round_number,
            "rounds_per_match": self.state.rounds_per_match,
            "phase": self.state.phase.value,
            "current_row_id": self.state.current_row.id if self.state.phase in {GamePhase.REVEAL, GamePhase.ROW_SCORING, GamePhase.ROW_RESULTS} and self.state.current_row else None,
            "scored_row_order": list(self.state.scored_row_order),
            "winner_id": self.state.winner_id,
            "events": [{"kind": event.kind, "message": event.message, "data": dict(event.data)} for event in self.state.events],
            "legal_slots": [[row_id, index] for row_id, index in self.legal_slots(viewer.id)],
            # JSON-safe pause payload; None while no elimination awaits input.
            "pending_elimination": dict(self.state.get("pending_elimination") or {}) or None,
        }

    def _card_view(self, card: CardInstance) -> dict:
        definition = self.cards[card.definition_id]
        return {
            "id": card.id,
            "definition_id": definition.id,
            "name": definition.name,
            "image": definition.image,
            "base_points": definition.base_points,
            "effect_name": definition.effect_name,
            "effect_text": definition.effect_text,
            "owner_id": card.owner_id,
            "revealed": card.revealed,
            "current_points": card.get("current_points"),
            "activated_abilities": list(card.get("activated_abilities") or []),
            "eliminated": bool(card.get("eliminated", False)),
            "applied_bonuses": dict(card.get("applied_bonuses") or {}),
        }

    def _draw_for_active_player(self) -> None:
        player = self.state.active_player
        requested = self.config.draw_per_turn
        drawn = min(requested, len(player.draw_pile))
        for _ in range(drawn):
            player.hand.append(player.draw_pile.pop())
        if drawn:
            self._emit("draw", f"{player.name} draws {drawn} card{'s' if drawn != 1 else ''}.", {"player_id": player.id, "count": drawn})
        if requested and drawn < requested and player.id not in self._empty_draw_piles_warned:
            logger.warning(
                "%s's draw pile ran out while drawing; played cards will not be recycled.",
                player.name,
            )
            self._empty_draw_piles_warned.add(player.id)

    def _advance_turn(self) -> None:
        self.state.active_player_index = (self.state.active_player_index + 1) % len(self.state.players)
        self.state.turn_number += 1
        self._emit("turn", f"It is {self.state.active_player.name}'s turn.", {
            "player_id": self.state.active_player.id, "turn": self.state.turn_number,
        })

    def _reveal_card(self, row: BattleRow, slot_index: int, card: CardInstance) -> None:
        """Reveal one card, resolve abilities, then queue presentation events.

        Order matters: the authoritative reveal flag flips first, the reveal
        presentation is queued (so flips and audio start immediately), then
        ability phases A/B resolve with each activation's presentation queued
        right after that ability's effects apply.
        """
        if card.revealed:
            return
        card.revealed = True
        definition = self.cards[card.definition_id]
        owner = self.state.player(card.owner_id)
        self._emit("card_revealed", f"{owner.name} reveals {definition.name} (base {definition.base_points}).", {
            "row_id": row.id,
            "row_number": row.number,
            "slot_index": slot_index,
            "card_id": card.id,
            "player_id": owner.id,
            "name": definition.name,
            "image": definition.image,
            "full_card_art": definition.full_card_art,
            "base_points": definition.base_points,
            "effect_name": definition.effect_name,
            "effect_text": definition.effect_text,
        })
        self._resolve_reveal_triggers(card, row, slot_index)

    def _resolve_reveal_triggers(self, card: CardInstance, row: BattleRow, slot_index: int) -> None:
        """Run the central ability manager: Phase A pending, Phase B this card."""
        self.abilities.on_card_revealed(
            self.state, card, row, slot_index, emit=self._emit_ability_activation,
        )

    def _emit_ability_activation(self, activation: AbilityActivation) -> None:
        """Queue presentation for an ability whose effects have just applied."""
        self._emit(
            "ability_activated",
            (
                f"{activation.owner_name}'s {activation.ability.name} activates: "
                f"{activation.ability.presentation.floating_text} "
                f"({activation.current_points - activation.delta} -> "
                f"{activation.current_points} points)."
            ),
            activation.event_data(),
        )

    def _emit(self, kind: str, message: str, data: dict) -> None:
        self.state.events.append(GameEvent(kind, message, data))

