from __future__ import annotations

import logging
import random
import time
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
KITE_CRAZY_SLOTS_ABILITY_ID = "hxh_07_crazy_slots"
SOGEKING_ABILITY_ID = "usopp_sogeking_shoot_down_flag"


def _card_resolved_for_reveal(card: CardInstance | None) -> bool:
    """A slot is finished with the reveal pass when empty, revealed, or eliminated.

    Law can leave an intentionally blocked empty source slot after moving a
    card. Such a slot must not hold the global reveal counter open forever.
    """
    return card is None or card.revealed or getattr(card, "eliminated", False)


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
    for player_id, amount in dict(row.get("sogeking_bonus_points") or {}).items():
        if player_id in scores:
            scores[player_id] += int(amount)
    return scores


def row_uses_reverse_fate(row, cards) -> bool:
    """Whether an unsealed, revealed Tobi reverses this row's result."""
    if row.get("abilities_disabled", False):
        return False
    for slot_index, card in enumerate(row.slots):
        if slot_index >= 4:
            break
        if (card is None or not card.revealed or getattr(card, "eliminated", False)
                or bool(card.get("ability_suppressed", False))):
            continue
        definition = cards.get(card.definition_id)
        if any(
            effect.type == "reverse_row_winner"
            for ability in (getattr(definition, "abilities", None) or [])
            for effect in ability.effects
        ):
            return True
    return False


