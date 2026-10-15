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


@dataclass(frozen=True)
class CardDefinition:
    id: str
    name: str
    image: str
    base_points: int
    effect_name: str
    effect_text: str


@dataclass
class CardInstance:
    id: str
    definition_id: str
    owner_id: str
    revealed: bool = False


@dataclass
class PlayerState:
    id: str
    name: str
    draw_pile: list[CardInstance] = field(default_factory=list)
    hand: list[CardInstance] = field(default_factory=list)
    used: list[CardInstance] = field(default_factory=list)
    match_points: int = 0
    round_points: int = 0


@dataclass
class BattleRow:
    id: str
    number: int
    capacity: int
    slots: list[CardInstance | None]
    scores: dict[str, int] = field(default_factory=dict)
    winner_id: str | None = None
    scored: bool = False
    awarded_points: int = 0

    @property
    def is_full(self) -> bool:
        return all(card is not None for card in self.slots)

    @property
    def placed_count(self) -> int:
        return sum(card is not None for card in self.slots)


@dataclass(frozen=True)
class GameEvent:
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class GameState:
    players: list[PlayerState]
    rows: list[BattleRow]
    rounds_per_match: int
    active_player_index: int = 0
    turn_number: int = 1
    round_number: int = 1
    phase: GamePhase = GamePhase.PLACEMENT
    row_index: int = 0
    reveal_card_index: int = 0
    final_reveal_active: bool = False
    winner_id: str | None = None
    round_winner_id: str | None = None
    scored_row_order: list[str] = field(default_factory=list)
    events: list[GameEvent] = field(default_factory=list)

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

