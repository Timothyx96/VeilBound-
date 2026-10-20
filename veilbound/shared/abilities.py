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
4. **Phase C** - each eligible ability activates exactly once (guarded by the
   owner card instance's ``activated_abilities`` record). Immediate effects
   resolve then; target-selection effects resolve after a valid choice.

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
SUPPORTED_TRIGGERS = frozenset({"on_reveal", "on_next_opposing_placement"})
SUPPORTED_CONDITION_TYPES = frozenset({
    "character_revealed", "same_row", "same_row_any", "last_placed_in_row",
    "no_other_friendly_in_row", "same_row_type",
})
SUPPORTED_EFFECT_TYPES = frozenset({
    "add_current_points",
    "add_current_points_per_friendly_type",
    "add_current_points_to_friendly_type",
    "set_current_points",
    "set_base_points",
    "eliminate_target",
    "remove_target_bonus",
    "steal_target_bonus",
    "revive_target",
    "reduce_target_base_points",
})
SUPPORTED_ACTIVATION_LIMITS = frozenset({"once_per_card_per_match", "continuous"})
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
    character: str | None = None
    characters: tuple[str, ...] = ()
    target_type: str | None = None
    target_side: str | None = None


@dataclass(frozen=True)
class Effect:
    """A validated gameplay mutation declared by an ability."""

    type: str
    amount: int = 0
    target_type: str | None = None
    require_revealed: bool = False


@dataclass(frozen=True)
class AbilityPresentation:
    """UI feedback description; presentation never mutates game state."""

    floating_text: str
    animation: str
    sound: str | None = None
    transformed_card_art: str | None = None


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
    # Shared target restrictions for elimination and other target effects.
    target_scope: str = "any_row"
    target_side: str = "opposing"
    allowed_types: tuple[str, ...] = ()
    max_base_points: int | None = None
    min_base_points: int | None = None


@dataclass(frozen=True)
class AbilityActivation:
    """One activation; target-selection effects may still be pending."""

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
    affected_card_ids: tuple[str, ...] = ()

    @property
    def requires_target_selection(self) -> bool:
        return any(
            effect.type in {
                "eliminate_target", "remove_target_bonus", "steal_target_bonus",
                "revive_target", "reduce_target_base_points",
            }
            for effect in self.ability.effects
        )

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
            "sound": self.ability.presentation.sound,
            "transformed_card_art": self.ability.presentation.transformed_card_art,
            "is_elimination": self.is_elimination,
            "requires_target_selection": self.requires_target_selection,
            "affected_card_ids": list(self.affected_card_ids),
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
        if condition_type in {"last_placed_in_row", "no_other_friendly_in_row"}:
            if "character" in item:
                raise AbilityValidationError(
                    f"{where}: {condition_type!r} does not take a 'character'."
                )
            conditions.append(Condition(type=str(condition_type)))
        elif condition_type == "same_row_any":
            references = item.get("characters")
            if (not isinstance(references, list) or not references
                    or any(not isinstance(reference, str) or not reference.strip()
                           for reference in references)):
                raise AbilityValidationError(
                    f"{where}: 'characters' must be a non-empty list of roster ids or exact names."
                )
            if "character" in item:
                raise AbilityValidationError(f"{where}: 'same_row_any' uses 'characters', not 'character'.")
            conditions.append(Condition(
                type=str(condition_type),
                characters=tuple(reference.strip() for reference in references),
            ))
        elif condition_type == "same_row_type":
            target_type = item.get("target_type")
            target_side = item.get("target_side")
            if not isinstance(target_type, str) or not target_type.strip():
                raise AbilityValidationError(f"{where}: 'target_type' must be a non-empty card type.")
            if not isinstance(target_side, str) or target_side not in {"friendly", "opposing"}:
                raise AbilityValidationError(
                    f"{where}: 'target_side' must be 'friendly' or 'opposing'."
                )
            if "character" in item or "characters" in item:
                raise AbilityValidationError(
                    f"{where}: 'same_row_type' uses 'target_type' and 'target_side'."
                )
            conditions.append(Condition(
                type=str(condition_type),
                target_type=target_type.strip(),
                target_side=target_side,
            ))
        else:
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
        target_type = None
        require_revealed = False
        if effect_type in {"reduce_target_base_points", "steal_target_bonus"}:
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a positive integer.")
        elif effect_type == "add_current_points":
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a non-zero integer.")
        elif effect_type in {
            "add_current_points_to_friendly_type",
            "add_current_points_per_friendly_type",
        }:
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a non-zero integer.")
            target_type = item.get("target_type")
            if not isinstance(target_type, str) or not target_type.strip():
                raise AbilityValidationError(f"{where}: 'target_type' must be a non-empty card type.")
            target_type = target_type.strip()
            if effect_type == "add_current_points_per_friendly_type":
                require_revealed = item.get("require_revealed", False)
                if not isinstance(require_revealed, bool):
                    raise AbilityValidationError(f"{where}: 'require_revealed' must be a boolean.")
        elif effect_type in {"set_current_points", "set_base_points"}:
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a non-negative integer.")
        else:
            # Target-selection effects carry no numeric amount; resolution
            # happens through the pending-elimination pause, not a delta.
            amount = 0
        effects.append(Effect(
            type=str(effect_type), amount=int(amount), target_type=target_type,
            require_revealed=require_revealed,
        ))

    activation_limit = raw.get("activation_limit", "once_per_card_per_match")
    if activation_limit not in SUPPORTED_ACTIVATION_LIMITS:
        raise AbilityValidationError(
            f"{context}: unsupported activation_limit {activation_limit!r} for ability "
            f"{ability_id!r}; supported limits: {sorted(SUPPORTED_ACTIVATION_LIMITS)}."
        )
    if any(effect.type == "add_current_points_per_friendly_type" for effect in effects):
        if activation_limit != "continuous":
            raise AbilityValidationError(
                f"{context}: 'add_current_points_per_friendly_type' requires "
                "activation_limit 'continuous'."
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
    sound = presentation_raw.get("sound")
    if sound is not None and (not isinstance(sound, str) or not sound.strip()):
        raise AbilityValidationError(
            f"{context}: presentation 'sound' must be a non-empty path string or null."
        )
    transformed_card_art = presentation_raw.get("transformed_card_art")
    if (transformed_card_art is not None
            and (not isinstance(transformed_card_art, str) or not transformed_card_art.strip())):
        raise AbilityValidationError(
            f"{context}: presentation 'transformed_card_art' must be a non-empty path string or null."
        )

    # Target-selection abilities reuse the same targeting restrictions.
    has_elimination = any(
        effect.type in {
            "eliminate_target", "remove_target_bonus", "steal_target_bonus",
            "revive_target", "reduce_target_base_points",
        }
        for effect in effects
    )
    target_scope = "any_row"
    target_side = "opposing"
    allowed_types: tuple[str, ...] = ()
    max_base_points: int | None = None
    min_base_points: int | None = None
    if has_elimination:
        targeting_raw = raw.get("targeting", {})
        if not isinstance(targeting_raw, dict):
            raise AbilityValidationError(
                f"{context}: 'targeting' must be a JSON object for target-selection abilities."
            )
        target_scope = targeting_raw.get("target_scope", "any_row")
        if target_scope not in {"same_row", "any_row"}:
            raise AbilityValidationError(
                f"{context}: targeting 'target_scope' must be 'same_row' or 'any_row'."
            )
        target_side = targeting_raw.get("target_side", "opposing")
        if target_side not in {"opposing", "friendly", "any"}:
            raise AbilityValidationError(
                f"{context}: targeting 'target_side' must be 'opposing', 'friendly', or 'any'."
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
            floating_text=floating_text.strip(), animation=str(animation),
            sound=sound.strip() if isinstance(sound, str) else None,
            transformed_card_art=(
                transformed_card_art.strip() if isinstance(transformed_card_art, str) else None
            ),
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
        abilities = definition.abilities
        if abilities is None:
            continue
        for ability in abilities:
            if ability.id in seen_ids:
                raise AbilityValidationError(
                    f"Duplicate ability id {ability.id!r} used by characters "
                    f"{seen_ids[ability.id]!r} and {card_id!r}."
                )
            seen_ids[ability.id] = card_id
            for condition in ability.conditions:
                if condition.type in {
                    "last_placed_in_row", "no_other_friendly_in_row", "same_row_type",
                }:
                    continue
                references = condition.characters if condition.type == "same_row_any" else (condition.character,)
                unknown = next((reference for reference in references
                                if resolve_character_reference(definitions, reference) is None), None)
                if unknown is not None:
                    raise AbilityValidationError(
                        f"Ability {ability.id!r} on character {card_id!r} references unknown "
                        f"character {unknown!r}; use a roster id or an exact name."
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
        self._reported_failures: set[tuple[str, str]] = set()
        # Index every ability by its id so bonus removal can look an ability up
        # without scanning every card definition.
        self._ability_by_id: dict[str, AbilityDefinition] = {}
        for definition in self.definitions.values():
            abilities = definition.abilities
            if abilities:
                for ability in abilities:
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
        emit_failure: Callable[[AbilityDefinition, Any, Any, int, str], None] | None = None,
        revived: bool = False,
        owner_only: bool = False,
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
                # Gon is event-triggered: an unresolved attempt waits for
                # Gon himself to be revealed again via revival. Other cards'
                # reveals must not retry his ability.
                if pending.ability.id == "gon_jajanken_rock_ultimate_transformation":
                    if owner_card.id != revealed_card.id or not revived:
                        kept.append(pending)
                        continue
                if owner_only and owner_card.id != revealed_card.id:
                    kept.append(pending)
                    continue
                if getattr(owner_card, "eliminated", False):
                    kept.append(pending)
                    continue
                if not getattr(owner_card, "revealed", False):
                    kept.append(pending)
                    continue
                if self._limit_reached(pending.ability, owner_card):
                    continue  # already activated; never rerun
                if self._row_closed_without_partner(pending.ability, owner_card, owner_row):
                    continue  # closed row without partner; can never activate
                if not self._has_effect_targets(pending.ability, owner_card, state):
                    self._report_failure_once(
                        pending.ability, owner_card, emit_failure, owner_row,
                        owner_index, "no_valid_targets",
                    )
                    kept.append(pending)
                    continue
                activation = self._try_activate(
                    pending.ability, owner_card, owner_row, owner_index, state
                )
                if activation is not None:
                    activations.append(activation)
                    if emit is not None:
                        emit(activation)
                    continue  # activated - removed from pending
                if self._has_effect_targets(pending.ability, owner_card, state):
                    kept.append(pending)

            # Phase B - the just-revealed character's own on-reveal abilities.
            definition = self.definitions.get(revealed_card.definition_id)
            if definition is not None:
                for ability in definition.abilities or []:
                    if ability.trigger != "on_reveal":
                        continue
                    if revived and ability.activation_limit != "once_per_card_per_match":
                        continue
                    if any(
                        effect.type == "add_current_points_per_friendly_type"
                        for effect in ability.effects
                    ):
                        # The contribution is a live count, maintained by the
                        # continuous recalculation below rather than a one-off
                        # reveal activation.
                        continue
                    if self._limit_reached(ability, revealed_card):
                        continue
                    if (
                        ability.id == "gon_jajanken_rock_ultimate_transformation"
                        and (str(revealed_card.id), ability.id) in self._reported_failures
                        and not revived
                    ):
                        # Replayed reveal callbacks are not new trigger events.
                        continue
                    already_pending = any(
                        item.ability.id == ability.id and item.owner_card_id == revealed_card.id
                        for item in kept
                    )
                    if already_pending:
                        continue
                    if not self._has_effect_targets(ability, revealed_card, state):
                        self._report_failure_once(
                            ability, revealed_card, emit_failure, row,
                            slot_index, "no_valid_targets",
                        )
                        self._sequence += 1
                        kept.append(_PendingAbility(
                            self._sequence, ability, revealed_card.id,
                        ))
                        continue
                    activation = self._try_activate(
                        ability, revealed_card, row, slot_index, state
                    )
                    if activation is not None:
                        activations.append(activation)
                        if emit is not None:
                            emit(activation)
                    elif self._has_effect_targets(ability, revealed_card, state):
                        self._sequence += 1
                        kept.append(
                            _PendingAbility(self._sequence, ability, revealed_card.id)
                        )

            self._pending = kept
            self.recalculate_continuous_bonuses(state)
            return activations
        finally:
            self._resolving = False

    def _report_failure_once(
        self,
        ability: AbilityDefinition,
        owner_card: Any,
        emit_failure: Callable[[AbilityDefinition, Any, Any, int, str], None] | None,
        row: Any,
        slot_index: int,
        reason: str,
    ) -> None:
        """Report a failed effect-target check once for this card ability."""
        key = (str(owner_card.id), ability.id)
        if key in self._reported_failures:
            return
        self._reported_failures.add(key)
        if emit_failure is not None:
            emit_failure(ability, owner_card, row, slot_index, reason)

    # ---------------------------------------------------------------- helpers

    def _limit_reached(self, ability: AbilityDefinition, card: Any) -> bool:
        if ability.activation_limit == "once_per_card_per_match":
            return (
                ability.id in (card.get("activated_abilities") or [])
                or ability.id in (card.get("resolved_abilities") or [])
            )
        # "continuous" abilities (or any other limit) can fire repeatedly.
        return False

    def reset_failure_reports(self, owner_card_id: str) -> None:
        """Allow unresolved abilities to report a new attempt after revival."""
        self._reported_failures = {
            key for key in self._reported_failures if key[0] != str(owner_card_id)
        }

    def retain_failed_attempt(self, state: Any, owner_card_id: str, ability_id: str) -> None:
        """Register an unresolved on-reveal ability again after a failed attempt."""
        owner = _find_card(state, owner_card_id)
        if owner is None:
            return
        card = owner[2]
        ability = next((
            item for item in (getattr(self.definitions.get(card.definition_id), "abilities", None) or [])
            if item.id == ability_id
        ), None)
        if ability is None or ability.trigger != "on_reveal":
            return
        if ability_id in (card.get("resolved_abilities") or []):
            return
        if any(item.ability.id == ability_id and item.owner_card_id == str(owner_card_id)
               for item in self._pending):
            return
        self._sequence += 1
        self._pending.append(_PendingAbility(self._sequence, ability, str(owner_card_id)))

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
            if condition.type == "same_row_type":
                if not all(
                    slot is not None and (slot.revealed or getattr(slot, "eliminated", False))
                    for slot in owner_row.slots
                ):
                    return False
                eligible = any(
                    slot is not None
                    and slot.id != owner_card.id
                    and slot.revealed
                    and not getattr(slot, "eliminated", False)
                    and (
                        slot.owner_id != owner_card.owner_id
                        if condition.target_side == "opposing"
                        else slot.owner_id == owner_card.owner_id
                    )
                    and getattr(self.definitions.get(slot.definition_id), "type", None)
                        == condition.target_type
                    for slot in owner_row.slots
                )
                if not eligible:
                    return True
                continue
            if condition.type not in {"same_row", "same_row_any"}:
                continue
            references = condition.characters if condition.type == "same_row_any" else (condition.character,)
            partner_ids = {
                partner_id for reference in references
                if (partner_id := resolve_character_reference(self.definitions, reference)) is not None
            }
            if not partner_ids:
                continue
            if not all(slot is not None and slot.revealed for slot in owner_row.slots):
                return False  # row still in flux; partner may yet arrive/reveal
            in_row = any(
                slot.definition_id in partner_ids and slot.owner_id == owner_card.owner_id
                and not getattr(slot, "eliminated", False)
                for slot in owner_row.slots
            )
            if not in_row:
                return True  # closed row without the partner: impossible
        return False

    def recalculate_continuous_bonuses(self, state: Any) -> list[Any]:
        """Recompute continuous bonuses for all cards that still have abilities.

        This ensures that continuous abilities apply/remove points as the board
        state changes (e.g., when a partner becomes revealed or is eliminated).
        Only abilities with activation_limit == "continuous" are handled here.
        """
        changed: list[Any] = []
        for row in state.rows:
            for slot in row.slots:
                if slot is None or getattr(slot, "eliminated", False):
                    continue
                # Identify owner row for the slot
                owner_row, _owner_index, _owner_card = _find_card(state, slot.id)
                if owner_row is None:
                    continue
                definition = self.definitions.get(slot.definition_id)
                if definition is None:
                    continue
                for ability in definition.abilities or []:
                    if ability.activation_limit != "continuous":
                        continue
                    point_effect = next((effect for effect in ability.effects if effect.type in {
                        "add_current_points", "add_current_points_per_friendly_type",
                    }), None)
                    if point_effect is None:
                        continue
                    conditions_ok = all(
                        self._condition_holds(condition, slot, owner_row, state)
                        for condition in ability.conditions
                    )
                    amount = point_effect.amount
                    if point_effect.type == "add_current_points_per_friendly_type":
                        amount *= sum(
                            1 for candidate in owner_row.slots
                            if candidate is not None
                            and candidate.id != slot.id
                            and candidate.owner_id == slot.owner_id
                            and not getattr(candidate, "eliminated", False)
                            and (not point_effect.require_revealed or candidate.revealed)
                            and getattr(
                                self.definitions.get(candidate.definition_id), "type", None
                            ) == point_effect.target_type
                        )
                    conditions_ok = conditions_ok and amount != 0
                    applied_bonuses = dict(slot.get("applied_bonuses") or {})
                    suppressed_bonuses = set(slot.get("suppressed_bonuses") or [])
                    if conditions_ok:
                        if ability.id in applied_bonuses and applied_bonuses[ability.id] != amount:
                            applied_bonuses[ability.id] = amount
                            changed.append(slot)
                            slot.applied_bonuses = applied_bonuses
                            slot.current_points = self._calculated_points(slot, applied_bonuses)
                        elif ability.id not in applied_bonuses and ability.id not in suppressed_bonuses:
                            applied_bonuses[ability.id] = amount
                            changed.append(slot)
                            slot.applied_bonuses = applied_bonuses
                            slot.current_points = self._calculated_points(slot, applied_bonuses)
                    else:
                        if ability.id in suppressed_bonuses:
                            suppressed_bonuses.remove(ability.id)
                            slot.suppressed_bonuses = sorted(suppressed_bonuses)
                        if ability.id in applied_bonuses:
                            del applied_bonuses[ability.id]
                            changed.append(slot)
                            slot.applied_bonuses = applied_bonuses
                            slot.current_points = self._calculated_points(slot, applied_bonuses)
        return changed

    def _condition_holds(
        self, condition: Condition, owner_card: Any, owner_row: Any, state: Any
    ) -> bool:
        if condition.type == "last_placed_in_row":
            last_index = next(
                (index for index in range(len(owner_row.slots) - 1, -1, -1)
                 if owner_row.slots[index] is not None),
                None,
            )
            return last_index is not None and owner_row.slots[last_index].id == owner_card.id
        if condition.type == "no_other_friendly_in_row":
            return not any(
                slot is not None
                and slot.id != owner_card.id
                and slot.owner_id == owner_card.owner_id
                and not getattr(slot, "eliminated", False)
                for slot in owner_row.slots
            )
        if condition.type == "same_row_any":
            partner_ids = {
                partner_id for reference in condition.characters
                if (partner_id := resolve_character_reference(self.definitions, reference)) is not None
            }
            return any(
                slot is not None
                and slot.revealed
                and not getattr(slot, "eliminated", False)
                and slot.definition_id in partner_ids
                and slot.owner_id == owner_card.owner_id
                for slot in owner_row.slots
            )
        if condition.type == "same_row_type":
            return any(
                candidate is not None
                and candidate.id != owner_card.id
                and candidate.revealed
                and not getattr(candidate, "eliminated", False)
                and (
                    candidate.owner_id != owner_card.owner_id
                    if condition.target_side == "opposing"
                    else candidate.owner_id == owner_card.owner_id
                )
                and getattr(
                    self.definitions.get(candidate.definition_id), "type", None
                ) == condition.target_type
                for candidate in owner_row.slots
            )
        if condition.character is None:
            return False
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

    def _has_effect_targets(self, ability: AbilityDefinition, owner_card: Any, state: Any) -> bool:
        """Do not activate an ability when one of its board effects has no recipient."""
        for effect in ability.effects:
            if effect.type == "add_current_points_to_friendly_type":
                if not any(
                    target is not None
                    and target.id != owner_card.id
                    and target.owner_id == owner_card.owner_id
                    and not getattr(target, "eliminated", False)
                    and getattr(self.definitions.get(target.definition_id), "type", None) == effect.target_type
                    for row in state.rows for target in row.slots
                ):
                    return False
            elif effect.type in {
                "eliminate_target", "remove_target_bonus", "steal_target_bonus",
                "revive_target", "reduce_target_base_points",
            }:
                owner_row = next((
                    row for row in state.rows
                    if any(card is not None and card.id == owner_card.id for card in row.slots)
                ), None)
                found = False
                for row in state.rows:
                    if ability.target_scope == "same_row" and (owner_row is None or row.id != owner_row.id):
                        continue
                    for target in row.slots:
                        if target is None:
                            continue
                        eliminated = bool(getattr(target, "eliminated", False))
                        needs_eliminated = effect.type == "revive_target"
                        if eliminated != needs_eliminated:
                            continue
                        if not needs_eliminated and not getattr(target, "revealed", False):
                            continue
                        if ability.target_side == "opposing" and target.owner_id == owner_card.owner_id:
                            continue
                        if ability.target_side == "friendly" and target.owner_id != owner_card.owner_id:
                            continue
                        target_definition = self.definitions.get(target.definition_id)
                        base = self._base_points(target)
                        if ability.max_base_points is not None and base > ability.max_base_points:
                            continue
                        if ability.min_base_points is not None and base < ability.min_base_points:
                            continue
                        if ability.allowed_types and getattr(target_definition, "type", None) not in ability.allowed_types:
                            continue
                        if effect.type in {"remove_target_bonus", "steal_target_bonus"}:
                            if int(getattr(target, "current_points", 0) or 0) <= base:
                                continue
                        found = True
                        break
                    if found:
                        break
                if not found:
                    return False
        return True

    def _try_activate(
        self,
        ability: AbilityDefinition,
        owner_card: Any,
        owner_row: Any,
        owner_index: int,
        state: Any,
    ) -> AbilityActivation | None:
        """Check every condition; if satisfied, apply effects once and record it."""
        if not self._has_effect_targets(ability, owner_card, state):
            return None
        for condition in ability.conditions:
            if not self._condition_holds(condition, owner_card, owner_row, state):
                return None

        definition = self.definitions[owner_card.definition_id]
        target_effects = {
            "eliminate_target", "remove_target_bonus", "steal_target_bonus",
            "revive_target", "reduce_target_base_points",
        }
        if any(effect.type in target_effects for effect in ability.effects):
            # Targeted abilities are atomic: defer every other mutation until
            # the chosen target has passed authoritative validation and the
            # target effect itself succeeds.
            base_points = self._base_points(owner_card)
            current_points = self._current_points(owner_card, base_points)
            activated = list(owner_card.get("activated_abilities") or [])
            if ability.id not in activated:
                activated.append(ability.id)
            owner_card.activated_abilities = activated
            return AbilityActivation(
                ability=ability,
                owner_card_id=owner_card.id,
                target_card_id=owner_card.id,
                row_id=owner_row.id,
                slot_index=owner_index,
                definition_id=owner_card.definition_id,
                owner_name=str(definition.name),
                delta=0,
                base_points=base_points,
                current_points=current_points,
                is_elimination=any(effect.type == "eliminate_target" for effect in ability.effects),
            )
        base_points = self._base_points(owner_card)
        before = self._current_points(owner_card, base_points)
        bonuses = dict(owner_card.get("applied_bonuses") or {})
        activation_key = f"{ability.id}:{owner_card.id}"
        affected_card_ids: list[str] = []
        for effect in ability.effects:
            if effect.type == "add_current_points":
                # Track each dynamic bonus by the ability that granted it so it
                # can be removed later (for example when a Dynamic Duo partner
                # is eliminated) without ever touching the immutable base.
                bonuses[ability.id] = effect.amount
            elif effect.type == "add_current_points_to_friendly_type":
                for row in state.rows:
                    for target in row.slots:
                        if target is None or target.id == owner_card.id:
                            continue
                        if target.owner_id != owner_card.owner_id or getattr(target, "eliminated", False):
                            continue
                        target_definition = self.definitions.get(target.definition_id)
                        if getattr(target_definition, "type", None) != effect.target_type:
                            continue
                        target_bonuses = dict(target.get("applied_bonuses") or {})
                        if activation_key in target_bonuses:
                            continue
                        target_bonuses[activation_key] = effect.amount
                        target.applied_bonuses = target_bonuses
                        target.current_points = self._calculated_points(target, target_bonuses)
                        affected_card_ids.append(target.id)
            elif effect.type == "set_current_points":
                # Track the adjustment separately so recalculation preserves
                # the exact point total without changing immutable base points.
                other_bonuses = sum(
                    amount for key, amount in bonuses.items() if key != activation_key
                )
                bonuses[activation_key] = effect.amount - base_points - other_bonuses
            elif effect.type == "set_base_points":
                # Base changes are stored on the match's card instance; the
                # roster definition remains the immutable source value.
                owner_card.base_points = effect.amount
                base_points = effect.amount
            # Target effects are not applied here: target selection
            # happens in the client layer via the pending-elimination state.
        owner_card.applied_bonuses = bonuses
        owner_card.current_points = self._calculated_points(owner_card, bonuses)
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
            delta=after - before,
            base_points=base_points,
            current_points=after,
            is_elimination=any(effect.type == "eliminate_target" for effect in ability.effects),
            affected_card_ids=tuple(affected_card_ids),
        )

    def apply_deferred_effects(self, ability: AbilityDefinition, owner_card: Any, state: Any) -> None:
        """Apply non-target effects after a targeted effect succeeds."""
        bonuses = dict(owner_card.get("applied_bonuses") or {})
        activation_key = f"{ability.id}:{owner_card.id}"
        base_points = self._base_points(owner_card)
        for effect in ability.effects:
            if effect.type == "add_current_points":
                bonuses[ability.id] = effect.amount
            elif effect.type == "add_current_points_to_friendly_type":
                for row in state.rows:
                    for target in row.slots:
                        if target is None or target.id == owner_card.id:
                            continue
                        if target.owner_id != owner_card.owner_id or getattr(target, "eliminated", False):
                            continue
                        target_definition = self.definitions.get(target.definition_id)
                        if getattr(target_definition, "type", None) != effect.target_type:
                            continue
                        target_bonuses = dict(target.get("applied_bonuses") or {})
                        target_bonuses[activation_key] = effect.amount
                        target.applied_bonuses = target_bonuses
                        target.current_points = self._calculated_points(target, target_bonuses)
            elif effect.type == "set_current_points":
                other_bonuses = sum(amount for key, amount in bonuses.items() if key != activation_key)
                bonuses[activation_key] = effect.amount - base_points - other_bonuses
            elif effect.type == "set_base_points":
                owner_card.base_points = effect.amount
                base_points = effect.amount
        owner_card.applied_bonuses = bonuses
        owner_card.current_points = self._calculated_points(owner_card, bonuses)

    def _base_points(self, card: Any) -> int:
        stored = card.get("base_points")
        if isinstance(stored, (int, float)) and not isinstance(stored, bool):
            return int(stored)
        definition = self.definitions.get(card.definition_id)
        return int(getattr(definition, "base_points", 0))

    def _calculated_points(self, card: Any, bonuses: dict[str, int] | None = None) -> int:
        """Build a total from the card instance's base and its ability bonuses."""
        applied = bonuses if bonuses is not None else dict(card.get("applied_bonuses") or {})
        return max(0, self._base_points(card) + sum(applied.values()))

    def reduce_base_points(self, card: Any, reduction_id: str, amount: int) -> bool:
        """Permanently reduce this card instance's base, once per activation."""
        reductions = dict(card.get("base_point_reductions") or {})
        if reduction_id in reductions:
            return False
        previous_base = self._base_points(card)
        new_base = max(0, previous_base - int(amount))
        reductions[reduction_id] = previous_base - new_base
        card.base_point_reductions = reductions
        card.base_points = new_base
        card.current_points = self._calculated_points(card)
        return True

    def steal_bonus_points(
        self, recipient: Any, target: Any, ability_id: str, maximum: int,
    ) -> int:
        """Transfer available bonus points without changing either base value."""
        recipient_key = f"{ability_id}:{recipient.id}"
        target_key = f"{ability_id}:steal:{recipient.id}:{target.id}"
        recipient_bonuses = dict(recipient.get("applied_bonuses") or {})
        target_bonuses = dict(target.get("applied_bonuses") or {})
        if recipient_key in recipient_bonuses or target_key in target_bonuses:
            return 0
        available = max(
            0,
            int(target.current_points) - self._base_points(target),
        )
        stolen = min(max(0, int(maximum)), available)
        if stolen <= 0:
            return 0
        target_bonuses[target_key] = -stolen
        recipient_bonuses[recipient_key] = stolen
        target.applied_bonuses = target_bonuses
        recipient.applied_bonuses = recipient_bonuses
        target.current_points = self._calculated_points(target, target_bonuses)
        recipient.current_points = self._calculated_points(recipient, recipient_bonuses)
        return stolen

    def recalculate_after_elimination(self, state: Any, eliminated_card: Any) -> list[Any]:
        """Recalculate live bonuses after an elimination."""
        return self._recalculate_live_bonuses(state, eliminated_source_id=eliminated_card.id)

    def recalculate_after_revival(self, state: Any, revived_card: Any) -> list[Any]:
        """Refresh all live contributions, including those on a revived card."""
        return self._recalculate_live_bonuses(state)

    def reset_gon_for_revival(self, state: Any, revived_card: Any) -> bool:
        """Restore Gon's original card state and clear his previous activation."""
        if revived_card.definition_id != "hxh_01":
            return False
        definition = self.definitions.get(revived_card.definition_id)
        if definition is None:
            return False

        gon_ability_id = "gon_jajanken_rock_ultimate_transformation"
        old_contribution = f"{gon_ability_id}:{revived_card.id}"
        for row in state.rows:
            for recipient in row.slots:
                if recipient is None:
                    continue
                bonuses = dict(recipient.get("applied_bonuses") or {})
                if old_contribution not in bonuses:
                    continue
                bonuses.pop(old_contribution, None)
                recipient.applied_bonuses = bonuses
                if not getattr(recipient, "eliminated", False):
                    recipient.current_points = self._calculated_points(recipient, bonuses)

        original_base = int(definition.base_points)
        revived_card.base_points = original_base
        revived_card.base_point_reductions = {}
        revived_card.applied_bonuses = {}
        revived_card.suppressed_bonuses = []
        revived_card.activated_abilities = []
        revived_card.resolved_abilities = []
        revived_card.current_points = original_base

        # Preserve an unresolved attempt. It is retried only when Gon himself
        # is revived, never when another card changes the board.
        for ability in definition.abilities or []:
            if (
                ability.id != gon_ability_id
                or ability.trigger != "on_reveal"
                or ability.activation_limit != "once_per_card_per_match"
                or not getattr(revived_card, "revealed", False)
            ):
                continue
            if not any(
                pending.ability.id == ability.id
                and pending.owner_card_id == revived_card.id
                for pending in self._pending
            ):
                self._sequence += 1
                self._pending.append(_PendingAbility(
                    self._sequence, ability, revived_card.id,
                ))
        return True

    def _recalculate_live_bonuses(
        self, state: Any, eliminated_source_id: str | None = None,
    ) -> list[Any]:
        """Drop invalid contributions and recompute active continuous effects."""
        changed: list[Any] = []
        for row in state.rows:
            for slot in row.slots:
                if slot is None:
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
                for ability_key, amount in bonuses.items():
                    # One-shot effects granted to other cards are keyed by
                    # ``ability_id:source_card_id``. Once that source leaves
                    # play, its contribution expires while other bonuses on
                    # the recipient remain untouched.
                    _ability_prefix, separator, source_card_id = ability_key.partition(":")
                    if separator and source_card_id == eliminated_source_id:
                        continue
                    source = _find_card(state, source_card_id) if separator else None
                    if source is not None and getattr(source[2], "eliminated", False):
                        continue
                    # Triggered contributions are activation-specific so
                    # multiple instances can independently grant the same effect.
                    ability_id = ability_key.split(":", 1)[0]
                    ability = self._ability_by_id.get(ability_id)
                    if ability is None:
                        continue
                    if all(
                        self._condition_holds(condition, slot, owner_row, state)
                        for condition in ability.conditions
                    ):
                        kept[ability_key] = amount
                if len(kept) == len(bonuses):
                    continue  # every bonus still valid: nothing to remove
                slot.applied_bonuses = kept
                slot.current_points = self._calculated_points(slot, kept)
                changed.append(slot)
        changed_ids = {card.id for card in changed}
        for card in self.recalculate_continuous_bonuses(state):
            if card.id not in changed_ids:
                changed.append(card)
                changed_ids.add(card.id)
        return changed


def _find_card(state: Any, card_id: str) -> tuple[Any, int, Any] | None:
    """Locate a placed card instance as (row, slot index, card)."""
    for row in state.rows:
        for index, slot in enumerate(row.slots):
            if slot is not None and slot.id == card_id:
                return row, index, slot
    return None