def determine_row_winner(row, scores, players, cards) -> str | None:
    """Apply row-wide overrides before the normal high-score winner rule."""
    # Automatic-win abilities take priority over every scoring modifier.
    automatic_winners: set[str] = set()
    for card in row.slots:
        if (card is None or not card.revealed
                or getattr(card, "eliminated", False)):
            continue
        definition = cards.get(card.definition_id)
        if not any(
            effect.type == "automatic_win_if_opposing_row_has_no_goat"
            for ability in (getattr(definition, "abilities", None) or [])
            for effect in ability.effects
        ):
            continue
        if not any(
            opposing is not None
            and opposing.owner_id != card.owner_id
            and not getattr(opposing, "eliminated", False)
            and getattr(cards.get(opposing.definition_id), "type", None) == "GOAT"
            for opposing in row.slots
        ):
            automatic_winners.add(card.owner_id)
    if len(automatic_winners) == 1:
        return next(iter(automatic_winners))

    first, second = players
    first_score, second_score = scores[first.id], scores[second.id]
    if first_score == second_score:
        return None
    if row_uses_reverse_fate(row, cards):
        return first.id if first_score < second_score else second.id
    return first.id if first_score > second_score else second.id


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
        self._draining_auto_revivals = False
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
        state.pending_movement_reveals = []
        state.pending_crazy_slots = None
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
        if state.get("pending_elimination") or state.get("pending_crazy_slots"):
            return []
        player = state.active_player if player_id is None else state.player(player_id)
        if player.id != state.active_player.id or not player.hand:
            return []
        legal: list[tuple[str, int]] = []
        for row in state.rows:
            next_index = self._next_placement_slot(row)
            if next_index is not None:
                legal.append((row.id, next_index))
        return legal

    @staticmethod
    def _next_placement_slot(row: BattleRow) -> int | None:
        """Return the same legal left-to-right slot for plays and relocations."""
        blocked = set(row.get("movement_blocked_slots", []))
        return next((
            index for index, card in enumerate(row.slots)
            if card is None and (index not in blocked or index == row.capacity - 1)
        ), None)

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
        if state.get("pending_crazy_slots"):
            return ActionResult(False, "Finish Kite's Crazy Slots ability before placing another card.")
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
        blocked = set(row.get("movement_blocked_slots", []))
        next_slot = self._next_placement_slot(row)
        if slot_index in blocked and slot_index != row.capacity - 1:
            return ActionResult(False, "That slot was closed by a character movement.")
        if slot_index != next_slot:
            return ActionResult(False, "Place the card in the next empty slot from left to right.")

        row_was_empty = all(placed is None for placed in row.slots)
        event_start = len(state.events)
        player.hand.remove(card)
        player.used.append(card)
        row.slots[slot_index] = card
        self._emit("placement", f"{player.name} places a face-down card in Row {row.number}.", {
            "player_id": player.id, "row_id": row.id, "slot_index": slot_index,
        })
        self._process_row_placement(card, row, slot_index, row_was_empty=row_was_empty)
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

    def _process_row_placement(
        self, card: CardInstance, row: BattleRow, slot_index: int,
        *, row_was_empty: bool,
    ) -> None:
        """Run the shared row-entry mechanics after a card occupies a slot.

        Both hand placements and Law relocations use this pipeline. It handles
        row-local placement effects, normal adjacent reveal resolution, traps,
        and continuous bonuses without changing the card's reveal state.
        """
        state = self.state
        placement_activations = self.abilities.on_card_placed(
            state, card, row, slot_index, emit=self._emit_ability_activation,
        )
        for activation in placement_activations:
            if not any(
                effect.type == "suppress_next_opposing_ability"
                for effect in activation.ability.effects
            ):
                continue
            target = next((
                placed for placed in row.slots
                if placed is not None and placed.id == activation.target_card_id
            ), None)
            if target is not None:
                self._emit(
                    "ability_suppressed",
                    f"{self.cards[target.definition_id].name}'s ability is suppressed by {activation.owner_name}.",
                    {
                        "ability_id": activation.ability.id,
                        "source_card_id": activation.owner_card_id,
                        "source_row_id": row.id,
                        "source_slot_index": activation.slot_index,
                        "target_card_id": target.id,
                        "target_row_id": row.id,
                        "target_slot_index": slot_index,
                        "visual_style": str(target.get("ability_suppression_style", "black")),
                    },
                )
        self.abilities.on_card_placed_in_empty_row(
            state, card, row, slot_index, row_was_empty=row_was_empty,
            emit=self._emit_ability_activation,
        )
        self._reveal_preceding_card(row, slot_index)
        self._resolve_zeno_trap(row, card, slot_index)
        self._resolve_dabi_blueflame(row, card, slot_index)
        self.abilities.recalculate_continuous_bonuses(state)

    def _resolve_dabi_blueflame(self, row, placed_card: CardInstance, slot_index: int) -> None:
        """Apply Dabi's instance-scoped base reduction to an enemy placed behind him."""
        if getattr(placed_card, "eliminated", False):
            return
        for dabi_index, dabi in enumerate(row.slots[:slot_index]):
            if (dabi is None or dabi.definition_id != "mha_03" or not dabi.revealed
                    or getattr(dabi, "eliminated", False) or dabi.owner_id == placed_card.owner_id):
                continue
            ability = next((
                item for item in (self.cards[dabi.definition_id].abilities or [])
                if item.id == "mha_03_blueflame"
            ), None)
            if ability is None:
                continue
            amount = next((effect.amount for effect in ability.effects
                           if effect.type == "reduce_target_base_points"), 0)
            reduction_id = f"{ability.id}:{dabi.id}:{placed_card.id}"
            if (amount > 0 and not self.elimination.is_protected_from_opposing_ability(
                    placed_card, dabi)):
                if self.abilities.reduce_base_points(placed_card, reduction_id, amount):
                    self._eliminate_if_base_is_zero(
                        placed_card, ability.id,
                    )
                    if placed_card.eliminated:
                        return

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
        if state.get("pending_crazy_slots"):
            return ActionResult(False, "Finish Kite's Crazy Slots ability before continuing the reveal.")
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

        if state.get("pending_crazy_slots"):
            return ActionResult(True, events=tuple(state.events[event_start:]))

        if all(_card_resolved_for_reveal(placed) for full_row in state.rows for placed in full_row.slots):
            state.final_reveal_active = False
            state.phase = GamePhase.ROW_SCORING
            self._emit("final_reveal_complete", "All cards are revealed. Compare scores starting with Row 1.", {})
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def spin_crazy_slots(self, player_id: str) -> ActionResult:
        """Choose Crazy Slots' result once and broadcast the synchronized spin."""
        state = self.state
        pending = state.get("pending_crazy_slots")
        if not pending or pending.get("status") != "awaiting_spin":
            return ActionResult(False, "Kite is not waiting for a Crazy Slots spin.")
        if str(player_id) != str(pending.get("owner_id", "")):
            return ActionResult(False, "Only Kite's player may spin Crazy Slots.")
        owner = next((
            card for row in state.rows for card in row.slots
            if card is not None and card.id == str(pending.get("owner_card_id", ""))
        ), None)
        if owner is None or owner.eliminated or not owner.revealed:
            state.pending_crazy_slots = None
            event_start = len(state.events)
            self._emit_ability_failed(
                ability_id=KITE_CRAZY_SLOTS_ABILITY_ID,
                ability_name="Crazy Slots",
                owner_card_id=str(pending.get("owner_card_id", "")),
                owner_name="Kite",
                row_id=str(pending.get("row_id", "")),
                slot_index=int(pending.get("slot_index", 0)),
                reason="owner_unavailable",
            )
            self._resume_reveal_after_target_selection()
            return ActionResult(True, events=tuple(state.events[event_start:]))

        result_number = self.roll_kite_slot(state, pending.get("outcomes", []))
        if result_number is None:
            return ActionResult(False, "Crazy Slots has no valid configured outcomes.")
        pending["result_number"] = result_number
        selected_outcome = next(
            outcome for outcome in pending["outcomes"]
            if int(outcome.get("number", 0)) == result_number
        )
        pending["result_name"] = str(selected_outcome.get("name", ""))
        pending["spin_started_at"] = time.time()
        pending["status"] = "spinning"
        pending["spin_id"] = f"{state.round_number}:{owner.id}:{len(state.events)}"
        event_start = len(state.events)
        self._emit(
            "crazy_slots_spin_started",
            "Kite's Crazy Slots begins spinning.",
            {
                "ability_id": KITE_CRAZY_SLOTS_ABILITY_ID,
                "ability_name": "Crazy Slots",
                "spin_id": pending["spin_id"],
                "owner_card_id": owner.id,
                "owner_id": owner.owner_id,
                "row_id": str(pending["row_id"]),
                "slot_index": int(pending["slot_index"]),
                "result_number": result_number,
                "spin_started_at": pending["spin_started_at"],
            },
        )
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def roll_kite_slot(self, match_state: GameState, outcomes: list[dict]) -> int | None:
        """Make the one authoritative Crazy Slots roll for this pending ability."""
        valid_numbers = [
            int(outcome["number"])
            for outcome in outcomes
            if isinstance(outcome, dict) and outcome.get("number") in {1, 2, 3}
        ]
        return self._rng.choice(valid_numbers) if len(valid_numbers) == 3 else None

    def complete_crazy_slots(self, player_id: str) -> ActionResult:
        """Apply the previously selected result after clients finish the spin."""
        state = self.state
        pending = state.get("pending_crazy_slots")
        if not pending or pending.get("status") != "spinning":
            return ActionResult(False, "There is no Crazy Slots spin to complete.")
        if str(player_id) != str(pending.get("owner_id", "")):
            return ActionResult(False, "Only Kite's player may complete the Crazy Slots result.")
        owner = next((
            card for row in state.rows for card in row.slots
            if card is not None and card.id == str(pending.get("owner_card_id", ""))
        ), None)
        if owner is None or owner.eliminated or not owner.revealed:
            state.pending_crazy_slots = None
            event_start = len(state.events)
            self._emit_ability_failed(
                ability_id=KITE_CRAZY_SLOTS_ABILITY_ID,
                ability_name="Crazy Slots",
                owner_card_id=str(pending.get("owner_card_id", "")),
                owner_name="Kite",
                row_id=str(pending.get("row_id", "")),
                slot_index=int(pending.get("slot_index", 0)),
                reason="owner_unavailable",
            )
            self._resume_reveal_after_target_selection()
            return ActionResult(True, events=tuple(state.events[event_start:]))

        event_start = len(state.events)
        result_number = int(pending.get("result_number", 0))
        ability_id = str(pending.get("ability_id", KITE_CRAZY_SLOTS_ABILITY_ID))
        row_id = str(pending.get("row_id", ""))
        slot_index = int(pending.get("slot_index", 0))
        outcome = next((
            item for item in pending.get("outcomes", [])
            if isinstance(item, dict) and int(item.get("number", 0)) == result_number
        ), None)
        if outcome is None:
            state.pending_crazy_slots = None
            event_start = len(state.events)
            self._emit_ability_failed(
                ability_id=ability_id,
                ability_name="Crazy Slots",
                owner_card_id=owner.id,
                owner_name="Kite",
                row_id=row_id,
                slot_index=slot_index,
                reason="invalid_result",
            )
            self._resume_reveal_after_target_selection()
            return ActionResult(True, events=tuple(state.events[event_start:]))
        spin_id = str(pending.get("spin_id", ""))
        result_name = str(outcome.get("name", f"Outcome {result_number}"))
        state.pending_crazy_slots = None
        current_row = next((row for row in state.rows if row.id == row_id), None)
        if current_row is None:
            event_start = len(state.events)
            self._emit_ability_failed(
                ability_id=ability_id,
                ability_name="Crazy Slots",
                owner_card_id=owner.id,
                owner_name="Kite",
                row_id=row_id,
                slot_index=slot_index,
                reason="row_unavailable",
            )
            self._resume_reveal_after_target_selection()
            return ActionResult(True, events=tuple(state.events[event_start:]))

        result_effects = outcome.get("effects", [])
        if not isinstance(result_effects, list) or len(result_effects) != 1:
            return ActionResult(False, "Crazy Slots outcome has invalid effects.")
        result_effect = result_effects[0]
        if not isinstance(result_effect, dict):
            return ActionResult(False, "Crazy Slots outcome has invalid effects.")
        result_effect_type = str(result_effect.get("type", ""))
        floating_text = str(outcome.get("description", ""))
        if result_effect_type == "add_current_points":
            amount = int(result_effect.get("amount", 0))
            bonuses = dict(owner.get("applied_bonuses") or {})
            bonuses[ability_id] = amount
            owner.applied_bonuses = bonuses
            owner.current_points = self.abilities._calculated_points(owner, bonuses)
            floating_text = f"+{amount} bonus points"
        elif result_effect_type == "grant_status":
            status = str(result_effect.get("status", ""))
            if status != "immune_to_elimination":
                return ActionResult(False, "Crazy Slots outcome has an unsupported status.")
            setattr(owner, status, True)
            floating_text = "Immune to elimination"

        self._emit(
            "crazy_slots_result",
            f"Crazy Slots lands on {result_number}: {result_name}.",
            {
                "ability_id": ability_id,
                "ability_name": "Crazy Slots",
                "owner_card_id": owner.id,
                "owner_id": owner.owner_id,
                "row_id": current_row.id,
                "slot_index": slot_index,
                "result_number": result_number,
                "result_name": result_name,
                "current_points": int(owner.current_points),
                "spin_id": spin_id,
            },
        )

        state.crazy_slots_results.append({
            "ability_id": ability_id,
            "owner_card_id": owner.id,
            "owner_id": owner.owner_id,
            "spin_id": spin_id,
            "result_number": result_number,
            "result_name": result_name,
        })

        if result_effect_type == "eliminate_target":
            target_scope = str(result_effect.get("target_scope", "same_row"))
            target_side = str(result_effect.get("target_side", "opposing"))
            allowed_types = tuple(str(item) for item in result_effect.get("allowed_types", []))
            valid_targets = self.elimination.get_valid_targets(
                state,
                owner,
                EliminationTargeting(
                    target_scope=target_scope,
                    target_side=target_side,
                    allowed_types=allowed_types,
                    ability_elimination=True,
                ),
            )
            if valid_targets:
                state.pending_elimination = {
                    "attacker_card_id": owner.id,
                    "attacker_owner_id": owner.owner_id,
                    "attacker_name": "Kite",
                    "ability_id": ability_id,
                    "ability_name": f"Crazy Slots - {result_name}",
                    "effect_amount": 0,
                    "floating_text": "DEADLY PRECISION",
                    "effect_type": "eliminate_target",
                    "sound": None,
                    "definition_id": owner.definition_id,
                    "row_id": current_row.id,
                    "slot_index": slot_index,
                    "targeting": {
                        "target_scope": target_scope,
                        "target_side": target_side,
                        "allowed_types": list(allowed_types),
                        "excluded_types": [],
                        "max_base_points": None,
                        "min_base_points": None,
                        "require_bonus_points": False,
                        "require_eliminated": False,
                    },
                    "valid_targets": [
                        {"row_id": row.id, "slot_index": index, "card_id": target.id}
                        for row, index, target in valid_targets
                    ],
                    "selection_ready": True,
                    "kite_crazy_slots": True,
                }
            else:
                floating_text = "NO VALID TARGET"
                self._emit(
                    "crazy_slots_no_target",
                    "No valid target",
                    {"ability_id": ability_id, "owner_card_id": owner.id,
                     "row_id": current_row.id, "slot_index": slot_index},
                )

        if not state.get("pending_elimination"):
            self._emit_ability_resolved(
                ability_id=ability_id,
                ability_name="Crazy Slots",
                owner_card_id=owner.id,
                definition_id=owner.definition_id,
                row_id=current_row.id,
                slot_index=slot_index,
                sound=None,
                target_card_id=owner.id,
                floating_text=floating_text,
            )
        self._resume_reveal_after_target_selection()
        return ActionResult(True, events=tuple(state.events[event_start:]))

    # ---------------------------------------------------------------- elimination

    def _resolve_zeno_trap(self, row, placed_card: CardInstance, slot_index: int) -> bool:
        """Automatically erase the newly placed enemy card when Zeno's trap is armed."""
        if (slot_index == 0 or getattr(placed_card, "eliminated", False)
                or bool(placed_card.get("immune_to_elimination", False))):
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
        eliminated = self.elimination.eliminate_target(
            self.state, placed_card, zeno, by_ability=True,
        )
        if not eliminated:
            self._emit(
                "elimination_blocked",
                f"{self.cards[placed_card.definition_id].name} is protected from opposing abilities.",
                {
                    "attacker_card_id": zeno.id,
                    "target_card_id": placed_card.id,
                    "row_id": row.id,
                    "slot_index": slot_index,
                    "ability_id": ability.id,
                },
            )
            return False
        self._award_trunks_saiyan_resolve(row, placed_card)
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
        effect_type = next(
            effect.type for effect in ability.effects
            if effect.type in {
                "eliminate_target", "remove_target_bonus", "steal_target_bonus", "copy_target_bonus",
                "revive_target", "heal_target_base_points", "reduce_target_base_points", "move_character",
            }
        )
        targeting = EliminationTargeting(
            target_scope=ability.target_scope,
            target_side=ability.target_side,
            allowed_types=ability.allowed_types,
            excluded_types=ability.excluded_types,
            max_base_points=ability.max_base_points,
            min_base_points=ability.min_base_points,
            character_set=ability.target_character_set,
            require_base_point_reduction=ability.require_base_point_reduction,
            require_bonus_points=any(
                effect.type in {"remove_target_bonus", "steal_target_bonus", "copy_target_bonus"}
                for effect in ability.effects
            ),
            require_eliminated=any(
                effect.type == "revive_target" for effect in ability.effects
            ),
            ability_elimination=(
                effect_type == "eliminate_target"
                and bool(target_activation.data.get("is_elimination", False))
            ),
            opposing_ability=effect_type in {
                "remove_target_bonus", "steal_target_bonus", "reduce_target_base_points", "move_character",
            },
        )
        valid_targets = self.elimination.get_valid_targets(state, owner_card, targeting)
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
                "excluded_types": list(targeting.excluded_types),
                "max_base_points": targeting.max_base_points,
                "min_base_points": targeting.min_base_points,
                "require_bonus_points": targeting.require_bonus_points,
                "require_eliminated": targeting.require_eliminated,
                "opposing_ability": targeting.opposing_ability,
                "character_set": targeting.character_set,
                "require_base_point_reduction": targeting.require_base_point_reduction,
            },
            "valid_targets": [
                {"row_id": r.id, "slot_index": i, "card_id": c.id}
                for r, i, c in valid_targets
            ],
        }
        state.pending_elimination["selection_ready"] = False
        if effect_type == "move_character":
            state.pending_elimination["stage"] = "target_character"
        if not valid_targets:
            # No legal targets: fail the elimination attempt, then continue
            # so a fully revealed board still scores.
            self.fail_elimination()
            return False
        return True

    def select_elimination_target(
        self, target_row_id: str, target_slot_index: int
    ) -> ActionResult:
        """Resolve a card target or destination-row choice for a pending ability.

        Every target is validated against live state before its effect is
        applied. Movement abilities reuse this entry point for both stages.
        """
        state = self.state
        pending = state.get("pending_elimination")
        if not pending:
            return ActionResult(False, "No ability target is awaiting selection.")
        if not pending.get("selection_ready", True):
            return ActionResult(False, "Wait for the ability activation to finish.")
        if (pending.get("effect_type") == "move_character"
                and pending.get("stage") == "destination_row"):
            return self._select_move_destination(target_row_id, target_slot_index)

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
        moving_character = str(pending.get("effect_type", "")) == "move_character"
        if attacker_card is None:
            if moving_character:
                return self._fail_pending_move("ability_owner_unavailable")
            state.pending_elimination = None
            return ActionResult(False, "The ability's owner card left the board.")
        if attacker_card.eliminated or (moving_character and not attacker_card.revealed):
            if moving_character:
                return self._fail_pending_move("ability_owner_unavailable")
            state.pending_elimination = None
            return ActionResult(False, "The ability's owner card can no longer resolve its ability.")
        if moving_character:
            owner_row = next((
                row for row in state.rows
                if any(card is not None and card.id == attacker_card.id for card in row.slots)
            ), None)
            if (owner_row is None or owner_row.scored
                    or owner_row.id != str(pending.get("row_id", ""))):
                return self._fail_pending_move("ability_owner_moved")

        targeting = EliminationTargeting(
            target_scope=str(pending["targeting"].get("target_scope", "any_row")),
            target_side=str(pending["targeting"].get("target_side", "opposing")),
            allowed_types=tuple(pending["targeting"].get("allowed_types") or ()),
            excluded_types=tuple(pending["targeting"].get("excluded_types") or ()),
            max_base_points=pending["targeting"].get("max_base_points"),
            min_base_points=pending["targeting"].get("min_base_points"),
            require_bonus_points=bool(pending["targeting"].get("require_bonus_points", False)),
            require_eliminated=bool(pending["targeting"].get("require_eliminated", False)),
            ability_elimination=str(pending.get("effect_type", "")) == "eliminate_target",
            opposing_ability=bool(pending["targeting"].get("opposing_ability", False)),
            character_set=pending["targeting"].get("character_set"),
            require_base_point_reduction=bool(
                pending["targeting"].get("require_base_point_reduction", False)
            ),
        )
        valid_targets = self.elimination.get_valid_targets(state, attacker_card, targeting)
        valid_keys = {(r.id, i) for r, i, _ in valid_targets}
        if (target_row_id, target_slot_index) not in valid_keys:
            if moving_character:
                return self._refresh_move_target_selection(
                    pending, valid_targets,
                    "That character is no longer a valid target. Choose another highlighted character.",
                )
            return ActionResult(False, "That card is no longer a valid ability target.")

        event_start = len(state.events)
        effect_type = str(pending.get("effect_type", "eliminate_target"))
        followup_event_start: int | None = None
        revived_unrevealed_card = False
        target_definition = self.cards.get(target_card.definition_id)
        target_name = str(getattr(target_definition, "name", "Unknown character"))
        copied_bonus = 0
        if effect_type == "move_character":
            destinations = self._move_destination_targets(row.id)
            if not destinations:
                return self._fail_pending_move("no_legal_destination")
            pending["stage"] = "destination_row"
            pending["selected_target_card_id"] = target_card.id
            pending["selected_target_row_id"] = row.id
            pending["selected_target_slot_index"] = target_slot_index
            pending["valid_targets"] = destinations
            return ActionResult(True, events=())
        if effect_type == "revive_target":
            if not target_card.eliminated or row.slots[target_slot_index] is not target_card:
                return ActionResult(False, "That character is no longer eligible for revival.")
            revived_unrevealed_card = not target_card.revealed
            gon_reset = self.abilities.reset_gon_for_revival(state, target_card)
            row.eliminated_slots = [
                index for index in (row.get("eliminated_slots") or [])
                if index != target_slot_index
            ]
            target_card.eliminated = False
            if gon_reset:
                # reset_gon_for_revival already restored Gon's full original
                # state (base points, bonuses, activations) and re-registers
                # his pending ability for one revival retry.
                pass
            else:
                # Revival preserves match-scoped base-point changes. Existing
                # bonus cleanup remains the normal revival rule.
                if self.cards.get(target_card.definition_id) is not None:
                    target_card.bonus_point_reductions = {}
                    target_card.applied_bonuses = {}
                    target_card.suppressed_bonuses = []
                    target_card.activated_abilities = []
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
                    "name": target_name,
                    "target_name": target_name,
                    "row_id": target_row_id,
                    "slot_index": target_slot_index,
                    "current_points": int(target_card.current_points),
                    "base_points": self.abilities._base_points(target_card),
                    "resolved_abilities": list(target_card.get("resolved_abilities") or []),
                    "image": str(target_definition.image) if target_definition else "",
                    "type": str(target_definition.type) if target_definition else "",
                    "effect_name": str(target_definition.effect_name) if target_definition else "",
                    "effect_text": str(target_definition.effect_text) if target_definition else "",
                    "full_card_art": str(target_definition.full_card_art) if target_definition else "",
                },
            )
            # Gon gets one explicit retry attempt as part of his revival event.
            # The manager limits this pass to the revived owner; ordinary card
            # reveals and point recalculations cannot trigger his pending ability.
            # Only Gon's revival retries his pending on_reveal ability. For all
            # other revivals the card's state has already been reset and its
            # abilities must not re-activate; doing so would flip unrelated
            # cards and advance the reveal sequence unexpectedly.
            if gon_reset and target_card.revealed:
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
        elif effect_type == "heal_target_base_points":
            previous_base = self.abilities._base_points(target_card)
            previous_points = int(target_card.current_points)
            restored = self.abilities.restore_base_point_reductions(target_card)
            if restored <= 0:
                return ActionResult(False, "That character no longer has reduced base points.")
            self.abilities.recalculate_continuous_bonuses(state)
            self._emit(
                "base_points_restored",
                f"{target_name}'s base points were restored by {pending.get('attacker_name', 'Unknown character')}.",
                {
                    "attacker_card_id": attacker_card.id,
                    "attacker_name": str(pending.get("attacker_name", "Unknown character")),
                    "ability_id": str(pending.get("ability_id", "")),
                    "target_card_id": target_card.id,
                    "target_name": target_name,
                    "row_id": target_row_id,
                    "slot_index": target_slot_index,
                    "restored_amount": restored,
                    "previous_base_points": previous_base,
                    "base_points": self.abilities._base_points(target_card),
                    "previous_points": previous_points,
                    "current_points": int(target_card.current_points),
                },
            )
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
        elif effect_type == "copy_target_bonus":
            base_points = self.abilities._base_points(target_card)
            copied_bonus = int(target_card.current_points) - base_points
            if copied_bonus <= 0:
                return ActionResult(False, "That card no longer has bonus points to copy.")
            owner_bonuses = dict(attacker_card.get("applied_bonuses") or {})
            owner_bonuses[str(pending.get("ability_id", ""))] = copied_bonus
            attacker_card.applied_bonuses = owner_bonuses
            attacker_card.current_points = self.abilities._calculated_points(attacker_card, owner_bonuses)
            self._emit(
                "bonus_points_copied",
                f"{pending.get('attacker_name', 'Unknown character')} copied {copied_bonus} bonus points from {target_name}.",
                {
                    "attacker_card_id": attacker_card.id,
                    "attacker_name": str(pending.get("attacker_name", "Unknown character")),
                    "target_card_id": target_card.id,
                    "target_name": target_name,
                    "row_id": target_row_id,
                    "slot_index": target_slot_index,
                    "amount": copied_bonus,
                    "current_points": int(attacker_card.current_points),
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
            if not self.elimination.eliminate_target(
                state, target_card, attacker_card, by_ability=True,
            ):
                return ActionResult(False, "That character cannot be eliminated by this ability.")
            self._award_trunks_saiyan_resolve(row, target_card)
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

        if not pending.get("suppress_ability_resolved", False):
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
                floating_text=(
                    f"+{copied_bonus}"
                    if effect_type == "copy_target_bonus"
                    else str(pending.get("floating_text", ""))
                ),
            )
        self._eliminate_zero_base_point_cards(
            str(pending.get("ability_id", "")),
        )
        state.pending_elimination = None
        if followup_event_start is not None:
            self._begin_pending_elimination(followup_event_start)
        if revived_unrevealed_card and not target_card.eliminated:
            # Zeno can erase a newly placed face-down card before its normal
            # reveal. Once Whis restores it, reveal it immediately through the
            # ordinary reveal pipeline instead of leaving it for round-end.
            revival_reveal_start = len(state.events)
            self._reveal_card(row, target_slot_index, target_card)
            if state.phase == GamePhase.REVEAL and state.final_reveal_active:
                state.reveal_card_index += 1
            self._begin_pending_elimination(revival_reveal_start)
        # Resume queued row checks after the chosen effect resolves, then use
        # the same completion test as every other reveal continuation.
        self._resume_reveal_after_target_selection()
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def select_destination_row(self, row_id: str) -> ActionResult:
        """Resolve the destination stage of a character-movement ability."""
        return self.select_elimination_target(row_id, -1)

    def _move_destination_targets(self, source_row_id: str) -> list[dict[str, str]]:
        """Return unscored rows other than the source with an open slot."""
        return [
            {"row_id": row.id, "target_kind": "row"}
            for row in self.state.rows
            if row.id != source_row_id
            and not row.scored
            and self._next_placement_slot(row) is not None
        ]

    def _select_move_destination(
        self, destination_row_id: str, target_slot_index: int,
    ) -> ActionResult:
        """Validate and complete Law's second, destination-row choice."""
        state = self.state
        pending = state.get("pending_elimination") or {}
        if target_slot_index != -1:
            return ActionResult(False, "Choose a highlighted destination row.")

        event_start = len(state.events)
        attacker_card_id = str(pending.get("attacker_card_id", ""))
        attacker_card = next((
            card for row in state.rows for card in row.slots
            if card is not None and card.id == attacker_card_id
        ), None)
        attacker_row = next((
            row for row in state.rows
            if any(card is not None and card.id == attacker_card_id for card in row.slots)
        ), None)
        if (attacker_card is None or attacker_row is None
                or attacker_card.eliminated or not attacker_card.revealed
                or attacker_row.scored
                or attacker_row.id != str(pending.get("row_id", ""))):
            return self._fail_pending_move("ability_owner_unavailable")

        targeting_data = pending.get("targeting") or {}
        targeting = EliminationTargeting(
            target_scope=str(targeting_data.get("target_scope", "same_row")),
            target_side=str(targeting_data.get("target_side", "opposing")),
            allowed_types=tuple(targeting_data.get("allowed_types") or ()),
            excluded_types=tuple(targeting_data.get("excluded_types") or ()),
            max_base_points=targeting_data.get("max_base_points"),
            min_base_points=targeting_data.get("min_base_points"),
            require_bonus_points=bool(targeting_data.get("require_bonus_points", False)),
            require_eliminated=bool(targeting_data.get("require_eliminated", False)),
        )
        valid_targets = self.elimination.get_valid_targets(state, attacker_card, targeting)
        selected_card_id = str(pending.get("selected_target_card_id", ""))
        source_row_id = str(pending.get("selected_target_row_id", ""))
        source_row = next((row for row in state.rows if row.id == source_row_id), None)
        selected_location = next((
            (row, index, card)
            for row in state.rows
            for index, card in enumerate(row.slots)
            if card is not None and card.id == selected_card_id
        ), None)
        if selected_location is None or source_row is None:
            return self._refresh_move_target_selection(
                pending, valid_targets,
                "That character is no longer available. Choose another highlighted character.",
            )
        target_row, target_index, target_card = selected_location
        if (target_row.id != source_row.id or target_row.id != attacker_row.id
                or source_row.scored
                or not any(row.id == target_row.id and index == target_index and card.id == target_card.id
                           for row, index, card in valid_targets)):
            return self._refresh_move_target_selection(
                pending, valid_targets,
                "That character is no longer a valid target. Choose another highlighted character.",
            )

        destinations = self._move_destination_targets(source_row.id)
        destination = next((
            row for row in state.rows
            if row.id == destination_row_id
        ), None)
        if destination is None or destination_row_id not in {
            item["row_id"] for item in destinations
        }:
            if not destinations:
                return self._fail_pending_move("no_legal_destination")
            pending["valid_targets"] = destinations
            self._emit(
                "target_selection_updated",
                "That row is no longer a valid destination. Choose a highlighted row.",
                {"ability_id": str(pending.get("ability_id", "")), "stage": "destination_row"},
            )
            return ActionResult(True, events=tuple(state.events[event_start:]))

        destination_index = self._next_placement_slot(destination)
        if destination_index is None:
            return self._refresh_move_target_selection(
                pending, valid_targets,
                "That row no longer has a legal open slot. Choose another highlighted row.",
            )
        destination_was_empty = all(card is None for card in destination.slots)
        destination.slots[destination_index] = target_card
        destination.movement_blocked_slots = [
            index for index in destination.get("movement_blocked_slots", [])
            if index != destination_index
        ]
        source_row.slots[target_index] = None
        # Mark the vacated slot as consumed for row counting, including the
        # final slot. The normal placement selector still permits that final
        # slot as a refill, matching its existing special-case rule.
        source_row.movement_blocked_slots = sorted({
            *source_row.get("movement_blocked_slots", []), target_index,
        })
        source_row.eliminated_slots = [
            index for index in (source_row.get("eliminated_slots") or [])
            if index != target_index
        ]
        target_definition = self.cards.get(target_card.definition_id)
        target_name = str(getattr(target_definition, "name", "Unknown character"))
        source_row_id = source_row.id
        destination_row_id = destination.id

        # Keep Law's selected effect available to its resolver before row-entry
        # effects can create their own pending selections.
        resolved_ability = next((
            ability for ability in (
                getattr(self.cards.get(attacker_card.definition_id), "abilities", None) or []
            ) if ability.id == str(pending.get("ability_id", ""))
        ), None)
        if resolved_ability is not None:
            self.abilities.apply_deferred_effects(resolved_ability, attacker_card, state)
        self._emit(
            "character_moved",
            f"{target_name} moved from Row {source_row.number} to Row {destination.number}.",
            {
                "card_id": target_card.id,
                "card_name": target_name,
                "owner_id": target_card.owner_id,
                "source_row_id": source_row_id,
                "source_row_number": source_row.number,
                "source_slot_index": target_index,
                "destination_row_id": destination_row_id,
                "destination_row_number": destination.number,
                "destination_slot_index": destination_index,
            },
        )
        # Relocation is a placement into the destination row for event and
        # reveal purposes. Keep the distinct character_moved event above for
        # movement presentation and include the origin so clients can retain
        # the Law-specific movement animation.
        self._emit(
            "placement",
            f"{target_name} is placed in Row {destination.number} by Law's ability.",
            {
                "player_id": target_card.owner_id,
                "row_id": destination.id,
                "slot_index": destination_index,
                "source": "law_movement",
                "card_id": target_card.id,
            },
        )
        self._emit_ability_resolved(
            ability_id=str(pending.get("ability_id", "")),
            ability_name=str(pending.get("ability_name", "")),
            owner_card_id=attacker_card.id,
            definition_id=str(pending.get("definition_id", attacker_card.definition_id)),
            row_id=str(pending.get("row_id", "")),
            slot_index=int(pending.get("slot_index", 0)),
            sound=pending.get("sound"),
            target_card_id=target_card.id,
            target_name=target_name,
            floating_text=str(pending.get("floating_text", "")),
        )
        self._eliminate_zero_base_point_cards(
            str(pending.get("ability_id", "")),
        )
        state.pending_elimination = None
        # The destination uses the exact same row-entry pipeline as a normal
        # play. Its preceding-card reveal is processed first; source-row checks
        # wait if a revealed ability needs target selection.
        state.pending_movement_reveals = [
            {"row_id": source_row.id, "placed_slot_index": target_index},
        ]
        self._process_row_placement(
            target_card, destination, destination_index,
            row_was_empty=destination_was_empty,
        )
        self._begin_pending_elimination(event_start)
        self._resume_reveal_after_target_selection()
        player_ids = tuple(player.id for player in state.players)
        source_row.scores = calculate_row_scores(source_row, player_ids, self.cards)
        destination.scores = calculate_row_scores(destination, player_ids, self.cards)
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def _refresh_move_target_selection(
        self, pending: dict, valid_targets, message: str,
    ) -> ActionResult:
        """Return to Law's first choice after stale target state is detected."""
        if not valid_targets:
            return self._fail_pending_move("no_valid_targets")
        if not self._move_destination_targets(str(pending.get("row_id", ""))):
            return self._fail_pending_move("no_legal_destination")
        event_start = len(self.state.events)
        pending["stage"] = "target_character"
        for key in ("selected_target_card_id", "selected_target_row_id", "selected_target_slot_index"):
            pending.pop(key, None)
        pending["valid_targets"] = [
            {"row_id": row.id, "slot_index": index, "card_id": card.id}
            for row, index, card in valid_targets
        ]
        self._emit(
            "target_selection_updated",
            message,
            {"ability_id": str(pending.get("ability_id", "")), "stage": "target_character"},
        )
        return ActionResult(True, events=tuple(self.state.events[event_start:]))

    def _fail_pending_move(self, reason: str) -> ActionResult:
        """Clear an interrupted movement choice and report the failed ability."""
        state = self.state
        pending = state.get("pending_elimination") or {}
        event_start = len(state.events)
        self._emit_ability_failed(
            ability_id=str(pending.get("ability_id", "")),
            ability_name=str(pending.get("ability_name", "")),
            owner_card_id=str(pending.get("attacker_card_id", "")),
            owner_name=str(pending.get("attacker_name", "Unknown character")),
            row_id=str(pending.get("row_id", "")),
            slot_index=int(pending.get("slot_index", 0)),
            reason=reason,
        )
        state.pending_elimination = None
        self._resume_reveal_after_target_selection()
        return ActionResult(True, events=tuple(state.events[event_start:]))

    def _resume_reveal_after_target_selection(self) -> None:
        """Continue reveal scoring once a staged target selection has ended."""
        state = self.state
        self._drain_pending_auto_revivals()
        # A row-movement check can reveal a card with a staged ability choice.
        # Keep source/destination checks ordered and resume the next one only
        # after that choice is resolved, avoiding overwriting pending choices.
        queued_checks = state.get("pending_movement_reveals") or []
        while queued_checks and not state.pending_elimination and not state.pending_crazy_slots:
            check = queued_checks.pop(0)
            row = next((item for item in state.rows if item.id == check.get("row_id")), None)
            if row is not None:
                event_start = len(state.events)
                self._reveal_preceding_card(row, int(check.get("placed_slot_index", -1)))
                self._begin_pending_elimination(event_start)
        state.pending_movement_reveals = queued_checks
        if (state.phase == GamePhase.REVEAL and not state.pending_elimination
                and not state.pending_crazy_slots and all(
            _card_resolved_for_reveal(placed)
            for row in state.rows for placed in row.slots
        )):
            state.final_reveal_active = False
            state.phase = GamePhase.ROW_SCORING
            self._emit(
                "final_reveal_complete",
                "All cards are revealed. Compare scores starting with Row 1.",
                {},
            )

    def _drain_pending_auto_revivals(self) -> None:
        """Resolve Sakura's queued automatic targets through normal revival."""
        state = self.state
        batches = state.get("pending_auto_revivals") or []
        if (not batches or self._draining_auto_revivals or state.pending_elimination
                or state.pending_crazy_slots):
            return
        self._draining_auto_revivals = True
        try:
            while batches and not state.pending_elimination and not state.pending_crazy_slots:
                batch = batches[0]
                if batch["targets"]:
                    target = batch["targets"].pop(0)
                    pending = dict(batch["pending"])
                    pending["selection_ready"] = True
                    state.pending_elimination = pending
                    result = self.select_elimination_target(
                        str(target["row_id"]), int(target["slot_index"]),
                    )
                    if not result.accepted and state.pending_elimination is not None:
                        state.pending_elimination = None
                    continue
                pending = batch["pending"]
                self._emit_ability_resolved(
                    ability_id=str(pending.get("ability_id", "")),
                    ability_name=str(pending.get("ability_name", "")),
                    owner_card_id=str(pending.get("attacker_card_id", "")),
                    definition_id=str(pending.get("definition_id", "")),
                    row_id=str(pending.get("row_id", "")),
                    slot_index=int(pending.get("slot_index", 0)),
                    sound=pending.get("sound"),
                    floating_text=str(pending.get("floating_text", "")),
                )
                batches.pop(0)
            state.pending_auto_revivals = batches
        finally:
            self._draining_auto_revivals = False

    def _eliminate_if_base_is_zero(
        self, card: CardInstance, ability_id: str,
    ) -> bool:
        """Apply universal state-based elimination when base points hit zero."""
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
        if not self.elimination.eliminate_zero_base_point_card(self.state, card):
            return False
        self._award_trunks_saiyan_resolve(row, card)
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

    def _eliminate_zero_base_point_cards(self, ability_id: str) -> None:
        """Sweep every live board card after a base-point mutation sequence."""
        for row in self.state.rows:
            for card in row.slots:
                if (card is not None and not card.eliminated
                        and self.abilities._base_points(card) <= 0):
                    self._eliminate_if_base_is_zero(
                        card, ability_id,
                    )

    def _award_trunks_saiyan_resolve(self, row, eliminated_card: CardInstance) -> None:
        """Grant +5 per same-row friendly elimination to each active Trunks."""
        for trunks in row.slots:
            if (trunks is None or trunks.id == eliminated_card.id
                    or trunks.definition_id != "dragonball_06"
                    or trunks.owner_id != eliminated_card.owner_id
                    or trunks.eliminated or not trunks.revealed
                    or trunks.get("ability_suppressed", False)
                    or row.get("abilities_disabled", False)):
                continue
            ability = next((
                item for item in (self.cards[trunks.definition_id].abilities or [])
                if item.id == "dragonball_06_saiyan_resolve"
            ), None)
            if ability is None:
                continue
            key = f"{ability.id}:{trunks.id}:{eliminated_card.id}"
            bonuses = dict(trunks.get("applied_bonuses") or {})
            if key in bonuses:
                continue
            bonuses[key] = ability.effects[0].amount
            trunks.applied_bonuses = bonuses
            trunks.current_points = self.abilities._calculated_points(trunks, bonuses)
            self._emit(
                "bonus_points_gained",
                f"Future Trunks gains +{ability.effects[0].amount} from an ally's elimination.",
                {"row_id": row.id, "slot_index": row.slots.index(trunks),
                 "target_card_id": trunks.id, "ability_id": ability.id,
                 "resolution_id": f"saiyan_resolve:{trunks.id}:{eliminated_card.id}",
                 "floating_text": f"+{ability.effects[0].amount}",
                 "current_points": int(trunks.current_points)},
            )

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
        if state.get("pending_crazy_slots"):
            return ActionResult(False, "Finish Crazy Slots before scoring the row.")
        if state.get("pending_auto_revivals"):
            return ActionResult(False, "Wait for automatic revival abilities to finish.")
        if state.get("pending_sogeking_cinematic"):
            return ActionResult(False, "Wait for Sogeking's cutscene to finish before scoring.")
        if not all(
            row.is_full and all(_card_resolved_for_reveal(card) for card in row.slots)
            for row in state.rows
        ):
            return ActionResult(False, "The final reveal sequence must finish before scoring.")
        row = state.current_row
        if row is None or row.scored:
            return ActionResult(False, "This row has already been scored.")
        scores = calculate_row_scores(row, tuple(player.id for player in state.players), self.cards)
        synergy = self._usopp_sogeking_for_scoring(row, scores)
        if synergy is not None:
            event_start = len(state.events)
            cinematic_id = f"{state.round_number}:{row.id}:{synergy['usopp'].id}"
            state.pending_sogeking_cinematic = {
                "cinematic_id": cinematic_id,
                "row_id": row.id,
                "row_number": row.number,
                "team_player_id": synergy["player_id"],
                "usopp_card_id": synergy["usopp"].id,
                "bonus_points": synergy["bonus"],
            }
            self._emit(
                "sogeking_cinematic_started",
                "Usopp prepares the Sogeking flag shot!",
                dict(state.pending_sogeking_cinematic),
            )
            return ActionResult(True, events=tuple(state.events[event_start:]))
        winner_id = determine_row_winner(row, scores, state.players, self.cards)
        if winner_id is None:
            row.winner_id = None
            row.awarded_points = 0
            result_text = f"Row {row.number} is tied. No points are awarded."
        else:
            winner = state.player(winner_id)
            row.winner_id = winner_id
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

    def _usopp_sogeking_for_scoring(self, row: BattleRow, scores: dict[str, int]):
        """Return Usopp only when the row is losing and +6 wins outright."""
        if row.get("sogeking_bonus_points"):
            return None
        usopp = next((
            card for card in row.slots
            if card is not None and card.definition_id == "onepiece_08"
            and card.revealed and not getattr(card, "eliminated", False)
        ), None)
        if usopp is None:
            return None
        if any(
            card is not None and card.definition_id == "mha_04"
            for card in row.slots
        ):
            return None
        definition = self.cards.get(usopp.definition_id)
        ability = next((
            item for item in (getattr(definition, "abilities", None) or [])
            if item.id == SOGEKING_ABILITY_ID
        ), None)
        effect = next((
            item for item in (ability.effects if ability is not None else ())
            if item.type == "usopp_sogeking_flag_shot"
        ), None)
        if effect is None:
            return None
        bonus = effect.amount
        players = self.state.players
        opposing_player = next((
            player for player in players if player.id != usopp.owner_id
        ), None)
        if opposing_player is None:
            return None
        own_score = int(scores.get(usopp.owner_id, 0))
        opposing_score = int(scores.get(opposing_player.id, 0))
        # A tie is never an eligible activation. +6 must put Usopp's team
        # strictly ahead by raw score, even if another rule decides row wins.
        if own_score >= opposing_score or own_score + bonus <= opposing_score:
            return None
        winner_before = determine_row_winner(row, scores, players, self.cards)
        if winner_before == usopp.owner_id:
            return None
        scores_after_bonus = dict(scores)
        scores_after_bonus[usopp.owner_id] = scores_after_bonus.get(usopp.owner_id, 0) + bonus
        winner_after = determine_row_winner(row, scores_after_bonus, players, self.cards)
        if winner_after != usopp.owner_id:
            return None
        return {
            "player_id": usopp.owner_id,
            "usopp": usopp,
            "bonus": bonus,
        }

    def complete_sogeking_cinematic(self, cinematic_id: str) -> ActionResult:
        """Apply Usopp's row-only +6 after all synchronized presentation finishes."""
        state = self.state
        pending = state.get("pending_sogeking_cinematic") or {}
        if not pending or str(pending.get("cinematic_id", "")) != str(cinematic_id):
            return ActionResult(False, "There is no matching Sogeking cutscene to complete.")
        row = next((row for row in state.rows if row.id == str(pending.get("row_id", ""))), None)
        if row is None or row.scored or row is not state.current_row:
            state.pending_sogeking_cinematic = None
            return ActionResult(False, "The Sogeking row is no longer available to score.")
        event_start = len(state.events)
        bonus_points = int(pending.get("bonus_points", 0))
        team_player_id = str(pending.get("team_player_id", ""))
        bonuses = dict(row.get("sogeking_bonus_points") or {})
        if team_player_id in bonuses or bonus_points != 6:
            state.pending_sogeking_cinematic = None
            return ActionResult(False, "Sogeking's row bonus was already applied or is invalid.")
        usopp = next((
            card for card in row.slots
            if card is not None and card.id == str(pending.get("usopp_card_id", ""))
            and card.definition_id == "onepiece_08"
            and card.owner_id == team_player_id
            and not getattr(card, "eliminated", False)
        ), None)
        if usopp is None:
            state.pending_sogeking_cinematic = None
            return ActionResult(False, "Usopp is no longer eligible to complete his ability.")
        bonuses[team_player_id] = bonus_points
        row.sogeking_bonus_points = bonuses
        for field_name in ("activated_abilities", "resolved_abilities"):
            ability_ids = list(usopp.get(field_name) or [])
            if SOGEKING_ABILITY_ID not in ability_ids:
                ability_ids.append(SOGEKING_ABILITY_ID)
            setattr(usopp, field_name, ability_ids)
        state.pending_sogeking_cinematic = None
        self._emit(
            "sogeking_cinematic_completed",
            "Sogeking shoots down the flag! +6 row score.",
            {
                **dict(pending),
                "usopp_card_id": str(pending.get("usopp_card_id", "")),
            },
        )
        result = self.score_current_row()
        if not result.accepted:
            row.sogeking_bonus_points = {}
            return result
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
        pending_crazy_view = dict(self.state.get("pending_crazy_slots") or {}) or None
        if (pending_crazy_view is not None
                and pending_crazy_view.get("status") == "spinning"):
            started_at = float(pending_crazy_view.get("spin_started_at", 0.0) or 0.0)
            pending_crazy_view["spin_elapsed"] = max(0.0, time.time() - started_at)
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
                        "ability_suppressed": bool(card.get("ability_suppressed", False)),
                        "ability_suppression_blocks_bonus_points": bool(card.get("ability_suppression_blocks_bonus_points", True)),
                        "ability_suppression_style": str(card.get("ability_suppression_style", "black")),
                        "immune_to_elimination": bool(card.get("immune_to_elimination", False)),
                        "card": self._card_view(card) if card.revealed else None,
                    }
                    for card in row.slots
                ],
                "scores": dict(row.scores) if row.scored else None,
                "winner_id": row.winner_id if row.scored else None,
                "scored": row.scored,
                "awarded_points": row.awarded_points,
                "sogeking_bonus_points": dict(row.get("sogeking_bonus_points") or {}),
                "eliminated_slots": list(row.get("eliminated_slots") or []),
                "movement_blocked_slots": [
                    index for index in row.get("movement_blocked_slots", [])
                    if 0 <= index < len(row.slots) and row.slots[index] is None
                ],
                "abilities_disabled": bool(row.get("abilities_disabled", False)),
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
            "pending_crazy_slots": pending_crazy_view,
            "pending_sogeking_cinematic": (
                dict(self.state.get("pending_sogeking_cinematic") or {}) or None
            ),
            "crazy_slots_results": list(self.state.get("crazy_slots_results") or []),
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
            "ability_suppressed": bool(card.get("ability_suppressed", False)),
            "ability_suppression_blocks_bonus_points": bool(card.get("ability_suppression_blocks_bonus_points", True)),
            "ability_suppression_style": str(card.get("ability_suppression_style", "black")),
            "immune_to_elimination": bool(card.get("immune_to_elimination", False)),
            "applied_bonuses": dict(card.get("applied_bonuses") or {}),
            "suppressed_bonuses": list(card.get("suppressed_bonuses") or []),
            "base_point_reductions": dict(card.get("base_point_reductions") or {}),
            "bonus_point_reductions": dict(card.get("bonus_point_reductions") or {}),
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

    def _reveal_preceding_card(self, row: BattleRow, placed_slot_index: int) -> None:
        """Apply the normal placement reveal check for a slot in this row."""
        if placed_slot_index <= 0 or placed_slot_index >= row.capacity:
            return
        previous = row.slots[placed_slot_index - 1]
        if previous is not None and not getattr(previous, "eliminated", False):
            self._reveal_card(
                row, placed_slot_index - 1, previous, revealed_by=row.slots[placed_slot_index],
            )

    def _reveal_card(
        self, row: BattleRow, slot_index: int, card: CardInstance,
        *, revealed_by: CardInstance | None = None,
    ) -> None:
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
        self._resolve_reveal_triggers(card, row, slot_index, revealed_by=revealed_by)

    def _resolve_reveal_triggers(
        self, card: CardInstance, row: BattleRow, slot_index: int,
        *, revealed_by: CardInstance | None = None,
    ) -> None:
        """Run the central ability manager: Phase A pending, Phase B this card."""
        was_resolving = self.abilities.is_resolving
        self.abilities.on_card_revealed(
            self.state, card, row, slot_index,
            emit=self._emit_ability_activation,
            emit_failure=self._handle_ability_failure,
            revealed_by=revealed_by,
        )
        if not was_resolving:
            self._drain_pending_auto_revivals()

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
            definition = self.cards.get(owner_card.definition_id)
            ability = next(
                (
                    item
                    for item in (getattr(definition, "abilities", None) or [])
                    if item.id == ability_id
                ),
                None,
            )
            is_once_per_match = (
                ability is not None
                and ability.activation_limit == "once_per_card_per_match"
            )
            # A once-per-match reveal-only ability that failed must stay
            # consumed: it cannot retry when a target appears later. Gon's
            # explicit revival retry is preserved because his failure path
            # never reaches activation records.
            if not is_once_per_match:
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
        event_data = activation.event_data()
        floating_text = event_data["floating_text"]
        if activation.requires_crazy_slots_spin:
            slot_choice = next((
                effect for effect in activation.ability.effects
                if effect.type == "random_slot_choice"
            ), None)
            outcomes = []
            if slot_choice is not None:
                for outcome in slot_choice.outcomes:
                    nested = outcome.effect
                    outcomes.append({
                        "number": outcome.number,
                        "name": outcome.name,
                        "description": outcome.description,
                        "effects": [{
                            "type": nested.type,
                            "amount": nested.amount,
                            "status": nested.status,
                            "target_scope": outcome.target_scope,
                            "target_side": outcome.target_side,
                            "allowed_types": list(outcome.allowed_types),
                        }],
                    })
            self.state.pending_crazy_slots = {
                "ability_id": activation.ability.id,
                "ability_name": activation.ability.name,
                "owner_card_id": activation.owner_card_id,
                "owner_id": next((
                    card.owner_id for row in self.state.rows for card in row.slots
                    if card is not None and card.id == activation.owner_card_id
                ), ""),
                "row_id": activation.row_id,
                "slot_index": activation.slot_index,
                "status": "awaiting_spin",
                "result_number": None,
                "spin_id": None,
                "outcomes": outcomes,
            }
        self._emit(
            "ability_activated",
            (
                f"{activation.owner_name}'s {activation.ability.name} activates: "
                f"{floating_text} "
                f"({activation.current_points - activation.delta} -> "
                f"{activation.current_points} points)."
            ),
            event_data,
        )
        if any(effect.type == "disable_row_abilities" for effect in activation.ability.effects):
            sealed_row = next((row for row in self.state.rows if row.id == activation.row_id), None)
            if sealed_row is not None:
                for target_index, target in enumerate(sealed_row.slots):
                    if target is None or target.id == activation.owner_card_id:
                        continue
                    self._emit(
                        "ability_suppressed",
                        f"{target.id}'s ability is sealed by {activation.owner_name}.",
                        {
                            "ability_id": activation.ability.id,
                            "source_card_id": activation.owner_card_id,
                            "source_row_id": sealed_row.id,
                            "source_slot_index": activation.slot_index,
                            "target_card_id": target.id,
                            "target_row_id": sealed_row.id,
                            "target_slot_index": target_index,
                            "visual_style": "shanks_red",
                        },
                    )
        if any(
            effect.type == "eliminate_revealing_opponent"
            for effect in activation.ability.effects
        ):
            target_location = next((
                (row, index, target)
                for row in self.state.rows
                for index, target in enumerate(row.slots)
                if target is not None and target.id == activation.target_card_id
            ), None)
            owner_card = next((
                target for row in self.state.rows for target in row.slots
                if target is not None and target.id == activation.owner_card_id
            ), None)
            if target_location is not None and owner_card is not None:
                target_row, target_index, target_card = target_location
                if self.elimination.eliminate_target(
                    self.state, target_card, owner_card, by_ability=True,
                ):
                    target_name = str(getattr(
                        self.cards.get(target_card.definition_id), "name", "Unknown character",
                    ))
                    self._award_trunks_saiyan_resolve(target_row, target_card)
                    self.abilities.recalculate_after_elimination(self.state, target_card)
                    self._emit(
                        "elimination",
                        f"{target_name} has been eliminated by {activation.owner_name}.",
                        {
                            "attacker_card_id": activation.owner_card_id,
                            "attacker_name": activation.owner_name,
                            "target_card_id": target_card.id,
                            "target_name": target_name,
                            "row_id": target_row.id,
                            "slot_index": target_index,
                            "definition_id": target_card.definition_id,
                        },
                    )
                    self._emit_ability_resolved(
                        ability_id=activation.ability.id,
                        ability_name=activation.ability.name,
                        owner_card_id=activation.owner_card_id,
                        definition_id=activation.definition_id,
                        row_id=activation.row_id,
                        slot_index=activation.slot_index,
                        sound=activation.ability.presentation.sound,
                        target_card_id=target_card.id,
                        target_name=target_name,
                        floating_text=activation.ability.presentation.floating_text,
                    )
                    self._eliminate_zero_base_point_cards(activation.ability.id)
            return
        if any(
            effect.type == "revive_all_targets"
            for effect in activation.ability.effects
        ):
            self._start_automatic_revival(activation)
            return
        # Immediate point effects resolve now. Target-selection effects wait
        # until select_elimination_target validates and applies the chosen card.
        if not activation.requires_target_selection and not activation.requires_crazy_slots_spin:
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
            self._eliminate_zero_base_point_cards(activation.ability.id)

    def _start_automatic_revival(self, activation: AbilityActivation) -> None:
        """Queue every eligible card through the ordinary revival resolver."""
        owner = next((
            card for row in self.state.rows for card in row.slots
            if card is not None and card.id == activation.owner_card_id
        ), None)
        if owner is None or owner.eliminated:
            return
        ability = activation.ability
        targeting = EliminationTargeting(
            target_scope=ability.target_scope,
            target_side=ability.target_side,
            allowed_types=ability.allowed_types,
            excluded_types=ability.excluded_types,
            max_base_points=ability.max_base_points,
            min_base_points=ability.min_base_points,
            character_set=ability.target_character_set,
            require_eliminated=True,
        )
        targets = self.elimination.get_valid_targets(self.state, owner, targeting)
        batches = self.state.get("pending_auto_revivals") or []
        queued_ids = {
            str(target.get("card_id", ""))
            for batch in batches
            for target in batch.get("targets", [])
        }
        targets = [
            (row, index, card) for row, index, card in targets
            if card.id not in queued_ids
        ]
        if not targets:
            self._emit_ability_resolved(
                ability_id=ability.id,
                ability_name=ability.name,
                owner_card_id=owner.id,
                definition_id=activation.definition_id,
                row_id=activation.row_id,
                slot_index=activation.slot_index,
                sound=ability.presentation.sound,
                floating_text=ability.presentation.floating_text,
            )
            return
        pending = {
            "attacker_card_id": owner.id,
            "attacker_owner_id": owner.owner_id,
            "attacker_name": activation.owner_name,
            "ability_id": ability.id,
            "ability_name": ability.name,
            "effect_amount": 0,
            "floating_text": ability.presentation.floating_text,
            "effect_type": "revive_target",
            "sound": ability.presentation.sound,
            "definition_id": activation.definition_id,
            "row_id": activation.row_id,
            "slot_index": activation.slot_index,
            "suppress_ability_resolved": True,
            "targeting": {
                "target_scope": targeting.target_scope,
                "target_side": targeting.target_side,
                "allowed_types": list(targeting.allowed_types),
                "excluded_types": list(targeting.excluded_types),
                "max_base_points": targeting.max_base_points,
                "min_base_points": targeting.min_base_points,
                "require_bonus_points": False,
                "require_eliminated": True,
                "opposing_ability": False,
                "character_set": targeting.character_set,
                "require_base_point_reduction": False,
            },
            "valid_targets": [
                {"row_id": row.id, "slot_index": index, "card_id": card.id}
                for row, index, card in targets
            ],
        }
        batches = self.state.get("pending_auto_revivals")
        if not isinstance(batches, list):
            batches = []
        batches.append({
            "pending": pending,
            "targets": list(pending["valid_targets"]),
        })
        self.state.pending_auto_revivals = batches

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

