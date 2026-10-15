from __future__ import annotations

import logging
import random
from collections.abc import Iterable, Mapping

from .config import MATCH_ROUNDS, PLAYER_POOL_SIZE, GameConfig
from .definitions import load_card_definitions
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
    """Score revealed cards only; future scoring effects plug in after base totals."""
    scores = {player_id: 0 for player_id in player_ids}
    for card in row.slots:
        if card is not None and card.revealed:
            scores[card.owner_id] += cards[card.definition_id].base_points
    # Future ability scoring effects belong here and operate on the resolved row state.
    return scores


class GameEngine:
    """Pygame-independent authority for card placement and round scoring."""

    def __init__(self, config: GameConfig | None = None, seed: int | None = None):
        self.config = config or GameConfig()
        self.cards = load_card_definitions()
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
        return CardInstance(f"card-{self._card_serial:05d}", definition_id, owner_id)

    def start_new_match(self, seed: int | None = None) -> GameState:
        if seed is not None:
            self._rng = random.Random(seed)
        players = [PlayerState("player-1", "Player 1"), PlayerState("player-2", "Player 2")]
        self.state = GameState(players=players, rows=[], rounds_per_match=self.config.rounds_per_match)
        self._empty_draw_piles_warned.clear()
        for player in players:
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
        state.phase = GamePhase.PLACEMENT
        state.winner_id = None
        state.round_winner_id = None
        state.scored_row_order = []
        state.events = []

        for player in state.players:
            player.round_points = 0
        self._emit("round_started", f"Round {state.round_number} begins. Fill every slot in the four rows.", {"round": state.round_number})
        self._draw_for_active_player()

    def legal_slots(self, player_id: str | None = None) -> list[tuple[str, int]]:
        state = self.state
        if state.phase != GamePhase.PLACEMENT:
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
        if all(placed is not None and placed.revealed for full_row in state.rows for placed in full_row.slots):
            state.final_reveal_active = False
            state.phase = GamePhase.ROW_SCORING
            self._emit("final_reveal_complete", "All cards are revealed. Compare scores starting with Row 1.", {})
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def score_current_row(self) -> ActionResult:
        state = self.state
        if state.phase != GamePhase.ROW_SCORING:
            return ActionResult(False, "A row can be scored only after all its cards are revealed.")
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
        self._draw_for_active_player()
        self._emit("turn", f"It is {self.state.active_player.name}'s turn.", {
            "player_id": self.state.active_player.id, "turn": self.state.turn_number,
        })

    def _reveal_card(self, row: BattleRow, slot_index: int, card: CardInstance) -> None:
        """Reveal one card and emit the single event used by audio and animation."""
        if card.revealed:
            return
        card.revealed = True
        self._resolve_reveal_triggers(card, row, slot_index)
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
            "base_points": definition.base_points,
            "effect_name": definition.effect_name,
            "effect_text": definition.effect_text,
        })

    def _resolve_reveal_triggers(self, card: CardInstance, row: BattleRow, slot_index: int) -> None:
        """Future ability hook: resolve registered reveal effects in deterministic order."""
        # Character effect text is descriptive only; no ability handlers are installed.
        return None

    def _emit(self, kind: str, message: str, data: dict) -> None:
        self.state.events.append(GameEvent(kind, message, data))

