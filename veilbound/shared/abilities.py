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

from .protection import passive_protection_blocks

logger = logging.getLogger(__name__)

# Supported vocabulary - unsupported values fail loudly at load time so a typo
# can never silently disable an ability.
SUPPORTED_TRIGGERS = frozenset({
    "on_reveal", "on_next_opposing_placement", "on_first_placement_in_row",
    "on_next_placement_in_row", "on_row_scoring",
})
SUPPORTED_CONDITION_TYPES = frozenset({
    "character_revealed", "same_row", "same_row_any", "last_placed_in_row",
    "no_other_friendly_in_row", "same_row_type", "adjacent_character",
    "opposing_card_higher_base_points",
})
SUPPORTED_EFFECT_TYPES = frozenset({
    "add_current_points",
    "add_current_points_per_friendly_type",
    "add_current_points_per_friendly_character",
    "add_current_points_per_opposing_excess_over_friendly",
    "reverse_row_winner",
    "automatic_win_if_opposing_row_has_no_goat",
    "luffy_usopp_sogeking_bonus",
    "add_current_points_to_friendly_type",
    "add_current_points_per_opposing_type_after_owner",
    "add_current_points_per_opposing_character",
    "set_current_points",
    "set_base_points",
    "copy_next_opposing_base_points",
    "suppress_next_opposing_ability",
    "random_slot_choice",
    "grant_status",
    "cannot_be_eliminated_by_opposing_ability",
    "ignore_opposing_abilities",
    "disable_row_abilities",
    "gain_points_on_friendly_elimination",
    "heal_friendly_base_points",
    "eliminate_target",
    "eliminate_revealing_opponent",
    "remove_target_bonus",
    "steal_target_bonus",
    "copy_target_bonus",
    "revive_target",
    "heal_target_base_points",
    "reduce_target_base_points",
    "reduce_next_card_points",
    "move_character",
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
    max_amount: int | None = None
    include_self: bool = False
    min_count: int = 0
    preserve_bonus_points: bool = False
    visual_style: str = "black"
    status: str | None = None
    outcomes: tuple[SlotOutcome, ...] = ()


@dataclass(frozen=True)
class SlotOutcome:
    number: int
    name: str
    description: str
    effect: Effect
    target_scope: str = "any_row"
    target_side: str = "opposing"
    allowed_types: tuple[str, ...] = ()


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
    excluded_types: tuple[str, ...] = ()
    max_base_points: int | None = None
    min_base_points: int | None = None
    target_character_set: str | None = None
    require_base_point_reduction: bool = False


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
                "eliminate_target", "remove_target_bonus", "steal_target_bonus", "copy_target_bonus",
                "revive_target", "heal_target_base_points", "reduce_target_base_points", "move_character",
            }
            for effect in self.ability.effects
        ) and not any(
            effect.type == "reduce_target_base_points"
            and self.ability.target_scope == "same_row"
            and self.ability.target_side == "opposing"
            and not self.ability.allowed_types
            for effect in self.ability.effects
        )

    @property
    def requires_crazy_slots_spin(self) -> bool:
        return any(
            effect.type == "random_slot_choice"
            for effect in self.ability.effects
        )

    def event_data(self) -> dict[str, Any]:
        """JSON-safe payload for the ``ability_activated`` presentation event."""
        floating_text = self.ability.presentation.floating_text
        if any(effect.type == "copy_next_opposing_base_points" for effect in self.ability.effects):
            floating_text = f"BASE {self.base_points}"
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
            "floating_text": floating_text,
            "animation": self.ability.presentation.animation,
            "sound": self.ability.presentation.sound,
            "transformed_card_art": self.ability.presentation.transformed_card_art,
            "is_elimination": self.is_elimination,
            "requires_target_selection": self.requires_target_selection,
            "requires_crazy_slots_spin": self.requires_crazy_slots_spin,
            "affected_card_ids": list(self.affected_card_ids),
        }


@dataclass(frozen=True)
class _PendingAbility:
    """A registered ability whose conditions were not satisfied at reveal time."""

    sequence: int
    ability: AbilityDefinition
    owner_card_id: str


