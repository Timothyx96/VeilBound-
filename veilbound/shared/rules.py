from __future__ import annotations

import logging
import random
from collections.abc import Iterable, Mapping

from .abilities import AbilityActivation, AbilityDefinition, AbilityManager
from .config import MATCH_ROUNDS, PLAYER_POOL_SIZE, GameConfig
from .definitions import load_card_definitions
from .elimination import EliminationManager, EliminationTargeting
from .models import (
    ActionResult, BattleRow, CardInstance, GameEvent, GamePhase, GameState,
    PlayerState,
)


ROW_COUNT = 4
logger = logging.getLogger(__name__)
ZENO_TRAP_ABILITY_ID = "zeno_erase_from_existence_trap"


def _card_resolved_for_reveal(card: CardInstance | None) -> bool:
    """A card is finished with the reveal pass when revealed or eliminated."""
    return card is not None and (card.revealed or getattr(card, "eliminated", False))


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
        # Shared authority for ability target validation and resolution.
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
            suppressed_bonuses=[],
            base_point_reductions={},
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
            prompt = (
                "Select the ability target before continuing."
                if state.pending_elimination.get("effect_type") != "eliminate_target"
                else "Select an elimination target before continuing."
            )
            return ActionResult(False, prompt)
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
            if previous is not None and not getattr(previous, "eliminated", False):
                self._reveal_card(row, slot_index - 1, previous)
        self._resolve_zeno_trap(row, card, slot_index)
        # Continuous placement conditions depend on the latest occupied slot,
        # including cards that have not yet been revealed.
        self.abilities.recalculate_continuous_bonuses(state)
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
                card is not None and not card.revealed and not getattr(card, "eliminated", False)
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
            prompt = (
                "Select the ability target before continuing the reveal."
                if state.pending_elimination.get("effect_type") != "eliminate_target"
                else "Select an elimination target before continuing the reveal."
            )
            return ActionResult(False, prompt)
        event_start = len(state.events)
        next_card = next((
            (row, index, card)
            for row in state.rows
            for index, card in enumerate(row.slots)
            if card is not None and not card.revealed and not getattr(card, "eliminated", False)
        ), None)
        if next_card is None:
            if all(_card_resolved_for_reveal(placed) for full_row in state.rows for placed in full_row.slots):
                state.final_reveal_active = False
                state.phase = GamePhase.ROW_SCORING
                self._emit("final_reveal_complete", "All cards are revealed. Compare scores starting with Row 1.", {})
                return ActionResult(True, events=tuple(state.events[event_start:]))
            return ActionResult(False, "Every card is already revealed.")
        row, hidden, card = next_card
        self._reveal_card(row, hidden, card)
        state.reveal_card_index += 1

        # A reveal may trigger an elimination ability: pause the reveal flow
        # for player target selection (fails through when no target exists).
        if self._begin_pending_elimination(event_start):
            # Do not advance to the next card; wait for target selection.
            return ActionResult(True, events=tuple(state.events[event_start:]))

        if all(_card_resolved_for_reveal(placed) for full_row in state.rows for placed in full_row.slots):
            state.final_reveal_active = False
            state.phase = GamePhase.ROW_SCORING
            self._emit("final_reveal_complete", "All cards are revealed. Compare scores starting with Row 1.", {})
        return ActionResult(True, events=tuple(state.events[event_start:]))

    # ---------------------------------------------------------------- elimination

    def _resolve_zeno_trap(self, row, placed_card: CardInstance, slot_index: int) -> bool:
        """Automatically erase the newly placed enemy card when Zeno's trap is armed."""
        if slot_index == 0 or getattr(placed_card, "eliminated", False):
            return False
        zeno = row.slots[0]
        if (
            zeno is None
            or zeno.definition_id != "dragonball_09"
            or zeno.owner_id == placed_card.owner_id
            or getattr(zeno, "eliminated", False)
            or ZENO_TRAP_ABILITY_ID in (zeno.get("activated_abilities") or [])
        ):
            return False
        ability = next((
            item for item in (self.cards[zeno.definition_id].abilities or [])
            if item.id == ZENO_TRAP_ABILITY_ID
            and item.trigger == "on_next_opposing_placement"
        ), None)
        if ability is None:
            return False

        # play_card only permits the next empty slot from left to right, so a
        # Zeno in slot 0 was necessarily placed first into an empty row.
        # Mark consumption before mutating state so re-entry cannot duplicate it.
        zeno.activated_abilities = [
            *(zeno.get("activated_abilities") or []), ZENO_TRAP_ABILITY_ID,
        ]
        self.elimination.eliminate_target(self.state, placed_card, zeno)
        self.abilities.recalculate_after_elimination(self.state, placed_card)
        source_name = self.cards[zeno.definition_id].name
        target_name = self.cards[placed_card.definition_id].name
        self._emit(
            "elimination",
            f"{target_name} has been eliminated by {source_name}.",
            {
                "attacker_card_id": zeno.id,
                "attacker_name": source_name,
                "target_card_id": placed_card.id,
                "target_name": target_name,
                "row_id": row.id,
                "slot_index": slot_index,
                "definition_id": placed_card.definition_id,
                "ability_id": ability.id,
                "elimination_id": f"{zeno.id}:{placed_card.id}",
                "animate": False,
            },
        )
        self._emit_ability_resolved(
            ability_id=ability.id,
            ability_name=ability.name,
            owner_card_id=zeno.id,
            definition_id=zeno.definition_id,
            row_id=row.id,
            slot_index=0,
            sound=ability.presentation.sound,
            target_card_id=placed_card.id,
            target_name=target_name,
        )
        return True

    def _begin_pending_elimination(self, event_start: int) -> bool:
        """Arm the shared target-selection pause when an ability needs a target.

        Scans the events appended since ``event_start`` for a targeted ability
        activation. When found, arms ``state.pending_elimination``
        with a JSON-safe targeting payload and the current valid targets.
        If an elimination has no valid targets it fails immediately. Other
        target effects with no valid targets continue without pausing. Returns
        True when the flow must pause for player target selection.
        """
        state = self.state
        target_activation = next((
            event for event in state.events[event_start:]
            if event.kind == "ability_activated"
            and event.data.get("requires_target_selection", event.data.get("is_elimination"))
        ), None)
        if target_activation is None:
            return False
        owner_card_id = str(target_activation.data.get("owner_card_id", ""))
        owner_card = next((
            slot for r in state.rows for slot in r.slots
            if slot is not None and slot.id == owner_card_id
        ), None)
        if owner_card is None:
            return False
        definition = self.cards[owner_card.definition_id]
        ability_id = str(target_activation.data.get("ability_id", ""))
        ability = next((
            item for item in (getattr(definition, "abilities", None) or [])
            if item.id == ability_id
        ), None)
        if ability is None:
            return False
        targeting = EliminationTargeting(
            target_scope=ability.target_scope,
            target_side=ability.target_side,
            allowed_types=ability.allowed_types,
            max_base_points=ability.max_base_points,
            min_base_points=ability.min_base_points,
            require_bonus_points=any(
                effect.type in {"remove_target_bonus", "steal_target_bonus"}
                for effect in ability.effects
            ),
            require_eliminated=any(
                effect.type == "revive_target" for effect in ability.effects
            ),
        )
        valid_targets = self.elimination.get_valid_targets(state, owner_card, targeting)
        effect_type = next(
            effect.type for effect in ability.effects
            if effect.type in {
                "eliminate_target", "remove_target_bonus", "steal_target_bonus",
                "revive_target", "reduce_target_base_points",
            }
        )
        if not valid_targets and effect_type != "eliminate_target":
            # A non-elimination target effect with no eligible card does not pause the
            # reveal flow or emit a successful-resolution event.
            self._emit_ability_failed(
                ability_id=ability.id,
                ability_name=ability.name,
                owner_card_id=owner_card.id,
                owner_name=str(definition.name),
                row_id=str(target_activation.data.get("row_id", "")),
                slot_index=int(target_activation.data.get("slot_index", 0)),
                reason="no_valid_targets",
            )
            return False
        # JSON-safe pending payload: plain scalars only, so the authority can
        # serialize it into network views.
        state.pending_elimination = {
            "attacker_card_id": owner_card.id,
            "attacker_owner_id": owner_card.owner_id,
            "attacker_name": str(definition.name),
            "ability_id": ability.id,
            "ability_name": ability.name,
            "effect_amount": next(
                (effect.amount for effect in ability.effects
                 if effect.type == effect_type),
                0,
            ),
            "floating_text": str(target_activation.data.get("floating_text", "")),
            "effect_type": effect_type,
            "sound": ability.presentation.sound,
            "definition_id": owner_card.definition_id,
            "row_id": str(target_activation.data.get("row_id", "")),
            "slot_index": int(target_activation.data.get("slot_index", 0)),
            "targeting": {
                "target_scope": targeting.target_scope,
                "target_side": targeting.target_side,
                "allowed_types": list(targeting.allowed_types),
                "max_base_points": targeting.max_base_points,
                "min_base_points": targeting.min_base_points,
                "require_bonus_points": targeting.require_bonus_points,
                "require_eliminated": targeting.require_eliminated,
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
        """Resolve a player's ability target selection.

        Validates that the chosen slot is still a legal target, eliminates it,
        clears the pending-elimination state, and returns the resolution event.
        Returns a rejected ActionResult if the target is no longer valid or no
        ability target selection is pending.
        """
        state = self.state
        pending = state.get("pending_elimination")
        if not pending:
            return ActionResult(False, "No ability target is awaiting selection.")

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
            require_bonus_points=bool(pending["targeting"].get("require_bonus_points", False)),
            require_eliminated=bool(pending["targeting"].get("require_eliminated", False)),
        )
        valid_targets = self.elimination.get_valid_targets(state, attacker_card, targeting)
        valid_keys = {(r.id, i) for r, i, _ in valid_targets}
        if (target_row_id, target_slot_index) not in valid_keys:
            return ActionResult(False, "That card is no longer a valid ability target.")

        event_start = len(state.events)
        effect_type = str(pending.get("effect_type", "eliminate_target"))
        followup_event_start: int | None = None
        target_definition = self.cards.get(target_card.definition_id)
        target_name = str(getattr(target_definition, "name", "Unknown character"))
        if effect_type == "revive_target":
            if not target_card.eliminated or row.slots[target_slot_index] is not target_card:
                return ActionResult(False, "That character is no longer eligible for revival.")
            gon_reset = self.abilities.reset_gon_for_revival(state, target_card)
            row.eliminated_slots = [
                index for index in (row.get("eliminated_slots") or [])
                if index != target_slot_index
            ]
            target_card.eliminated = False
            if not gon_reset:
                target_card.current_points = self.abilities._calculated_points(target_card)
            self.abilities.reset_failure_reports(target_card.id)
            self.abilities.recalculate_after_revival(state, target_card)
            self._emit(
                "revival",
                f"{target_name} was revived by {pending.get('attacker_name', 'Unknown character')}.",
                {
                    "attacker_card_id": attacker_card.id,
                    "attacker_name": str(pending.get("attacker_name", "Unknown character")),
                    "target_card_id": target_card.id,
                    "target_name": target_name,
                    "row_id": target_row_id,
                    "slot_index": target_slot_index,
                    "current_points": int(target_card.current_points),
                    "base_points": self.abilities._base_points(target_card),
                    "resolved_abilities": list(target_card.get("resolved_abilities") or []),
                },
            )
            # Gon gets one explicit retry attempt as part of his revival event.
            # The manager limits this pass to the revived owner; ordinary card
            # reveals and point recalculations cannot trigger his pending ability.
            if target_card.revealed:
                followup_event_start = len(state.events)
                self.abilities.on_card_revealed(
                    state,
                    target_card,
                    row,
                    target_slot_index,
                    emit=self._emit_ability_activation,
                    emit_failure=self._handle_ability_failure,
                    revived=True,
                    owner_only=gon_reset,
                )
            self.abilities.recalculate_continuous_bonuses(state)
        elif effect_type == "steal_target_bonus":
            previous_target_points = int(target_card.current_points)
            previous_attacker_points = int(attacker_card.current_points)
            stolen = self.abilities.steal_bonus_points(
                attacker_card,
                target_card,
                str(pending.get("ability_id", "")),
                int(pending.get("effect_amount", 0)),
            )
            if stolen <= 0:
                return ActionResult(False, "That card no longer has stealable bonus points.")
            self._emit(
                "bonus_points_stolen",
                f"{pending.get('attacker_name', 'Unknown character')} stole {stolen} bonus points from {target_name}.",
                {
                    "attacker_card_id": attacker_card.id,
                    "attacker_name": str(pending.get("attacker_name", "Unknown character")),
                    "target_card_id": target_card.id,
                    "target_name": target_name,
                    "row_id": target_row_id,
                    "slot_index": target_slot_index,
                    "amount": stolen,
                    "attacker_previous_points": previous_attacker_points,
                    "attacker_current_points": int(attacker_card.current_points),
                    "target_previous_points": previous_target_points,
                    "target_current_points": int(target_card.current_points),
                },
            )
        elif effect_type == "remove_target_bonus":
            base_points = self.abilities._base_points(target_card)
            previous_points = int(target_card.current_points)
            # A continuous bonus already active on the target must stay
            # suppressed through scoring, until its own condition ceases.
            suppressed = set(target_card.get("suppressed_bonuses") or [])
            applied = dict(target_card.get("applied_bonuses") or {})
            continuous_ids = {
                ability.id
                for ability in (getattr(target_definition, "abilities", None) or [])
                if ability.activation_limit == "continuous"
            }
            suppressed.update(continuous_ids.intersection(applied))
            target_card.suppressed_bonuses = sorted(suppressed)
            target_card.applied_bonuses = {}
            target_card.current_points = base_points
            self._emit(
                "bonus_points_removed",
                f"{target_name}'s bonus points were removed by {pending.get('attacker_name', 'Unknown character')}.",
                {
                    "attacker_card_id": str(pending["attacker_card_id"]),
                    "attacker_name": str(pending.get("attacker_name", "Unknown character")),
                    "target_card_id": target_card.id,
                    "target_name": target_name,
                    "row_id": target_row_id,
                    "slot_index": target_slot_index,
                    "previous_points": previous_points,
                    "current_points": base_points,
                },
            )
        elif effect_type == "reduce_target_base_points":
            previous_base = self.abilities._base_points(target_card)
            previous_points = int(target_card.current_points)
            reduction_id = (
                f"{pending.get('ability_id', '')}:{pending['attacker_card_id']}"
            )
            amount = int(pending.get("effect_amount", 0))
            if not self.abilities.reduce_base_points(target_card, reduction_id, amount):
                return ActionResult(False, "This ability's base-point reduction was already applied.")
            self._emit(
                "base_points_reduced",
                f"{target_name}'s base points were reduced by {amount}.",
                {
                    "attacker_card_id": str(pending["attacker_card_id"]),
                    "attacker_name": str(pending.get("attacker_name", "Unknown character")),
                    "target_card_id": target_card.id,
                    "target_name": target_name,
                    "row_id": target_row_id,
                    "slot_index": target_slot_index,
                    "previous_base_points": previous_base,
                    "base_points": self.abilities._base_points(target_card),
                    "previous_points": previous_points,
                    "current_points": int(target_card.current_points),
                },
            )
            self._eliminate_if_base_is_zero(
                target_card, str(pending.get("ability_id", "")),
            )
        else:
            # Resolve elimination through the centralized elimination function.
            attacker_name = str(pending.get("attacker_name", "Unknown character"))
            self.elimination.eliminate_target(state, target_card, attacker_card)
            self.abilities.recalculate_after_elimination(state, target_card)
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

        resolved_ability = next((
            item for item in (getattr(self.cards.get(attacker_card.definition_id), "abilities", None) or [])
            if item.id == str(pending.get("ability_id", ""))
        ), None)
        if resolved_ability is not None:
            self.abilities.apply_deferred_effects(resolved_ability, attacker_card, state)

        self._emit_ability_resolved(
            ability_id=str(pending.get("ability_id", "")),
            ability_name=str(pending.get("ability_name", "")),
            owner_card_id=str(pending["attacker_card_id"]),
            definition_id=str(pending.get("definition_id", attacker_card.definition_id)),
            row_id=str(pending.get("row_id", "")),
            slot_index=int(pending.get("slot_index", 0)),
            sound=pending.get("sound"),
            target_card_id=target_card.id,
            target_name=target_name,
            floating_text=str(pending.get("floating_text", "")),
        )
        state.pending_elimination = None
        if followup_event_start is not None:
            self._begin_pending_elimination(followup_event_start)
        # The pause skipped the reveal-completion check; re-run it so a fully
        # revealed board proceeds to row scoring instead of stalling.
        if state.phase == GamePhase.REVEAL and not state.pending_elimination and all(
            placed is not None and placed.revealed
            for full_row in state.rows for placed in full_row.slots
        ):
            state.final_reveal_active = False
            state.phase = GamePhase.ROW_SCORING
            self._emit("final_reveal_complete", "All cards are revealed. Compare scores starting with Row 1.", {})
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def _eliminate_if_base_is_zero(self, card: CardInstance, ability_id: str) -> bool:
        """Eliminate a live card as soon as an ability leaves its base at zero."""
        if card.eliminated or self.abilities._base_points(card) > 0:
            return False
        located = next((
            (row, index)
            for row in self.state.rows
            for index, placed in enumerate(row.slots)
            if placed is not None and placed.id == card.id
        ), None)
        if located is None:
            return False
        row, slot_index = located
        definition = self.cards.get(card.definition_id)
        character_name = str(getattr(definition, "name", "Unknown character"))
        self.elimination.eliminate_target(self.state, card, card)
        self.abilities.recalculate_after_elimination(self.state, card)
        self._emit(
            "death",
            f"{character_name} Died",
            {
                "target_card_id": card.id,
                "target_name": character_name,
                "row_id": row.id,
                "slot_index": slot_index,
                "definition_id": card.definition_id,
                "ability_id": ability_id,
                "elimination_id": f"death:{card.id}",
                "animate": True,
            },
        )
        return True

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
        if pending.get("effect_type") == "eliminate_target":
            self._emit(
                "elimination_failed",
                f"{attacker_name}'s elimination attempt failed: no valid targets.",
                {
                    "attacker_card_id": str(pending["attacker_card_id"]),
                    "attacker_name": attacker_name,
                },
            )
        self._emit_ability_failed(
            ability_id=str(pending.get("ability_id", "")),
            ability_name=str(pending.get("ability_name", "")),
            owner_card_id=str(pending.get("attacker_card_id", "")),
            owner_name=attacker_name,
            row_id=str(pending.get("row_id", "")),
            slot_index=int(pending.get("slot_index", 0)),
            reason="cancelled_or_no_valid_targets",
        )
        state.pending_elimination = None
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def score_current_row(self) -> ActionResult:
        state = self.state
        if state.phase != GamePhase.ROW_SCORING:
            return ActionResult(False, "A row can be scored only after all its cards are revealed.")
        if state.get("pending_elimination"):
            prompt = (
                "Select the ability target before scoring."
                if state.pending_elimination.get("effect_type") != "eliminate_target"
                else "Select an elimination target before scoring."
            )
            return ActionResult(False, prompt)
        if not all(
            row.is_full and all(_card_resolved_for_reveal(card) for card in row.slots)
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
            if not next_row.is_full or not all(_card_resolved_for_reveal(card) for card in next_row.slots):
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
                        "eliminated": bool(card.get("eliminated", False)),
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
            "base_points": self.abilities._base_points(card),
            "effect_name": definition.effect_name,
            "effect_text": definition.effect_text,
            "owner_id": card.owner_id,
            "revealed": card.revealed,
            "current_points": card.get("current_points"),
            "activated_abilities": list(card.get("activated_abilities") or []),
            "resolved_abilities": list(card.get("resolved_abilities") or []),
            "eliminated": bool(card.get("eliminated", False)),
            "applied_bonuses": dict(card.get("applied_bonuses") or {}),
            "suppressed_bonuses": list(card.get("suppressed_bonuses") or []),
            "base_point_reductions": dict(card.get("base_point_reductions") or {}),
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
        if card.revealed or getattr(card, "eliminated", False):
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
            "base_points": self.abilities._base_points(card),
            "effect_name": definition.effect_name,
            "effect_text": definition.effect_text,
        })
        self._resolve_reveal_triggers(card, row, slot_index)

    def _resolve_reveal_triggers(self, card: CardInstance, row: BattleRow, slot_index: int) -> None:
        """Run the central ability manager: Phase A pending, Phase B this card."""
        self.abilities.on_card_revealed(
            self.state, card, row, slot_index,
            emit=self._emit_ability_activation,
            emit_failure=self._handle_ability_failure,
        )

    def _handle_ability_failure(
        self,
        ability: AbilityDefinition,
        owner_card: CardInstance,
        row: BattleRow,
        slot_index: int,
        reason: str,
    ) -> None:
        definition = self.cards.get(owner_card.definition_id)
        if definition is None:
            return
        self._emit_ability_failed(
            ability_id=ability.id,
            ability_name=ability.name,
            owner_card_id=owner_card.id,
            owner_name=str(definition.name),
            row_id=row.id,
            slot_index=slot_index,
            reason=reason,
        )

    def _emit_ability_failed(
        self,
        *,
        ability_id: str,
        ability_name: str,
        owner_card_id: str,
        owner_name: str,
        row_id: str,
        slot_index: int,
        reason: str,
    ) -> None:
        """Emit shared failure feedback after an ability cannot resolve."""
        owner_card = next((
            slot for row in self.state.rows for slot in row.slots
            if slot is not None and slot.id == owner_card_id
        ), None)
        if owner_card is not None:
            owner_card.activated_abilities = [
                used for used in (owner_card.get("activated_abilities") or [])
                if used != ability_id
            ]
            self.abilities.retain_failed_attempt(
                self.state, owner_card_id, ability_id,
            )
        self._emit(
            "ability_failed",
            f"{owner_name}'s {ability_name} failed",
            {
                "ability_id": ability_id,
                "ability_name": ability_name,
                "owner_card_id": owner_card_id,
                "owner_name": owner_name,
                "row_id": row_id,
                "slot_index": slot_index,
                "reason": reason,
            },
        )

    def _emit_ability_activation(self, activation: AbilityActivation) -> None:
        """Queue activation feedback and resolve effects needing no target choice."""
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
        # Immediate point effects resolve now. Target-selection effects wait
        # until select_elimination_target validates and applies the chosen card.
        if not activation.requires_target_selection:
            self._emit_ability_resolved(
                ability_id=activation.ability.id,
                ability_name=activation.ability.name,
                owner_card_id=activation.owner_card_id,
                definition_id=activation.definition_id,
                row_id=activation.row_id,
                slot_index=activation.slot_index,
                sound=activation.ability.presentation.sound,
                target_card_id=activation.target_card_id,
            )
            owner_card = next((
                slot for row in self.state.rows for slot in row.slots
                if slot is not None and slot.id == activation.owner_card_id
            ), None)
            if owner_card is not None:
                self._eliminate_if_base_is_zero(owner_card, activation.ability.id)

    def _emit_ability_resolved(
        self,
        *,
        ability_id: str,
        ability_name: str,
        owner_card_id: str,
        definition_id: str,
        row_id: str,
        slot_index: int,
        sound: str | None,
        target_card_id: str | None = None,
        target_name: str | None = None,
        floating_text: str | None = None,
    ) -> None:
        """Emit one completion event only after an ability effect succeeds."""
        owner_card = next((
            slot for row in self.state.rows for slot in row.slots
            if slot is not None and slot.id == owner_card_id
        ), None)
        if owner_card is not None:
            resolved = list(owner_card.get("resolved_abilities") or [])
            if ability_id not in resolved:
                resolved.append(ability_id)
            owner_card.resolved_abilities = resolved
            activated = list(owner_card.get("activated_abilities") or [])
            if ability_id not in activated:
                activated.append(ability_id)
            owner_card.activated_abilities = activated
        data = {
            "resolution_id": str(len(self.state.events)),
            "ability_id": ability_id,
            "ability_name": ability_name,
            "owner_card_id": owner_card_id,
            "definition_id": definition_id,
            "row_id": row_id,
            "slot_index": slot_index,
            "sound": sound,
        }
        if target_card_id is not None:
            data["target_card_id"] = target_card_id
        if target_name is not None:
            data["target_name"] = target_name
        if floating_text:
            data["floating_text"] = floating_text
        self._emit(
            "ability_resolved",
            f"{ability_name} resolved successfully.",
            data,
        )

    def _emit(self, kind: str, message: str, data: dict) -> None:
        self.state.events.append(GameEvent(kind, message, data))

