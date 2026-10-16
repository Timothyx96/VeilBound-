from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GamePhase(str, Enum):
    PLACEMENT = "placement"
    REVEAL = "reveal"
    ROW_SCORING = "row_scoring"
    ROW_RESULTS = "row_results"
    ROUND_RESULTS = "round_results"
    MATCH_RESULTS = "match_results"


class StateRecord(dict):
    """A JSON-serializable record that keeps the prototype's attribute API."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name: str, value) -> None:
        self[name] = value


@dataclass(frozen=True)
class CardDefinition:
    id: str
    name: str
    image: str
    base_points: int
    effect_name: str
    effect_text: str
    full_card_art: str = ""


class CardInstance(StateRecord):
    def __init__(self, id: str, definition_id: str, owner_id: str, revealed: bool = False):
        super().__init__(id=id, definition_id=definition_id, owner_id=owner_id, revealed=revealed)


class PlayerState(StateRecord):
    def __init__(
        self,
        id: str,
        name: str,
        draw_pile: list[CardInstance] | None = None,
        hand: list[CardInstance] | None = None,
        used: list[CardInstance] | None = None,
        match_points: int = 0,
        round_points: int = 0,
    ):
        super().__init__(
            id=id, name=name, draw_pile=draw_pile or [], hand=hand or [], used=used or [],
            match_points=match_points, round_points=round_points,
        )


class BattleRow(StateRecord):
    def __init__(
        self,
        id: str,
        number: int,
        capacity: int,
        slots: list[CardInstance | None],
        scores: dict[str, int] | None = None,
        winner_id: str | None = None,
        scored: bool = False,
        awarded_points: int = 0,
        point_value: int | None = None,
        eliminated_slots: list[int] | None = None,
    ):
        super().__init__(
            id=id, number=number, capacity=capacity, point_value=capacity if point_value is None else point_value,
            slots=slots, scores=scores or {}, winner_id=winner_id, scored=scored,
            awarded_points=awarded_points, eliminated_slots=eliminated_slots or [],
        )

    @property
    def is_full(self) -> bool:
        return all(card is not None for card in self.slots)

    @property
    def placed_count(self) -> int:
        return sum(card is not None for card in self.slots)


class GameEvent(StateRecord):
    def __init__(self, kind: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(kind=kind, message=message, data=data or {})


class GameState(StateRecord):
    def __init__(
        self,
        players: list[PlayerState],
        rows: list[BattleRow],
        rounds_per_match: int,
        active_player_index: int = 0,
        turn_number: int = 1,
        round_number: int = 1,
        phase: GamePhase = GamePhase.PLACEMENT,
        row_index: int = 0,
        reveal_card_index: int = 0,
        final_reveal_active: bool = False,
        winner_id: str | None = None,
        round_winner_id: str | None = None,
        scored_row_order: list[str] | None = None,
        events: list[GameEvent] | None = None,
        viewer_id: str | None = None,
    ):
        super().__init__(
            players=players, rows=rows, rounds_per_match=rounds_per_match,
            active_player_index=active_player_index, turn_number=turn_number, round_number=round_number,
            phase=phase, row_index=row_index, reveal_card_index=reveal_card_index,
            final_reveal_active=final_reveal_active, winner_id=winner_id, round_winner_id=round_winner_id,
            scored_row_order=scored_row_order or [], events=events or [], pending_ability_choice=None,
            viewer_id=viewer_id,
        )

    @property
    def active_player(self) -> PlayerState:
        return self.players[self.active_player_index]

    @property
    def current_row(self) -> BattleRow | None:
        if 0 <= self.row_index < len(self.rows):
            return self.rows[self.row_index]
        return None

    def player(self, player_id: str) -> PlayerState:
        for player in self.players:
            if player.id == player_id:
                return player
        raise KeyError(player_id)


@dataclass(frozen=True)
class ActionResult:
    accepted: bool
    reason: str = ""
    events: tuple[GameEvent, ...] = ()