@dataclass(frozen=True)
class _PendingPlacementAbility:
    """Ability waiting for the next card placement in its owner's row."""

    row_id: str
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
        if condition_type in {
            "last_placed_in_row", "no_other_friendly_in_row",
            "opposing_card_higher_base_points",
        }:
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
        max_amount = None
        include_self = False
        min_count = 0
        preserve_bonus_points = False
        visual_style = "black"
        status = None
        outcomes: tuple[SlotOutcome, ...] = ()
        if effect_type in {"reduce_target_base_points", "steal_target_bonus", "reduce_next_card_points"}:
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a positive integer.")
        elif effect_type == "add_current_points":
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a non-zero integer.")
        elif effect_type == "luffy_usopp_sogeking_bonus":
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount != 6:
                raise AbilityValidationError(f"{where}: Sogeking's scoring bonus must be exactly 6.")
        elif effect_type in {
            "add_current_points_to_friendly_type",
            "add_current_points_per_friendly_type",
            "add_current_points_per_opposing_type_after_owner",
        }:
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a non-zero integer.")
            target_type = item.get("target_type")
            if not isinstance(target_type, str) or not target_type.strip():
                raise AbilityValidationError(f"{where}: 'target_type' must be a non-empty card type.")
            target_type = target_type.strip()
            require_revealed = False
            if effect_type in {"add_current_points_per_friendly_type", "add_current_points_per_opposing_type_after_owner"}:
                if not isinstance(require_revealed, bool):
                    raise AbilityValidationError(f"{where}: 'require_revealed' must be a boolean.")
            if effect_type == "add_current_points_per_friendly_type":
                include_self = item.get("include_self", False)
                if not isinstance(include_self, bool):
                    raise AbilityValidationError(f"{where}: 'include_self' must be a boolean.")
        elif effect_type == "add_current_points_per_opposing_character":
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a positive integer.")
            target_type = item.get("target_type")
            if target_type is not None:
                if not isinstance(target_type, str) or not target_type.strip():
                    raise AbilityValidationError(f"{where}: 'target_type' must be a non-empty card type or null.")
                target_type = target_type.strip()
            max_amount = item.get("max_amount")
            if max_amount is not None and (
                isinstance(max_amount, bool) or not isinstance(max_amount, int)
                or max_amount <= 0
            ):
                raise AbilityValidationError(f"{where}: 'max_amount' must be a positive integer.")
        elif effect_type == "add_current_points_per_friendly_character":
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a positive integer.")
            min_count = item.get("min_count", 1)
            if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count <= 0:
                raise AbilityValidationError(f"{where}: 'min_count' must be a positive integer.")
        elif effect_type == "add_current_points_per_opposing_excess_over_friendly":
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a positive integer.")
        elif effect_type == "suppress_next_opposing_ability":
            amount = 0
            preserve_bonus_points = item.get("preserve_bonus_points", False)
            if not isinstance(preserve_bonus_points, bool):
                raise AbilityValidationError(f"{where}: 'preserve_bonus_points' must be a boolean.")
            visual_style = item.get("visual_style", "black")
            if visual_style not in {"black", "time_freeze"}:
                raise AbilityValidationError(
                    f"{where}: unsupported suppression visual_style {visual_style!r}."
                )
        elif effect_type == "grant_status":
            amount = 0
            status = item.get("status")
            if status != "immune_to_elimination":
                raise AbilityValidationError(
                    f"{where}: unsupported status {status!r}."
                )
        elif effect_type == "random_slot_choice":
            amount = 0
            outcomes_raw = item.get("outcomes")
            if not isinstance(outcomes_raw, list) or len(outcomes_raw) != 3:
                raise AbilityValidationError(f"{where}: random slot choice requires three outcomes.")
            parsed_outcomes: list[SlotOutcome] = []
            for outcome_raw in outcomes_raw:
                if not isinstance(outcome_raw, dict):
                    raise AbilityValidationError(f"{where}: every slot outcome must be an object.")
                number = outcome_raw.get("number")
                name = outcome_raw.get("name")
                description = outcome_raw.get("description")
                nested = outcome_raw.get("effects")
                if (isinstance(number, bool) or not isinstance(number, int)
                        or number not in {1, 2, 3}
                        or not isinstance(name, str) or not name.strip()
                        or not isinstance(description, str) or not description.strip()
                        or not isinstance(nested, list) or len(nested) != 1
                        or not isinstance(nested[0], dict)):
                    raise AbilityValidationError(f"{where}: malformed numbered slot outcome.")
                if any(parsed.number == number for parsed in parsed_outcomes):
                    raise AbilityValidationError(f"{where}: duplicate slot outcome number {number}.")
                nested_raw = nested[0]
                nested_type = nested_raw.get("type")
                outcome_scope = nested_raw.get("target_scope", "any_row")
                outcome_side = nested_raw.get("target_side", "opposing")
                allowed_raw = nested_raw.get("allowed_types", [])
                allowed_types: tuple[str, ...] = ()
                if nested_type == "add_current_points":
                    nested_amount = nested_raw.get("amount")
                    if (isinstance(nested_amount, bool) or not isinstance(nested_amount, int)
                            or nested_amount <= 0):
                        raise AbilityValidationError(f"{where}: point outcome amount must be positive.")
                    nested_effect = Effect(type=nested_type, amount=nested_amount)
                elif nested_type == "grant_status":
                    nested_status = nested_raw.get("status")
                    if nested_status != "immune_to_elimination":
                        raise AbilityValidationError(f"{where}: unsupported outcome status {nested_status!r}.")
                    nested_effect = Effect(type=nested_type, status=nested_status)
                elif nested_type == "eliminate_target":
                    if (outcome_scope != "same_row" or outcome_side != "opposing"
                            or not isinstance(allowed_raw, list)
                            or not all(isinstance(card_type, str) and card_type.strip() for card_type in allowed_raw)):
                        raise AbilityValidationError(f"{where}: elimination outcome has invalid targeting.")
                    allowed_types = tuple(card_type.strip() for card_type in allowed_raw)
                    nested_effect = Effect(type=nested_type)
                else:
                    raise AbilityValidationError(f"{where}: unsupported slot outcome effect {nested_type!r}.")
                parsed_outcomes.append(SlotOutcome(
                    number=number,
                    name=name.strip(),
                    description=description.strip(),
                    effect=nested_effect,
                    target_scope=str(outcome_scope),
                    target_side=str(outcome_side),
                    allowed_types=allowed_types,
                ))
            if {outcome.number for outcome in parsed_outcomes} != {1, 2, 3}:
                raise AbilityValidationError(f"{where}: slot outcome numbers must be 1, 2, and 3.")
            outcomes = tuple(sorted(parsed_outcomes, key=lambda outcome: outcome.number))
        elif effect_type == "gain_points_on_friendly_elimination":
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise AbilityValidationError(f"{where}: 'amount' must be a positive integer.")
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
            require_revealed=require_revealed, max_amount=max_amount,
            include_self=include_self, min_count=min_count,
            preserve_bonus_points=preserve_bonus_points, visual_style=visual_style,
            status=status, outcomes=outcomes,
        ))

    activation_limit = raw.get("activation_limit", "once_per_card_per_match")
    if activation_limit not in SUPPORTED_ACTIVATION_LIMITS:
        raise AbilityValidationError(
            f"{context}: unsupported activation_limit {activation_limit!r} for ability "
            f"{ability_id!r}; supported limits: {sorted(SUPPORTED_ACTIVATION_LIMITS)}."
        )
    if any(effect.type in {
        "add_current_points_per_friendly_type",
        "add_current_points_per_opposing_character",
        "add_current_points_per_friendly_character",
        "add_current_points_per_opposing_excess_over_friendly",
    } for effect in effects):
        if activation_limit != "continuous":
            raise AbilityValidationError(
                f"{context}: continuous point-per-character effects require "
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
            "eliminate_target", "remove_target_bonus", "steal_target_bonus", "copy_target_bonus",
            "revive_target", "heal_target_base_points", "reduce_target_base_points", "move_character",
        }
        for effect in effects
    )
    target_scope = "any_row"
    target_side = "opposing"
    allowed_types: tuple[str, ...] = ()
    excluded_types: tuple[str, ...] = ()
    max_base_points: int | None = None
    min_base_points: int | None = None
    target_character_set: str | None = None
    require_base_point_reduction = False
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
        excluded_types_raw = targeting_raw.get("excluded_types", [])
        if not isinstance(excluded_types_raw, list):
            raise AbilityValidationError(
                f"{context}: targeting 'excluded_types' must be a list."
            )
        excluded_types = tuple(str(t) for t in excluded_types_raw)
        target_character_set_raw = targeting_raw.get("target_character_set")
        if target_character_set_raw is not None:
            if not isinstance(target_character_set_raw, str) or not target_character_set_raw.strip():
                raise AbilityValidationError(
                    f"{context}: targeting 'target_character_set' must be a non-empty string or null."
                )
            target_character_set = target_character_set_raw.strip()
        require_base_point_reduction = targeting_raw.get("require_base_point_reduction", False)
        if not isinstance(require_base_point_reduction, bool):
            raise AbilityValidationError(
                f"{context}: targeting 'require_base_point_reduction' must be a boolean."
            )
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
        excluded_types=excluded_types,
        max_base_points=max_base_points,
        min_base_points=min_base_points,
        target_character_set=target_character_set,
        require_base_point_reduction=require_base_point_reduction,
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
                    "opposing_card_higher_base_points",
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
        self._pending_placements: list[_PendingPlacementAbility] = []
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

    def row_abilities_disabled_for_card(self, row: Any, card: Any, state: Any) -> bool:
        """Apply row-wide ability locks while honoring Luffy's opposing-only immunity."""
        disabling_sources = [
            source for source in row.slots
            if source is not None and not getattr(source, "eliminated", False)
            and any(
                ability.id in set(source.get("activated_abilities") or [])
                or ability.id in set(source.get("resolved_abilities") or [])
                for ability in (getattr(self.definitions.get(source.definition_id), "abilities", None) or [])
                if any(effect.type == "disable_row_abilities" for effect in ability.effects)
            )
        ]
        if not disabling_sources:
            return bool(row.get("abilities_disabled", False)) and not self._row_has_known_seal_source(row)
        if not row.get("abilities_disabled", False):
            return False
        if not self._has_broad_opposing_immunity(card):
            return True
        return any(source.owner_id == card.owner_id for source in disabling_sources)

    def _row_has_known_seal_source(self, row: Any) -> bool:
        return any(
            source is not None and any(
                any(effect.type == "disable_row_abilities" for effect in ability.effects)
                for ability in (getattr(self.definitions.get(source.definition_id), "abilities", None) or [])
            )
            for source in row.slots
        )

    def _has_broad_opposing_immunity(self, card: Any) -> bool:
        definition = self.definitions.get(card.definition_id)
        return any(
            effect.type == "ignore_opposing_abilities"
            for ability in (getattr(definition, "abilities", None) or [])
            for effect in ability.effects
        )

    @property
    def pending_abilities(self) -> tuple[tuple[int, str, str], ...]:
        """Registered-but-ineligible abilities as (sequence, ability id, card id)."""
        return tuple(
            (item.sequence, item.ability.id, item.owner_card_id)
            for item in sorted(self._pending, key=lambda item: (item.sequence, item.ability.id))
        )

    def on_card_placed_in_empty_row(
        self,
        state: Any,
        placed_card: Any,
        row: Any,
        slot_index: int,
        *,
        row_was_empty: bool,
        emit: Callable[[AbilityActivation], None] | None = None,
    ) -> list[AbilityActivation]:
        """Resolve abilities triggered only when their card enters an empty row."""
        if (not row_was_empty or self._resolving
                or self.row_abilities_disabled_for_card(row, placed_card, state)):
            return []
        self._resolving = True
        try:
            definition = self.definitions.get(placed_card.definition_id)
            if definition is None:
                return []
            activations: list[AbilityActivation] = []
            for ability in definition.abilities or []:
                if ability.trigger != "on_first_placement_in_row":
                    continue
                if self._limit_reached(ability, placed_card):
                    continue
                activation = self._try_activate(
                    ability, placed_card, row, slot_index, state,
                )
                if activation is None:
                    continue
                activations.append(activation)
                if emit is not None:
                    emit(activation)
            return activations
        finally:
            self._resolving = False

    def on_card_placed(
        self,
        state: Any,
        placed_card: Any,
        row: Any,
        slot_index: int,
        emit: Callable[[AbilityActivation], None] | None = None,
    ) -> list[AbilityActivation]:
        """Resolve row-local next-placement effects and arm the newly placed card."""
        if self._resolving or self.row_abilities_disabled_for_card(row, placed_card, state):
            return []
        activations: list[AbilityActivation] = []
        remaining: list[_PendingPlacementAbility] = []
        pending_now = sorted(
            self._pending_placements,
            key=lambda item: (item.owner_card_id, item.ability.id),
        )
        for pending in pending_now:
            if pending.row_id != row.id:
                remaining.append(pending)
                continue
            # A card cannot satisfy its own pending effect. This also protects
            # against duplicate placement notifications for the same instance.
            if pending.owner_card_id == placed_card.id:
                remaining.append(pending)
                continue
            owner_location = _find_card(state, pending.owner_card_id)
            if owner_location is None:
                continue
            owner_row, owner_index, owner_card = owner_location
            if (owner_row.id != row.id or getattr(owner_card, "eliminated", False)
                    or self._is_ability_suppressed(owner_card)):
                continue
            if self.row_abilities_disabled_for_card(owner_row, owner_card, state):
                continue

            if (pending.ability.trigger == "on_next_opposing_placement"
                    and placed_card.owner_id == owner_card.owner_id):
                # Opposing-placement effects wait through any number of allied
                # placements in the row.
                remaining.append(pending)
                continue

            ability = pending.ability
            activated = list(owner_card.get("activated_abilities") or [])
            if ability.id in activated or ability.id in (owner_card.get("resolved_abilities") or []):
                continue
            # This is a one-shot: the first subsequent placement consumes it,
            # including an allied placement that produces no point change.
            activated.append(ability.id)
            owner_card.activated_abilities = activated
            target_card_id = owner_card.id
            affected_card_ids: tuple[str, ...] = ()
            if (placed_card.owner_id != owner_card.owner_id
                    and any(effect.type == "copy_next_opposing_base_points" for effect in ability.effects)):
                before = self._current_points(owner_card, self._base_points(owner_card))
                owner_card.base_points = self._base_points(placed_card)
                owner_card.current_points = self._calculated_points(owner_card)
                definition = self.definitions[owner_card.definition_id]
                activation = AbilityActivation(
                    ability=ability,
                    owner_card_id=owner_card.id,
                    target_card_id=owner_card.id,
                    row_id=owner_row.id,
                    slot_index=owner_index,
                    definition_id=owner_card.definition_id,
                    owner_name=str(definition.name),
                    delta=owner_card.current_points - before,
                    base_points=self._base_points(owner_card),
                    current_points=owner_card.current_points,
                )
                activations.append(activation)
                if emit is not None:
                    emit(activation)
            elif (placed_card.owner_id != owner_card.owner_id
                    and any(effect.type == "suppress_next_opposing_ability" for effect in ability.effects)):
                if passive_protection_blocks(placed_card, owner_card, self.definitions):
                    # Broad immunity also prevents the suppression itself.
                    continue
                suppression_effect = next(
                    effect for effect in ability.effects
                    if effect.type == "suppress_next_opposing_ability"
                )
                before = self._current_points(owner_card, self._base_points(owner_card))
                placed_card.ability_suppressed = True
                placed_card.ability_suppression_blocks_bonus_points = not suppression_effect.preserve_bonus_points
                placed_card.ability_suppression_style = suppression_effect.visual_style
                if not suppression_effect.preserve_bonus_points:
                    placed_card.applied_bonuses = {}
                    placed_card.current_points = self._calculated_points(placed_card, {})
                self._pending = [
                    item for item in self._pending
                    if item.owner_card_id != placed_card.id
                ]
                definition = self.definitions[owner_card.definition_id]
                activation = AbilityActivation(
                    ability=ability,
                    owner_card_id=owner_card.id,
                    target_card_id=placed_card.id,
                    row_id=owner_row.id,
                    slot_index=owner_index,
                    definition_id=owner_card.definition_id,
                    owner_name=str(definition.name),
                    delta=0,
                    base_points=self._base_points(owner_card),
                    current_points=before,
                    affected_card_ids=(placed_card.id,),
                )
                activations.append(activation)
                if emit is not None:
                    emit(activation)
            else:
                resolved = list(owner_card.get("resolved_abilities") or [])
                if ability.id not in resolved:
                    resolved.append(ability.id)
                owner_card.resolved_abilities = resolved

        self._pending_placements = remaining

        definition = self.definitions.get(placed_card.definition_id)
        if self._is_ability_suppressed(placed_card):
            return activations
        for ability in getattr(definition, "abilities", None) or []:
            if (ability.trigger not in {"on_next_placement_in_row", "on_next_opposing_placement"}
                    or self._limit_reached(ability, placed_card)):
                continue
            if not any(effect.type in {
                "copy_next_opposing_base_points", "suppress_next_opposing_ability",
            } for effect in ability.effects):
                continue
            if any(
                item.owner_card_id == placed_card.id and item.ability.id == ability.id
                for item in self._pending_placements
            ):
                continue
            self._pending_placements.append(_PendingPlacementAbility(
                row_id=row.id, ability=ability, owner_card_id=placed_card.id,
            ))
        return activations

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
        revealed_by: Any = None,
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
            if self.row_abilities_disabled_for_card(row, revealed_card, state):
                kept_pending = []
                for item in self._pending:
                    location = _find_card(state, item.owner_card_id)
                    if (location is None or location[0].id != row.id
                            or not self.row_abilities_disabled_for_card(row, location[2], state)):
                        kept_pending.append(item)
                self._pending = kept_pending
                kept_placements = []
                for item in self._pending_placements:
                    location = _find_card(state, item.owner_card_id)
                    if (item.row_id != row.id or location is None
                            or not self.row_abilities_disabled_for_card(row, location[2], state)):
                        kept_placements.append(item)
                self._pending_placements = kept_placements
                return []
            activations: list[AbilityActivation] = []
            kept: list[_PendingAbility] = []

            # Phase A - previously registered abilities in deterministic order.
            for pending in sorted(
                self._pending, key=lambda item: (item.sequence, item.ability.id)
            ):
                if self._is_passive_rule_modifier(pending.ability):
                    continue
                owner = _find_card(state, pending.owner_card_id)
                if owner is None:
                    continue  # owner left the board; drop the registration
                owner_row, owner_index, owner_card = owner
                if self.row_abilities_disabled_for_card(owner_row, owner_card, state):
                    continue
                if self._is_ability_suppressed(owner_card):
                    continue
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
                    if (
                        pending.ability.activation_limit == "once_per_card_per_match"
                        and pending.ability.id != "gon_jajanken_rock_ultimate_transformation"
                    ):
                        # A once-per-match reveal-only ability that found no
                        # target is consumed: it must not retry when a target
                        # appears later. Gon keeps his explicit revival retry.
                        activated = list(owner_card.get("activated_abilities") or [])
                        if pending.ability.id not in activated:
                            activated.append(pending.ability.id)
                            owner_card.activated_abilities = activated
                        continue
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
            if definition is not None and not self._is_ability_suppressed(revealed_card):
                for ability in definition.abilities or []:
                    if ability.trigger != "on_reveal":
                        continue
                    if self._is_passive_rule_modifier(ability):
                        continue
                    if not self._ability_position_is_eligible(ability, slot_index):
                        continue
                    if revived:
                        # A revived card's own on-reveal abilities must not
                        # re-activate: its activation history was reset during
                        # revival. Gon's explicit retry is handled separately
                        # in Phase A, so skipping all abilities here is safe.
                        continue
                    if any(effect.type == "eliminate_revealing_opponent" for effect in ability.effects):
                        if self._limit_reached(ability, revealed_card):
                            continue
                        activation = self._try_activate(
                            ability, revealed_card, row, slot_index, state,
                            triggering_card=revealed_by,
                        )
                        if activation is not None:
                            activations.append(activation)
                            if emit is not None:
                                emit(activation)
                        continue
                    if any(
                        effect.type in {
                            "add_current_points_per_friendly_type",
                            "add_current_points_per_opposing_character",
                            "add_current_points_per_friendly_character",
                            "add_current_points_per_opposing_excess_over_friendly",
                        }
                        for effect in ability.effects
                    ) or (
                        ability.activation_limit == "continuous"
                        and any(condition.type in {
                            "adjacent_character", "opposing_card_higher_base_points",
                        } for condition in ability.conditions)
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
                        if all(
                            effect.type == "heal_friendly_base_points"
                            for effect in ability.effects
                        ):
                            # A heal with nobody wounded is a quiet no-op; it
                            # should not show a failed-target message.
                            continue
                        self._report_failure_once(
                            ability, revealed_card, emit_failure, row,
                            slot_index, "no_valid_targets",
                        )
                        self._sequence += 1
                        if (
                            ability.activation_limit == "once_per_card_per_match"
                            and ability.id != "gon_jajanken_rock_ultimate_transformation"
                        ):
                            # A once-per-match reveal-only ability that found no
                            # target is consumed: it must not retry when a
                            # target appears later. Gon keeps his explicit
                            # revival retry.
                            activated = list(revealed_card.get("activated_abilities") or [])
                            if ability.id not in activated:
                                activated.append(ability.id)
                                revealed_card.activated_abilities = activated
                            continue
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
                if owner_row_disabled := any(
                    candidate.id == slot.id
                    and self.row_abilities_disabled_for_card(owner_row, slot, state)
                    for owner_row in state.rows
                    for candidate in owner_row.slots if candidate is not None
                ):
                    continue
                # Identify owner row for the slot
                owner_row, _owner_index, _owner_card = _find_card(state, slot.id)
                if owner_row is None:
                    continue
                definition = self.definitions.get(slot.definition_id)
                if definition is None:
                    continue
                if self._is_ability_suppressed(slot):
                    if self._blocks_additional_point_gains(slot):
                        applied = dict(slot.get("applied_bonuses") or {})
                        if applied:
                            slot.applied_bonuses = {}
                            slot.current_points = self._calculated_points(slot, {})
                            changed.append(slot)
                    continue
                for ability in definition.abilities or []:
                    if ability.activation_limit != "continuous":
                        continue
                    point_effect = next((
                        effect for effect in ability.effects
                        if effect.type in {
                            "add_current_points",
                            "add_current_points_per_friendly_type",
                            "add_current_points_per_opposing_type_after_owner",
                        }
                        or effect.type == "add_current_points_per_friendly_type"
                        or effect.type == "add_current_points_per_opposing_character"
                        or effect.type == "add_current_points_per_friendly_character"
                        or effect.type == "add_current_points_per_opposing_excess_over_friendly"
                    ), None)
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
                            and (point_effect.include_self or candidate.id != slot.id)
                            and candidate.owner_id == slot.owner_id
                            and not getattr(candidate, "eliminated", False)
                            and (not point_effect.require_revealed or candidate.revealed)
                            and getattr(
                                self.definitions.get(candidate.definition_id), "type", None
                            ) == point_effect.target_type
                        )
                    elif point_effect.type == "add_current_points_per_opposing_type_after_owner":
                        owner_slot_index = next(
                            (
                                index
                                for index, candidate in enumerate(owner_row.slots)
                                if candidate is not None and candidate.id == slot.id
                            ),
                            None,
                        )
                        if owner_slot_index is not None:
                            amount *= sum(
                                1
                                for index, candidate in enumerate(owner_row.slots)
                                if candidate is not None
                                and candidate.id != slot.id
                                and candidate.owner_id != slot.owner_id
                                and index > owner_slot_index
                                and not getattr(candidate, "eliminated", False)
                                and (not point_effect.require_revealed or candidate.revealed)
                                and getattr(
                                    self.definitions.get(candidate.definition_id), "type", None
                                )
                                == point_effect.target_type
                            )
                    elif point_effect.type == "add_current_points_per_opposing_character":
                        opposing_count = sum(
                            1
                            for candidate in owner_row.slots
                            if candidate is not None
                            and candidate.id != slot.id
                            and candidate.owner_id != slot.owner_id
                            and not getattr(candidate, "eliminated", False)
                            and (
                                point_effect.target_type is None
                                or getattr(
                                    self.definitions.get(candidate.definition_id), "type", None
                                ) == point_effect.target_type
                            )
                        )
                        amount = min(
                            amount * opposing_count,
                            point_effect.max_amount or amount * opposing_count,
                        )
                    elif point_effect.type == "add_current_points_per_friendly_character":
                        friendly_count = sum(
                            1
                            for candidate in owner_row.slots
                            if candidate is not None
                            and candidate.id != slot.id
                            and candidate.owner_id == slot.owner_id
                            and not getattr(candidate, "eliminated", False)
                        )
                        amount = (
                            amount * friendly_count
                            if friendly_count >= point_effect.min_count
                            else 0
                        )
                    elif point_effect.type == "add_current_points_per_opposing_excess_over_friendly":
                        allied_count = sum(
                            1
                            for candidate in owner_row.slots
                            if candidate is not None
                            and candidate.owner_id == slot.owner_id
                            and not getattr(candidate, "eliminated", False)
                        )
                        opposing_count = sum(
                            1
                            for candidate in owner_row.slots
                            if candidate is not None
                            and candidate.owner_id != slot.owner_id
                            and not getattr(candidate, "eliminated", False)
                        )
                        amount *= max(0, opposing_count - allied_count)
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
        if condition.type == "adjacent_character":
            partner_id = resolve_character_reference(self.definitions, condition.character or "")
            owner_index = next(
                (index for index, candidate in enumerate(owner_row.slots)
                 if candidate is not None and candidate.id == owner_card.id),
                None,
            )
            if partner_id is None or owner_index is None:
                return False
            return any(
                abs(index - owner_index) == 1
                and candidate is not None
                and candidate.definition_id == partner_id
                and candidate.owner_id == owner_card.owner_id
                and candidate.revealed
                and not getattr(candidate, "eliminated", False)
                for index, candidate in enumerate(owner_row.slots)
            )
        if condition.type == "opposing_card_higher_base_points":
            owner_base = self._base_points(owner_card)
            return any(
                candidate is not None
                and candidate.id != owner_card.id
                and candidate.owner_id != owner_card.owner_id
                and candidate.revealed
                and not getattr(candidate, "eliminated", False)
                and self._base_points(candidate) > owner_base
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
            if effect.type == "heal_friendly_base_points":
                if not any(
                    target is not None
                    and target.id != owner_card.id
                    and target.owner_id == owner_card.owner_id
                    and not getattr(target, "eliminated", False)
                    and sum(
                        max(0, int(amount))
                        for amount in dict(target.get("base_point_reductions") or {}).values()
                    ) > 0
                    for row in state.rows for target in row.slots
                ):
                    return False
            elif effect.type == "add_current_points_to_friendly_type":
                if not any(
                    target is not None
                    and target.id != owner_card.id
                    and target.owner_id == owner_card.owner_id
                    and not getattr(target, "eliminated", False)
                    and not self._blocks_additional_point_gains(target)
                    and getattr(self.definitions.get(target.definition_id), "type", None) == effect.target_type
                    for row in state.rows for target in row.slots
                ):
                    return False
            elif effect.type in {
                "eliminate_target", "remove_target_bonus", "steal_target_bonus", "copy_target_bonus",
                "revive_target", "heal_target_base_points", "reduce_target_base_points", "move_character",
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
                        if (effect.type in {
                                "eliminate_target", "remove_target_bonus", "steal_target_bonus",
                                "reduce_target_base_points", "move_character",
                            }
                                and passive_protection_blocks(target, owner_card, self.definitions)):
                            continue
                        target_definition = self.definitions.get(target.definition_id)
                        if (ability.target_character_set is not None
                                and getattr(target_definition, "character_set", "")
                                != ability.target_character_set):
                            continue
                        if effect.type == "heal_target_base_points" and ability.require_base_point_reduction:
                            reductions = dict(target.get("base_point_reductions") or {})
                            if sum(max(0, int(amount)) for amount in reductions.values()) <= 0:
                                continue
                        base = self._base_points(target)
                        if ability.max_base_points is not None and base > ability.max_base_points:
                            continue
                        if ability.min_base_points is not None and base < ability.min_base_points:
                            continue
                        if ability.allowed_types and getattr(target_definition, "type", None) not in ability.allowed_types:
                            continue
                        if getattr(target_definition, "type", None) in ability.excluded_types:
                            continue
                        if effect.type in {"remove_target_bonus", "steal_target_bonus", "copy_target_bonus"}:
                            if int(getattr(target, "current_points", 0) or 0) <= base:
                                continue
                        found = True
                        break
                    if found:
                        break
                if not found:
                    return False
                if effect.type == "move_character" and (
                    owner_row is None
                    or not any(
                        row.id != owner_row.id
                        and not row.scored
                        and any(candidate is None for candidate in row.slots)
                        for row in state.rows
                    )
                ):
                    return False
        return True

    def _try_activate(
        self,
        ability: AbilityDefinition,
        owner_card: Any,
        owner_row: Any,
        owner_index: int,
        state: Any,
        triggering_card: Any = None,
    ) -> AbilityActivation | None:
        """Check every condition; if satisfied, apply effects once and record it."""
        if self._is_ability_suppressed(owner_card):
            return None
        if not self._ability_position_is_eligible(ability, owner_index):
            return None
        if any(effect.type == "eliminate_revealing_opponent" for effect in ability.effects):
            if (
                triggering_card is None
                or triggering_card.owner_id == owner_card.owner_id
                or getattr(triggering_card, "eliminated", False)
                or bool(triggering_card.get("immune_to_elimination", False))
                or self._base_points(triggering_card) > self._base_points(owner_card)
                or passive_protection_blocks(triggering_card, owner_card, self.definitions)
                or owner_index + 1 >= len(owner_row.slots)
                or owner_row.slots[owner_index + 1] is None
                or owner_row.slots[owner_index + 1].id != triggering_card.id
            ):
                return None
            definition = self.definitions[owner_card.definition_id]
            base_points = self._base_points(owner_card)
            current_points = self._current_points(owner_card, base_points)
            activated = list(owner_card.get("activated_abilities") or [])
            if ability.id not in activated:
                activated.append(ability.id)
            owner_card.activated_abilities = activated
            return AbilityActivation(
                ability=ability,
                owner_card_id=owner_card.id,
                target_card_id=triggering_card.id,
                row_id=owner_row.id,
                slot_index=owner_index,
                definition_id=owner_card.definition_id,
                owner_name=str(definition.name),
                delta=0,
                base_points=base_points,
                current_points=current_points,
                is_elimination=True,
            )
        if not self._has_effect_targets(ability, owner_card, state):
            return None
        for condition in ability.conditions:
            if not self._condition_holds(condition, owner_card, owner_row, state):
                return None

        # Rule modifiers are read directly by the central rules managers and
        # stay active for the card's lifetime. They are not reveal-time actions.
        if self._is_passive_rule_modifier(ability):
            return None

        if any(effect.type == "disable_row_abilities" for effect in ability.effects):
            owner_row.abilities_disabled = True
            kept_pending = []
            for item in self._pending:
                location = _find_card(state, item.owner_card_id)
                if (location is None or location[0].id != owner_row.id
                        or (location[2].owner_id != owner_card.owner_id
                            and self._has_broad_opposing_immunity(location[2]))):
                    kept_pending.append(item)
            self._pending = kept_pending
            kept_placements = []
            for item in self._pending_placements:
                location = _find_card(state, item.owner_card_id)
                if (item.row_id != owner_row.id or location is None
                        or (location[2].owner_id != owner_card.owner_id
                            and self._has_broad_opposing_immunity(location[2]))):
                    kept_placements.append(item)
            self._pending_placements = kept_placements

        if any(effect.type == "random_slot_choice" for effect in ability.effects):
            # The server applies Crazy Slots only after the synchronized spin
            # presentation completes. Activation is recorded now to prevent
            # duplicate reveal callbacks from opening another spin.
            definition = self.definitions[owner_card.definition_id]
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
            )

        definition = self.definitions[owner_card.definition_id]
        target_effects = {
            "eliminate_target", "remove_target_bonus", "steal_target_bonus", "copy_target_bonus",
            "revive_target", "heal_target_base_points", "reduce_target_base_points", "move_character",
        }
        # reduce_target_base_points with same_row+opposing is auto-processing,
        # not player-targeted
        has_target_effect = any(
            effect.type in target_effects
            for effect in ability.effects
        )
        is_auto_reduce = any(
            effect.type == "reduce_target_base_points"
            and ability.target_scope == "same_row"
            and ability.target_side == "opposing"
            and not ability.allowed_types
            for effect in ability.effects
        )
        if has_target_effect and not is_auto_reduce:
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
            elif effect.type == "heal_friendly_base_points":
                for row in state.rows:
                    for target in row.slots:
                        if (target is None or target.id == owner_card.id
                                or target.owner_id != owner_card.owner_id
                                or getattr(target, "eliminated", False)):
                            continue
                        restored = self.restore_base_point_reductions(target)
                        if restored <= 0:
                            continue
                        affected_card_ids.append(target.id)
                if affected_card_ids:
                    # Preserve current bonuses and recheck conditional bonuses
                    # after every friendly base value has been restored.
                    self.recalculate_continuous_bonuses(state)
            elif effect.type == "add_current_points_to_friendly_type":
                for row in state.rows:
                    for target in row.slots:
                        if target is None or target.id == owner_card.id:
                            continue
                        if target.owner_id != owner_card.owner_id or getattr(target, "eliminated", False):
                            continue
                        if self._blocks_additional_point_gains(target):
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
            elif effect.type == "reduce_next_card_points":
                next_index = owner_index + 1
                if next_index < len(owner_row.slots):
                    target = owner_row.slots[next_index]
                    if (target is not None and target.owner_id != owner_card.owner_id
                            and not getattr(target, "eliminated", False)
                            and not passive_protection_blocks(target, owner_card, self.definitions)):
                        remaining = min(effect.amount, max(0, int(target.current_points)))
                        target_bonuses = dict(target.get("applied_bonuses") or {})
                        target.applied_bonuses = target_bonuses
                        bonus_reductions = dict(target.get("bonus_point_reductions") or {})
                        available_bonus = max(0, sum(target_bonuses.values()) - sum(bonus_reductions.values()))
                        bonus_reduction = min(remaining, available_bonus)
                        reduction_key = f"{ability.id}:{owner_card.id}"
                        if bonus_reduction:
                            bonus_reductions[reduction_key] = bonus_reduction
                            target.bonus_point_reductions = bonus_reductions
                            remaining -= bonus_reduction
                        base_reduction = min(remaining, self._base_points(target))
                        if base_reduction:
                            self.reduce_base_points(
                                target, f"{reduction_key}:base", base_reduction,
                            )
                        target.current_points = self._calculated_points(target, target_bonuses)
                        affected_card_ids.append(target.id)
            elif (
                effect.type == "reduce_target_base_points"
                and ability.target_scope == "same_row"
                and ability.target_side == "opposing"
                and not ability.allowed_types
            ):
                # Netero's ability: reduce base points of ALL opposing characters in his row
                owner_row = next(
                    (row for row in state.rows if any(card is not None and card.id == owner_card.id for card in row.slots)),
                    None,
                )
                if owner_row is not None:
                    reduction_id = f"{ability.id}:{owner_card.id}"
                    for target in owner_row.slots:
                        if target is None:
                            continue
                        if getattr(target, "eliminated", False):
                            continue
                        if target.owner_id == owner_card.owner_id:
                            continue  # Skip friendly characters
                        if passive_protection_blocks(target, owner_card, self.definitions):
                            continue
                        # Apply the base point reduction to this target
                        if not self.reduce_base_points(target, reduction_id, effect.amount):
                            # Already reduced; skip
                            continue
                        # Update current points after reduction
                        target.current_points = self._calculated_points(target)
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
            elif effect.type == "grant_status":
                if effect.status == "immune_to_elimination":
                    owner_card.immune_to_elimination = True
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

    @staticmethod
    def _ability_position_is_eligible(ability: AbilityDefinition, slot_index: int) -> bool:
        """Check position-based restrictions against the card's current row slot."""
        if ability.id == "tobi_reverse_fate":
            return 0 <= slot_index < 4
        return True

    @staticmethod
    def _is_passive_rule_modifier(ability: AbilityDefinition) -> bool:
        """Whether an ability only declares a persistent rule modifier."""
        return bool(ability.effects) and all(
            effect.type in {
                "cannot_be_eliminated_by_opposing_ability",
                "ignore_opposing_abilities",
                "gain_points_on_friendly_elimination",
            }
            for effect in ability.effects
        )

    def apply_deferred_effects(self, ability: AbilityDefinition, owner_card: Any, state: Any) -> None:
        """Apply non-target effects after a targeted effect succeeds."""
        if self._is_ability_suppressed(owner_card):
            return
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
                        if self._blocks_additional_point_gains(target):
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

    @staticmethod
    def _is_ability_suppressed(card: Any) -> bool:
        return bool(card.get("ability_suppressed", False))

    @staticmethod
    def _blocks_additional_point_gains(card: Any) -> bool:
        return bool(card.get("ability_suppressed", False)) and bool(
            card.get("ability_suppression_blocks_bonus_points", True)
        )

    def _calculated_points(self, card: Any, bonuses: dict[str, int] | None = None) -> int:
        """Build a total from the card instance's base and its ability bonuses."""
        applied = bonuses if bonuses is not None else dict(card.get("applied_bonuses") or {})
        reductions = dict(card.get("bonus_point_reductions") or {})
        return max(0, self._base_points(card) + sum(applied.values()) - sum(reductions.values()))

    def restore_base_point_reductions(self, card: Any) -> int:
        """Restore one card's recorded base reductions without changing its bonuses."""
        reductions = dict(card.get("base_point_reductions") or {})
        restored = sum(max(0, int(amount)) for amount in reductions.values())
        if restored <= 0:
            return 0
        card.base_points = self._base_points(card) + restored
        card.base_point_reductions = {}
        card.current_points = self._calculated_points(card)
        return restored

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
            row.abilities_disabled = any(
                source is not None and not getattr(source, "eliminated", False)
                and any(
                    ability.id in set(source.get("activated_abilities") or [])
                    or ability.id in set(source.get("resolved_abilities") or [])
                    for ability in (getattr(self.definitions.get(source.definition_id), "abilities", None) or [])
                    if any(effect.type == "disable_row_abilities" for effect in ability.effects)
                )
                for source in row.slots
            )
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








