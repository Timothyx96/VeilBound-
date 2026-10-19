"""Reusable character-elimination mechanic.

Provides targeting validation and elimination resolution that character
abilities can configure.  Decoupled from rendering and input.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from veilbound.shared.models import BattleRow, CardInstance, GameState


@dataclass(frozen=True)
class EliminationTargeting:
    target_scope: str = "any_row"
    target_side: str = "opposing"
    allowed_types: tuple[str, ...] = field(default_factory=tuple)
    max_base_points: int | None = None
    min_base_points: int | None = None

    def __post_init__(self) -> None:
        if self.target_scope not in {"same_row", "any_row"}:
            raise ValueError(
                f"target_scope must be 'same_row' or 'any_row', got {self.target_scope!r}"
            )
        if self.target_side != "opposing":
            raise ValueError(
                f"target_side must be 'opposing', got {self.target_side!r}"
            )


class EliminationManager:
    def __init__(self, cards):
        self.cards = cards

    def get_valid_targets(
        self,
        state: GameState,
        attacker_card: CardInstance,
        targeting: EliminationTargeting,
    ) -> list[tuple[BattleRow, int, CardInstance]]:
        attacker_row = self._find_card_row(state, attacker_card.id)
        if attacker_row is None:
            return []
        targets: list[tuple[BattleRow, int, CardInstance]] = []
        for row in state.rows:
            if targeting.target_scope == "same_row" and row.id != attacker_row.id:
                continue
            for slot_index, card in enumerate(row.slots):
                if card is None:
                    continue
                if self._is_eligible_target(card, attacker_card, targeting, state):
                    targets.append((row, slot_index, card))
        return targets

    def _is_eligible_target(self, card, attacker_card, targeting, state) -> bool:
        if not card.revealed:
            return False
        if getattr(card, "eliminated", False):
            return False
        if card.owner_id == attacker_card.owner_id:
            return False
        definition = self.cards.get(card.definition_id)
        base_points = getattr(definition, "base_points", None) if definition else None
        if base_points is None:
            return False
        if targeting.min_base_points is not None and base_points < targeting.min_base_points:
            return False
        if targeting.max_base_points is not None and base_points > targeting.max_base_points:
            return False
        if targeting.allowed_types:
            card_type = getattr(definition, "type", None)
            if card_type not in targeting.allowed_types:
                return False
        return True

    def _find_card_row(self, state, card_id):
        for row in state.rows:
            for slot in row.slots:
                if slot is not None and slot.id == card_id:
                    return row
        return None

    def eliminate_target(self, state, target_card, attacker_card) -> None:
        if getattr(target_card, "eliminated", False):
            return
        target_card.eliminated = True
        target_card.current_points = 0
        row = self._find_card_row(state, target_card.id)
        if row is not None:
            eliminated = list(row.get("eliminated_slots") or [])
            for index, slot in enumerate(row.slots):
                if slot is not None and slot.id == target_card.id:
                    if index not in eliminated:
                        eliminated.append(index)
                    break
            row.eliminated_slots = eliminated