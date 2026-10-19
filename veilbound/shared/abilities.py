"""Central character-ability system: JSON definitions and reveal-time resolution.

Abilities are declared per character in the roster JSON and executed only by
:class:`AbilityManager`, which the authoritative
:class:`~veilbound.shared.rules.GameEngine` owns. Rendering, animation, and
scoring code never implement ability rules.

Resolution order when a card becomes revealed (documented contract):

1. The board state is updated first (``card.revealed = True``) by the caller.
2. **Phase A** - previously registered abilities (owner revealed earlier, not
   yet eligible) are re-evaluated in registration order - which equals owner
   reveal order - ties broken by ascending ability id.
3. **Phase B** - the newly revealed card's own on-reveal abilities run next.
4. **Phase C** - each eligible ability applies its effects exactly once
   (guarded by the owner card instance's ``activated_abilities`` record) and
   its presentation is emitted immediately after that ability's effects.

Duplicate activation is prevented by both the pending-registration bookkeeping
and the per-card-instance activation record, so repeated condition checks,
animation frames, or duplicated events cannot grant the bonus twice. A
re-entrancy guard makes circular ability chains impossible: a nested
resolution pass is logged and skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Supported vocabulary - unsupported values fail loudly at load time so a typo
# can never silently disable an ability.
SUPPORTED_TRIGGERS = frozenset({"on_reveal"})
SUPPORTED_CONDITION_TYPES = frozenset({"character_revealed", "same_row"})
SUPPORTED_EFFECT_TYPES = frozenset({"add_current_points", "eliminate_target"})
SUPPORTED_ACTIVATION_LIMITS = frozenset({"once_per_card_per_match"})
SUPPORTED_ANIMATIONS = frozenset({"points_gain", "elimination"})


class AbilityValidationError(ValueError):
    """Raised when a character's ability definition is malformed or unsupported."""


@dataclass(frozen=True)
class Condition:
    """A requirement that must hold before the ability activates.

    ``character`` stores the raw JSON reference (roster id or exact character
    name); it is validated against the roster when character data is loaded.
    """

    type: str
    character: str


@dataclass(frozen=True)
class Effect:
    """A single gameplay mutation; currently only point adjustments."""

    type: str
    amount: int = 0


@dataclass(frozen=True)
class AbilityPresentation:
    """UI feedback description; presentation never mutates game state."""

    floating_text: str
    animation: str


@dataclass(frozen=True)
class AbilityDefinition:
    """Validated, immutable ability schema parsed from one character's JSON."""

    id: str
    name: str
    description: str
    trigger: str
    conditions: tuple[Condition, ...]
    effects: tuple[Effect, ...]
    activation_limit: str
    presentation: AbilityPresentation
    # Elimination targeting; only meaningful when an effect is eliminate_target.
    target_scope: str = "any_row"
    target_side: str = "opposing"
    allowed_types: tuple[str, ...] = ()
    max_base_points: int | None = None
    min_base_points: int | None = None


@dataclass(frozen=True)
class AbilityActivation:
    """One resolved activation: effects are already applied when this exists."""

    ability: AbilityDefinition
    owner_card_id: str
    target_card_id: str
    row_id: str
    slot_index: int
    definition_id: str
    owner_name: str
    delta: int
    base_points: int
    current_points: int
    is_elimination: bool = False

    def event_data(self) -> dict[str, Any]:
        """JSON-safe payload for the ``ability_activated`` presentation event."""
        return {
            "ability_id": self.ability.id,
            "ability_name": self.ability.name,
            "owner_card_id": self.owner_card_id,
            "target_card_id": self.target_card_id,
            "row_id": self.row_id,
            "slot_index": self.slot_index,
            "definition_id": self.definition_id,
            "name": self.owner_name,
            "delta": self.delta,
            "base_points": self.base_points,
            "current_points": self.current_points,
            "floating_text": self.ability.presentation.floating_text,
            "animation": self.ability.presentation.animation,
            "is_elimination": self.is_elimination,
        }


@dataclass(frozen=True)
class _PendingAbility:
    """A registered ability whose conditions were not satisfied at reveal time."""

    sequence: int
    ability: AbilityDefinition
    owner_card_id: str


def _require_str(raw: dict, key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AbilityValidationError(f"{context}: '{key}' must be a non-empty string.")
    return value.strip()


def parse_ability(raw: Any, *, context: str) -> AbilityDefinition:
    """Parse and validate one ``ability`` object from character JSON.

    Raises :class:`AbilityValidationError` naming the offending character and
    field so bad data is caught while character data loads.
    """
    if not isinstance(raw, dict):
        raise AbilityValidationError(f"{context}: 'ability' must be a JSON object.")
    ability_id = _require_str(raw, "id", context)
    name = _require_str(raw, "name", context)
    description = str(raw.get("description", ""))

    trigger = _require_str(raw, "trigger", context)
    if trigger not in SUPPORTED_TRIGGERS:
        raise AbilityValidationError(
            f"{context}: unsupported trigger {trigger!r} for ability {ability_id!r}; "
            f"supported triggers: {sorted(SUPPORTED_TRIGGERS)}."
        )

    conditions_raw = raw.get("conditions", [])
    if not isinstance(conditions_raw, list):
        raise AbilityValidationError(f"{context}: 'conditions' must be a list.")
    conditions: list[Condition] = []
    for index, item in enumerate(conditions_raw, start=1):
        where = f"{context}: condition #{index} of ability {ability_id!r}"
        if not isinstance(item, dict):
            raise AbilityValidationError(f"{where} must be a JSON object.")
        condition_type = item.get("type")
        if condition_type not in SUPPORTED_CONDITION_TYPES:
            raise AbilityValidationError(
                f"{where}: unsupported condition type {condition_type!r}; "
                f"supported conditions: {sorted(SUPPORTED_CONDITION_TYPES)}."
            )
        character = item.get("character")
        if not isinstance(character, str) or not character.strip():
            raise AbilityValidationError(
                f"{where}: 'character' must be a non-empty roster id or exact character name."
            )
        conditions.append(Condition(type=str(condition_type), character=character.strip()))

    effects_raw = raw.get("effects")
    if not isinstance(effects_raw, list) or not effects_raw:
        raise AbilityValidationError(f"{context}: 'effects' must be a non-empty list.")
    effects: list[Effect] = []
    for index, item in enumerate(effects_raw, start=1):
        where = f"{context}: effect #{index} of ability {ability_id!r}"
        if not isinstance(item, dict):
            raise AbilityValidationError(f"{where} must be a JSON object.")
        effect_type = item.get("type")
        if effect_type not in SUPPORTED_EFFECT_TYPES:
            raise AbilityValidationError(
                f"{where}: unsupported effect type {effect_type!r}; "
                f"supported effects: {sorted(SUPPORTED_EFFECT_TYPES)}."
            )
        if effect_type == "add_current_points":
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a non-zero integer.")
        else:
            # eliminate_target carries no numeric amount; target selection
            # happens through the pending-elimination pause, not a delta.
            amount = 0
        effects.append(Effect(type=str(effect_type), amount=int(amount)))

    activation_limit = raw.get("activation_limit", "once_per_card_per_match")
    if activation_limit not in SUPPORTED_ACTIVATION_LIMITS:
        raise AbilityValidationError(
            f"{context}: unsupported activation_limit {activation_limit!r} for ability "
            f"{ability_id!r}; supported limits: {sorted(SUPPORTED_ACTIVATION_LIMITS)}."
        )

    presentation_raw = raw.get("presentation", {})
    if not isinstance(presentation_raw, dict):
        raise AbilityValidationError(f"{context}: 'presentation' must be a JSON object.")
    total = sum(effect.amount for effect in effects)
    default_text = f"+{total}" if total > 0 else str(total)
    floating_text = presentation_raw.get("floating_text", default_text)
    if not isinstance(floating_text, str) or not floating_text.strip():
        raise AbilityValidationError(
            f"{context}: presentation 'floating_text' must be a non-empty string."
        )
    animation = presentation_raw.get("animation", "points_gain")
    if animation not in SUPPORTED_ANIMATIONS:
        raise AbilityValidationError(
            f"{context}: unsupported presentation animation {animation!r} for ability "
            f"{ability_id!r}; supported animations: {sorted(SUPPORTED_ANIMATIONS)}."
        )

    # Elimination targeting: only validated when an effect is eliminate_target.
    has_elimination = any(effect.type == "eliminate_target" for effect in effects)
    target_scope = "any_row"
    target_side = "opposing"
    allowed_types: tuple[str, ...] = ()
    max_base_points: int | None = None
    min_base_points: int | None = None
    if has_elimination:
        targeting_raw = raw.get("targeting", {})
        if not isinstance(targeting_raw, dict):
            raise AbilityValidationError(
                f"{context}: 'targeting' must be a JSON object for eliminate_target abilities."
            )
        target_scope = targeting_raw.get("target_scope", "any_row")
        if target_scope not in {"same_row", "any_row"}:
            raise AbilityValidationError(
                f"{context}: targeting 'target_scope' must be 'same_row' or 'any_row'."
            )
        target_side = targeting_raw.get("target_side", "opposing")
        if target_side != "opposing":
            raise AbilityValidationError(
                f"{context}: targeting 'target_side' must be 'opposing'."
            )
        allowed_types_raw = targeting_raw.get("allowed_types", [])
        if not isinstance(allowed_types_raw, list):
            raise AbilityValidationError(
                f"{context}: targeting 'allowed_types' must be a list."
            )
        allowed_types = tuple(str(t) for t in allowed_types_raw)
        max_base_points = targeting_raw.get("max_base_points")
        if max_base_points is not None:
            if isinstance(max_base_points, bool) or not isinstance(max_base_points, int):
                raise AbilityValidationError(
                    f"{context}: targeting 'max_base_points' must be an integer or null."
                )
        min_base_points = targeting_raw.get("min_base_points")
        if min_base_points is not None:
            if isinstance(min_base_points, bool) or not isinstance(min_base_points, int):
                raise AbilityValidationError(
                    f"{context}: targeting 'min_base_points' must be an integer or null."
                )
        if (min_base_points is not None and max_base_points is not None
                and min_base_points > max_base_points):
            raise AbilityValidationError(
                f"{context}: targeting 'min_base_points' cannot exceed 'max_base_points'."
            )

    return AbilityDefinition(
        id=ability_id,
        name=name,
        description=description,
        trigger=trigger,
        conditions=tuple(conditions),
        effects=tuple(effects),
        activation_limit=str(activation_limit),
        presentation=AbilityPresentation(
            floating_text=floating_text.strip(), animation=str(animation)
        ),
        target_scope=target_scope,
        target_side=target_side,
        allowed_types=allowed_types,
        max_base_points=max_base_points,
        min_base_points=min_base_points,
    )


def resolve_character_reference(definitions: Mapping[str, Any], reference: str) -> str | None:
    """Resolve a JSON character reference to a roster id (id first, then exact name)."""
    if reference in definitions:
        return reference
    for card_id, definition in definitions.items():
        if getattr(definition, "name", None) == reference:
            return card_id
    return None


def validate_ability_references(definitions: Mapping[str, Any]) -> None:
    """Validate every loaded ability against the full roster; raises on problems."""
    seen_ids: dict[str, str] = {}
    for card_id, definition in definitions.items():
        ability = getattr(definition, "ability", None)
        if ability is None:
            continue
        if ability.id in seen_ids:
            raise AbilityValidationError(
                f"Duplicate ability id {ability.id!r} used by characters "
                f"{seen_ids[ability.id]!r} and {card_id!r}."
            )
        seen_ids[ability.id] = card_id
        for condition in ability.conditions:
            if resolve_character_reference(definitions, condition.character) is None:
                raise AbilityValidationError(
                    f"Ability {ability.id!r} on character {card_id!r} references unknown "
                    f"character {condition.character!r}; use a roster id or an exact name."
                )


@dataclass
class AbilityManager:
    """Owns pending registrations and resolves abilities deterministically.

    One manager instance lives on the authoritative engine and is reset at the
    start of every match. Remote clients never resolve abilities; they replay
    the ``ability_activated`` events the authority broadcasts.
    """

    definitions: Mapping[str, Any]

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Clear registrations for a new match (activation records live on cards)."""
        self._pending: list[_PendingAbility] = []
        self._sequence = 0
        self._resolving = False
        # Index every ability by its id so bonus removal can look an ability up
        # without scanning every card definition.
        self._ability_by_id: dict[str, AbilityDefinition] = {}
        for definition in self.definitions.values():
            ability = getattr(definition, "ability", None)
            if ability is not None:
                self._ability_by_id[ability.id] = ability

    @property
    def pending_abilities(self) -> tuple[tuple[int, str, str], ...]:
        """Registered-but-ineligible abilities as (sequence, ability id, card id)."""
        return tuple(
            (item.sequence, item.ability.id, item.owner_card_id)
            for item in sorted(self._pending, key=lambda item: (item.sequence, item.ability.id))
        )

    def on_card_revealed(
        self,
        state: Any,
        revealed_card: Any,
        row: Any,
        slot_index: int,
        emit: Callable[[AbilityActivation], None] | None = None,
    ) -> list[AbilityActivation]:
        """Run Phase A then Phase B for one freshly revealed card.

        ``emit`` (when provided) receives each activation immediately after its
        effects are applied, preserving presentation order per the contract.
        """
        if self._resolving:
            # A future effect must not recursively restart resolution.
            logger.warning("Ability resolution re-entered; skipping nested pass.")
            return []
        self._resolving = True
        try:
            activations: list[AbilityActivation] = []
            kept: list[_PendingAbility] = []

            # Phase A - previously registered abilities in deterministic order.
            for pending in sorted(
                self._pending, key=lambda item: (item.sequence, item.ability.id)
            ):
                owner = _find_card(state, pending.owner_card_id)
                if owner is None:
                    continue  # owner left the board; drop the registration
                owner_row, owner_index, owner_card = owner
                if self._limit_reached(pending.ability, owner_card):
                    continue  # already activated; never rerun
                if self._row_closed_without_partner(pending.ability, owner_card, owner_row):
                    continue  # closed row without partner; can never activate
                activation = self._try_activate(
                    pending.ability, owner_card, owner_row, owner_index, state
                )
                if activation is not None:
                    activations.append(activation)
                    if emit is not None:
                        emit(activation)
                    continue  # activated - removed from pending
                kept.append(pending)

            # Phase B - the just-revealed character's own on-reveal abilities.
            definition = self.definitions.get(revealed_card.definition_id)
            ability = getattr(definition, "ability", None)
            if (
                ability is not None
                and ability.trigger == "on_reveal"
                and not self._limit_reached(ability, revealed_card)
            ):
                already_pending = any(
                    item.ability.id == ability.id and item.owner_card_id == revealed_card.id
                    for item in kept
                )
                if not already_pending:
                    activation = self._try_activate(
                        ability, revealed_card, row, slot_index, state
                    )
                    if activation is not None:
                        activations.append(activation)
                        if emit is not None:
                            emit(activation)
                    else:
                        self._sequence += 1
                        kept.append(
                            _PendingAbility(self._sequence, ability, revealed_card.id)
                        )

            self._pending = kept
            return activations
        finally:
            self._resolving = False

    # ---------------------------------------------------------------- helpers

    def _limit_reached(self, ability: AbilityDefinition, card: Any) -> bool:
        if ability.activation_limit == "once_per_card_per_match":
            return ability.id in (card.get("activated_abilities") or [])
        return False

    def _row_closed_without_partner(
        self, ability: AbilityDefinition, owner_card: Any, owner_row: Any
    ) -> bool:
        """True when a pending same-row synergy can never become eligible.

        Board positions never change after placement, so once the owner's row
        is completely filled and revealed without a revealed same-player
        partner, the ``same_row`` condition can never turn true. Dropping the
        registration keeps ``pending_abilities`` truthful instead of leaking
        stale entries. Returns False while the row could still change (empty
        slots or hidden cards).
        """
        for condition in ability.conditions:
            if condition.type != "same_row":
                continue
            partner_id = resolve_character_reference(self.definitions, condition.character)
            if partner_id is None:
                continue
            if not all(slot is not None and slot.revealed for slot in owner_row.slots):
                return False  # row still in flux; partner may yet arrive/reveal
            in_row = any(
                slot.definition_id == partner_id and slot.owner_id == owner_card.owner_id
                for slot in owner_row.slots
            )
            if not in_row:
                return True  # closed row without the partner: impossible
        return False

    def _condition_holds(
        self, condition: Condition, owner_card: Any, owner_row: Any, state: Any
    ) -> bool:
        partner_id = resolve_character_reference(self.definitions, condition.character)
        if partner_id is None:
            logger.warning(
                "Ability condition references unknown character %r; treating as failed.",
                condition.character,
            )
            return False
        if condition.type == "character_revealed":
            # Hidden cards can never satisfy a reveal condition, and an
            # eliminated partner is no longer an active ally on the board.
            return any(
                slot is not None
                and slot.revealed
                and not getattr(slot, "eliminated", False)
                and slot.definition_id == partner_id
                for row in state.rows
                for slot in row.slots
            )
        if condition.type == "same_row":
            # The partner must share the owner's row *and* belong to the same
            # player *and* be revealed *and* still be in play: an eliminated
            # partner no longer satisfies a same-row synergy.
            return any(
                slot is not None
                and slot.revealed
                and not getattr(slot, "eliminated", False)
                and slot.definition_id == partner_id
                and slot.owner_id == owner_card.owner_id
                for slot in owner_row.slots
            )
        return False  # unreachable: parsing rejects unknown types

    def _current_points(self, card: Any, base_points: int) -> int:
        value = card.get("current_points")
        return base_points if value is None else int(value)

    def _try_activate(
        self,
        ability: AbilityDefinition,
        owner_card: Any,
        owner_row: Any,
        owner_index: int,
        state: Any,
    ) -> AbilityActivation | None:
        """Check every condition; if satisfied, apply effects once and record it."""
        for condition in ability.conditions:
            if not self._condition_holds(condition, owner_card, owner_row, state):
                return None

        definition = self.definitions[owner_card.definition_id]
        base_points = int(definition.base_points)
        delta = 0
        bonuses = dict(owner_card.get("applied_bonuses") or {})
        for effect in ability.effects:
            if effect.type == "add_current_points":
                # Track each dynamic bonus by the ability that granted it so it
                # can be removed later (for example when a Dynamic Duo partner
                # is eliminated) without ever touching the immutable base.
                bonuses[ability.id] = effect.amount
                delta += effect.amount
            # eliminate_target effects are not applied here: target selection
            # happens in the client layer via the pending-elimination state.
        owner_card.applied_bonuses = bonuses
        owner_card.current_points = base_points + sum(bonuses.values())
        after = owner_card.current_points

        activated = list(owner_card.get("activated_abilities") or [])
        activated.append(ability.id)
        owner_card.activated_abilities = activated

        return AbilityActivation(
            ability=ability,
            owner_card_id=owner_card.id,
            target_card_id=owner_card.id,  # effects target the ability's owner
            row_id=owner_row.id,
            slot_index=owner_index,
            definition_id=owner_card.definition_id,
            owner_name=str(definition.name),
            delta=delta,
            base_points=base_points,
            current_points=after,
            is_elimination=any(effect.type == "eliminate_target" for effect in ability.effects),
        )

    def _base_points(self, card: Any) -> int:
        stored = card.get("base_points")
        if isinstance(stored, (int, float)) and not isinstance(stored, bool):
            return int(stored)
        definition = self.definitions.get(card.definition_id)
        return int(getattr(definition, "base_points", 0))

    def recalculate_after_elimination(self, state: Any, eliminated_card: Any) -> list[Any]:
        """Remove dynamic bonuses that depended on the eliminated card.

        This is the reusable Dynamic Duo elimination system: for every card
        that still carries dynamic bonuses, each bonus survives only while its
        ability's conditions hold on the live board. Because an eliminated
        partner never satisfies a reveal or same-row condition
        (see ``_condition_holds``), losing the partner removes the bonus.
        Eliminated cards themselves keep ``current_points == 0`` and are never
        resurrected here. Bonuses keyed by ability id, so the same bonus can
        never be stacked twice.

        Returns the list of card instances whose points changed.
        """
        changed: list[Any] = []
        for row in state.rows:
            for slot in row.slots:
                if slot is None or slot is eliminated_card:
                    continue
                if getattr(slot, "eliminated", False):
                    # Eliminated cards score zero for the rest of the match;
                    # never resurrect their points here.
                    continue
                bonuses = dict(slot.get("applied_bonuses") or {})
                if not bonuses:
                    continue
                located = _find_card(state, slot.id)
                if located is None:
                    continue
                owner_row, _owner_index, _owner_card = located
                kept: dict[str, int] = {}
                for ability_id, amount in bonuses.items():
                    ability = self._ability_by_id.get(ability_id)
                    if ability is None:
                        continue
                    if all(
                        self._condition_holds(condition, slot, owner_row, state)
                        for condition in ability.conditions
                    ):
                        kept[ability_id] = amount
                if len(kept) == len(bonuses):
                    continue  # every bonus still valid: nothing to remove
                slot.applied_bonuses = kept
                slot.current_points = self._base_points(slot) + sum(kept.values())
                changed.append(slot)
        return changed


def _find_card(state: Any, card_id: str) -> tuple[Any, int, Any] | None:
    """Locate a placed card instance as (row, slot index, card)."""
    for row in state.rows:
        for index, slot in enumerate(row.slots):
            if slot is not None and slot.id == card_id:
                return row, index, slot
    return None





