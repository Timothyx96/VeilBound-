from __future__ import annotations

import math
import random
import time
from dataclasses import replace

import pygame

from veilbound.client.assets import AssetLoader
from veilbound.client.ability_panel import AbilityInspectionPanel
from veilbound.client.layout import CARD_HEIGHT_RATIO, CARD_WIDTH_RATIO, BoardLayout, DEBUG_HOVER_SCALE, DEBUG_HOVER_NEIGHBOR_PUSH, DEBUG_HOVER_SECOND_PUSH
from veilbound.client.settings import AppSettings, animation_ms_for_preset, load_settings
from veilbound.client.video_player import VideoPlayer
from veilbound.client.theme import (
    BG, CREAM, DEBUG, GOLD, P1, P2, P2_CARD_OUTLINE, PANEL, PANEL_LIGHT,
    DUO_ENERGY, DUO_ENERGY_DIM,
    SLOT_BORDER_WIDTH, SLOT_CARD_MARGIN, SLOT_CORNER_ORNAMENTS_ENABLED,
    SLOT_EMPTY_BORDER, SLOT_EMPTY_FILL, SLOT_GLOW_PADDING, SLOT_GLOW_STEPS,
    SLOT_HOVER_BORDER, SLOT_HOVER_GLOW, SLOT_INVALID_BORDER, SLOT_INVALID_FILL,
    SLOT_OCCUPIED_BORDER, SLOT_OCCUPIED_FILL,
    SLOT_RADIUS, SLOT_SHADOW, SLOT_GOLD, SLOT_VALID_BORDER,
    SLOT_VALID_FILL, SLOT_VALID_GLOW, TEXT_MUTED_GOLD, TEXT_SHADOW, TEXT_WHITE,
)
from veilbound.client.typography import (
    FONTS,
    get_font,
    preload_fonts,
    render_text,
    render_text_with_spacing,
    wrap_text,
)
from veilbound.shared.abilities import resolve_character_reference
from veilbound.shared.models import BattleRow, CardDefinition, GameEvent, GamePhase, GameState
from veilbound.shared.scoring import calculate_row_scores
from veilbound.shared.rules import row_uses_reverse_fate


# Floating ability text (e.g. "+3") rises and fades above the target card.
FLOATING_TEXT_SECONDS = 1.4
FLOATING_TEXT_RISE_PX = 46

class Renderer:
    def __init__(self, assets: AssetLoader | None = None, settings: AppSettings | None = None):
        preload_fonts()
        self.assets = assets or AssetLoader()
        self.settings = settings or load_settings()
        self.animation_ms = self.settings.animation_ms
        self.background = self.assets.load_image(self.settings.background_path)
        self.card_back_art = {
            "blue": self.assets.load_image("assets/art/card_backs/Card_Back_Blue.png"),
            "red": self.assets.load_image("assets/art/card_backs/Card_Back_Red.png"),
        }
        self.card_front_art = self.assets.load_image(
            "assets/art/card front/Antieke Perkamentachtergrond met Gouden Hoekornamenten.png"
        )
        self.ability_panel = AbilityInspectionPanel(self.assets)
        self.card_frame_art = self._prepare_card_frame()
        self.options_background = self.assets.load_image("assets/backgrounds/Options Background.png")
        self.menu_background = self.assets.load_image("assets/backgrounds/Veilbound_S1.png")
        self._background_cache_size: tuple[int, int] | None = None
        self._background_cache: tuple[pygame.Surface, pygame.Surface] | None = None
        self._menu_bg_cache_size: tuple[int, int] | None = None
        self._menu_bg_cache: pygame.Surface | None = None
        self._menu_effect_layer: pygame.Surface | None = None
        self._menu_effect_layer_size: tuple[int, int] | None = None
        menu_rng = random.Random(7101)
        self._menu_ambient_particles = [
            {
                "x": menu_rng.random(), "y": menu_rng.random(),
                "speed": menu_rng.uniform(0.006, 0.023),
                "drift": menu_rng.uniform(0.002, 0.012),
                "phase": menu_rng.random() * math.tau,
                "size": menu_rng.choice((1, 1, 1, 2)),
                "energy": menu_rng.random() < 0.24,
            }
            for _ in range(36)
        ]
        self._menu_blossoms = [
            {
                "x": menu_rng.random(), "y": menu_rng.random(),
                "speed": menu_rng.uniform(0.012, 0.034),
                "drift": menu_rng.uniform(0.004, 0.018),
                "phase": menu_rng.random() * math.tau,
                "size": menu_rng.uniform(2.0, 4.2),
            }
            for _ in range(15)
        ]
        self._render_size: tuple[int, int] | None = None
        self._overlay_cache: dict[tuple[int, int, int, int, int, int], pygame.Surface] = {}
        self._row_backplates: dict[tuple[int, int], pygame.Surface] = {}
        self._row_panel_cache: dict[tuple[int, int], pygame.Surface | None] = {}
        self._slot_surface_cache: dict[tuple[str, tuple[int, int], bool], pygame.Surface] = {}
        self._card_frame_cache: dict[tuple[int, int], pygame.Surface] = {}
        self._card_front_cache: dict[tuple[int, int], pygame.Surface] = {}
        self._card_face_cache: dict[tuple[str, tuple[int, int], str], pygame.Surface] = {}
        self._card_back_cache: dict[tuple[str, tuple[int, int]], pygame.Surface] = {}
        self._spotlight_portrait_cache: dict[tuple[str, tuple[int, int]], pygame.Surface] = {}
        self.flip_started: dict[tuple[str, int], float] = {}
        self.notice = "Drag a card into a valid empty slot."
        self.notice_until = 0.0
        self.room_label: str | None = None
        self.turn_confirmation_player_id: str | None = None
        self.last_card_reveal = ""
        self.reveal_card: dict | None = None
        self.reveal_card_visible_after = 0.0
        self.reveal_card_until = 0.0
        self.score_animation_row_id: str | None = None
        self.score_animation_started = 0.0
        # Timed floating ability texts; presentation only, never mutates points.
        self.floating_texts: list[dict] = []
        self._endeavor_reveal_texts: dict[str, dict] = {}
        self.trunks_bonus_animations: list[dict] = []
        self.whis_resurrection_animations: list[dict] = []
        self.whis_healing_animations: list[dict] = []
        # Dedupe key -> start time, so a duplicated event cannot replay text.
        self.floating_texts_started: dict[tuple[str, str, str], float] = {}
        self._processed_ability_resolution_ids: set[str] = set()
        self.reverse_fate_animation: dict | None = None
        self.luffy_haki_animations: list[dict] = []
        self._processed_luffy_haki: set[str] = set()
        self._scene_surface: pygame.Surface | None = None
        self.sogeking_cinematic: dict | None = None
        self.sogeking_video: VideoPlayer | None = None
        self.sogeking_cinematic_complete = False
        # Dynamic Duo energy arc animation; one shot per genuine same-player
        # pair activation. Populated by note_events for an ability_activated
        # event and resolved against live state during draw so the arc
        # endpoints always derive from actual card screen positions.
        # ``duo_queue`` holds activations waiting their turn; ``duo_animation``
        # holds the one currently playing.
        # Shape: {"owner_key": (row_id, slot_index),
        #         "partner_key": (row_id, slot_index) | None,
        #         "started": float}
        self.duo_animation: dict | None = None
        self.duo_queue: list[dict] = []
        # Instance-scoped black-energy travel animations for Chrollo's
        # suppression effect. Persistent card marks come from synchronized state.
        self.ability_suppression_animations: list[dict] = []
        # Debug Mode: horizontally scrollable full-roster hand. The scroll
        # offset lives here so input, tooltips, and drawing share one value.
        self.debug_mode = False
        self.debug_scroll = 0.0
        # Elimination presentation state.
        self.elimination_targets: list[tuple[str, int]] = []  # (row_id, slot_index) highlighted
        self.elimination_announcement: str | None = None      # "NAME HAS BEEN ELIMINATED BY NAME"
        self.elimination_announced_at: float = 0.0
        self.elimination_flash_card: tuple[str, int] | None = None  # card being flipped + X'd
        self.elimination_flash_started: float | None = None
        self._elimination_flash_is_selection_target = False
        self.elimination_failed_message: str | None = None
        self.elimination_failed_at: float = 0.0
        self._processed_elimination_events: set[str] = set()
        self._selection_overlay_strength = 0.0
        self._selection_overlay_updated_at: float | None = None
        self._selection_overlay_effect_type: str | None = None
        self._selection_target_keys: set[tuple[str, int]] = set()
        self.selected_target_visual: dict | None = None
        self._selection_dim_surface: pygame.Surface | None = None
        self._selection_vignette_cache_size: tuple[int, int] | None = None
        self._selection_vignette_surface: pygame.Surface | None = None
        self._selection_aura_surfaces: dict[tuple[int, int], pygame.Surface] = {}
        self._selection_invalid_dim_surfaces: dict[tuple[int, int], pygame.Surface] = {}
        # Kite's overlay is native Pygame; only the authoritative pending result
        # drives its reel. The old wand/mouth presentation is intentionally gone.
        self.crazy_slots_animation: dict | None = None
        self.crazy_slots_spin_ready = False
        self.crazy_slots_overlay_ready = False
        self.crazy_slots_animation_complete = False
        self.crazy_slots_spin_rect: pygame.Rect | None = None
        self.crazy_slots_continue_rect: pygame.Rect | None = None
        self.crazy_slots_continue_pending = False
        self.crazy_slots_button_focused = True
        self.crazy_slots_current_digit = 1
        self.crazy_slots_current_cell = 0
        self.crazy_slots_current_speed = 0.0
        self._crazy_slots_overlay_started: float | None = None
        self._crazy_slots_background: pygame.Surface | None = None
        self._crazy_slots_background_size: tuple[int, int] | None = None
        self._crazy_slots_vignette: pygame.Surface | None = None

    def layout(self, state: GameState, size: tuple[int, int], debug_mode: bool | None = None) -> BoardLayout:
        active_debug = self.debug_mode if debug_mode is None else debug_mode
        return BoardLayout(size, state, debug_mode=active_debug)

    def reset_animations(self) -> None:
        self.flip_started.clear()
        self.last_card_reveal = ""
        self.floating_texts.clear()
        self._endeavor_reveal_texts.clear()
        self.trunks_bonus_animations.clear()
        self.whis_resurrection_animations.clear()
        self.whis_healing_animations.clear()
        self.floating_texts_started.clear()
        self._processed_ability_resolution_ids.clear()
        self.reverse_fate_animation = None
        self.luffy_haki_animations.clear()
        self._processed_luffy_haki.clear()
        self._clear_sogeking_cinematic()
        self.reveal_card = None
        self.reveal_card_visible_after = 0.0
        self.reveal_card_until = 0.0
        self.score_animation_row_id = None
        self.duo_animation = None
        self.duo_queue.clear()
        self.ability_suppression_animations.clear()
        self.elimination_targets = []
        self.elimination_announcement = None
        self.elimination_announced_at = 0.0
        self.elimination_flash_card = None
        self.elimination_flash_started = None
        self._elimination_flash_is_selection_target = False
        self.elimination_failed_message = None
        self.elimination_failed_at = 0.0
        self._processed_elimination_events.clear()
        self._selection_overlay_strength = 0.0
        self._selection_overlay_updated_at = None
        self._selection_overlay_effect_type = None
        self._selection_target_keys.clear()
        self.selected_target_visual = None
        self.crazy_slots_animation = None
        self.crazy_slots_spin_ready = False
        self.crazy_slots_overlay_ready = False
        self.crazy_slots_animation_complete = False
        self.crazy_slots_spin_rect = None
        self.crazy_slots_continue_rect = None
        self.crazy_slots_continue_pending = False
        self._crazy_slots_overlay_started = None
        self._crazy_slots_background = None
        self._crazy_slots_background_size = None
        self._crazy_slots_vignette = None

    @property
    def duo_duration(self) -> float:
        """Duration of the one-shot Dynamic Duo energy arc, in seconds."""
        return max(0, self.animation_ms.get("duo_energy_ms", 0)) / 1000

    @property
    def animation_preset(self) -> str:
        """Canonical name of the active animation preset (Slow/Normal/Fast)."""
        return self.settings.animation_preset

    def set_animation_preset(self, preset_name: str) -> None:
        """Switch the live animation timings (in-game speed control).

        Resolves the preset through the config-backed helper, so unknown names
        fall back to the config's selected preset and can never desync the
        game. Presentation only: swaps the timing dict the renderer and input
        controller already read every frame. No engine state is touched.
        """
        canonical, timings = animation_ms_for_preset(preset_name)
        self.settings = replace(
            self.settings,
            animation_preset=canonical,
            animation_ms=dict(timings),
        )
        self.animation_ms = self.settings.animation_ms

    @property
    def duo_animation_pending(self) -> bool:
        """True while a Dynamic Duo activation has not yet finished animating.

        The input controller uses this to gate the next character reveal until
        the arc has played out. Includes queued activations waiting their turn.
        """
        return self.duo_animation is not None or bool(self.duo_queue)

    @property
    def duo_animation_deadline(self) -> float:
        """Absolute time at which the pending Dynamic Duo animation finishes."""
        anim = self.duo_animation
        if anim is not None:
            return anim["started"] + self.duo_duration
        if self.duo_queue:
            return self.duo_queue[0]["started"] + self.duo_duration
        return 0.0

    def note_events(self, events: tuple[GameEvent, ...]) -> None:
        now = time.monotonic()
        for event in events:
            if event.kind == "sogeking_cinematic_started":
                self._begin_sogeking_cinematic(event.data, now)
            elif event.kind == "sogeking_cinematic_completed":
                self._clear_sogeking_cinematic()
            elif event.kind in {"elimination", "death"}:
                data = event.data
                eliminated_key = (
                    str(data.get("row_id", "")),
                    int(data.get("slot_index", -1)),
                )
                self.flip_started.pop(eliminated_key, None)
                self._elimination_flash_is_selection_target = bool(
                    event.kind == "elimination"
                    and self._selection_overlay_effect_type == "eliminate_target"
                    and eliminated_key in self._selection_target_keys
                )
                elimination_id = data.get("elimination_id")
                if elimination_id is not None:
                    elimination_id = str(elimination_id)
                    if elimination_id in self._processed_elimination_events:
                        continue
                    self._processed_elimination_events.add(elimination_id)
                if event.kind == "death":
                    self.elimination_announcement = f"{data.get('target_name', '?')} Died"
                else:
                    self.elimination_announcement = (
                        f"{str(data.get('target_name', '?')).upper()} "
                        f"HAS BEEN ELIMINATED BY "
                        f"{str(data.get('attacker_name', '?')).upper()}"
                    )
                self.elimination_announced_at = now
                if data.get("animate", True):
                    self.elimination_flash_card = (
                        str(data.get("row_id", "")),
                        int(data.get("slot_index", -1)),
                    )
                    self.elimination_flash_started = now
                else:
                    self.elimination_flash_card = None
                    self.elimination_flash_started = None
                self.elimination_targets = []
            elif event.kind == "elimination_failed":
                self.elimination_failed_message = "ELIMINATION ATTEMPT FAILED"
                self.elimination_failed_at = now
                self.elimination_targets = []
                self._selection_target_keys.clear()
                self.selected_target_visual = None
                self.elimination_announcement = None
                self.elimination_flash_card = None
                self.elimination_flash_started = None
                self._elimination_flash_is_selection_target = False
            elif event.kind == "ability_failed":
                self.elimination_failed_message = event.message
                self.elimination_failed_at = now
                self.elimination_targets = []
                self._selection_target_keys.clear()
                self.selected_target_visual = None
                self.elimination_announcement = None
                self.elimination_flash_card = None
                self.elimination_flash_started = None
                self._elimination_flash_is_selection_target = False
            elif event.kind == "card_revealed":
                key = (event.data["row_id"], event.data["slot_index"])
                if key in self.flip_started:
                    continue
                self.flip_started[key] = time.monotonic()
                endeavor_text = self._endeavor_reveal_texts.pop(
                    str(event.data.get("card_id", "")), None,
                )
                if endeavor_text is not None:
                    self._queue_ability_floating_text({
                        **endeavor_text,
                        "row_id": event.data["row_id"],
                        "slot_index": event.data["slot_index"],
                    }, now)
                self.last_card_reveal = event.message
                now = time.monotonic()
                self.reveal_card = dict(event.data)
                self.reveal_card_visible_after = now + self.animation_ms["card_flip_ms"] / 1000
                self.reveal_card_until = self.reveal_card_visible_after + self.animation_ms["card_information_ms"] / 1000
            elif event.kind == "ability_suppressed":
                visual_style = str(event.data.get("visual_style", "black"))
                # Shanks uses the live, orbiting seal marker on affected cards.
                # Do not draw a second red trail/impact over card faces.
                if visual_style != "shanks_red":
                    self.ability_suppression_animations.append({
                        "row_id": str(event.data.get("target_row_id", "")),
                        "source_slot_index": int(event.data.get("source_slot_index", -1)),
                        "target_slot_index": int(event.data.get("target_slot_index", -1)),
                        "visual_style": visual_style,
                        "started": now,
                    })
            elif event.kind == "elimination_blocked":
                self._queue_ability_floating_text({
                    "ability_id": str(event.data.get("ability_id", "blocked_elimination")),
                    "owner_card_id": str(event.data.get("target_card_id", "")),
                    "row_id": str(event.data.get("row_id", "")),
                    "slot_index": int(event.data.get("slot_index", -1)),
                    "floating_text": "BLOCKED",
                }, now)
            elif event.kind == "bonus_points_gained" and str(event.data.get("ability_id", "")) == "dragonball_06_saiyan_resolve":
                self._queue_ability_floating_text({
                    **event.data,
                    "owner_card_id": event.data.get("target_card_id", ""),
                }, now)
                self.trunks_bonus_animations.append({
                    "row_id": str(event.data.get("row_id", "")),
                    "slot_index": int(event.data.get("slot_index", -1)),
                    "started": now,
                })
            elif event.kind == "revival":
                # A revived card flips back over so it becomes visible on the
                # board. Restart the flip so the elimination look is replaced
                # by the face-up card after the authoritative state confirms.
                # The revival also clears the per-slot elimination flash so
                # the red-X overlay that the flash would draw is not painted
                # over the revived card.
                key = (event.data["row_id"], event.data["slot_index"])
                self.flip_started[key] = time.monotonic()
                self.last_card_reveal = event.message
                now = time.monotonic()
                self._remember_selected_target(event.data, "revive_target", now)
                self.reveal_card = dict(event.data)
                self.reveal_card_visible_after = now + self.animation_ms["card_flip_ms"] / 1000
                self.reveal_card_until = self.reveal_card_visible_after + self.animation_ms["card_information_ms"] / 1000
                if str(event.data.get("attacker_name", "")).casefold() == "whis":
                    self.whis_resurrection_animations.append({
                        "row_id": str(event.data["row_id"]),
                        "slot_index": int(event.data["slot_index"]),
                        "started": now,
                    })
                self.elimination_flash_card = None
                self.elimination_flash_started = None
                self._elimination_flash_is_selection_target = False
                self.elimination_announcement = None
            elif event.kind == "character_moved":
                # Reveal and elimination animations are keyed by slot, while a
                # move changes which instance occupies both affected slots.
                # Discard their old visual state so it cannot follow the slot.
                source_key = (
                    str(event.data.get("source_row_id", "")),
                    int(event.data.get("source_slot_index", -1)),
                )
                destination_key = (
                    str(event.data.get("destination_row_id", "")),
                    int(event.data.get("destination_slot_index", -1)),
                )
                self.flip_started.pop(source_key, None)
                self.flip_started.pop(destination_key, None)
                if self.elimination_flash_card in {source_key, destination_key}:
                    self.elimination_flash_card = None
                    self.elimination_flash_started = None
                    self._elimination_flash_is_selection_target = False
                selected = self.selected_target_visual
                if selected and selected.get("key") in {source_key, destination_key}:
                    self.selected_target_visual = None
            elif event.kind in {
                "bonus_points_stolen", "bonus_points_copied",
                "bonus_points_removed", "base_points_reduced",
            }:
                effect_type = {
                    "bonus_points_stolen": "steal_target_bonus",
                    "bonus_points_copied": "copy_target_bonus",
                    "bonus_points_removed": "remove_target_bonus",
                    "base_points_reduced": "reduce_target_base_points",
                }[event.kind]
                self._remember_selected_target(event.data, effect_type, now)
            elif event.kind == "base_points_restored":
                self._remember_selected_target(event.data, "heal_target_base_points", now)
            elif event.kind == "row_scored":
                self.score_animation_row_id = event.data["row_id"]
                self.score_animation_started = time.monotonic()
            elif event.kind == "ability_activated":
                if str(event.data.get("ability_id", "")) == "whis_celestial_restoration":
                    healed_card_ids = tuple(
                        str(card_id) for card_id in event.data.get("affected_card_ids", ())
                    )
                    if len(healed_card_ids) >= 2:
                        self.whis_healing_animations.append({
                            "owner_card_id": str(event.data.get("owner_card_id", "")),
                            "row_id": str(event.data.get("row_id", "")),
                            "slot_index": int(event.data.get("slot_index", -1)),
                            "target_card_ids": healed_card_ids,
                            "started": now,
                        })
                if str(event.data.get("ability_id", "")) == "luffy_warrior_of_liberation_eliminate":
                    owner_id = str(event.data.get("owner_card_id", ""))
                    haki_id = str(event.data.get("resolution_id") or owner_id)
                    if haki_id and haki_id not in self._processed_luffy_haki:
                        self._processed_luffy_haki.add(haki_id)
                        self.luffy_haki_animations.append(
                            self._new_luffy_haki_animation(event.data, now)
                        )
                if str(event.data.get("ability_id", "")) == "tobi_reverse_fate":
                    self.reverse_fate_animation = {
                        "row_id": str(event.data.get("row_id", "")),
                        "started": now,
                    }
                # Target-selection abilities only show success presentation
                # after their target has been validated and the effect applied.
                if event.data.get("requires_crazy_slots_spin", False):
                    self.crazy_slots_animation = None
                    self.crazy_slots_spin_ready = False
                    self.crazy_slots_overlay_ready = False
                    self.crazy_slots_animation_complete = False
                    self.crazy_slots_spin_rect = None
                    self.crazy_slots_continue_rect = None
                    self._crazy_slots_background = None
                    self._crazy_slots_background_size = None
                elif not event.data.get("requires_target_selection", False):
                    if str(event.data.get("ability_id", "")) == "mha_05_hellflame_first_in_empty_row":
                        # Endeavor's bonus is applied at placement, but keep its
                        # floating point presentation hidden until the card flips.
                        owner_id = str(event.data.get("owner_card_id", ""))
                        if owner_id:
                            self._endeavor_reveal_texts[owner_id] = dict(event.data)
                    else:
                        self._queue_ability_floating_text(event.data, now)
                    transformed_art = event.data.get("transformed_card_art")
                    if (isinstance(transformed_art, str) and self.reveal_card is not None
                            and self.reveal_card.get("row_id") == event.data.get("row_id")
                            and self.reveal_card.get("slot_index") == event.data.get("slot_index")):
                        self.reveal_card["full_card_art"] = transformed_art
                # Dynamic Duo energy arc: schedule a one-shot arc between the
                # activating card and its same-row partner. The partner is
                # resolved against live state during draw so the arc endpoints
                # always derive from actual card screen positions. An owner
                # activates at most once per match, so duplicate/replayed
                # events for the same owner are ignored.
                owner_key = (
                    str(event.data.get("row_id", "")),
                    int(event.data.get("slot_index", -1)),
                )
                owner_id = str(event.data.get("owner_card_id", ""))
                already_queued = any(
                    item["owner_key"] == owner_key for item in self.duo_queue
                ) or (
                    self.duo_animation is not None
                    and self.duo_animation.get("owner_key") == owner_key
                )
                if (owner_id and owner_key[1] >= 0 and not already_queued
                        and not event.data.get("requires_crazy_slots_spin", False)):
                    self.duo_queue.append(
                        {"owner_key": owner_key, "partner_key": None, "started": now}
                    )
            elif event.kind == "crazy_slots_spin_started":
                self.crazy_slots_animation = {
                    "spin_id": str(event.data.get("spin_id", "")),
                    "started": now,
                    "started_at": float(event.data.get("spin_elapsed", 0.0) or 0.0),
                }
                self.crazy_slots_spin_ready = False
                self.crazy_slots_animation_complete = False
                self._crazy_slots_overlay_started = None
            elif event.kind == "crazy_slots_no_target":
                self.notice = "No valid target"
                self.notice_until = now + 3.5
            elif event.kind == "crazy_slots_result":
                # The full-screen scene closes only after the rendered spin has
                # reported completion and the authoritative effect is applied.
                self.crazy_slots_animation = None
                self.crazy_slots_spin_ready = False
                self.crazy_slots_overlay_ready = False
                self.crazy_slots_animation_complete = False
                self.crazy_slots_spin_rect = None
                self.crazy_slots_continue_rect = None
                self.crazy_slots_continue_pending = False
                self._crazy_slots_background = None
                self._crazy_slots_background_size = None
                self._crazy_slots_overlay_started = None
            elif event.kind == "ability_resolved" and event.data.get("floating_text"):
                self._queue_ability_floating_text(event.data, now)
            self.notice = event.message
            self.notice_until = time.monotonic() + 3.5

    def _queue_ability_floating_text(self, data: dict, now: float) -> None:
        """Show ability text once, using resolution IDs for delayed success events."""
        ability_id = str(data.get("ability_id", ""))
        owner_card_id = str(data.get("owner_card_id", ""))
        resolution_id = data.get("resolution_id")
        if resolution_id is not None:
            resolution_id = str(resolution_id)
            processed = getattr(self, "_processed_ability_resolution_ids", None)
            if processed is None:
                processed = self._processed_ability_resolution_ids = set()
            if resolution_id in processed:
                return
            processed.add(resolution_id)
            dedupe_token = resolution_id
        else:
            dedupe_token = str(data.get("current_points", 0))
            key = (ability_id, owner_card_id, dedupe_token)
            started = self.floating_texts_started.get(key)
            if started is not None and now - started < FLOATING_TEXT_SECONDS:
                return
        key = (ability_id, owner_card_id, dedupe_token)
        self.floating_texts_started[key] = now
        self.floating_texts.append({
            "row_id": data.get("row_id"),
            "slot_index": data.get("slot_index"),
            "text": str(data.get("floating_text", "")),
            "started": now,
        })

    def _resolve_duo_animation(self, state: GameState, cards: dict[str, CardDefinition]) -> None:
        """Advance the Dynamic Duo queue and resolve the next partner.

        The current animation expires after ``duo_duration``; once it does, the
        next queued activation is resolved against live state so its arc
        endpoints come from actual card screen positions. Activations that are
        not genuine same-player Dynamic Duo pairs are discarded silently.
        """
        while self.duo_animation is not None:
            if time.monotonic() < self.duo_animation["started"] + self.duo_duration:
                return  # current animation still playing
            self.duo_animation = None  # expired
        while self.duo_queue:
            anim = self.duo_queue.pop(0)
            row_id, slot_index = anim["owner_key"]
            partner_key = self._find_duo_partner(state, cards, row_id, slot_index)
            if partner_key is not None:
                anim["partner_key"] = partner_key
                self.duo_animation = anim
                return
            # Not a genuine Dynamic Duo activation: discard and try the next.

    def _find_duo_partner(
        self, state: GameState, cards: dict[str, CardDefinition], row_id: str, slot_index: int
    ) -> tuple[str, int] | None:
        """Locate the activating card's Dynamic Duo partner in the same row.

        A Dynamic Duo ability references another character via a
        ``character_revealed`` condition. The partner must be revealed, share
        the owner's row and player, and match that referenced definition.
        """
        target_row = None
        for row in state.rows:
            if row.id == row_id:
                target_row = row
                break
        if target_row is None or not 0 <= slot_index < len(target_row.slots):
            return None
        owner_slot = target_row.slots[slot_index]
        if owner_slot is None:
            return None
        definition = cards.get(owner_slot.definition_id) if owner_slot.revealed else None
        if definition is None:
            return None
        ability = getattr(definition, "ability", None)
        if ability is None:
            return None
        partner_def_id = None
        for condition in ability.conditions:
            if getattr(condition, "type", None) == "character_revealed":
                partner_def_id = resolve_character_reference(cards, condition.character)
                break
        if partner_def_id is None:
            return None
        for index, slot in enumerate(target_row.slots):
            if (
                index == slot_index
                or slot is None
                or not slot.revealed
                or slot.owner_id != owner_slot.owner_id
                or slot.definition_id != partner_def_id
            ):
                continue
            return (row_id, index)
        return None

    def _draw_duo_energy(self, screen, layout: BoardLayout, now: float) -> None:
        """Draw the timed purple energy arc for the active Dynamic Duo pair.

        Sequence: both cards glow purple → a glowing energy ball travels along
        a curved arc from the owner card toward the partner → a flash at the
        target card → fade out. Expired animations are cleared here.
        """
        anim = self.duo_animation
        if anim is None:
            return
        duration = self.duo_duration
        if duration <= 0:
            self.duo_animation = None
            return
        elapsed = now - anim["started"]
        if elapsed >= duration:
            self.duo_animation = None
            return
        progress = elapsed / duration  # 0 .. 1
        fade = 1.0
        if progress >= 0.90:
            fade = max(0.0, (1.0 - progress) / 0.10)

        owner_rect = layout.slot_rects.get(anim["owner_key"])
        partner_key = anim.get("partner_key")
        partner_rect = layout.slot_rects.get(partner_key) if partner_key else None
        if owner_rect is None or partner_rect is None:
            self.duo_animation = None
            return

        ox, oy = owner_rect.center
        px, py = partner_rect.center
        size = screen.get_size()
        layer = pygame.Surface(size, pygame.SRCALPHA)

        # 1. Purple card highlights pulse for the whole duration, fading out at
        #    the very end.
        highlight_alpha = int(110 * fade * (0.6 + 0.4 * math.sin(elapsed * 14.0)))
        if highlight_alpha > 0:
            for rect in (owner_rect, partner_rect):
                glow = rect.inflate(16, 16)
                surf = pygame.Surface(glow.size, pygame.SRCALPHA)
                pygame.draw.rect(
                    surf, (*DUO_ENERGY, highlight_alpha), surf.get_rect(),
                    width=5, border_radius=10,
                )
                layer.blit(surf, glow.topleft)

        if progress < 0.72:
            # 2. Curved arc travel: faint full arc plus a glowing energy ball
            #    moving from the owner card toward the partner card.
            t = progress / 0.72
            mid_x = (ox + px) / 2.0
            mid_y = (oy + py) / 2.0
            arc_height = max(70.0, abs(px - ox) * 0.55)
            cx, cy = mid_x, mid_y - arc_height  # arc bows upward
            bx = (1 - t) ** 2 * ox + 2 * (1 - t) * t * cx + t ** 2 * px
            by = (1 - t) ** 2 * oy + 2 * (1 - t) * t * cy + t ** 2 * py

            points = []
            for i in range(0, 17):
                ti = i / 16.0
                ax = (1 - ti) ** 2 * ox + 2 * (1 - ti) * ti * cx + ti ** 2 * px
                ay = (1 - ti) ** 2 * oy + 2 * (1 - ti) * ti * cy + ti ** 2 * py
                points.append((ax, ay))
            arc_alpha = max(0, int(90 * fade * (1.0 - progress)))
            if arc_alpha > 0 and len(points) > 1:
                arc_layer = pygame.Surface(size, pygame.SRCALPHA)
                pygame.draw.lines(arc_layer, (*DUO_ENERGY, arc_alpha), False, points, 5)
                layer.blit(arc_layer, (0, 0))

            glow = pygame.Surface((52, 52), pygame.SRCALPHA)
            pygame.draw.circle(glow, (*DUO_ENERGY, int(60 * fade)), (26, 26), 22)
            layer.blit(glow, glow.get_rect(center=(round(bx), round(by))))
            ball = pygame.Surface((20, 20), pygame.SRCALPHA)
            pygame.draw.circle(ball, (*DUO_ENERGY, 255), (10, 10), 8)
            pygame.draw.circle(ball, (255, 255, 255, 220), (10, 10), 4)
            layer.blit(ball, ball.get_rect(center=(round(bx), round(by))))
        elif progress < 0.90:
            # 3. Flash at the target card.
            flash = (progress - 0.72) / 0.18
            alpha = max(0, int(180 * fade * (1.0 - flash)))
            radius = int(36 + flash * 70)
            surf = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
            pygame.draw.circle(surf, (*DUO_ENERGY, alpha), (radius, radius), radius)
            layer.blit(surf, surf.get_rect(center=(px, py)))
            flash_alpha = int(160 * fade * (1.0 - flash * 0.5))
            if flash_alpha > 0:
                glow = partner_rect.inflate(20, 20)
                surf = pygame.Surface(glow.size, pygame.SRCALPHA)
                pygame.draw.rect(
                    surf, (*DUO_ENERGY, flash_alpha), surf.get_rect(),
                    width=4, border_radius=10,
                )
                layer.blit(surf, glow.topleft)
        # 4. Fade out: handled by ``fade`` above as progress approaches 1.0.

        screen.blit(layer, (0, 0))

    def draw(self, screen, state: GameState, cards: dict[str, CardDefinition], legal_slots: list[tuple[str, int]], drag_id: str | None = None, mouse_pos: tuple[int, int] | None = None, fps: float = 0.0):
        display_surface = screen
        draw_now = time.monotonic()
        self._sync_sogeking_cinematic(state, draw_now)
        sogeking_active = self.sogeking_cinematic is not None
        self.luffy_haki_animations = [
            animation for animation in self.luffy_haki_animations
            if draw_now - animation["started"] < 1.55
        ]
        shaking = bool(self.luffy_haki_animations)
        if shaking or sogeking_active:
            size = display_surface.get_size()
            if self._scene_surface is None or self._scene_surface.get_size() != size:
                self._scene_surface = pygame.Surface(size)
            screen = self._scene_surface
        if self._render_size != screen.get_size():
            self._render_size = screen.get_size()
            self._overlay_cache.clear()
            self._row_backplates.clear()
            self._slot_surface_cache.clear()
            self._card_frame_cache.clear()
            self._card_front_cache.clear()
            self._card_face_cache.clear()
            self._card_back_cache.clear()
            self._spotlight_portrait_cache.clear()
            self._selection_dim_surface = None
            self._selection_vignette_cache_size = None
            self._selection_vignette_surface = None
            self._selection_aura_surfaces.clear()
            self._selection_invalid_dim_surfaces.clear()
        screen.fill(BG)
        self._draw_background(screen)
        layout = self.layout(state, screen.get_size())
        now = time.monotonic()
        self._expire_elimination_flash(state, now)
        self._resolve_duo_animation(state, cards)
        self._draw_header(screen, state)
        if DEBUG:
            self._draw_fps(screen, fps)
        self._draw_rows(screen, state, cards, layout, set(legal_slots), drag_id, mouse_pos)
        self._draw_trunks_bonus_animations(screen, layout, now)
        self._draw_ability_suppression_animation(screen, state, layout, now)
        self._draw_reverse_fate_animation(screen, state, cards, layout, now)
        self._draw_elimination_flash(screen, state, cards, layout, now)
        reveal_view_active = self._reveal_view_active(state)
        self._draw_card_spotlight(screen, state, mouse_pos)
        self._draw_whis_resurrection_animations(screen, layout, now)
        self._draw_whis_healing_animations(screen, state, layout, now)
        self._draw_row_comparison(screen, state)
        self._draw_status(screen, state, layout)
        self._draw_hand(screen, state, cards, layout, drag_id, mouse_pos)
        self._draw_floating_texts(screen, layout)
        self._draw_footer(screen, layout)
        # Failed-elimination message.
        self._draw_elimination_failed(screen, now)
        # Card / ability popups render above the board and eliminated cards.
        if not reveal_view_active:
            self._draw_effect_tooltip(screen, state, cards, layout, mouse_pos)
        # Elimination announcement (large, center screen).
        self._draw_elimination_announcement(screen, now, layout)
        if state.phase == GamePhase.ROUND_RESULTS:
            self._draw_round_results(screen, state, layout)
        elif state.phase == GamePhase.MATCH_RESULTS:
            self._draw_match_results(screen, state, layout)
        self._draw_duo_energy(screen, layout, now)
        active_selection = self._draw_target_selection_overlay(
            screen, state, layout, now
        )
        if active_selection:
            self._redraw_valid_selection_targets(screen, state, cards, layout, now)
        self._draw_selection_highlights(screen, state, cards, layout, now, mouse_pos)
        self._draw_invalid_selection_targets(screen, state, layout)
        self._draw_selected_target_visual(screen, state, layout, now)
        self._draw_luffy_haki_effect(screen, layout, now)
        if self._selection_overlay_effect_type == "eliminate_target" and (
            active_selection or self._selection_overlay_strength > 0.001
        ):
            # Keep the existing prompt content and coordinates; draw it above
            # the scene dimmer so it stays readable throughout the choice.
            self._draw_elimination_instruction(
                screen, layout, self._selection_overlay_strength
            )
        self._draw_crazy_slots_screen(screen, state, now, mouse_pos)
        if sogeking_active:
            self._draw_sogeking_cinematic(display_surface, screen, state, cards, layout, now)
        elif shaking:
            display_surface.fill(BG)
            offset = self._luffy_haki_camera_offset(now, screen.get_size())
            display_surface.blit(screen, offset)
        return layout

    CRAZY_SLOTS_OPTIONS = (
        ("Powerful Strike", "+4 bonus points", "strike"),
        ("Survival Instinct", "Immune to elimination", "shield"),
        ("Deadly Precision", "Eliminate an opposing Regular", "target"),
    )
    CRAZY_SLOTS_PURPLE = (90, 47, 200)
    CRAZY_SLOTS_VIOLET = (184, 128, 255)
    CRAZY_SLOTS_GOLD = (245, 196, 94)

    def _crazy_slots_timing(self, result_number: int) -> tuple[float, float, float, int]:
        """Return the shared reel timing and exact landing for an outcome."""
        # Timing is identical on both clients even when their local reduced-
        # motion preference differs. That keeps the authoritative CONTINUE
        # moment synchronized while reduced motion can still simplify glows.
        spin_up = 0.30
        constant = 1.50
        deceleration = 3.00
        landing_cell = 69 + (result_number - 1)
        return spin_up, constant, deceleration, landing_cell

    def _crazy_slots_motion(self, pending: dict, now: float) -> tuple[float, float, float, bool]:
        spin_id = str(pending.get("spin_id", ""))
        if (self.crazy_slots_animation is None
                or self.crazy_slots_animation.get("spin_id") != spin_id):
            # A reconnect can resume only an authoritative, stored pending roll.
            self.crazy_slots_animation = {
                "spin_id": spin_id,
                "started": now,
                "started_at": float(pending.get("spin_elapsed", 0.0) or 0.0),
            }
        animation = self.crazy_slots_animation
        result = int(pending.get("result_number") or 1)
        spin_up, constant, deceleration, landing_cell = self._crazy_slots_timing(result)
        elapsed = max(
            0.0,
            float(animation.get("started_at", pending.get("spin_elapsed", 0.0)) or 0.0)
            + (now - float(animation["started"])),
        )
        total = spin_up + constant + deceleration
        spin_up_cells = 0.5 * 26 * spin_up
        constant_cells = 26 * constant
        deceleration_cells = landing_cell - spin_up_cells - constant_cells
        if elapsed < spin_up:
            ratio = elapsed / spin_up
            distance = spin_up_cells * ratio * ratio
            speed = 26 * ratio
        elif elapsed < spin_up + constant:
            phase = elapsed - spin_up
            distance = spin_up_cells + 26 * phase
            speed = 26.0
        elif elapsed < total:
            phase = (elapsed - spin_up - constant) / deceleration
            ease = 1.0 - (1.0 - phase) ** 3
            distance = spin_up_cells + constant_cells + deceleration_cells * ease
            speed = (3 * deceleration_cells / deceleration) * (1.0 - phase) ** 2
        else:
            distance = float(landing_cell)
            speed = 0.0
        done = elapsed >= total
        if done and animation.get("landed_at") is None:
            animation["landed_at"] = now - max(0.0, elapsed - total)
        self.crazy_slots_current_digit = (int(math.floor(distance)) % 3) + 1
        self.crazy_slots_current_cell = int(math.floor(distance))
        self.crazy_slots_current_speed = speed
        if done:
            self.crazy_slots_current_digit = result
        return distance, speed, min(1.0, elapsed / max(0.001, total)), done

    def _draw_crazy_slots_screen(
        self, screen, state: GameState, now: float,
        mouse_pos: tuple[int, int] | None = None,
    ) -> None:
        """Native Pygame slot-machine scene exclusive to Kite."""
        pending = state.get("pending_crazy_slots") if hasattr(state, "get") else None
        if not isinstance(pending, dict):
            self.crazy_slots_spin_rect = None
            self.crazy_slots_continue_rect = None
            self.crazy_slots_continue_pending = False
            self.crazy_slots_animation = None
            self.crazy_slots_animation_complete = False
            self._crazy_slots_background = None
            self._crazy_slots_background_size = None
            self._crazy_slots_vignette = None
            return
        self.crazy_slots_spin_rect = None
        self.crazy_slots_continue_rect = None
        if not self.crazy_slots_overlay_ready:
            return

        size = screen.get_size()
        reduced = bool(getattr(self.settings, "reduced_motion", False))
        if self._crazy_slots_background is None or self._crazy_slots_background_size != size:
            source = screen.copy()
            if reduced:
                self._crazy_slots_background = source
            else:
                blur_size = (max(1, size[0] // 6), max(1, size[1] // 6))
                softened = pygame.transform.smoothscale(source, blur_size)
                self._crazy_slots_background = pygame.transform.smoothscale(softened, size)
            self._crazy_slots_background_size = size
            self._crazy_slots_vignette = self._make_crazy_slots_vignette(size)

        status = str(pending.get("status", "awaiting_spin"))
        result_number = int(pending.get("result_number") or 0)
        distance = 0.0
        speed = 0.0
        progress = 0.0
        landed = False
        elapsed = 0.0
        if status == "spinning":
            distance, speed, progress, landed = self._crazy_slots_motion(pending, now)
            elapsed = max(
                0.0,
                float(self.crazy_slots_animation.get("started_at", 0.0) or 0.0)
                + (now - float(self.crazy_slots_animation["started"])),
            )
            self.crazy_slots_animation_complete = landed
        else:
            self.crazy_slots_animation_complete = False

        screen.blit(self._crazy_slots_background, (0, 0))
        fade = 1.0
        if self.crazy_slots_animation is None or status != "spinning":
            if self._crazy_slots_overlay_started is None:
                self._crazy_slots_overlay_started = now
            fade = min(1.0, max(0.0, (now - self._crazy_slots_overlay_started) / 0.24))
        darkness = pygame.Surface(size, pygame.SRCALPHA)
        darkness.fill((4, 2, 13, round(140 * fade)))
        screen.blit(darkness, (0, 0))
        if self._crazy_slots_vignette is not None and not reduced:
            screen.blit(self._crazy_slots_vignette, (0, 0))

        width, height = size
        margin = max(12, min(28, int(min(width, height) * 0.035)))
        panel = pygame.Rect(margin, margin, max(1, width - margin * 2), max(1, height - margin * 2))
        panel_surface = pygame.Surface(panel.size, pygame.SRCALPHA)
        pygame.draw.rect(panel_surface, (9, 6, 24, round(189 * fade)), panel_surface.get_rect(), border_radius=22)
        pygame.draw.rect(
            panel_surface, (*self.CRAZY_SLOTS_PURPLE, round(255 * fade)),
            panel_surface.get_rect(), width=2, border_radius=22,
        )
        screen.blit(panel_surface, panel.topleft)

        title_font = get_font("title", max(24, min(45, int(height * 0.064))))
        body_font = get_font("ui", max(15, min(23, int(height * 0.032))))
        small_font = get_font("label_small", max(12, min(18, int(height * 0.024))))
        title = title_font.render("KITE'S CRAZY SLOTS", True, (250, 237, 255))
        title_rect = title.get_rect(center=(panel.centerx, panel.top + max(36, int(panel.height * 0.075))))
        screen.blit(title, title_rect)
        subtitle = body_font.render(
            "Fate is in motion. One outcome will decide the battle.",
            True, (195, 179, 216),
        )
        subtitle_rect = subtitle.get_rect(center=(panel.centerx, title_rect.bottom + 18))
        screen.blit(subtitle, subtitle_rect)
        divider_y = subtitle_rect.bottom + 13
        pygame.draw.line(screen, (90, 47, 200), (panel.centerx - 100, divider_y), (panel.centerx - 12, divider_y), 2)
        pygame.draw.line(screen, (90, 47, 200), (panel.centerx + 12, divider_y), (panel.centerx + 100, divider_y), 2)
        diamond = [(panel.centerx, divider_y - 8), (panel.centerx + 8, divider_y),
                   (panel.centerx, divider_y + 8), (panel.centerx - 8, divider_y)]
        pygame.draw.polygon(screen, self.CRAZY_SLOTS_GOLD, diamond)

        compact = width < 760
        inner_width = panel.width - max(32, int(panel.width * 0.09))
        table_card_h = max(52, min(94, int(panel.height * (0.105 if compact else 0.13))))
        table_gap = 9 if compact else 15
        table_h = table_card_h * 3 + table_gap * 2 if compact else table_card_h
        button_h = max(46, min(62, int(panel.height * 0.085)))
        bottom_reserved = button_h + max(45, int(panel.height * 0.09))
        table_bottom = panel.bottom - bottom_reserved
        table_top = table_bottom - table_h
        reel_top = divider_y + 22
        reel_max_h = max(96, table_top - reel_top - 17)
        reel_h = min(285, reel_max_h)
        reel_w = min(int(inner_width * (0.76 if compact else 0.62)), int(panel.width * 0.74))
        reel_w = max(190, reel_w)
        reel = pygame.Rect(0, 0, reel_w, reel_h)
        reel.center = (panel.centerx, reel_top + reel_h // 2)

        frame_pop = 0
        landing_age = None
        if landed and self.crazy_slots_animation is not None:
            landing_age = max(0.0, now - float(self.crazy_slots_animation.get("landed_at", now)))
            if landing_age < 0.6:
                frame_pop = round(8 * (1 - landing_age / 0.6))
        frame = reel.inflate(22 + frame_pop, 20 + frame_pop)
        frame_glow = pygame.Surface(frame.inflate(18, 18).size, pygame.SRCALPHA)
        pygame.draw.rect(
            frame_glow, (109, 55, 223, round(66 * fade)), frame_glow.get_rect(),
            width=10, border_radius=22,
        )
        screen.blit(frame_glow, frame_glow.get_rect(center=frame.center).topleft)
        pygame.draw.rect(screen, (14, 8, 29), frame, border_radius=18)
        pygame.draw.rect(screen, (90, 47, 200), frame, width=2, border_radius=18)
        pygame.draw.rect(screen, (52, 30, 82), reel, border_radius=10)
        pygame.draw.rect(screen, (30, 17, 49), reel.inflate(-8, -8), border_radius=7)

        # 34 evenly spaced marquee bulbs around the purple reel frame.
        cycle = 2.4 if status != "spinning" else 0.45
        marquee_elapsed = elapsed if status == "spinning" else max(0.0, now - float(self._crazy_slots_overlay_started or now))
        bulb_phase = 0.0 if reduced else (marquee_elapsed % cycle) / cycle
        chase_index = int(bulb_phase * 34) % 34
        for bulb_index in range(34):
            t = bulb_index / 34
            perimeter = 2 * (frame.width + frame.height)
            offset = t * perimeter
            if offset < frame.width:
                point = (round(frame.left + offset), frame.top)
            elif offset < frame.width + frame.height:
                point = (frame.right, round(frame.top + offset - frame.width))
            elif offset < 2 * frame.width + frame.height:
                point = (round(frame.right - (offset - frame.width - frame.height)), frame.bottom)
            else:
                point = (frame.left, round(frame.bottom - (offset - 2 * frame.width - frame.height)))
            if landed and landing_age is not None and landing_age < 0.6:
                brightness = 1.0
                bulb_color = self.CRAZY_SLOTS_GOLD
            elif not reduced and (bulb_index - chase_index) % 34 in {0, 1, 2, 3}:
                brightness = 1.0 - ((bulb_index - chase_index) % 34) * 0.15
                bulb_color = self.CRAZY_SLOTS_GOLD if landed else self.CRAZY_SLOTS_VIOLET
            else:
                brightness = 0.30
                bulb_color = (127, 91, 174)
            radius = max(2, min(5, round(min(width, height) * 0.005)))
            pygame.draw.circle(screen, (57, 37, 84), point, radius + 2)
            color = tuple(round(channel * brightness) for channel in bulb_color)
            pygame.draw.circle(screen, color, point, radius)

        # Gold payline markers at each side of the reel.
        mid_y = reel.centery
        pygame.draw.polygon(screen, self.CRAZY_SLOTS_GOLD, [
            (reel.left - 18, mid_y), (reel.left - 7, mid_y - 8), (reel.left - 7, mid_y + 8),
        ])
        pygame.draw.polygon(screen, self.CRAZY_SLOTS_GOLD, [
            (reel.right + 18, mid_y), (reel.right + 7, mid_y - 8), (reel.right + 7, mid_y + 8),
        ])

        previous_clip = screen.get_clip()
        screen.set_clip(reel.inflate(-14, -12))
        if status == "spinning":
            cell_h = max(1, int(reel.height * 0.78))
            center_index = math.floor(distance)
            fraction = distance - center_index
            fast_blur = speed > 9 and not reduced
            for item_offset in range(-2, 3):
                cell_number = ((center_index + item_offset) % 3) + 1
                y = reel.centery + round((item_offset - fraction) * cell_h)
                font = get_font("title", cell_h)
                mask = font.render(str(cell_number), True, (255, 255, 255))
                rect = mask.get_rect(center=(reel.centerx, y))
                if fast_blur and abs(item_offset - fraction) < 1.2:
                    for blur_offset, alpha in ((-9, 34), (9, 34), (-5, 58), (5, 58)):
                        ghost = mask.copy()
                        ghost.fill((255, 255, 255, alpha), special_flags=pygame.BLEND_RGBA_MULT)
                        screen.blit(ghost, rect.move(0, blur_offset))
                self._blit_gold_glyph(screen, mask, rect)
        else:
            question_size = max(62, int(reel.height * 0.78))
            question = get_font("title", question_size).render("?", True, (255, 255, 255))
            question_rect = question.get_rect(center=reel.center)
            pulse = 0.82 if reduced else 0.72 + 0.28 * (0.5 + 0.5 * math.sin(now * 2.2))
            self._blit_gold_glyph(screen, question, question_rect, pulse)
        screen.set_clip(previous_clip)

        # Top/bottom cylinder shade and reflective glass streaks.
        shade = pygame.Surface(reel.size, pygame.SRCALPHA)
        pygame.draw.rect(shade, (6, 3, 12, 100), (0, 0, reel.width, max(10, reel.height // 8)))
        pygame.draw.rect(shade, (6, 3, 12, 105), (0, reel.height - max(10, reel.height // 8), reel.width, max(10, reel.height // 8)))
        pygame.draw.line(shade, (255, 255, 255, 30), (12, 8), (reel.width - 18, 4), 2)
        screen.blit(shade, reel.topleft)

        # Pay table: three distinct outcome cards, horizontal on desktop and stacked on narrow screens.
        if compact:
            card_w = inner_width
            card_rects = [
                pygame.Rect(panel.centerx - card_w // 2, table_top + i * (table_card_h + table_gap), card_w, table_card_h)
                for i in range(3)
            ]
        else:
            gap = 14
            card_w = (inner_width - gap * 2) // 3
            start_x = panel.centerx - inner_width // 2
            card_rects = [
                pygame.Rect(start_x + i * (card_w + gap), table_top, card_w, table_card_h)
                for i in range(3)
            ]

        scan_digit = self.crazy_slots_current_digit if speed < 9 else 0
        won = result_number if landed else 0
        icon_color = self.CRAZY_SLOTS_GOLD
        for index, ((name, description, icon), card_rect) in enumerate(zip(self.CRAZY_SLOTS_OPTIONS, card_rects), start=1):
            is_winner = won == index
            is_scan = status == "spinning" and not landed and scan_digit == index
            subdued = landed and not is_winner
            option_surface = pygame.Surface(card_rect.size, pygame.SRCALPHA)
            fill_color = (37, 23, 58, 246 if not subdued else 95)
            border_color = self.CRAZY_SLOTS_GOLD if is_winner else self.CRAZY_SLOTS_VIOLET
            border_width = 3 if is_winner or is_scan else 2
            pygame.draw.rect(option_surface, fill_color, option_surface.get_rect(), border_radius=12)
            if is_scan:
                pygame.draw.rect(option_surface, (233, 191, 255, 255), option_surface.get_rect(), width=3, border_radius=12)
            else:
                pygame.draw.rect(
                    option_surface, (*border_color, 255 if not subdued else 90),
                    option_surface.get_rect(), width=border_width, border_radius=12,
                )
            if is_winner:
                glow = pygame.Surface(card_rect.inflate(14, 14).size, pygame.SRCALPHA)
                pygame.draw.rect(glow, (245, 196, 94, 62), glow.get_rect(), width=7, border_radius=16)
                screen.blit(glow, glow.get_rect(center=card_rect.center).topleft)
            badge_center = (19, card_rect.height // 2)
            pygame.draw.circle(option_surface, (245, 196, 94) if is_winner else (74, 48, 108), badge_center, 13)
            badge = get_font("label_small", 15).render(str(index), True, (20, 11, 28))
            option_surface.blit(badge, badge.get_rect(center=badge_center))
            icon_x = 42
            icon_y = card_rect.height // 2
            if icon == "strike":
                pygame.draw.line(option_surface, icon_color, (icon_x - 5, icon_y + 10), (icon_x + 8, icon_y - 10), 3)
                pygame.draw.line(option_surface, icon_color, (icon_x + 2, icon_y - 8), (icon_x + 8, icon_y - 10), 2)
                pygame.draw.line(option_surface, icon_color, (icon_x + 8, icon_y - 10), (icon_x + 6, icon_y - 3), 2)
            elif icon == "shield":
                points = [(icon_x, icon_y - 12), (icon_x + 10, icon_y - 8), (icon_x + 8, icon_y + 5),
                          (icon_x, icon_y + 12), (icon_x - 8, icon_y + 5), (icon_x - 10, icon_y - 8)]
                pygame.draw.polygon(option_surface, icon_color, points, width=2)
            else:
                pygame.draw.circle(option_surface, icon_color, (icon_x, icon_y), 9, width=2)
                pygame.draw.circle(option_surface, icon_color, (icon_x, icon_y), 2)
                pygame.draw.line(option_surface, icon_color, (icon_x - 13, icon_y), (icon_x - 6, icon_y), 2)
                pygame.draw.line(option_surface, icon_color, (icon_x + 6, icon_y), (icon_x + 13, icon_y), 2)
            text_x = 60
            name_font = get_font("ui", max(14, min(20, int(card_rect.height * 0.27))))
            desc_font = get_font("label_small", max(12, min(16, int(card_rect.height * 0.19))))
            name_color = self.CRAZY_SLOTS_GOLD if is_winner else (240, 227, 255)
            name_text = name_font.render(name, True, name_color)
            option_surface.blit(name_text, (text_x, max(7, card_rect.height // 2 - 22)))
            desc_text = desc_font.render(description, True, (202, 187, 221))
            option_surface.blit(desc_text, (text_x, card_rect.height // 2 + 2))
            if subdued:
                option_surface.set_alpha(89)
            screen.blit(option_surface, card_rect.topleft)

        owner_id = str(pending.get("owner_id", ""))
        viewer_id = state.get("viewer_id") if hasattr(state, "get") else None
        viewer_can_interact = viewer_id is None or str(viewer_id) == owner_id
        button_width = min(280, max(170, int(inner_width * 0.34)))
        button = pygame.Rect(0, 0, button_width, button_h)
        button.center = (panel.centerx, panel.bottom - button_h // 2 - 16)
        button_text = None
        if status == "awaiting_spin":
            if viewer_can_interact and self.crazy_slots_spin_ready:
                button_text = "SPIN"
                self.crazy_slots_spin_rect = button
            elif viewer_can_interact:
                button_text = "SPIN"
            else:
                prompt = small_font.render("Waiting for the active player to spin...", True, (200, 184, 220))
                screen.blit(prompt, prompt.get_rect(center=button.center))
        elif status == "spinning":
            if landed:
                if viewer_can_interact and self.crazy_slots_continue_pending:
                    button_text = "CONTINUING..."
                elif viewer_can_interact:
                    button_text = "CONTINUE"
                    self.crazy_slots_continue_rect = button
                else:
                    prompt = small_font.render("Waiting for the opponent to continue...", True, (200, 184, 220))
                    screen.blit(prompt, prompt.get_rect(center=button.center))
            elif viewer_can_interact:
                button_text = "SPINNING..."
            else:
                prompt = small_font.render("Opponent is spinning...", True, (225, 211, 241))
                screen.blit(prompt, prompt.get_rect(center=button.center))
        if button_text is not None:
            hovered = mouse_pos is not None and button.collidepoint(mouse_pos)
            button_surface = pygame.Surface(button.size, pygame.SRCALPHA)
            button_fill = (112, 56, 190) if not hovered else (153, 83, 235)
            if status == "spinning" and (not landed or self.crazy_slots_continue_pending):
                button_fill = (70, 48, 91)
            pygame.draw.rect(button_surface, (*button_fill, 255), button_surface.get_rect(), border_radius=15)
            pygame.draw.rect(button_surface, (169, 124, 232), button_surface.get_rect(), width=2, border_radius=15)
            sheen = int((now * 130) % max(1, button.width + 50)) - 25
            pygame.draw.line(button_surface, (255, 255, 255, 42), (sheen, 4), (sheen + 35, button.height - 4), 3)
            screen.blit(button_surface, button.topleft)
            button_font = get_font("ui", max(19, min(27, int(button_h * 0.43))))
            label = button_font.render(button_text, True, (255, 248, 255))
            screen.blit(label, label.get_rect(center=button.center))
            if self.crazy_slots_button_focused and viewer_can_interact and (
                (status == "awaiting_spin" and self.crazy_slots_spin_ready) or
                (status == "spinning" and landed)
            ):
                pygame.draw.rect(screen, (250, 236, 255), button.inflate(8, 8), width=2, border_radius=18)

        if landed and landing_age is not None:
            result_alpha = min(255, int(255 * min(1.0, landing_age / 0.25)))
            result = self.CRAZY_SLOTS_OPTIONS[result_number - 1][1]
            result_line = get_font("ui", max(17, min(24, int(height * 0.034)))).render(
                result, True, self.CRAZY_SLOTS_GOLD,
            )
            result_line.set_alpha(result_alpha)
            result_y = table_bottom + max(10, (bottom_reserved - button_h) // 3)
            screen.blit(result_line, result_line.get_rect(center=(panel.centerx, result_y)))
            if landing_age < 0.6:
                spark_layer = pygame.Surface(size, pygame.SRCALPHA)
                for particle in range(22):
                    angle = particle * (math.tau / 22) + 0.07
                    travel = max(0.0, landing_age / 0.6)
                    radius = 12 + travel * min(width, height) * 0.19
                    px = round(reel.centerx + math.cos(angle) * radius)
                    py = round(reel.centery + math.sin(angle) * radius)
                    alpha = round(220 * (1 - travel))
                    pygame.draw.circle(spark_layer, (*self.CRAZY_SLOTS_GOLD, alpha), (px, py), 2 + particle % 3)
                screen.blit(spark_layer, (0, 0))

    @staticmethod
    def _blit_gold_glyph(screen, glyph: pygame.Surface, rect: pygame.Rect, opacity: float = 1.0) -> None:
        gradient = pygame.Surface(glyph.get_size(), pygame.SRCALPHA)
        top = (255, 236, 164)
        bottom = (176, 111, 34)
        for y in range(glyph.get_height()):
            ratio = y / max(1, glyph.get_height() - 1)
            color = tuple(round(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
            pygame.draw.line(gradient, (*color, round(255 * opacity)), (0, y), (glyph.get_width(), y))
        gradient.blit(glyph, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        screen.blit(gradient, rect)

    @staticmethod
    def _make_crazy_slots_vignette(size: tuple[int, int]) -> pygame.Surface:
        """Create a cached dark edge treatment that keeps the board recognizable."""
        width, height = size
        surface = pygame.Surface(size, pygame.SRCALPHA)
        layers = max(1, min(24, min(width, height) // 32))
        for layer in range(layers):
            inset = layer * min(width, height) // (layers * 2)
            rect = pygame.Rect(inset, inset, width - inset * 2, height - inset * 2)
            if rect.width <= 0 or rect.height <= 0:
                break
            alpha = round(27 * (1 - layer / max(1, layers)) ** 2)
            pygame.draw.rect(surface, (0, 0, 0, alpha), rect, width=max(1, min(width, height) // (layers * 2)))
        return surface
    # ----------------------------------------------------------- target selection visuals

    ELIMINATION_ANNOUNCEMENT_SECONDS = 2.5
    ELIMINATION_FLIP_SECONDS = 1.0
    ELIMINATION_FAILED_SECONDS = 3.0
    ELIMINATION_RED = (220, 40, 40)
    ELIMINATION_PURPLE = DUO_ENERGY
    ELIMINATION_BRIGHT_PURPLE = (225, 174, 255)
    SELECTION_PALETTES = {
        "elimination": {
            "color": ELIMINATION_PURPLE,
            "bright": ELIMINATION_BRIGHT_PURPLE,
            "target_kinds": ("character",),
        },
        "revive": {
            "color": (58, 230, 112),
            "bright": (174, 255, 187),
            "target_kinds": ("character",),
        },
        "point_giving": {
            "color": (255, 207, 48),
            "bright": (255, 244, 146),
            "target_kinds": ("character",),
        },
        "bonus_point_transfer": {
            "color": (255, 126, 36),
            "bright": (255, 194, 123),
            "target_kinds": ("character",),
        },
        "base_point_reduction": {
            "color": (244, 48, 58),
            "bright": (255, 174, 180),
            "target_kinds": ("character",),
        },
        "healing": {
            "color": (75, 205, 246),
            "bright": (226, 251, 255),
            "target_kinds": ("character",),
        },
        # Ready for a future ability to first select a character, then a row.
        # No movement gameplay or destination-row interaction is added here.
        "character_movement": {
            "color": (48, 146, 255),
            "bright": (165, 220, 255),
            "target_kinds": ("character", "row"),
        },
    }
    SELECTION_EFFECT_CATEGORIES = {
        "eliminate_target": "elimination",
        "revive_target": "revive",
        "add_current_points_to_target": "point_giving",
        "give_target_points": "point_giving",
        "steal_target_bonus": "bonus_point_transfer",
        "copy_target_bonus": "bonus_point_transfer",
        "reduce_target_base_points": "base_point_reduction",
        "heal_target_base_points": "healing",
        "move_character": "character_movement",
        "move_character_target": "character_movement",
        "move_character_destination": "character_movement",
        "select_destination_row": "character_movement",
    }

    def _active_target_selection(self, state: GameState) -> dict | None:
        """Return an authoritative active selection payload visible to this viewer."""
        pending = state.get("pending_elimination") if hasattr(state, "get") else None
        if not isinstance(pending, dict) or not pending.get("effect_type"):
            return None
        if not pending.get("selection_ready", True):
            return None
        if not isinstance(pending.get("valid_targets"), list) or not pending["valid_targets"]:
            return None
        if state.viewer_id is not None and pending.get("attacker_owner_id") != str(state.viewer_id):
            return None
        return pending

    def _selection_palette(self, effect_type: str) -> dict:
        """Resolve palette and target-kind configuration for an effect type."""
        category = self.SELECTION_EFFECT_CATEGORIES.get(effect_type, "elimination")
        return self.SELECTION_PALETTES[category]

    def _selection_colors(self, effect_type: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """Get the normal and peak colors for this target-selection category."""
        palette = self._selection_palette(effect_type)
        return palette["color"], palette["bright"]

    def _selection_target_kinds(self, effect_type: str) -> tuple[str, ...]:
        """Describe whether a palette is prepared for cards, rows, or both."""
        return self._selection_palette(effect_type)["target_kinds"]

    def _draw_selection_target_visual(
        self, screen, rect: pygame.Rect, effect_type: str, now: float,
        *, hovered: bool = False, target_kind: str = "character",
        phase_seed: float = 0.0,
    ) -> None:
        """Draw the shared aura on a selectable character or future row target."""
        if target_kind not in self._selection_target_kinds(effect_type):
            return
        selection_color, bright_color = self._selection_colors(effect_type)
        pulse = (math.sin(now * 2.1 + phase_seed) + 1.0) * 0.5
        flash_wave = math.sin(now * 1.25 + phase_seed + 0.7)
        flash = max(0.0, flash_wave) ** 14
        boost = 1.65 if hovered else 1.0
        glow_alpha = (0.62 + pulse * 0.48 + flash * 0.55) * boost
        margin = 40
        aura_size = (rect.width + margin * 2, rect.height + margin * 2)
        aura = self._selection_aura_surfaces.get(aura_size)
        if aura is None:
            aura = pygame.Surface(aura_size, pygame.SRCALPHA)
            self._selection_aura_surfaces[aura_size] = aura
        else:
            aura.fill((0, 0, 0, 0))
        base_rect = pygame.Rect(margin, margin, rect.width, rect.height)
        for offset, alpha, thickness, color in (
            (5, 86, 12, bright_color),
            (12, 66, 10, selection_color),
            (21, 43, 8, bright_color),
            (30, 25, 6, selection_color),
        ):
            aura_rect = base_rect.inflate(offset * 2, offset * 2)
            pygame.draw.rect(
                aura, (*color, round(alpha * glow_alpha)), aura_rect,
                width=thickness, border_radius=SLOT_RADIUS + offset,
            )
        for wave_index in range(2):
            wave = (now * 0.31 + wave_index * 0.5 + phase_seed * 0.1) % 1.0
            wave_offset = round(8 + wave * 29 + (4 if hovered else 0))
            wave_rect = base_rect.inflate(wave_offset * 2, wave_offset * 2)
            wave_alpha = round(
                (42 + 28 * pulse + 35 * flash)
                * math.sin(math.pi * wave)
                * (1.7 if hovered else 1.0)
            )
            pygame.draw.rect(
                aura, (*bright_color, wave_alpha), wave_rect,
                width=4 if hovered else 3,
                border_radius=SLOT_RADIUS + wave_offset,
            )
        spark_count = 5
        aura_center = (margin + rect.width // 2, margin + rect.height // 2)
        for spark_index in range(spark_count):
            angle = (
                now * (0.75 if hovered else 0.58) + phase_seed
                + spark_index * math.tau / spark_count
            )
            spark_x = round(aura_center[0] + math.cos(angle) * (rect.width / 2 + 8))
            spark_y = round(aura_center[1] + math.sin(angle) * (rect.height / 2 + 8))
            spark_alpha = min(
                255,
                round((112 + 72 * pulse + 60 * flash) * (1.45 if hovered else 1.0)),
            )
            pygame.draw.circle(
                aura, (*bright_color, spark_alpha), (spark_x, spark_y),
                2 if not hovered else 3,
            )
        screen.blit(aura, aura.get_rect(center=rect.center))
        if hovered:
            pygame.draw.rect(
                screen, bright_color, rect.inflate(2, 2), width=5,
                border_radius=SLOT_RADIUS + 2,
            )
        intensity = min(
            1.0, 0.18 + pulse * 0.42 + flash * 0.52 + (0.36 if hovered else 0.0)
        )
        outline_color = tuple(
            round(base + (bright - base) * intensity)
            for base, bright in zip(selection_color, bright_color)
        )
        pygame.draw.rect(
            screen, outline_color, rect, width=4 if hovered else 3,
            border_radius=SLOT_RADIUS,
        )

    def _remember_selected_target(self, data: dict, effect_type: str, now: float) -> None:
        """Hold a selected card's category-colored edge through its resolution animation."""
        try:
            row_id = str(data.get("row_id", ""))
            slot_index = int(data.get("slot_index", -1))
        except (TypeError, ValueError):
            return
        if row_id and slot_index >= 0:
            self.selected_target_visual = {
                "key": (row_id, slot_index),
                "effect_type": effect_type,
                "started": now,
                "duration": 0.9,
            }

    def _draw_target_selection_overlay(
        self, screen, state: GameState, layout: BoardLayout, now: float,
    ) -> bool:
        """Dim the full scene during any active target choice, then ease it back."""
        pending = self._active_target_selection(state)
        active = pending is not None
        if active:
            self._selection_overlay_effect_type = str(pending["effect_type"])
            self._selection_target_keys = {
                (str(item.get("row_id", "")), int(item.get("slot_index", -1)))
                for item in pending.get("valid_targets", [])
                if item.get("target_kind", "character") == "character"
            }
            # If another choice interrupts a previous resolution highlight, the
            # new authoritative target list owns the selection visuals now.
            self.selected_target_visual = None
        elif state.get("pending_elimination"):
            # A pending choice owned by another viewer must not inherit a
            # previous local selection's palette, target, or prompt.
            self._selection_overlay_effect_type = None
            self._selection_target_keys.clear()
            self.selected_target_visual = None

        previous = self._selection_overlay_updated_at
        self._selection_overlay_updated_at = now
        if previous is None:
            delta = 0.0
        else:
            delta = max(0.0, min(0.1, now - previous))
        target_strength = 1.0 if active else 0.0
        # Exponential easing remains smooth at different frame rates and fades
        # the board back in when selection resolves or is cancelled.
        amount = 1.0 - math.exp(-delta * 13.0)
        self._selection_overlay_strength += (
            target_strength - self._selection_overlay_strength
        ) * amount
        strength = self._selection_overlay_strength
        if strength <= 0.001 or not layout.row_rects:
            if not active and strength <= 0.001:
                self._selection_overlay_effect_type = None
                self._selection_target_keys.clear()
            return active

        size = screen.get_size()
        if self._selection_dim_surface is None or self._selection_dim_surface.get_size() != size:
            self._selection_dim_surface = pygame.Surface(size)
            self._selection_dim_surface.fill((5, 4, 15))
        self._selection_dim_surface.set_alpha(round(128 * strength))
        screen.blit(self._selection_dim_surface, (0, 0))
        vignette = self._selection_vignette(size)
        vignette.set_alpha(round(48 * strength))
        screen.blit(vignette, (0, 0))
        return active

    def _selection_vignette(self, size: tuple[int, int]) -> pygame.Surface:
        """Return a cached soft edge shade, built small then smoothly enlarged."""
        if self._selection_vignette_surface is not None and self._selection_vignette_cache_size == size:
            return self._selection_vignette_surface
        width, height = size
        low_size = (max(24, width // 12), max(24, height // 12))
        low = pygame.Surface(low_size, pygame.SRCALPHA)
        low_width, low_height = low_size
        for y in range(low_height):
            ny = (y / max(1, low_height - 1) - 0.5) * 2.0
            for x in range(low_width):
                nx = (x / max(1, low_width - 1) - 0.5) * 2.0
                radius = math.sqrt(nx * nx + ny * ny)
                edge = max(0.0, min(1.0, (radius - 0.56) / 0.86))
                alpha = round(62 * edge * edge)
                low.set_at((x, y), (4, 2, 12, alpha))
        self._selection_vignette_surface = pygame.transform.smoothscale(low, size)
        self._selection_vignette_cache_size = size
        return self._selection_vignette_surface

    def _redraw_valid_selection_targets(
        self, screen, state: GameState, cards: dict[str, CardDefinition],
        layout: BoardLayout, now: float,
    ) -> None:
        """Restore target card faces after the scene-wide darkening pass."""
        pending = self._active_target_selection(state)
        if not pending:
            return
        rows = {row.id: row for row in state.rows}
        for item in pending.get("valid_targets") or []:
            if item.get("target_kind", "character") != "character":
                continue
            row_id = str(item.get("row_id", ""))
            slot_index = int(item.get("slot_index", -1))
            row = rows.get(row_id)
            if row is None or not 0 <= slot_index < len(row.slots):
                continue
            card = row.slots[slot_index]
            slot_rect = layout.slot_rects.get((row_id, slot_index))
            if card is None or slot_rect is None:
                continue
            self._draw_placed_card(
                screen, self._portrait_card_rect(slot_rect), row, slot_index,
                card, cards, now,
            )

    def _draw_invalid_selection_targets(
        self, screen, state: GameState, layout: BoardLayout,
    ) -> None:
        """Keep occupied cards outside the valid set subdued under target auras."""
        pending = self._active_target_selection(state)
        if not pending:
            return
        valid_targets = {
            (str(item.get("row_id", "")), int(item.get("slot_index", -1)))
            for item in (pending.get("valid_targets") or [])
            if item.get("target_kind", "character") == "character"
        }
        strength = self._selection_overlay_strength
        for row in state.rows:
            for slot_index, card in enumerate(row.slots):
                if card is None or (row.id, slot_index) in valid_targets:
                    continue
                slot_rect = layout.slot_rects.get((row.id, slot_index))
                if slot_rect is None:
                    continue
                card_rect = self._portrait_card_rect(slot_rect)
                dim = self._selection_invalid_dim_surfaces.get(card_rect.size)
                if dim is None:
                    dim = pygame.Surface(card_rect.size)
                    dim.fill((3, 2, 12))
                    self._selection_invalid_dim_surfaces[card_rect.size] = dim
                dim.set_alpha(round(76 * strength))
                screen.blit(dim, card_rect.topleft)

    def _draw_selected_target_visual(
        self, screen, state: GameState, layout: BoardLayout, now: float,
    ) -> None:
        """Keep the resolved target steady and bright through its short animation."""
        selected = self.selected_target_visual
        if selected is None or state.get("pending_elimination"):
            return
        started = float(selected.get("started", now))
        duration = max(0.01, float(selected.get("duration", 0.9)))
        elapsed = now - started
        if elapsed >= duration:
            self.selected_target_visual = None
            return
        key = selected.get("key")
        if not isinstance(key, tuple) or len(key) != 2:
            self.selected_target_visual = None
            return
        slot_rect = layout.slot_rects.get((str(key[0]), int(key[1])))
        if slot_rect is None:
            self.selected_target_visual = None
            return
        rect = self._portrait_card_rect(slot_rect)
        effect_type = str(selected.get("effect_type", ""))
        color, bright = self._selection_colors(effect_type)
        fade_in = min(1.0, elapsed / 0.10)
        fade_out = min(1.0, max(0.0, (duration - elapsed) / 0.22))
        strength = min(fade_in, fade_out)
        margin = 16
        glow_rect = rect.inflate(margin * 2, margin * 2)
        glow = pygame.Surface(glow_rect.size, pygame.SRCALPHA)
        base_rect = pygame.Rect(margin, margin, rect.width, rect.height)
        for offset, alpha, thickness, tint in (
            (4, 118, 10, bright), (10, 72, 8, color),
        ):
            pygame.draw.rect(
                glow, (*tint, round(alpha * strength)),
                base_rect.inflate(offset * 2, offset * 2),
                width=thickness, border_radius=SLOT_RADIUS + offset,
            )
        screen.blit(glow, glow_rect.topleft)
        outline = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(
            outline, (*bright, round(255 * strength)), outline.get_rect(),
            width=5, border_radius=SLOT_RADIUS,
        )
        screen.blit(outline, rect.topleft)

    def _draw_elimination_instruction(
        self, screen, layout: BoardLayout, strength: float,
    ) -> None:
        """Show a short, readable prompt directly above the game board."""
        if strength <= 0.001 or not layout.row_rects:
            return
        first_row_top = min(rect.top for rect in layout.row_rects.values())
        font = get_font("label_small", 21)
        label = font.render("Choose a character to eliminate", True, TEXT_WHITE)
        padding_x, padding_y = 20, 7
        panel = pygame.Rect(
            0, 0, label.get_width() + padding_x * 2,
            label.get_height() + padding_y * 2,
        )
        panel.center = (screen.get_width() // 2, max(88, first_row_top - 24))
        panel_surface = pygame.Surface(panel.size, pygame.SRCALPHA)
        pygame.draw.rect(
            panel_surface, (28, 12, 48, round(218 * strength)),
            panel_surface.get_rect(), border_radius=12,
        )
        pygame.draw.rect(
            panel_surface, (*self.ELIMINATION_PURPLE, round(220 * strength)),
            panel_surface.get_rect(), width=2, border_radius=12,
        )
        screen.blit(panel_surface, panel.topleft)
        label.set_alpha(round(255 * strength))
        screen.blit(label, label.get_rect(center=panel.center))

    def _draw_selection_highlights(
        self, screen, state: GameState, cards: dict[str, CardDefinition],
        layout: BoardLayout, now: float, mouse_pos: tuple[int, int] | None,
    ) -> None:
        """Draw the shared animated selection treatment on authoritative targets.

        Effect type selects the palette; the engine's valid_targets list remains
        the sole source of target eligibility.

        In multiplayer, only the player who owns the ability should see the
        selection outlines. The pending_elimination payload contains
        attacker_owner_id to identify the ability owner, and we only draw
        outlines for the viewer/player who owns this selection.
        """
        pending = self._active_target_selection(state)
        if pending:
            effect_type = str(pending.get("effect_type", ""))
            for item in pending.get("valid_targets") or []:
                target_kind = str(item.get("target_kind", "character"))
                row_id = str(item.get("row_id", ""))
                if target_kind == "row":
                    rect = layout.row_rects.get(row_id)
                    if rect is None:
                        continue
                    phase_seed = sum(map(ord, row_id)) * 0.19
                elif target_kind == "character":
                    try:
                        slot_index = int(item.get("slot_index", -1))
                    except (TypeError, ValueError):
                        continue
                    slot_rect = layout.slot_rects.get((row_id, slot_index))
                    if slot_rect is None:
                        continue
                    # Frame the card itself, matching _draw_placed_card.
                    rect = self._portrait_card_rect(slot_rect)
                    phase_seed = (sum(map(ord, row_id)) + slot_index * 37) * 0.19
                else:
                    continue
                hovered = mouse_pos is not None and rect.collidepoint(mouse_pos)
                self._draw_selection_target_visual(
                    screen, rect, effect_type, now, hovered=hovered,
                    target_kind=target_kind, phase_seed=phase_seed,
                )
            return
        if state.get("pending_elimination"):
            # Do not show stale/local fallback targets while another viewer
            # owns a pending selection or the authoritative target set is empty.
            return

        # Legacy staged targets retain the original static purple outline.
        for row_id, slot_index in self.elimination_targets:
            slot_rect = layout.slot_rects.get((row_id, slot_index))
            if slot_rect is None:
                continue
            rect = self._portrait_card_rect(slot_rect)
            glow_rect = rect.inflate(10, 10)
            for spread, alpha in ((4, 120), (2, 200), (0, 255)):
                glow = pygame.Surface(
                    (glow_rect.width + spread * 2, glow_rect.height + spread * 2),
                    pygame.SRCALPHA,
                )
                pygame.draw.rect(
                    glow, (*DUO_ENERGY, alpha), glow.get_rect(),
                    width=3, border_radius=SLOT_RADIUS + spread,
                )
                screen.blit(glow, glow.get_rect(center=rect.center))
            pygame.draw.rect(
                screen, self.ELIMINATION_PURPLE, rect, width=3,
                border_radius=SLOT_RADIUS,
            )

    def _draw_elimination_announcement(self, screen, now: float, layout: BoardLayout) -> None:
        """Draw the large center-screen elimination announcement."""
        if self.elimination_announcement is None:
            return
        if self.elimination_announced_at is None:
            return
        elapsed = now - self.elimination_announced_at
        if elapsed >= self.ELIMINATION_ANNOUNCEMENT_SECONDS:
            self.elimination_announcement = None
            self.elimination_announced_at = 0.0
            return
        fade = 1.0
        if elapsed < 0.25:
            fade = elapsed / 0.25
        elif elapsed > self.ELIMINATION_ANNOUNCEMENT_SECONDS - 0.5:
            fade = (self.ELIMINATION_ANNOUNCEMENT_SECONDS - elapsed) / 0.5
        width, height = screen.get_size()
        font = get_font("title", 48)
        text = font.render(self.elimination_announcement, True, self.ELIMINATION_RED)
        shadow = font.render(self.elimination_announcement, True, (0, 0, 0))
        text_rect = text.get_rect(center=(width // 2, height // 3 - 40 - round(height * 0.09)))
        screen.blit(shadow, shadow.get_rect(center=(text_rect.centerx + 3, text_rect.centery + 3)))
        alpha_layer = pygame.Surface(text.get_size(), pygame.SRCALPHA)
        alpha_layer.fill((255, 255, 255, int(255 * fade)))
        text.blit(alpha_layer, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        screen.blit(text, text_rect)

    def _draw_elimination_cross(self, screen: pygame.Surface, rect: pygame.Rect) -> None:
        """Draw the persistent red elimination cross on a turned-over card."""
        x_thickness = max(4, rect.width // 40)
        x_margin = max(6, rect.width // 8)
        pygame.draw.line(
            screen, self.ELIMINATION_RED,
            (rect.left + x_margin, rect.top + x_margin),
            (rect.right - x_margin, rect.bottom - x_margin),
            x_thickness,
        )
        pygame.draw.line(
            screen, self.ELIMINATION_RED,
            (rect.right - x_margin, rect.top + x_margin),
            (rect.left + x_margin, rect.bottom - x_margin),
            x_thickness,
        )

    def _eliminated_card_for_slot(self, state: GameState, row_id: str, slot_index: int):
        """Return the card instance occupying an elimination-flash slot."""
        for row in state.rows:
            if row.id != row_id:
                continue
            if 0 <= slot_index < len(row.slots):
                return row.slots[slot_index]
        return None

    def _expire_elimination_flash(self, state: GameState, now: float) -> None:
        """Keep the temporary flip only while its authoritative card is eliminated."""
        if self.elimination_flash_card is None:
            return
        if self.elimination_flash_started is None:
            self.elimination_flash_card = None
            self._elimination_flash_is_selection_target = False
            return
        row_id, slot_index = self.elimination_flash_card
        card = self._eliminated_card_for_slot(state, row_id, slot_index)
        expired = now - self.elimination_flash_started >= self.ELIMINATION_FLIP_SECONDS
        if card is None or not getattr(card, "eliminated", False) or expired:
            self.elimination_flash_card = None
            self.elimination_flash_started = None
            self._elimination_flash_is_selection_target = False

    @staticmethod
    def _owner_card_outline(owner_id: str):
        """Return the ownership outline color independently of card state."""
        return P1 if owner_id == "player-1" else P2_CARD_OUTLINE

    def _draw_elimination_flash(
        self, screen, state: GameState, cards: dict[str, CardDefinition],
        layout: BoardLayout, now: float,
    ) -> None:
        """Animate an eliminated card flipping to its back on the card layer.

        The active flash slot is skipped by ``_draw_placed_card`` while this
        animation runs, so the original face-up card is never visible behind
        the turning card. Once the flip ends, the ordinary card layer renders
        the persistent back and cross from the card's authoritative state.
        """
        if self.elimination_flash_card is None:
            return
        if self.elimination_flash_started is None:
            return
        row_id, slot_index = self.elimination_flash_card
        rect = layout.slot_rects.get((row_id, slot_index))
        if rect is None:
            self.elimination_flash_card = None
            self.elimination_flash_started = None
            self._elimination_flash_is_selection_target = False
            return
        card_rect = self._portrait_card_rect(rect)
        slot = self._eliminated_card_for_slot(state, row_id, slot_index)
        if slot is None or not getattr(slot, "eliminated", False):
            self.elimination_flash_card = None
            self.elimination_flash_started = None
            self._elimination_flash_is_selection_target = False
            return
        owner_id = getattr(slot, "owner_id", "player-1") if slot is not None else "player-1"
        elapsed = now - self.elimination_flash_started
        # Phase A: flip to back (visual mirror of card_flip).
        if elapsed < self.ELIMINATION_FLIP_SECONDS:
            progress = min(1.0, elapsed / self.ELIMINATION_FLIP_SECONDS)
            flip_scale = max(0.04, abs(1.0 - progress * 2.0))
            scaled_width = max(2, round(card_rect.width * flip_scale))
            back = self._make_card_back_surface(card_rect.size, owner_id)
            surface = pygame.transform.smoothscale(back, (scaled_width, card_rect.height))
            screen.blit(surface, surface.get_rect(center=card_rect.center))
            pygame.draw.rect(
                screen, self._owner_card_outline(owner_id),
                card_rect, width=3, border_radius=6,
            )
            if getattr(slot, "ability_suppressed", False):
                self._draw_suppression_marker(
                    screen, card_rect, now,
                    str(slot.get("ability_suppression_style", "black")),
                )
            if self._elimination_flash_is_selection_target:
                self._draw_selected_elimination_outline(screen, card_rect)
            return

    def _draw_selected_elimination_outline(
        self, screen: pygame.Surface, rect: pygame.Rect,
    ) -> None:
        """Hold a steady purple edge on the chosen card during its flip."""
        margin = 16
        glow_rect = rect.inflate(margin * 2, margin * 2)
        glow = pygame.Surface(glow_rect.size, pygame.SRCALPHA)
        base_rect = pygame.Rect(margin, margin, rect.width, rect.height)
        for offset, alpha, width in ((5, 110, 9), (10, 62, 7)):
            pygame.draw.rect(
                glow, (225, 174, 255, alpha), base_rect.inflate(offset * 2, offset * 2),
                width=width, border_radius=SLOT_RADIUS + offset,
            )
        screen.blit(glow, glow_rect.topleft)
        pygame.draw.rect(
            screen, (225, 174, 255), rect, width=5,
            border_radius=SLOT_RADIUS,
        )

    def _draw_elimination_failed(self, screen, now: float) -> None:
        """Draw failure feedback at the same height as elimination announcements."""
        if self.elimination_failed_message is None:
            return
        if self.elimination_failed_at is None:
            return
        elapsed = now - self.elimination_failed_at
        if elapsed >= self.ELIMINATION_FAILED_SECONDS:
            self.elimination_failed_message = None
            self.elimination_failed_at = 0.0
            return
        fade = 1.0
        if elapsed < 0.25:
            fade = elapsed / 0.25
        elif elapsed > self.ELIMINATION_FAILED_SECONDS - 0.5:
            fade = (self.ELIMINATION_FAILED_SECONDS - elapsed) / 0.5
        width, height = screen.get_size()
        font = get_font("title", 44)
        text = font.render(self.elimination_failed_message, True, self.ELIMINATION_RED)
        shadow = font.render(self.elimination_failed_message, True, (0, 0, 0))
        text_rect = text.get_rect(
            center=(width // 2, height // 3 - 40 - round(height * 0.09))
        )
        screen.blit(shadow, shadow.get_rect(center=(text_rect.centerx + 3, text_rect.centery + 3)))
        alpha_layer = pygame.Surface(text.get_size(), pygame.SRCALPHA)
        alpha_layer.fill((255, 255, 255, int(255 * fade)))
        text.blit(alpha_layer, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        screen.blit(text, text_rect)

    def _draw_background(self, screen) -> None:
        if self.background is None:
            return
        size = screen.get_size()
        if self._background_cache_size != size or self._background_cache is None:
            fit = self.assets.scale_to_fit(self.background, size)
            source_width, source_height = self.background.get_size()
            scale = max(size[0] / source_width, size[1] / source_height)
            cover_size = (
                max(size[0], round(source_width * scale)),
                max(size[1], round(source_height * scale)),
            )
            cover = pygame.transform.smoothscale(self.background, cover_size)
            cover_rect = cover.get_rect(center=(size[0] // 2, size[1] // 2))
            crop = pygame.Rect(-cover_rect.x, -cover_rect.y, size[0], size[1])
            cover = cover.subsurface(crop).copy()
            self._background_cache_size = size
            self._background_cache = (cover, fit)
        cover, fit = self._background_cache
        screen.blit(cover, (0, 0))
        screen.blit(fit, fit.get_rect(center=screen.get_rect().center))
        screen.blit(self._overlay(screen.get_size(), (8, 12, 22, 145)), (0, 0))

    def _draw_menu_background(self, screen) -> None:
        """Draw Season 1 art with a restrained, resolution-aware lobby ambience."""
        if self.menu_background is None:
            screen.fill(BG)
            return
        size = screen.get_size()
        if self._menu_bg_cache_size != size or self._menu_bg_cache is None:
            self._menu_bg_cache = self.assets.scale_cover(self.menu_background, size)
            self._menu_bg_cache_size = size
        now = time.monotonic()
        background = self._menu_bg_cache
        screen.blit(background, (0, 0))
        source_width, source_height = self.menu_background.get_size()
        scale = max(background.get_width() / source_width, background.get_height() / source_height)
        crop_x = (source_width * scale - background.get_width()) / 2
        crop_y = (source_height * scale - background.get_height()) / 2

        def art_point(x_ratio: float, y_ratio: float) -> tuple[int, int]:
            """Map a point in the original art through its static cover-crop."""
            return (
                round(x_ratio * source_width * scale - crop_x),
                round(y_ratio * source_height * scale - crop_y),
            )

        if self._menu_effect_layer_size != size or self._menu_effect_layer is None:
            self._menu_effect_layer = pygame.Surface(size, pygame.SRCALPHA)
            self._menu_effect_layer_size = size
        effects = self._menu_effect_layer
        effects.fill((0, 0, 0, 0))
        width, height = size

        # Purple moonlight breathes very gently around the already painted moon.
        moon = art_point(0.713, 0.085)
        pulse = 0.5 + 0.5 * math.sin(now * 0.42)
        moon_radius = max(12, round(min(size) * 0.074))
        pygame.draw.circle(effects, (148, 83, 255, round(5 + pulse * 7)), moon, moon_radius)
        pygame.draw.circle(
            effects, (211, 166, 255, round(10 + pulse * 8)), moon,
            round(moon_radius * 0.80), width=max(1, round(min(size) * 0.0015)),
        )

        # A low-alpha diagonal glint travels across the baked-in VeilBound title.
        title_left = art_point(0.31, 0.145)[0]
        title_right = art_point(0.68, 0.145)[0]
        title_top = art_point(0.31, 0.145)[1]
        title_bottom = art_point(0.31, 0.285)[1]
        title_phase = (now / 13.0) % 1.0
        shimmer_x = title_left + round((title_right - title_left) * title_phase)
        effects.set_clip(pygame.Rect(title_left, title_top, title_right - title_left, title_bottom - title_top))
        pygame.draw.line(
            effects, (255, 225, 164, 17),
            (shimmer_x - round(width * 0.035), title_bottom),
            (shimmer_x + round(width * 0.035), title_top),
            max(1, round(width * 0.003)),
        )
        effects.set_clip(None)

        # Two barely visible drifting haze wisps suggest movement in the clouds
        # without repainting or masking any part of the provided background.
        for index, (base_x, base_y, span) in enumerate(((0.18, 0.43, 0.045), (0.82, 0.39, 0.035))):
            cx, cy = art_point(base_x + math.sin(now * 0.035 + index) * span, base_y)
            pygame.draw.ellipse(
                effects, (168, 127, 255, 5),
                pygame.Rect(cx - round(width * 0.07), cy - round(height * 0.012),
                            round(width * 0.14), round(height * 0.024)),
            )

        # Lantern light breathes at the two foreground lanterns.
        for index, x_ratio in enumerate((0.083, 0.918)):
            center = art_point(x_ratio, 0.72)
            lantern_pulse = 0.5 + 0.5 * math.sin(now * 1.35 + index * 1.7)
            pygame.draw.circle(
                effects, (255, 176, 74, round(8 + lantern_pulse * 8)),
                center, max(4, round(min(size) * 0.022)),
            )

        # Fine moving highlights follow the existing waterfall paths.
        falls = ((0.155, 0.585, 0.72), (0.255, 0.625, 0.745), (0.714, 0.58, 0.75), (0.775, 0.635, 0.76))
        for index, (x_ratio, top_ratio, bottom_ratio) in enumerate(falls):
            top_y = art_point(x_ratio, top_ratio)[1]
            bottom_y = art_point(x_ratio, bottom_ratio)[1]
            path_height = bottom_y - top_y
            for stream in range(2):
                travel = (now * (0.07 + stream * 0.012) + stream * 0.48 + index * 0.19) % 1.0
                x = art_point(x_ratio, top_ratio)[0] + stream * 2 - 1
                y = round(top_y + path_height * travel)
                pygame.draw.line(
                    effects, (203, 199, 255, 24), (x, y),
                    (x + 1, min(bottom_y, y + max(5, round(height * 0.045)))),
                    1,
                )

        # Small highlights glide along the wet arena floor.
        for index in range(5):
            travel = (now * 0.026 + index * 0.21) % 1.0
            x, y = art_point(
                0.20 + travel * 0.60,
                0.86 + 0.025 * math.sin(travel * math.tau),
            )
            pygame.draw.ellipse(
                effects, (205, 174, 255, 28),
                pygame.Rect(x - max(2, round(width * 0.009)), y,
                            max(4, round(width * 0.018)), max(1, round(height * 0.0018))),
            )

        # Seeded particles drift deterministically, so no per-frame randomness
        # or unbounded particle creation is used.
        for particle in self._menu_ambient_particles:
            phase = particle["phase"]
            x_ratio = particle["x"] + math.sin(now * 0.24 + phase) * particle["drift"]
            y_ratio = (particle["y"] - now * particle["speed"]) % 1.04 - 0.02
            x = round((x_ratio % 1.0) * width)
            y = round(y_ratio * height)
            if particle["energy"]:
                color = (195, 150, 255, 64)
                pygame.draw.circle(effects, (135, 90, 235, 17), (x, y), 4)
            else:
                color = (238, 218, 255, 48)
            pygame.draw.circle(effects, color, (x, y), particle["size"])

        for petal in self._menu_blossoms:
            phase = petal["phase"]
            x_ratio = (petal["x"] + math.sin(now * 0.42 + phase) * petal["drift"]) % 1.0
            y_ratio = (petal["y"] + now * petal["speed"]) % 1.08 - 0.04
            x = x_ratio * width
            y = y_ratio * height
            radius = petal["size"] * min(size) / 900
            angle = now * 0.7 + phase
            along_x, along_y = math.cos(angle) * radius, math.sin(angle) * radius * 0.55
            across_x, across_y = -math.sin(angle) * radius * 0.38, math.cos(angle) * radius * 0.38
            points = [
                (round(x - along_x), round(y - along_y)),
                (round(x + across_x), round(y + across_y)),
                (round(x + along_x), round(y + along_y)),
                (round(x - across_x), round(y - across_y)),
            ]
            pygame.draw.polygon(effects, (244, 111, 164, 76), points)

        screen.blit(effects, (0, 0))

    def _overlay(self, size: tuple[int, int], color: tuple[int, int, int, int]) -> pygame.Surface:
        key = (*size, *color)
        overlay = self._overlay_cache.get(key)
        if overlay is None:
            overlay = pygame.Surface(size, pygame.SRCALPHA)
            overlay.fill(color)
            self._overlay_cache[key] = overlay
        return overlay

    def _draw_floating_texts(self, screen, layout) -> None:
        """Draw timed floating ability text (e.g. "+3") above its target card.

        Presentation only: the authority already applied the point change when
        the event fired. Expired entries are pruned during drawing and each
        entry has an independent lifetime, so future overlaps stack cleanly.
        """
        if not self.floating_texts:
            return
        now = time.monotonic()
        self.floating_texts = [
            item for item in self.floating_texts
            if now - item["started"] < FLOATING_TEXT_SECONDS
        ]
        for item in self.floating_texts:
            rect = layout.slot_rects.get((item["row_id"], item["slot_index"]))
            if rect is None:
                continue
            progress = min(1.0, (now - item["started"]) / FLOATING_TEXT_SECONDS)
            rise = int(FLOATING_TEXT_RISE_PX * progress)
            if progress < 0.55:
                alpha = 255
            else:
                alpha = max(0, int(255 * (1.0 - (progress - 0.55) / 0.45)))
            rendered = render_text(item["text"], "number_capacity", shadow=True)
            if alpha < 255:
                layer = pygame.Surface(rendered.get_size(), pygame.SRCALPHA)
                layer.fill((255, 255, 255, alpha))
                rendered.blit(layer, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            screen.blit(rendered, rendered.get_rect(center=(rect.centerx, rect.y - 20 - rise)))

    def _draw_trunks_bonus_animations(self, screen, layout, now: float) -> None:
        """Brief blue-gold energy pulse when Saiyan Resolve grants points."""
        duration = 0.85
        self.trunks_bonus_animations = [
            item for item in self.trunks_bonus_animations
            if now - item["started"] < duration
        ]
        for item in self.trunks_bonus_animations:
            rect = layout.slot_rects.get((item["row_id"], item["slot_index"]))
            if rect is None:
                continue
            progress = max(0.0, min(1.0, (now - item["started"]) / duration))
            radius = int(18 + 34 * progress)
            alpha = int(190 * (1.0 - progress))
            layer = pygame.Surface((radius * 2 + 8, radius * 2 + 8), pygame.SRCALPHA)
            center = (radius + 4, radius + 4)
            pygame.draw.circle(layer, (80, 190, 255, alpha), center, radius, width=3)
            pygame.draw.circle(layer, (255, 205, 90, max(0, alpha - 45)), center, max(5, radius - 7), width=2)
            screen.blit(layer, layer.get_rect(center=rect.center))

    def _draw_whis_resurrection_animations(self, screen, layout, now: float) -> None:
        """Draw the localized celestial burst for each Whis revival event."""
        duration = 1.65
        self.whis_resurrection_animations = [
            item for item in self.whis_resurrection_animations
            if now - item["started"] < duration
        ]
        if not self.whis_resurrection_animations:
            return
        layer = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
        for item in self.whis_resurrection_animations:
            rect = layout.slot_rects.get((item["row_id"], item["slot_index"]))
            if rect is None:
                continue
            progress = min(1.0, max(0.0, (now - item["started"]) / duration))
            fade = min(1.0, (1.0 - progress) * 2.1)
            center = rect.center
            orbit_rect = rect.inflate(max(18, round(rect.width * 0.32)), max(18, round(rect.height * 0.32)))
            aura_alpha = round(95 * fade * min(1.0, progress * 4.0 + 0.18))
            pygame.draw.rect(
                layer, (74, 221, 255, aura_alpha), orbit_rect,
                width=max(2, round(min(rect.size) * 0.035)), border_radius=12,
            )
            pygame.draw.arc(
                layer, (183, 244, 255, round(180 * fade)), orbit_rect,
                now * 1.4, now * 1.4 + math.pi * 1.35,
                max(1, round(min(rect.size) * 0.025)),
            )
            pygame.draw.arc(
                layer, (255, 224, 155, round(135 * fade)), orbit_rect.inflate(8, 8),
                -now * 1.1, -now * 1.1 + math.pi * 0.95,
                max(1, round(min(rect.size) * 0.018)),
            )

            # Cyan and white motes spiral around the card, then rise and fade.
            for particle in range(14):
                angle = particle * (math.tau / 14) + progress * math.tau * 1.8
                orbit_x = rect.width * (0.57 + 0.09 * math.sin(progress * math.tau + particle))
                orbit_y = rect.height * (0.58 + 0.10 * math.cos(progress * math.tau + particle * 0.7))
                lift = progress * rect.height * (0.18 + (particle % 4) * 0.035)
                x = round(center[0] + math.cos(angle) * orbit_x)
                y = round(center[1] + math.sin(angle) * orbit_y - lift)
                particle_alpha = round(220 * fade * (0.45 + 0.55 * abs(math.sin(progress * math.pi + particle))))
                color = (
                    (255, 244, 206, particle_alpha) if particle % 5 == 0
                    else (231, 252, 255, particle_alpha) if particle % 3 == 0
                    else (103, 220, 255, particle_alpha)
                )
                pygame.draw.circle(layer, color, (x, y), 2 if particle % 3 == 0 else 1)

            # A soft golden-white bloom grows around the return, then clears.
            bloom = max(0.0, 1.0 - abs(progress - 0.56) / 0.28)
            if bloom > 0:
                glow_radius = round(max(rect.size) * (0.48 + bloom * 0.42))
                glow_alpha = round(32 * bloom)
                pygame.draw.circle(layer, (255, 246, 211, glow_alpha), center, glow_radius, width=3)
                pygame.draw.circle(
                    layer, (102, 222, 255, round(44 * bloom)), center,
                    round(glow_radius * 0.84), width=2,
                )

            # The resurrection crest expands outward after the flip begins.
            burst = min(1.0, max(0.0, (progress - 0.48) / 0.38))
            if burst > 0.0:
                radius = round(max(rect.size) * (0.18 + burst * 0.78))
                burst_alpha = round(150 * fade * (1.0 - burst * 0.42))
                pygame.draw.circle(
                    layer, (244, 253, 255, burst_alpha), center, radius,
                    width=max(1, round(min(rect.size) * 0.018)),
                )
                pygame.draw.circle(
                    layer, (255, 218, 133, round(burst_alpha * 0.72)), center,
                    max(1, round(radius * 0.72)),
                    width=max(1, round(min(rect.size) * 0.012)),
                )
        screen.blit(layer, (0, 0))

    def _draw_whis_healing_animations(self, screen, state, layout, now: float) -> None:
        """Send a celestial pulse across the board when two or more allies healed."""
        duration = 1.25
        self.whis_healing_animations = [
            item for item in self.whis_healing_animations
            if now - item["started"] < duration
        ]
        if not self.whis_healing_animations:
            return
        layer = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
        width, height = screen.get_size()
        for item in self.whis_healing_animations:
            progress = min(1.0, max(0.0, (now - item["started"]) / duration))
            fade = 1.0 - progress
            source_rect = layout.slot_rects.get((item["row_id"], item["slot_index"]))
            if source_rect is None:
                continue
            origin = source_rect.center
            healed_rects = []
            healed_ids = set(item["target_card_ids"])
            for row in state.rows:
                for slot_index, card in enumerate(row.slots):
                    if card is None or card.id not in healed_ids or getattr(card, "eliminated", False):
                        continue
                    rect = layout.slot_rects.get((row.id, slot_index))
                    if rect is not None:
                        healed_rects.append(rect)

            # Two sweeping rings carry Whis's light outward across the board.
            for ring_index in range(2):
                ring_progress = min(1.0, max(0.0, progress * 1.25 - ring_index * 0.18))
                if ring_progress <= 0:
                    continue
                ring_width = round(max(width, height) * (0.08 + ring_progress * 0.75))
                ring_height = round(max(width, height) * (0.05 + ring_progress * 0.43))
                ring_rect = pygame.Rect(0, 0, ring_width, ring_height)
                ring_rect.center = origin
                alpha = round(115 * fade * (1.0 - ring_index * 0.28))
                pygame.draw.ellipse(
                    layer, (87, 222, 255, alpha), ring_rect,
                    width=max(2, round(min(width, height) * 0.004)),
                )
                gold_rect = ring_rect.inflate(-max(4, round(width * 0.018)), -max(4, round(height * 0.018)))
                if gold_rect.width > 0 and gold_rect.height > 0:
                    pygame.draw.ellipse(
                        layer, (255, 233, 177, round(alpha * 0.56)), gold_rect,
                        width=max(1, round(min(width, height) * 0.002)),
                    )

            for target_index, target_rect in enumerate(healed_rects):
                target = target_rect.center
                pygame.draw.line(
                    layer, (124, 225, 255, round(58 * fade)), origin, target,
                    max(1, round(min(width, height) * 0.002)),
                )
                halo = target_rect.inflate(12, 12)
                pygame.draw.rect(
                    layer, (170, 243, 255, round(145 * fade)), halo,
                    width=max(2, round(min(target_rect.size) * 0.025)), border_radius=9,
                )
                pygame.draw.arc(
                    layer, (255, 231, 167, round(185 * fade)), halo.inflate(7, 7),
                    now * 2.0 + target_index, now * 2.0 + target_index + math.pi * 1.25,
                    max(1, round(min(target_rect.size) * 0.018)),
                )
                for mote in range(4):
                    travel = (progress * 1.35 + mote * 0.24 + target_index * 0.13) % 1.0
                    x = round(origin[0] + (target[0] - origin[0]) * travel)
                    y = round(origin[1] + (target[1] - origin[1]) * travel)
                    pygame.draw.circle(
                        layer, (230, 251, 255, round(210 * fade)), (x, y),
                        2 if mote % 2 == 0 else 1,
                    )
        screen.blit(layer, (0, 0))

    def _reveal_view_active(self, state) -> bool:
        return (
            state.phase in {GamePhase.PLACEMENT, GamePhase.REVEAL, GamePhase.ROW_SCORING}
            and self.reveal_card is not None
            and time.monotonic() < self.reveal_card_until
        )

    def _draw_card_spotlight(self, screen, state, mouse_pos) -> None:
        if state.phase not in {GamePhase.PLACEMENT, GamePhase.REVEAL, GamePhase.ROW_SCORING} or self.reveal_card is None:
            return
        now = time.monotonic()
        if now < self.reveal_card_visible_after or now >= self.reveal_card_until:
            return

        width, height = screen.get_size()
        screen.blit(self._overlay(screen.get_size(), (4, 8, 17, 168)), (0, 0))

        full_card_art = str(self.reveal_card.get("full_card_art", ""))
        full_art = None
        if full_card_art:
            full_art = self.assets.load_image(full_card_art)
        has_full_art = full_art is not None

        card_max_w = 340
        card_max_h = 500
        card_w = min(card_max_w, width - 48)
        card_h = min(card_max_h, int(card_w * CARD_HEIGHT_RATIO / CARD_WIDTH_RATIO))
        card_rect = pygame.Rect(0, 0, card_w, card_h)

        effect_name = str(self.reveal_card.get("effect_name", "Effect"))
        description = str(self.reveal_card.get("effect_text", ""))
        name_font = get_font("ability_title")
        desc_font = get_font("ability_desc")

        name_lines = wrap_text(effect_name.upper(), "ability_title", 260)
        name_line_h = name_font.get_linesize()
        name_block_h = len(name_lines) * name_line_h

        desc_lines = wrap_text(description, "ability_desc", 260)
        desc_line_h = desc_font.get_linesize()
        desc_block_h = len(desc_lines) * desc_line_h

        panel_pad = 24
        panel_top_pad = 20
        panel_bottom_pad = 20
        name_max_w = max((name_font.size(line)[0] for line in name_lines), default=0)
        desc_max_w = max((desc_font.size(line)[0] for line in desc_lines), default=0)
        ability_w = min(320, max(260, panel_pad * 2 + max(name_max_w, desc_max_w)))
        ability_h = panel_top_pad + name_block_h + 20 + desc_block_h + panel_bottom_pad
        ability_h = min(420, max(170, ability_h))
        ability_rect = pygame.Rect(0, 0, ability_w, ability_h)

        gap = 24
        combined_w = ability_w + gap + card_w
        combined_h = max(ability_h, card_h)

        if combined_w > width - 48:
            combined_w = max(card_w, ability_w)
            combined_h = card_h + gap + ability_h
            side_by_side = False
        else:
            side_by_side = True

        group_rect = pygame.Rect(0, 0, combined_w, combined_h)
        group_rect.center = (width // 2, height // 2)

        card_rect.x = group_rect.x
        card_rect.y = group_rect.y + (combined_h - card_h) // 2
        if side_by_side:
            ability_rect.x = card_rect.right + gap
            ability_rect.y = group_rect.y + (combined_h - ability_h) // 2
        else:
            card_rect.y = group_rect.y
            ability_rect.x = group_rect.x
            ability_rect.y = card_rect.bottom + gap

        panel_surface = pygame.Surface((ability_w, ability_h), pygame.SRCALPHA)
        panel_surface.fill(PANEL)
        pygame.draw.rect(
            panel_surface, TEXT_MUTED_GOLD, panel_surface.get_rect(), width=2, border_radius=8,
        )
        screen.blit(panel_surface, ability_rect)

        name_y = ability_rect.y + 20
        for line in name_lines:
            name_surf = name_font.render(line.upper(), True, GOLD)
            screen.blit(name_surf, name_surf.get_rect(centerx=ability_rect.centerx, y=name_y))
            name_y += name_line_h

        divider_y = name_y + 10
        divider_rect = pygame.Rect(
            ability_rect.x + 24, divider_y, ability_w - 48, 1,
        )
        pygame.draw.rect(screen, TEXT_MUTED_GOLD, divider_rect)

        desc_y = divider_y + 16
        max_desc_lines = max(1, (ability_rect.bottom - desc_y - 10) // desc_line_h)
        if len(desc_lines) > max_desc_lines:
            desc_lines = desc_lines[:max_desc_lines]
            desc_lines[-1] = desc_lines[-1].rstrip(" .") + "..."
        for line in desc_lines:
            desc_surf = desc_font.render(line, True, CREAM)
            screen.blit(desc_surf, desc_surf.get_rect(centerx=ability_rect.centerx, y=desc_y))
            desc_y += desc_line_h

        if has_full_art:
            key = ("full_card", full_card_art, card_rect.size)
            cached_full_art = self._spotlight_portrait_cache.get(key)
            if cached_full_art is None:
                cached_full_art = self.assets.scale_to_fit(full_art, card_rect.size)
                self._spotlight_portrait_cache[key] = cached_full_art
            screen.blit(cached_full_art, card_rect)
        else:
            screen.blit(self._card_front_surface(card_rect.size), card_rect)
            art_rect = pygame.Rect(card_rect.x + 10, card_rect.y + 10, card_w - 20, int(card_h * 0.45))
            image_path = str(self.reveal_card.get("image", ""))
            portrait_key = (image_path, art_rect.size)
            portrait = self._spotlight_portrait_cache.get(portrait_key)
            if portrait is None:
                art = self.assets.load_character(image_path)
                portrait = self.assets.scale_to_fit(art, art_rect.size)
                self._spotlight_portrait_cache[portrait_key] = portrait
            screen.blit(portrait, portrait.get_rect(center=art_rect.center))
            name = str(self.reveal_card.get("name", "Unknown character"))
            portrait_bottom = art_rect.bottom
            name_y = portrait_bottom + 24
            name_lines = self._card_name_lines(name, card_w - 20)
            name_extra = self._draw_centered_name_lines(
                screen, name_lines, (card_rect.centerx, name_y), card_w - 20,
                variant="black",
            )

    def _draw_effect_tooltip(self, screen, state, cards, layout, mouse_pos):
        hovered = None
        hovered_rect: pygame.Rect | None = None
        hand_player = state.player(state.viewer_id) if state.viewer_id else state.active_player
        if state.phase == GamePhase.PLACEMENT and self.turn_confirmation_player_id is None:
            if getattr(layout, "debug_mode", False) and self.debug_mode:
                from veilbound.client.layout import clamp_hand_scroll, debug_shifted_rects

                scroll = clamp_hand_scroll(self.debug_scroll, layout.debug_max_scroll)
                origin = [
                    pygame.Rect(rect.x - round(scroll), rect.y, rect.width, rect.height)
                    for rect in layout.hand_cards
                ]
                _, hover_index = layout.debug_visible_cards(scroll, mouse_pos)
                drawn = debug_shifted_rects(origin, hover_index)
                if hover_index is not None and hover_index < len(hand_player.hand):
                    hovered = hand_player.hand[hover_index]
                    hovered_rect = drawn[hover_index]
                clip = screen.get_rect()
            else:
                from veilbound.client.layout import debug_shifted_rects, debug_hover_index

                hover_index = debug_hover_index(layout.hand_cards, mouse_pos)
                shifted = debug_shifted_rects(list(layout.hand_cards), hover_index)
                if hover_index is not None and hover_index < len(hand_player.hand):
                    hovered = hand_player.hand[hover_index]
                    hovered_rect = shifted[hover_index]
                clip = screen.get_rect()
        else:
            clip = screen.get_rect()
        if hovered is None and hovered_rect is None:
            if mouse_pos is not None:
                for row in state.rows:
                    for slot_index, card in enumerate(row.slots):
                        if (card is not None and card.revealed
                                and layout.slot_rects[(row.id, slot_index)].collidepoint(mouse_pos)):
                            hovered = card
                            hovered_rect = layout.slot_rects[(row.id, slot_index)]
                            break
                    if hovered is not None:
                        break
        if hovered is not None and hovered_rect is not None:
            avoid = [
                rect for rect in layout.slot_rects.values()
                if not rect.colliderect(hovered_rect) and rect.colliderect(screen.get_rect())
            ]
            if not (getattr(layout, "debug_mode", False) and self.debug_mode):
                avoid.extend(
                    rect for rect in layout.hand_cards
                    if not rect.colliderect(hovered_rect) and rect.colliderect(screen.get_rect())
                )
            self.ability_panel.draw(
                screen, cards[hovered.definition_id], hovered, hovered_rect, clip, avoid,
            )
        else:
            self.ability_panel.draw(screen, None, None, None, clip)
    def _displayed_row_scores(self, row: BattleRow) -> dict[str, int]:
        if row.id != self.score_animation_row_id or not row.scored:
            return row.scores
        duration = self.animation_ms["score_count_ms"] / 1000
        if duration <= 0:
            progress = 1.0
        else:
            progress = min(1.0, max(0.0, (time.monotonic() - self.score_animation_started) / duration))
        return {player_id: int(score * progress) for player_id, score in row.scores.items()}

    def _draw_row_comparison(self, screen, state) -> None:
        if state.phase != GamePhase.ROW_RESULTS or state.current_row is None:
            return
        row = state.current_row
        scores = self._displayed_row_scores(row)
        width = min(600, screen.get_width() - 40)
        panel = pygame.Rect(0, 0, width, 196)
        panel.center = (screen.get_width() // 2, screen.get_height() // 2)
        pygame.draw.rect(screen, PANEL, panel, border_radius=16)
        pygame.draw.rect(screen, GOLD, panel, width=3, border_radius=16)
        self._text_center(screen, f"ROW {row.number} SCORE", (panel.centerx, panel.y + 24), "label_small")
        for index, player in enumerate(state.players):
            x = panel.x + panel.width // 4 + index * panel.width // 2
            self._text_center_fit(screen, player.name, (x, panel.y + 67), "ui", panel.width // 2 - 40)
            self._text_center(screen, str(scores[player.id]), (x, panel.y + 121), "number_row_score")
        if scores != row.scores:
            result_text = "Comparing scores..."
        elif row.winner_id is None:
            result_text = "Tie: no row points awarded"
        else:
            result_text = f"{state.player(row.winner_id).name} wins {row.awarded_points} row points"
        self._text_center_fit(screen, result_text, (panel.centerx, panel.y + 173), "label_small", panel.width - 28)

    @staticmethod
    def _new_luffy_haki_animation(data: dict, started: float) -> dict:
        owner_id = str(data.get("owner_card_id", "luffy"))
        seed = sum((index + 1) * ord(char) for index, char in enumerate(owner_id))
        rng = random.Random(seed)
        branches = []
        for _ in range(13):
            edge = rng.randrange(4)
            if edge == 0:
                point = (rng.random(), 0.0)
            elif edge == 1:
                point = (1.0, rng.random())
            elif edge == 2:
                point = (rng.random(), 1.0)
            else:
                point = (0.0, rng.random())
            points = [point]
            for step in range(1, 6):
                progress = step / 6
                x = point[0] + (0.5 - point[0]) * progress + rng.uniform(-0.06, 0.06)
                y = point[1] + (0.5 - point[1]) * progress + rng.uniform(-0.06, 0.06)
                points.append((min(1.0, max(0.0, x)), min(1.0, max(0.0, y))))
            points.append((rng.uniform(0.25, 0.75), rng.uniform(0.25, 0.75)))
            branches.append({"points": points, "delay": rng.uniform(0.0, 0.54), "seed": rng.random()})
        return {
            "row_id": str(data.get("row_id", "")),
            "slot_index": int(data.get("slot_index", -1)),
            "owner_card_id": owner_id,
            "started": started,
            "branches": branches,
        }

    @property
    def sogeking_cinematic_pending(self) -> bool:
        return self.sogeking_cinematic is not None

    @property
    def sogeking_cinematic_stage(self) -> str | None:
        return self.sogeking_cinematic.get("stage") if self.sogeking_cinematic else None

    def _begin_sogeking_cinematic(self, data: dict, started: float) -> None:
        cinematic_id = str(data.get("cinematic_id", ""))
        if not cinematic_id:
            return
        if (self.sogeking_cinematic is not None
                and self.sogeking_cinematic.get("cinematic_id") == cinematic_id):
            return
        self._clear_sogeking_cinematic()
        self.sogeking_cinematic = {
            "cinematic_id": cinematic_id,
            "row_id": str(data.get("row_id", "")),
            "usopp_card_id": str(data.get("usopp_card_id", "")),
            "started": started,
            "stage": "tension",
            "impact_started": 0.0,
        }
        try:
            self.sogeking_video = VideoPlayer(
                self.assets.resolve("assets/Cut_Scenes/Luffy_Usopp_CutScene.mp4"),
            )
        except (OSError, ValueError) as exc:
            self.sogeking_video = None
            self.notice = f"Sogeking cutscene unavailable: {exc}"
            self.notice_until = started + 5.0
        self.sogeking_cinematic_complete = False

    def _clear_sogeking_cinematic(self) -> None:
        if self.sogeking_video is not None:
            self.sogeking_video.close()
        self.sogeking_video = None
        self.sogeking_cinematic = None
        self.sogeking_cinematic_complete = False

    def _sync_sogeking_cinematic(self, state: GameState, now: float) -> None:
        pending = state.get("pending_sogeking_cinematic") or None
        if pending:
            if (self.sogeking_cinematic is None
                    or self.sogeking_cinematic.get("cinematic_id")
                    != str(pending.get("cinematic_id", ""))):
                self._begin_sogeking_cinematic(pending, now)
        elif self.sogeking_cinematic is not None:
            self._clear_sogeking_cinematic()
            return
        cinematic = self.sogeking_cinematic
        if cinematic is None:
            return
        elapsed = max(0.0, now - cinematic["started"])
        if cinematic["stage"] == "tension" and elapsed >= 2.35:
            cinematic["stage"] = "footage"
            if self.sogeking_video is not None:
                self.sogeking_video.start(now)
        if cinematic["stage"] == "footage":
            if self.sogeking_video is None:
                cinematic["stage"] = "impact"
                cinematic["impact_started"] = now
            else:
                self.sogeking_video.update(now)
                if self.sogeking_video.finished:
                    cinematic["stage"] = "impact"
                    cinematic["impact_started"] = now
        elif (cinematic["stage"] == "impact"
              and now - cinematic["impact_started"] >= 2.5):
            self.sogeking_cinematic_complete = True

    def _draw_sogeking_cinematic(
        self, display, scene, state, cards, layout, now: float,
    ) -> None:
        cinematic = self.sogeking_cinematic
        if cinematic is None:
            return
        stage = cinematic["stage"]
        if stage == "footage":
            if self.sogeking_video is not None:
                self.sogeking_video.draw(display)
            else:
                display.fill((5, 7, 19))
            # Fade smoothly from Usopp's spotlight into the first video frame.
            transition = min(1.0, max(0.0, (now - cinematic["started"] - 2.35) / 0.35))
            if transition < 1.0:
                veil = pygame.Surface(display.get_size(), pygame.SRCALPHA)
                veil.fill((0, 0, 8, round(255 * (1.0 - transition))))
                display.blit(veil, (0, 0))
            return

        width, height = display.get_size()
        row = next((item for item in state.rows if item.id == cinematic["row_id"]), None)
        if row is None:
            display.blit(scene, (0, 0))
            return
        actors: dict[str, tuple[int, pygame.Rect]] = {}
        opposing_rects: list[pygame.Rect] = []
        for slot_index, card in enumerate(row.slots):
            if card is None:
                continue
            slot_rect = layout.slot_rects.get((row.id, slot_index))
            if slot_rect is None:
                continue
            card_rect = self._portrait_card_rect(slot_rect)
            if card.id == cinematic["usopp_card_id"]:
                actors["usopp"] = (slot_index, card_rect)
        actor_owner = next((
            card.owner_id for card in row.slots
            if card is not None and card.id == cinematic["usopp_card_id"]
        ), None)
        for slot_index, card in enumerate(row.slots):
            if card is None or card.owner_id == actor_owner:
                continue
            slot_rect = layout.slot_rects.get((row.id, slot_index))
            if slot_rect is not None:
                opposing_rects.append(self._portrait_card_rect(slot_rect))

        elapsed = max(0.0, now - cinematic["started"])
        zoom = 1.0
        focus_x, focus_y = width * 0.5, height * 0.5
        if stage == "tension":
            progress = min(1.0, elapsed / 2.35)
            ease = progress * progress * (3.0 - 2.0 * progress)
            zoom = 1.0 + 0.12 * ease
            if actors:
                focus_x = sum(rect.centerx for _, rect in actors.values()) / len(actors)
                focus_y = sum(rect.centery for _, rect in actors.values()) / len(actors)
        offset_x = round(width * 0.5 - focus_x * zoom)
        offset_y = round(height * 0.5 - focus_y * zoom)
        impact_elapsed = max(0.0, now - cinematic.get("impact_started", now))
        if stage == "impact":
            fade = max(0.0, 1.0 - impact_elapsed / 2.5)
            amplitude = 13.0 * fade * min(1.0, impact_elapsed / 0.12)
            offset_x = round(math.sin(impact_elapsed * 71.0) * amplitude)
            offset_y = round(math.cos(impact_elapsed * 83.0) * amplitude * 0.62)

        scaled_scene = scene if zoom == 1.0 else pygame.transform.smoothscale(
            scene, (max(1, round(width * zoom)), max(1, round(height * zoom))),
        )
        display.fill((3, 4, 13))
        display.blit(scaled_scene, (offset_x, offset_y))
        if stage == "impact" and impact_elapsed < 0.24:
            transition = max(0.0, 1.0 - impact_elapsed / 0.24)
            veil = pygame.Surface((width, height), pygame.SRCALPHA)
            veil.fill((232, 240, 255, round(150 * transition)))
            display.blit(veil, (0, 0))

        def transformed(rect: pygame.Rect) -> pygame.Rect:
            return pygame.Rect(
                round(rect.x * zoom + offset_x), round(rect.y * zoom + offset_y),
                max(1, round(rect.width * zoom)), max(1, round(rect.height * zoom)),
            )

        if stage == "tension":
            dim_alpha = round(24 + 218 * min(1.0, elapsed / 2.35))
            dim = pygame.Surface((width, height), pygame.SRCALPHA)
            dim.fill((0, 0, 8, dim_alpha))
            display.blit(dim, (0, 0))
            glow_layer = pygame.Surface((width, height), pygame.SRCALPHA)
            for _actor_name, (_slot, source_rect) in actors.items():
                target_rect = transformed(source_rect)
                pulse = 0.88 + 0.12 * math.sin(now * 3.2)
                center = target_rect.center
                glow_color = (92, 180, 255, round(90 * pulse))
                pygame.draw.ellipse(glow_layer, glow_color, target_rect.inflate(34, 54))
                pygame.draw.ellipse(glow_layer, (245, 250, 255, 120), target_rect.inflate(12, 18), width=2)
                pygame.draw.ellipse(glow_layer, (255, 228, 165, 95), target_rect.inflate(25, 34), width=2)
                # Re-blit each character's actual card above the darkened board.
                source = scene.subsurface(source_rect).copy()
                if source.get_size() != target_rect.size:
                    source = pygame.transform.smoothscale(source, target_rect.size)
                display.blit(source, target_rect)
                pygame.draw.ellipse(glow_layer, (255, 246, 216, 170), target_rect.inflate(3, 6), width=2)
                pygame.draw.line(
                    glow_layer, (glow_color[0], glow_color[1], glow_color[2], 24),
                    (center[0], 0), (center[0], center[1]), max(1, target_rect.width // 4),
                )
            display.blit(glow_layer, (0, 0))
            return

        hero_centers = [transformed(rect).center for _, rect in actors.values()]
        if not hero_centers:
            return
        origin = (
            sum(point[0] for point in hero_centers) // len(hero_centers),
            sum(point[1] for point in hero_centers) // len(hero_centers),
        )
        targets = [transformed(rect).center for rect in opposing_rects]
        target = (
            sum(point[0] for point in targets) // len(targets),
            sum(point[1] for point in targets) // len(targets),
        ) if targets else (width - 32 if origin[0] < width // 2 else 32, origin[1])
        progress = min(1.0, impact_elapsed / 1.35)
        energy_fade = max(0.0, 1.0 - impact_elapsed / 2.5)
        curve = -min(height * 0.18, 145) if origin[1] > target[1] else min(height * 0.18, 145)
        points = []
        for index in range(25):
            t = index / 24
            bend = 2 * t * (1 - t)
            points.append((
                round(origin[0] + (target[0] - origin[0]) * t),
                round(origin[1] + (target[1] - origin[1]) * t + curve * bend),
            ))
        trail_count = max(2, round(progress * len(points)))
        visible_points = points[:trail_count]
        energy = pygame.Surface((width, height), pygame.SRCALPHA)
        if len(visible_points) > 1:
            pygame.draw.lines(energy, (72, 184, 255, round(160 * energy_fade)), False, visible_points, 18)
            pygame.draw.lines(energy, (255, 215, 116, round(230 * energy_fade)), False, visible_points, 8)
            pygame.draw.lines(energy, (255, 250, 220, round(245 * energy_fade)), False, visible_points, 2)
        for index in range(12):
            travel = (progress * 1.35 + index / 12) % 1.0
            if travel > progress:
                continue
            sample = points[min(len(points) - 1, round(travel * (len(points) - 1)))]
            radius = 2 + (index % 3)
            base_color = (90, 204, 255, 220) if index % 2 else (255, 220, 128, 230)
            color = (*base_color[:3], round(base_color[3] * energy_fade))
            pygame.draw.circle(energy, color, sample, radius)
        display.blit(energy, (0, 0))

        if impact_elapsed >= 0.82:
            flash_progress = min(1.0, (impact_elapsed - 0.82) / 0.42)
            alpha = round(220 * (1.0 - flash_progress) ** 2)
            flash = pygame.Surface((width, height), pygame.SRCALPHA)
            flash.fill((255, 246, 220, alpha))
            display.blit(flash, (0, 0))
            burst = pygame.Surface((width, height), pygame.SRCALPHA)
            burst_radius = max(8, round(min(width, height) * (0.015 + 0.22 * flash_progress)))
            pygame.draw.circle(burst, (255, 225, 151, alpha), target, burst_radius, width=max(2, burst_radius // 8))
            pygame.draw.circle(burst, (111, 201, 255, round(alpha * 0.78)), target, round(burst_radius * 0.7), width=max(2, burst_radius // 12))
            display.blit(burst, (0, 0))
        banner = render_text("SOGEKING SUCCESSFULLY SHOT THE FLAG!", "title", shadow=True)
        max_width = max(1, width - 48)
        if banner.get_width() > max_width:
            banner = pygame.transform.smoothscale(
                banner, (max_width, max(1, round(banner.get_height() * max_width / banner.get_width()))),
            )
        banner.set_alpha(round(255 * energy_fade))
        banner_rect = banner.get_rect(centerx=width // 2, top=max(24, round(height * 0.09)))
        display.blit(banner, banner_rect)
        bonus = render_text("+6", "number_row_score", shadow=True)
        bonus.set_alpha(round(255 * energy_fade))
        bonus_rect = bonus.get_rect(center=(target[0], target[1] - round(impact_elapsed * 32)))
        display.blit(bonus, bonus_rect)

    def _draw_luffy_haki_effect(self, screen, layout, now: float) -> None:
        if not self.luffy_haki_animations:
            return
        width, height = screen.get_size()
        diagonal = math.hypot(width, height)
        effects = pygame.Surface((width, height), pygame.SRCALPHA)
        active = []
        for animation in self.luffy_haki_animations:
            elapsed = now - animation["started"]
            if elapsed >= 1.55:
                continue
            active.append(animation)
            if elapsed < 0:
                continue
            card_rect = layout.slot_rects.get((animation["row_id"], animation["slot_index"]))
            origin = card_rect.center if card_rect is not None else (width // 2, height // 2)

            # Short atmospheric hit, then let the red/purple shock rings take over.
            darkness = 82 * max(0.0, 1.0 - elapsed / 0.30) ** 1.5
            if darkness > 0:
                effects.fill((4, 1, 11, round(darkness)))

            wave_progress = min(1.0, max(0.0, (elapsed - 0.16) / 0.94))
            if elapsed >= 0.16 and wave_progress < 1.0:
                radius = max(8, round(diagonal * wave_progress))
                fade = max(0.0, 1.0 - wave_progress) ** 1.2
                pygame.draw.circle(
                    effects, (153, 13, 48, round(200 * fade)), origin, radius,
                    width=max(2, round(9 * min(1.0, fade + 0.2))),
                )
                pygame.draw.circle(
                    effects, (108, 39, 174, round(175 * fade)), origin,
                    max(1, radius - max(5, round(10 * min(1.0, fade + 0.2)))),
                    width=max(1, round(3 * min(1.0, fade + 0.2))),
                )
                inner_radius = max(1, round(radius * 0.80))
                pygame.draw.circle(
                    effects, (220, 31, 89, round(66 * fade)), origin, inner_radius,
                    width=max(1, round(2 * fade)),
                )

            # Deterministic, jagged lightning flashes so every client sees the
            # same shape for this card without mutating any game coordinates.
            if 0.14 <= elapsed <= 1.02:
                for index, branch in enumerate(animation["branches"]):
                    pulse_at = 0.20 + branch["delay"]
                    width_flash = 0.055 + branch["seed"] * 0.035
                    pulse = max(0.0, 1.0 - abs(elapsed - pulse_at) / width_flash)
                    pulse = max(pulse, 0.62 * max(
                        0.0, 1.0 - abs(elapsed - (pulse_at + 0.22)) / (width_flash * 0.78),
                    ))
                    if pulse <= 0:
                        continue
                    points = [(round(x * width), round(y * height)) for x, y in branch["points"]]
                    alpha = round(238 * pulse)
                    pygame.draw.lines(effects, (85, 15, 132, round(alpha * 0.66)), False, points, 7)
                    pygame.draw.lines(effects, (222, 35, 93, alpha), False, points, 3)
                    pygame.draw.lines(effects, (246, 226, 255, alpha), False, points, 1)

            # A single screen-wide crimson-violet flash punctuates the second hit.
            flash = max(0.0, 1.0 - abs(elapsed - 0.50) / 0.135)
            if flash > 0:
                flash_layer = pygame.Surface((width, height), pygame.SRCALPHA)
                flash_layer.fill((155, 35, 132, round(94 * flash)))
                effects.blit(flash_layer, (0, 0))

            # Fine energy streaks race away from Luffy and fade before gameplay resumes.
            sparks_fade = max(0.0, 1.0 - max(0.0, elapsed - 0.42) / 0.84)
            if 0.18 <= elapsed <= 1.26:
                rng = random.Random(animation["owner_card_id"] + str(round(elapsed * 18)))
                for spark in range(22):
                    angle = math.tau * spark / 22 + rng.uniform(-0.12, 0.12)
                    distance = diagonal * min(0.9, max(0.0, (elapsed - 0.18) * 0.88))
                    px = origin[0] + math.cos(angle) * distance
                    py = origin[1] + math.sin(angle) * distance
                    length = (12 + rng.random() * 28) * sparks_fade
                    color = (214, 35, 101, round(190 * sparks_fade))
                    pygame.draw.line(
                        effects, color,
                        (round(px), round(py)),
                        (round(px - math.cos(angle) * length), round(py - math.sin(angle) * length)),
                        2,
                    )

        self.luffy_haki_animations = active
        screen.blit(effects, (0, 0))

    def _luffy_haki_camera_offset(self, now: float, size: tuple[int, int]) -> tuple[int, int]:
        # Strong opening impact, a smaller secondary hit at 0.5s, and a smooth
        # exponential return to the exact unshifted frame.
        if not self.luffy_haki_animations:
            return (0, 0)
        started = max(item["started"] for item in self.luffy_haki_animations)
        elapsed = max(0.0, now - started)
        scale = min(size) / 900.0
        opening = 17.0 * math.exp(-elapsed * 6.2)
        secondary = 7.5 * math.exp(-((elapsed - 0.50) / 0.105) ** 2)
        tail = 2.2 * math.exp(-max(0.0, elapsed - 0.68) * 8.5)
        amplitude = (opening + secondary + tail) * scale
        x = amplitude * (math.sin(now * 83.0 + 1.2) + 0.42 * math.sin(now * 137.0))
        y = amplitude * (math.cos(now * 91.0 + 0.4) + 0.36 * math.sin(now * 151.0))
        return round(x), round(y)

    def _draw_reverse_fate_animation(self, screen, state, cards, layout, now: float) -> None:
        """Briefly invert a row image with a purple/crimson distortion overlay."""
        animation = self.reverse_fate_animation
        if animation is None:
            return
        row_id = animation["row_id"]
        row = next((item for item in state.rows if item.id == row_id), None)
        if row is None or not row_uses_reverse_fate(row, cards):
            self.reverse_fate_animation = None
            return
        duration = 0.9
        progress = (now - animation["started"]) / duration
        if progress >= 1.0:
            self.reverse_fate_animation = None
            return
        if progress < 0:
            return

        rect = layout.row_rects[row_id]
        strength = math.sin(math.pi * progress)
        original = screen.subsurface(rect).copy()
        flipped = pygame.transform.rotate(original, 180)
        flipped.set_alpha(round(235 * strength))
        screen.blit(flipped, rect.topleft)

        distortion = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(distortion, (76, 18, 111, round(40 * strength)), distortion.get_rect())
        for band in range(5):
            base_y = round((band + 1) * rect.height / 6)
            offset = round(math.sin(progress * 16 + band * 1.7) * 8 * strength)
            color = (190, 47, 104, round(95 * strength)) if band % 2 else (155, 91, 241, round(100 * strength))
            pygame.draw.line(
                distortion, color,
                (max(0, offset), base_y),
                (min(rect.width, rect.width + offset), base_y - offset // 2),
                2,
            )
        for particle in range(12):
            x = int((particle * 73 + progress * 190) % max(1, rect.width))
            y = round((0.5 + 0.42 * math.sin(progress * 13 + particle * 2.4)) * rect.height)
            pygame.draw.circle(
                distortion, (210, 100, 255, round(130 * strength)),
                (x, y), 1 + particle % 2,
            )
        screen.blit(distortion, rect.topleft)

    def _draw_ability_suppression_animation(self, screen, state, layout, now: float) -> None:
        """Send a short black-energy pulse from Chrollo to the suppressed slot."""
        if not self.ability_suppression_animations:
            return
        duration = 1.15
        layer = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
        active: list[dict] = []
        for animation in self.ability_suppression_animations:
            if animation.get("visual_style") == "shanks_red":
                source_row = next(
                    (row for row in state.rows if row.id == animation["row_id"]),
                    None,
                )
                if source_row is None or not source_row.get("abilities_disabled", False):
                    continue
            progress = (now - animation["started"]) / duration
            if progress >= 1.0:
                continue
            active.append(animation)
            if progress < 0:
                continue
            key_source = (animation["row_id"], animation["source_slot_index"])
            key_target = (animation["row_id"], animation["target_slot_index"])
            source_rect = layout.slot_rects.get(key_source)
            target_rect = layout.slot_rects.get(key_target)
            if source_rect is None or target_rect is None:
                continue
            sx, sy = source_rect.center
            tx, ty = target_rect.center
            fade = min(1.0, (1.0 - progress) * 3.0)
            time_freeze = animation.get("visual_style") == "time_freeze"
            red_seal = animation.get("visual_style") == "shanks_red"
            trail_outer = ((94, 10, 29, round(155 * fade)) if red_seal else
                           (48, 25, 91, round(145 * fade)) if time_freeze else
                           (26, 22, 35, round(125 * fade)))
            trail_inner = ((255, 67, 88, round(225 * fade)) if red_seal else
                           (160, 91, 255, round(205 * fade)) if time_freeze else
                           (88, 77, 102, round(145 * fade)))
            control_x = (sx + tx) / 2
            control_y = min(sy, ty) - max(36, abs(tx - sx) * 0.22)
            curve = []
            for step in range(18):
                t = step / 17
                x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * control_x + t ** 2 * tx
                y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * control_y + t ** 2 * ty
                curve.append((round(x), round(y)))
            pygame.draw.lines(layer, trail_outer, False, curve, 10)
            pygame.draw.lines(layer, trail_inner, False, curve, 3)
            travel = min(1.0, progress / 0.72)
            x = (1 - travel) ** 2 * sx + 2 * (1 - travel) * travel * control_x + travel ** 2 * tx
            y = (1 - travel) ** 2 * sy + 2 * (1 - travel) * travel * control_y + travel ** 2 * ty
            pulse = 0.5 + 0.5 * math.sin(now * 17)
            orb_radius = round(9 + pulse * 5)
            orb_halo = ((196, 24, 53, round(215 * fade)) if red_seal else
                        (94, 37, 177, round(190 * fade)) if time_freeze else
                        (20, 17, 27, round(185 * fade)))
            orb_core = ((255, 98, 113, round(245 * fade)) if red_seal else
                        (184, 128, 255, round(240 * fade)) if time_freeze else
                        (67, 55, 83, round(230 * fade)))
            orb_center = ((93, 9, 25, round(250 * fade)) if red_seal else
                          (45, 20, 83, round(250 * fade)) if time_freeze else
                          (10, 9, 14, round(245 * fade)))
            pygame.draw.circle(layer, orb_halo, (round(x), round(y)), orb_radius + 7)
            pygame.draw.circle(layer, orb_core, (round(x), round(y)), orb_radius)
            pygame.draw.circle(layer, orb_center, (round(x), round(y)), max(2, orb_radius // 2))
            for particle in range(7):
                t = (travel + particle * 0.11) % 1.0
                px = (1 - t) ** 2 * sx + 2 * (1 - t) * t * control_x + t ** 2 * tx
                py = (1 - t) ** 2 * sy + 2 * (1 - t) * t * control_y + t ** 2 * ty
                drift = math.sin(now * 9 + particle * 2.1) * 7
                pygame.draw.circle(
                    layer, ((255, 101, 119, round(215 * fade)) if red_seal else
                            (184, 128, 255, round(205 * fade)) if time_freeze else
                            (47, 42, 57, round(150 * fade))),
                    (round(px), round(py + drift)), 2 + particle % 2,
                )
            if progress >= 0.72 and not red_seal:
                engulf = min(1.0, (progress - 0.72) / 0.28)
                radius = round(18 + engulf * max(target_rect.size) * 0.55)
                alpha = round(105 * fade * (1.0 - engulf * 0.55))
                engulf_color = ((126, 13, 35, alpha) if red_seal else
                                (55, 22, 105, alpha) if time_freeze else
                                (17, 15, 23, alpha))
                pygame.draw.circle(layer, engulf_color, target_rect.center, radius)
        self.ability_suppression_animations = active
        screen.blit(layer, (0, 0))

    @staticmethod
    def _draw_suppression_marker(
        screen, rect: pygame.Rect, now: float, visual_style: str = "black",
    ) -> None:
        """Persistent per-instance suppression aura that keeps card details legible."""
        if visual_style == "shanks_red":
            # Keep Shanks' seal as orbiting motes outside the card face. No
            # tint, flashing border, or glow should alter player/card colors.
            for particle in range(4):
                angle = now * 1.3 + particle * (math.tau / 4)
                px = rect.centerx + math.cos(angle) * (rect.width * 0.54)
                py = rect.centery + math.sin(angle) * (rect.height * 0.54)
                pygame.draw.circle(
                    screen, (224, 211, 177), (round(px), round(py)), 1 + particle % 2,
                )
            return
        if visual_style == "time_freeze":
            pulse = (math.sin(now * 3.0) + 1.0) / 2.0
            overlay = pygame.Surface(rect.size, pygame.SRCALPHA)
            overlay.fill((31, 12, 61, round(30 + pulse * 17)))
            screen.blit(overlay, rect.topleft)
            aura_rect = rect.inflate(10 + round(pulse * 4), 10 + round(pulse * 4))
            aura = pygame.Surface(aura_rect.size, pygame.SRCALPHA)
            pygame.draw.rect(
                aura, (100, 49, 174, round(105 + pulse * 75)), aura.get_rect(),
                width=4, border_radius=9,
            )
            screen.blit(aura, aura_rect.topleft)
            pygame.draw.rect(screen, (78, 39, 133), rect, width=2, border_radius=6)
            # Orbiting, faint clock-like points evoke Hit's frozen-time effect.
            for particle in range(6):
                angle = -now * 0.75 + particle * (math.tau / 6)
                px = rect.centerx + math.cos(angle) * (rect.width * 0.47)
                py = rect.centery + math.sin(angle) * (rect.height * 0.47)
                pygame.draw.circle(screen, (150, 104, 213), (round(px), round(py)), 1 + particle % 2)
            return
        pulse = (math.sin(now * 3.2) + 1.0) / 2.0
        overlay = pygame.Surface(rect.size, pygame.SRCALPHA)
        overlay.fill((8, 7, 12, round(22 + pulse * 16)))
        screen.blit(overlay, rect.topleft)
        aura_rect = rect.inflate(8 + round(pulse * 4), 8 + round(pulse * 4))
        aura = pygame.Surface(aura_rect.size, pygame.SRCALPHA)
        pygame.draw.rect(
            aura, (29, 24, 36, round(80 + pulse * 48)), aura.get_rect(),
            width=4, border_radius=9,
        )
        screen.blit(aura, aura_rect.topleft)
        pygame.draw.rect(screen, (44, 38, 53), rect, width=2, border_radius=6)
        for spark in range(4):
            angle = now * 1.3 + spark * (math.tau / 4)
            px = rect.centerx + math.cos(angle) * (rect.width * 0.46)
            py = rect.centery + math.sin(angle) * (rect.height * 0.46)
            pygame.draw.circle(screen, (52, 46, 63), (round(px), round(py)), 1 + spark % 2)

    def _draw_header(self, screen, state):
        width, _ = screen.get_size()
        for index, player in enumerate(state.players):
            x = 16 if index == 0 else width - 204
            rect = pygame.Rect(x, 10, 188, 66)
            pygame.draw.rect(screen, PANEL, rect, border_radius=11)
            accent = P1 if index == 0 else P2
            border_width = 3 if player.id == state.active_player.id and state.phase == GamePhase.PLACEMENT else 2
            pygame.draw.rect(screen, accent, rect, width=border_width, border_radius=11)
            self._text(screen, player.name, (rect.x + 12, rect.y + 7), "ui", max_width=rect.width - 24)
            self._text(screen, "MATCH PTS", (rect.x + 12, rect.y + 42), "label_small")
            self._text_right(
                screen, str(player.match_points), (rect.right - 12, rect.y + 37), "number_match", max_width=48,
            )

        title_width = max(100, width - 424)
        self._text_center_fit(screen, "VEILBOUND", (width // 2, 23), "title", title_width)
        self._text_center_fit(
            screen, f"ROUND {state.round_number} / {state.rounds_per_match}",
            (width // 2, 57), "label_small", title_width,
        )
    def _draw_fps(self, screen, fps: float) -> None:
        self._text_right(screen, f"FPS: {fps:.0f}", (screen.get_width() - 16, 84), "label_small")

    def _draw_rows(self, screen, state, cards, layout, legal_slots, drag_id, mouse_pos):
        now = time.monotonic()
        for row in state.rows:
            rect = layout.row_rects[row.id]
            box = layout.capacity_boxes[row.id]
            reverse_fate_active = row_uses_reverse_fate(row, cards)
            active_row = state.current_row is not None and state.current_row.id == row.id and state.phase in {
                GamePhase.REVEAL, GamePhase.ROW_SCORING, GamePhase.ROW_RESULTS,
            }
            if reverse_fate_active:
                outline = (177, 75, 226)
                pulse = (math.sin(now * 3.8) + 1.0) / 2.0
                glow_rect = rect.inflate(14, 14)
                glow = pygame.Surface(glow_rect.size, pygame.SRCALPHA)
                pygame.draw.rect(
                    glow, (222, 44, 116, round(34 + 28 * pulse)),
                    pygame.Rect(7, 7, rect.width, rect.height),
                    width=6, border_radius=16,
                )
                screen.blit(glow, glow_rect.topleft)
            elif row.scored:
                if row.winner_id == state.players[0].id:
                    outline = P1
                elif row.winner_id == state.players[1].id:
                    outline = P2
                else:
                    outline = (153, 163, 177)
            else:
                outline = GOLD if active_row else (78, 91, 116)
            backplate = self._row_backplates.get(rect.size)
            if backplate is None:
                backplate = pygame.Surface(rect.size, pygame.SRCALPHA)
                pygame.draw.rect(backplate, (*PANEL, 112), backplate.get_rect(), border_radius=10)
                self._row_backplates[rect.size] = backplate
            screen.blit(backplate, rect)
            pygame.draw.rect(
                screen, outline, rect,
                width=4 if reverse_fate_active else 3 if active_row or row.scored else 2,
                border_radius=10,
            )
            self._draw_row_box(screen, row, box, state, cards)

            for slot_index, slot in enumerate(row.slots):
                slot_rect = layout.slot_rects[(row.id, slot_index)]
                target = (row.id, slot_index) in legal_slots
                movement_blocked = (
                    slot is None
                    and slot_index in row.get("movement_blocked_slots", [])
                    and slot_index != row.capacity - 1
                )
                hovered = mouse_pos is not None and slot_rect.collidepoint(mouse_pos)
                if slot is None:
                    if movement_blocked:
                        slot_state = "movement_blocked"
                    elif drag_id is not None:
                        slot_state = "valid" if target else "invalid" if hovered else "empty"
                    else:
                        slot_state = "hover" if hovered else "ready" if target else "empty"
                    self.draw_slot(screen, slot_rect, slot_state, marker=target)
                    if movement_blocked:
                        pulse = (math.sin(now * 2.5) + 1.0) * 0.5
                        aura_rect = slot_rect.inflate(16, 16)
                        aura = pygame.Surface(aura_rect.size, pygame.SRCALPHA)
                        pygame.draw.rect(
                            aura, (48, 146, 255, round(42 + pulse * 54)),
                            aura.get_rect(), width=5, border_radius=SLOT_RADIUS + 9,
                        )
                        screen.blit(aura, aura_rect.topleft)
                        border = (round(70 + pulse * 45), round(160 + pulse * 75), 255)
                        pygame.draw.rect(
                            screen, border, slot_rect.inflate(2, 2), width=3,
                            border_radius=SLOT_RADIUS + 2,
                        )
                else:
                    self.draw_slot(screen, slot_rect, "occupied")
                    card_rect = self._portrait_card_rect(slot_rect)
                    self._draw_placed_card(screen, card_rect, row, slot_index, slot, cards, now)
                    if drag_id is not None and hovered:
                        self.draw_slot(screen, slot_rect, "invalid_overlay")

    def _prepare_card_frame(self) -> pygame.Surface | None:
        """Load the outline, key out its near-black background, and crop it."""
        image = self.assets.load_image("assets/art/ui/Card Outline .png")
        if image is None:
            return None
        image = image.copy()
        # The source has dark, partly opaque texture across its canvas. Exact
        # black colorkeying leaves that texture behind, so treat near-black as
        # transparent and keep the brighter gold/red line work.
        visible_mask = pygame.mask.from_threshold(
            image, (0, 0, 0, 255), (28, 28, 28, 255),
        )
        visible_mask.invert()
        key = visible_mask.to_surface(
            setcolor=(255, 255, 255, 255),
            unsetcolor=(0, 0, 0, 0),
        )
        bounds = key.get_bounding_rect(min_alpha=1)
        if bounds.width == 0 or bounds.height == 0:
            return None
        image.blit(key, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        image = image.subsurface(bounds).copy()
        return image

    def _draw_card_frame(self, screen, rect: pygame.Rect) -> None:
        if self.card_frame_art is None:
            return
        scale_x, scale_y = self.settings.card_frame_size
        frame_size = (
            max(1, round(rect.width * scale_x)),
            max(1, round(rect.height * scale_y)),
        )
        frame = self._card_frame_cache.get(frame_size)
        if frame is None:
            frame = pygame.transform.smoothscale(self.card_frame_art, frame_size)
            self._card_frame_cache[frame_size] = frame
        screen.blit(frame, frame.get_rect(center=rect.center))

    def _card_front_surface(self, size: tuple[int, int]) -> pygame.Surface:
        """Return the parchment front artwork scaled to a card/panel size."""
        cached = self._card_front_cache.get(size)
        if cached is not None:
            return cached
        if self.card_front_art is not None:
            # The source is square while cards are portrait; fill the card so
            # all four corner ornaments remain visible.
            front = pygame.transform.smoothscale(self.card_front_art, size)
        else:
            front = pygame.Surface(size, pygame.SRCALPHA)
            front.fill(CREAM)
        self._card_front_cache[size] = front
        return front

    def draw_slot(self, screen, rect: pygame.Rect, state: str, marker: bool = False) -> None:
        """Draw a cached slot skin for its visual state and size."""
        key = (state, rect.size, marker)
        slot_surface = self._slot_surface_cache.get(key)
        if slot_surface is None:
            slot_surface = self._make_slot_surface(state, rect.size, marker)
            self._slot_surface_cache[key] = slot_surface
        screen.blit(slot_surface, (rect.x - SLOT_GLOW_PADDING, rect.y - SLOT_GLOW_PADDING))

    def _make_slot_surface(self, state: str, size: tuple[int, int], marker: bool) -> pygame.Surface:
        width, height = size
        padding = SLOT_GLOW_PADDING
        surface = pygame.Surface((width + padding * 2, height + padding * 2), pygame.SRCALPHA)
        if pygame.display.get_surface() is not None:
            surface = surface.convert_alpha()
        slot_rect = pygame.Rect(padding, padding, width, height)

        styles = {
            "empty": (SLOT_EMPTY_FILL, SLOT_EMPTY_BORDER, None),
            "ready": (SLOT_EMPTY_FILL, SLOT_EMPTY_BORDER, None),
            "hover": (SLOT_EMPTY_FILL, SLOT_HOVER_BORDER, SLOT_HOVER_GLOW),
            "valid": (SLOT_VALID_FILL, SLOT_VALID_BORDER, SLOT_VALID_GLOW),
            "movement_blocked": ((11, 31, 57), (68, 169, 255), (48, 146, 255, 110)),
            "invalid": (SLOT_INVALID_FILL, SLOT_INVALID_BORDER, None),
            "invalid_overlay": (SLOT_INVALID_FILL, SLOT_INVALID_BORDER, None),
            "occupied": (SLOT_OCCUPIED_FILL, SLOT_OCCUPIED_BORDER, None),
        }
        fill, border, glow = styles.get(state, styles["empty"])

        if state != "invalid_overlay":
            shadow_rect = slot_rect.move(0, 2)
            pygame.draw.rect(surface, SLOT_SHADOW, shadow_rect, border_radius=SLOT_RADIUS)
        if glow is not None:
            self._draw_slot_glow(surface, slot_rect, glow)
        pygame.draw.rect(surface, fill, slot_rect, border_radius=SLOT_RADIUS)
        pygame.draw.rect(surface, border, slot_rect, width=SLOT_BORDER_WIDTH, border_radius=SLOT_RADIUS)

        if marker:
            marker_x = slot_rect.left - 4
            marker_y = slot_rect.centery
            marker = [
                (marker_x, marker_y - 3),
                (marker_x + 3, marker_y),
                (marker_x, marker_y + 3),
                (marker_x - 3, marker_y),
            ]
            pygame.draw.polygon(surface, SLOT_GOLD, marker)

        if SLOT_CORNER_ORNAMENTS_ENABLED:
            self._draw_slot_corner_ornaments(surface, slot_rect)
        return surface

    @staticmethod
    def _draw_slot_glow(surface: pygame.Surface, rect: pygame.Rect, color: tuple[int, int, int, int]) -> None:
        for spread, alpha in SLOT_GLOW_STEPS:
            glow_rect = rect.inflate(spread * 2, spread * 2)
            pygame.draw.rect(
                surface,
                (*color[:3], alpha),
                glow_rect,
                width=2,
                border_radius=SLOT_RADIUS + spread,
            )

    @staticmethod
    def _draw_slot_corner_ornaments(surface: pygame.Surface, rect: pygame.Rect) -> None:
        length = 5
        corners = (
            (rect.left + 2, rect.top + 2, 1, 1),
            (rect.right - 3, rect.top + 2, -1, 1),
            (rect.left + 2, rect.bottom - 3, 1, -1),
            (rect.right - 3, rect.bottom - 3, -1, -1),
        )
        for x, y, dx, dy in corners:
            pygame.draw.line(surface, SLOT_GOLD, (x, y), (x + dx * length, y), 1)
            pygame.draw.line(surface, SLOT_GOLD, (x, y), (x, y + dy * length), 1)

    ROW_PANEL_TEXTURE = "assets/art/ui/Row/Square Row Art.png"
    ROW_PANEL_RADIUS = 9

    def _row_panel_surface(self, size: tuple[int, int]) -> pygame.Surface | None:
        """Load, cover-crop, and round the dark-marble texture for a square panel."""
        cached = self._row_panel_cache.get(size)
        if cached is not None:
            return cached
        image = self.assets.load_image(self.ROW_PANEL_TEXTURE)
        if image is None:
            self._row_panel_cache[size] = None
            return None
        panel = AssetLoader.scale_cover(image, size)
        radius = self.ROW_PANEL_RADIUS
        if radius > 0:
            rounded = pygame.Surface(size, pygame.SRCALPHA)
            rounded.blit(panel, (0, 0))
            mask = pygame.Surface(size, pygame.SRCALPHA)
            pygame.draw.rect(mask, (255, 255, 255, 255), (0, 0, *size), border_radius=radius)
            rounded.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
            panel = rounded
        self._row_panel_cache[size] = panel
        return panel

    def _draw_row_box(self, screen, row: BattleRow, rect: pygame.Rect, state: GameState, cards):
        reverse_fate_active = row_uses_reverse_fate(row, cards)
        panel = self._row_panel_surface(rect.size)
        if panel is not None:
            screen.blit(panel, rect.topleft)
        else:
            fill = PANEL_LIGHT if not row.scored else (47, 59, 80)
            pygame.draw.rect(screen, fill, rect, border_radius=self.ROW_PANEL_RADIUS)
        # Keep the scored/active border affordance on top of the texture.
        pygame.draw.rect(
            screen,
            GOLD if row.scored else (113, 128, 151),
            rect,
            width=2,
            border_radius=self.ROW_PANEL_RADIUS,
        )

        self._text_center_fit(
            screen, f"ROW {row.number}",
            (rect.centerx, rect.y + round(rect.height * (0.09 if reverse_fate_active else 0.10))),
            "row_label", rect.width - 8,
        )
        self._text_center(
            screen, str(row.point_value),
            (rect.centerx, rect.y + round(rect.height * (0.26 if reverse_fate_active else 0.32))),
            "number_capacity",
        )
        if reverse_fate_active:
            badge = pygame.Rect(
                rect.x + 4,
                rect.y + round(rect.height * 0.36),
                rect.width - 8,
                max(16, round(rect.height * 0.12)),
            )
            pygame.draw.rect(screen, (57, 23, 77), badge, border_radius=5)
            pygame.draw.rect(screen, (177, 75, 226), badge, width=1, border_radius=5)
            label_font = get_font("label_small", max(10, round(rect.height * 0.095)))
            label = label_font.render("▼ LOWEST WINS", True, (239, 167, 255))
            max_width = max(1, badge.width - 4)
            if label.get_width() > max_width:
                label = pygame.transform.smoothscale(
                    label,
                    (max_width, max(1, round(label.get_height() * max_width / label.get_width()))),
                )
            screen.blit(label, label.get_rect(center=badge.center))

        display_scores = (
            self._displayed_row_scores(row)
            if row.scored
            else calculate_row_scores(row, tuple(player.id for player in state.players), cards)
        )
        number_size = max(16, round(rect.height * 0.20))
        square_size = max(9, round(rect.height * 0.10))
        first_row_y = rect.y + round(rect.height * (0.64 if reverse_fate_active else 0.58))
        row_spacing = max(16, round(rect.height * (0.19 if reverse_fate_active else 0.22)))
        for index, player in enumerate(state.players):
            accent = P1 if index == 0 else P2_CARD_OUTLINE
            self._draw_player_score(
                screen,
                rect,
                first_row_y + index * row_spacing,
                accent,
                str(display_scores[player.id]),
                f"P{index + 1}",
                number_size,
                square_size,
            )

    def _draw_player_score(self, screen, rect, center_y, accent, value, label, number_size, square_size):
        """Draw a colored square, a large white number, and a small player label."""
        number = get_font("number_card", number_size).render(value, True, TEXT_WHITE)
        label_surface = render_text(label, "label_small")
        gap = max(3, square_size // 2)
        total_width = square_size + gap + number.get_width() + gap + label_surface.get_width()
        x = rect.centerx - total_width // 2
        square = pygame.Rect(x, center_y - square_size // 2, square_size, square_size)
        pygame.draw.rect(screen, accent, square, border_radius=2)
        screen.blit(number, (square.right + gap, center_y - number.get_height() // 2))
        screen.blit(
            label_surface,
            (square.right + gap + number.get_width() + gap, center_y - label_surface.get_height() // 2),
        )

    def _draw_placed_card(self, screen, rect, row, slot_index, card, cards, now):
        # An eliminated card stays turned over for the rest of the match: show
        # its back plus the red elimination cross on the normal card layer.
        # The active elimination flash owns this slot while it animates, so
        # skip the permanent look there to avoid duplicate artwork.
        if (
            getattr(card, "eliminated", False)
            and self.elimination_flash_card == (row.id, slot_index)
            and self.elimination_flash_started is not None
        ):
            return
        if getattr(card, "eliminated", False):
            surface = self._make_card_back_surface(rect.size, card.owner_id)
            screen.blit(surface, rect)
            pygame.draw.rect(
                screen, self._owner_card_outline(card.owner_id),
                rect, width=3, border_radius=6,
            )
            if getattr(card, "ability_suppressed", False) and not row.get("abilities_disabled", False):
                self._draw_suppression_marker(
                    screen, rect, now,
                    str(card.get("ability_suppression_style", "black")),
                )
            self._draw_elimination_cross(screen, rect)
            return
        definition = cards.get(card.definition_id) if card.revealed else None
        reveal_time = self.flip_started.get((row.id, slot_index))
        flip_duration = max(1, self.animation_ms["card_flip_ms"]) / 1000
        progress = 1.0 if reveal_time is None and card.revealed else 0.0
        if reveal_time is not None:
            progress = min(1.0, max(0.0, (now - reveal_time) / flip_duration))
        face_up = card.revealed and (reveal_time is None or progress >= 0.5)
        owner_accent = self._owner_card_outline(card.owner_id)
        if face_up:
            if definition is None:
                return
            surface = self._make_card_face(
                definition,
                rect.size,
                transformed_card_art=self._activated_transformation_art(definition, card),
            )
        else:
            surface = self._make_card_back_surface(rect.size, card.owner_id)
        if reveal_time is not None and progress < 1.0:
            flip_scale = max(0.04, abs(1.0 - progress * 2.0))
            scaled_width = max(2, round(rect.width * flip_scale))
            surface = pygame.transform.smoothscale(surface, (scaled_width, rect.height))
            draw_rect = surface.get_rect(center=rect.center)
        else:
            draw_rect = rect
        screen.blit(surface, draw_rect)
        pygame.draw.rect(screen, owner_accent, rect, width=3, border_radius=6)
        if not card.revealed:
            self._draw_card_frame(screen, draw_rect)
            if getattr(card, "ability_suppressed", False):
                self._draw_suppression_marker(
                    screen, rect, now,
                    str(card.get("ability_suppression_style", "black")),
                )
            if bool(card.get("immune_to_elimination", False)):
                self._draw_immunity_shield(screen, rect)
            if row.get("abilities_disabled", False):
                self._draw_suppression_marker(screen, rect, now, "shanks_red")
            return
        # Persistent per-instance point indicator: the cached face underneath
        # uses roster art, while this overlay shows the live total and any
        # match-scoped base-point reduction.
        flip_finished = reveal_time is None or progress >= 1.0
        if flip_finished and definition is not None:
            current = getattr(card, "current_points", None)
            roster_base = getattr(definition, "base_points", None)
            base = getattr(card, "base_points", None)
            if base is None:
                base = roster_base
            if isinstance(current, bool) or isinstance(base, bool) or isinstance(roster_base, bool):
                return
            if (isinstance(current, (int, float)) and isinstance(base, (int, float))
                    and isinstance(roster_base, (int, float))):
                # Show copied or otherwise modified base values as well as
                # bonuses. Toga's copied points become her instance base, so
                # current == base even though the roster card face is stale.
                if int(current) != int(base) or int(base) != int(roster_base):
                    point_label = self._format_points(int(current), int(base))
                    badge_font = get_font("label_small")
                    badge_text = badge_font.render(point_label, True, BG)
                    badge_rect = badge_text.get_rect()
                    pill = pygame.Rect(0, 0, badge_rect.width + 12, badge_rect.height + 6)
                    pill.topright = (rect.right - 4, rect.y + 4)
                    pygame.draw.rect(screen, GOLD, pill, border_radius=8)
                    screen.blit(badge_text, badge_text.get_rect(center=pill.center))
        if getattr(card, "ability_suppressed", False) and not row.get("abilities_disabled", False):
            self._draw_suppression_marker(
                screen, rect, now,
                str(card.get("ability_suppression_style", "black")),
            )
        if bool(card.get("immune_to_elimination", False)):
            self._draw_immunity_shield(screen, rect)
        if row.get("abilities_disabled", False):
            self._draw_suppression_marker(screen, rect, now, "shanks_red")

    @staticmethod
    def _draw_immunity_shield(screen, rect: pygame.Rect) -> None:
        """Small gold-blue status badge for any card with elimination immunity."""
        center = (rect.left + 17, rect.top + 17)
        shield = [
            (center[0], center[1] - 10), (center[0] + 8, center[1] - 7),
            (center[0] + 7, center[1] + 3), (center[0], center[1] + 10),
            (center[0] - 7, center[1] + 3), (center[0] - 8, center[1] - 7),
        ]
        pygame.draw.circle(screen, (16, 19, 36), center, 13)
        pygame.draw.polygon(screen, (247, 205, 104), shield)
        inner = [
            (center[0], center[1] - 7), (center[0] + 5, center[1] - 5),
            (center[0] + 4, center[1] + 2), (center[0], center[1] + 7),
            (center[0] - 4, center[1] + 2), (center[0] - 5, center[1] - 5),
        ]
        pygame.draw.polygon(screen, (67, 153, 225), inner)
        pygame.draw.line(screen, (222, 244, 255), (center[0], center[1] - 4), (center[0], center[1] + 4), 2)

    @staticmethod
    def _format_points(active_points: int, base_points: int) -> str:
        """Render a live total and retain its pre-bonus instance base."""
        active = int(active_points)
        base = int(base_points)
        return f"{active} (base {base})" if active != base else str(active)

    @staticmethod
    def _portrait_card_rect(slot_rect: pygame.Rect) -> pygame.Rect:
        """Fit a card inside its slot with a uniform margin and 2:3 proportions."""
        available_width = max(1, slot_rect.width - SLOT_CARD_MARGIN * 2)
        available_height = max(1, slot_rect.height - SLOT_CARD_MARGIN * 2)
        card_width = min(
            available_width,
            max(1, available_height * CARD_WIDTH_RATIO // CARD_HEIGHT_RATIO),
        )
        card_height = min(
            available_height,
            max(1, round(card_width * CARD_HEIGHT_RATIO / CARD_WIDTH_RATIO)),
        )
        card_width = min(
            available_width,
            max(1, round(card_height * CARD_WIDTH_RATIO / CARD_HEIGHT_RATIO)),
        )
        rect = pygame.Rect(0, 0, card_width, card_height)
        rect.center = slot_rect.center
        return rect

    @staticmethod
    def _activated_transformation_art(definition: CardDefinition, card) -> str | None:
        resolved_state = card.get("resolved_abilities")
        activated = set(
            resolved_state if resolved_state is not None
            else (card.get("activated_abilities") or [])
        )
        for ability in getattr(definition, "abilities", None) or ():
            art = ability.presentation.transformed_card_art
            if ability.id in activated and art:
                return art
        return None

    def _make_card_face(
        self,
        definition: CardDefinition,
        size: tuple[int, int],
        transformed_card_art: str | None = None,
    ) -> pygame.Surface:
        full_card_art = transformed_card_art or definition.full_card_art
        key = (definition.id, size, full_card_art)
        cached = self._card_face_cache.get(key)
        if cached is not None:
            return cached
        width, height = size

        # Full card artwork takes priority and replaces the entire card face.
        full_art = None
        if full_card_art:
            full_art = self.assets.load_image(full_card_art)
        if full_art is not None:
            face = pygame.Surface(size, pygame.SRCALPHA)
            fitted = self.assets.scale_to_fit(full_art, size)
            face.blit(fitted, fitted.get_rect(center=(width // 2, height // 2)))
            self._card_face_cache[key] = face
            return face

        # Fallback: existing card front + anime image system.
        face = self._card_front_surface(size).copy()
        pygame.draw.rect(face, (151, 139, 114), face.get_rect(), width=2, border_radius=6)
        compact = height < 100
        name_lines = self._card_name_lines(definition.name, width - 8)
        wrapped_name = len(name_lines) > 1 and height >= 110
        art_height = max(
            8 if compact else 18,
            round(height * (0.22 if compact else 0.18 if wrapped_name else 0.29)),
        )
        art_area = pygame.Rect(3, 3, max(2, width - 6), max(2, art_height - 3))
        art = self.assets.load_character(definition.image)
        scaled = self.assets.scale_to_fit(art, art_area.size)
        face.blit(scaled, scaled.get_rect(center=art_area.center))
        pygame.draw.line(face, (153, 145, 127), (3, art_height), (width - 4, art_height), 1)

        if compact:
            name_y = art_height + max(7, get_font("card_name").get_linesize() // 2) + 2
            self._text_center_fit(
                face, definition.name, (width // 2, name_y), "card_name", width - 6,
                variant="parchment",
            )
            point_font_size = max(12, min(FONTS["number_card"]["size"], round(height * 0.24)))
            point_font = get_font("number_card", point_font_size)
            point_text = str(definition.base_points)
            point = point_font.render(point_text, True, FONTS["number_card"]["color"])
            shadow = point_font.render(point_text, True, TEXT_SHADOW)
            outlined = pygame.Surface((point.get_width() + 1, point.get_height() + 1), pygame.SRCALPHA)
            outlined.blit(shadow, (1, 1))
            outlined.blit(point, (0, 0))
            point_rect = outlined.get_rect(center=(width // 2, height - outlined.get_height() // 2 - 2))
            face.blit(outlined, point_rect)
            self._card_face_cache[key] = face
            return face

        if wrapped_name:
            self._draw_centered_name_lines(
                face, name_lines, (width // 2, 0), width - 8,
                variant="parchment", top=art_height + 1,
            )
            label_y = height - 45
        else:
            self._text_center_fit(
                face, definition.name, (width // 2, art_height + 10), "card_name", width - 6,
                variant="parchment",
            )
            label_y = height - 45
        self._text_center_fit(
            face, "BASE POINTS", (width // 2, label_y), "label_small", width - 8, variant="parchment",
        )
        self._text_center_fit(
            face, str(definition.base_points), (width // 2, height - 19), "number_card", width - 10, shadow=True,
        )
        self._card_face_cache[key] = face
        return face

    @staticmethod
    def _card_back_variant(player_id: str) -> str:
        return "blue" if player_id == "player-1" else "red"

    def _make_card_back_surface(self, size: tuple[int, int], player_id: str) -> pygame.Surface:
        variant = self._card_back_variant(player_id)
        key = (variant, size)
        cached = self._card_back_cache.get(key)
        if cached is not None:
            return cached
        artwork = self.card_back_art.get(variant)
        if artwork is not None:
            back = pygame.Surface(size, pygame.SRCALPHA)
            back.fill(BG)
            fitted_art = self.assets.scale_to_fit(artwork, size)
            back.blit(fitted_art, fitted_art.get_rect(center=back.get_rect().center))
        else:
            # Keep cards readable if the configured artwork is missing.
            back = pygame.Surface(size, pygame.SRCALPHA)
            accent = P1 if variant == "blue" else P2
            back.fill(PANEL)
            pygame.draw.rect(back, accent, back.get_rect(), width=2, border_radius=6)
        self._card_back_cache[key] = back
        return back

    def _draw_hand(self, screen, state, cards, layout, drag_id, mouse_pos, hover_index: int | None = None):
        if state.phase != GamePhase.PLACEMENT or self.turn_confirmation_player_id is not None:
            return
        hand_player = state.player(state.viewer_id) if state.viewer_id else state.active_player
        if getattr(layout, "debug_mode", False) and self.debug_mode:
            self._draw_debug_hand(screen, state, cards, layout, hand_player, drag_id, mouse_pos, hover_index)
            return
        from veilbound.client.layout import debug_shifted_rects, debug_hover_index

        if hover_index is None:
            hover_index = debug_hover_index(layout.hand_cards, mouse_pos)
        shifted_rects = debug_shifted_rects(
            list(layout.hand_cards), hover_index, scale=DEBUG_HOVER_SCALE
        )
        for index, card in enumerate(hand_player.hand):
            rect = shifted_rects[index]
            if card.id == drag_id:
                continue
            screen.blit(self._make_card_face(cards[card.definition_id], rect.size), rect)
        if drag_id and mouse_pos and hand_player.id == state.active_player.id:
            card = next((item for item in hand_player.hand if item.id == drag_id), None)
            if card:
                drag_height = max(1, round(layout.hand_cards[0].height * 1.1))
                drag_width = max(1, round(drag_height * CARD_WIDTH_RATIO / CARD_HEIGHT_RATIO))
                surface = self._make_card_face(cards[card.definition_id], (drag_width, drag_height))
                rect = surface.get_rect(center=mouse_pos)
                screen.blit(surface, rect)

    def _draw_debug_hand(
        self,
        screen,
        state,
        cards,
        layout,
        hand_player,
        drag_id,
        mouse_pos,
        hover_index: int | None = None,
    ):
        """Single-row scrollable strip with dock-style hover magnification.

        The hovered card enlarges to approximately 2× its normal size, bottom
        anchored, while its nearest neighbors shift aside to make room. The
        dragged card returns to normal size immediately so the board stays
        fully visible. Ability name and description are shown above the
        hovered card.
        """
        from veilbound.client.layout import clamp_hand_scroll, debug_shifted_rects

        scroll = clamp_hand_scroll(self.debug_scroll, layout.debug_max_scroll)
        self.debug_scroll = scroll
        viewport = layout.debug_viewport
        hand = hand_player.hand
        # Full shifted geometry (scroll + hover); drawing and hit-testing use
        # the same rects so the enlarged card is what the mouse sees.
        if hover_index is None:
            _, hover_index = layout.debug_visible_cards(scroll, mouse_pos)
        # Selection overrides hover: the selected card is drawn at normal size
        # so the player can clearly see what they are placing.
        if hover_index is not None and drag_id is not None:
            dragged = next((item for item in hand if item.id == drag_id), None)
            if dragged is not None and hand.index(dragged) == hover_index:
                hover_index = None
        shifted_origin = [
            pygame.Rect(rect.x - round(scroll), rect.y, rect.width, rect.height)
            for rect in layout.hand_cards
        ]
        drawn_rects = debug_shifted_rects(shifted_origin, hover_index, scale=DEBUG_HOVER_SCALE)
        previous_clip = screen.get_clip()
        screen.set_clip(viewport)
        try:
            for index, card in enumerate(hand):
                if index >= len(drawn_rects):
                    break
                rect = drawn_rects[index]
                if rect.right <= viewport.x or rect.x >= viewport.right:
                    continue
                if card.id == drag_id:
                    continue
                screen.blit(self._make_card_face(cards[card.definition_id], rect.size), rect)
        finally:
            screen.set_clip(previous_clip)
        # Edge fades hint that more cards lie off-screen in that direction.
        self._draw_debug_edge_fades(screen, layout, scroll)
        self._draw_debug_scrollbar(screen, layout, scroll)
        # Draw the dragged card as a normal-sized ghost at the cursor so the
        # player can see exactly what they are placing.
        if drag_id and mouse_pos and hand_player.id == state.active_player.id:
            card = next((item for item in hand if item.id == drag_id), None)
            if card and layout.hand_cards:
                base = layout.hand_cards[0]
                surface = self._make_card_face(cards[card.definition_id], base.size)
                rect = surface.get_rect(center=mouse_pos)
                screen.blit(surface, rect)
    def _draw_debug_scrollbar(self, screen, layout, scroll) -> None:
        track = layout.debug_track
        if track.width <= 0:
            return
        pygame.draw.rect(screen, (26, 34, 52), track, border_radius=4)
        pygame.draw.rect(screen, (78, 91, 116), track, width=1, border_radius=4)
        pygame.draw.rect(screen, GOLD, layout.debug_thumb_rect(scroll), border_radius=4)

    def _draw_debug_edge_fades(self, screen, layout, scroll) -> None:
        from veilbound.client.layout import clamp_hand_scroll

        viewport = layout.debug_viewport
        fade_width = 26
        offset = clamp_hand_scroll(scroll, layout.debug_max_scroll)
        if offset > 0:
            fade = pygame.Surface((fade_width, viewport.height), pygame.SRCALPHA)
            for x in range(fade_width):
                alpha = round(110 * (1.0 - x / max(1, fade_width)))
                fade.fill((8, 12, 22, alpha), pygame.Rect(x, 0, 1, viewport.height))
            screen.blit(fade, (viewport.x, viewport.y))
        if offset < layout.debug_max_scroll:
            fade = pygame.Surface((fade_width, viewport.height), pygame.SRCALPHA)
            for x in range(fade_width):
                alpha = round(110 * (x / max(1, fade_width)))
                fade.fill((8, 12, 22, alpha), pygame.Rect(x, 0, 1, viewport.height))
            screen.blit(fade, (viewport.right - fade_width, viewport.y))
    def _draw_status(self, screen, state, layout):
        if state.phase == GamePhase.PLACEMENT:
            if self.turn_confirmation_player_id is not None and not self._reveal_view_active(state):
                player = state.player(self.turn_confirmation_player_id)
                text = f"{player.name}, your turn. Press Enter or click to show your hand."
            else:
                empty = sum(row.capacity - row.placed_count for row in state.rows)
                text = f"Round {state.round_number}: {state.active_player.name} to place - {empty} slots remain"
                if (
                    state.viewer_id is not None
                    and state.active_player.id != state.viewer_id
                    and self.turn_confirmation_player_id is None
                ):
                    text = f"Waiting for {state.active_player.name} to place - {empty} slots remain"
        elif state.phase == GamePhase.REVEAL:
            text = self.last_card_reveal or f"Revealing Row {state.current_row.number}"
        elif state.phase == GamePhase.ROW_SCORING:
            text = f"Comparing Player 1 and Player 2 in Row {state.current_row.number}"
        elif state.phase == GamePhase.ROW_RESULTS:
            row = state.current_row
            scores = self._displayed_row_scores(row)
            text = f"Row {row.number}: P1 {scores[state.players[0].id]} - P2 {scores[state.players[1].id]}"
            if row.winner_id:
                text += f" - {state.player(row.winner_id).name} earns {row.awarded_points}"
            else:
                text += " - tie, no points awarded"
        elif state.phase == GamePhase.ROUND_RESULTS:
            text = f"Round {state.round_number} complete. Match points: {state.players[0].match_points} - {state.players[1].match_points}"
        else:
            text = "Match results"
        self._text_center_fit(
            screen, text, (screen.get_width() // 2, layout.status_y), "status_banner", screen.get_width() - 40,
        )

    def _draw_footer(self, screen, layout):
        footer = "Drag cards to slots  -  F11: fullscreen  -  Esc: Menu"
        room_label = getattr(self, "room_label", None)
        if room_label:
            footer += f"  -  {room_label}"
        self._text_center_fit(
            screen, footer, (screen.get_width() // 2, screen.get_height() - 12), "label_small", screen.get_width() - 24,
        )

    def _draw_round_results(self, screen, state, layout):
        self._draw_overlay_backdrop(screen)
        width = min(680, screen.get_width() - 36)
        panel = pygame.Rect(screen.get_width() // 2 - width // 2, screen.get_height() // 2 - 205, width, 410)
        pygame.draw.rect(screen, PANEL, panel, border_radius=18)
        pygame.draw.rect(screen, GOLD, panel, width=2, border_radius=18)
        self._text_center_fit(
            screen, f"ROUND {state.round_number} RESULTS", (panel.centerx, panel.y + 38), "title", panel.width - 28,
        )
        gap = 18
        column_width = (panel.width - 3 * gap) // 2
        for index, player in enumerate(state.players):
            col = pygame.Rect(panel.x + gap + index * (column_width + gap), panel.y + 76, column_width, 132)
            accent = P1 if index == 0 else P2
            pygame.draw.rect(screen, PANEL_LIGHT, col, border_radius=12)
            pygame.draw.rect(screen, accent, col, width=2, border_radius=12)
            self._text_center_fit(screen, player.name, (col.centerx, col.y + 24), "ui", col.width - 16)
            self._text_center(screen, f"{player.match_points}", (col.centerx, col.y + 66), "number_match")
            self._text_center_fit(
                screen, f"ROUND POINTS: {player.round_points}", (col.centerx, col.y + 108),
                "label_small", col.width - 16,
            )
        if state.round_winner_id is None:
            result = "Round tied"
        else:
            result = f"{state.player(state.round_winner_id).name} wins this round"
        self._text_center_fit(screen, result, (panel.centerx, panel.y + 240), "ui", panel.width - 32)
        self._text_center_fit(
            screen, "Cumulative match points carry into the next round.",
            (panel.centerx, panel.y + 275), "label_small", panel.width - 32,
        )
        label = f"Continue to Round {state.round_number + 1}" if state.round_number < state.rounds_per_match else "Show Match Result"
        pygame.draw.rect(screen, (66, 94, 127), layout.round_continue, border_radius=9)
        self._text_center_fit(screen, label, layout.round_continue.center, "ui", layout.round_continue.width - 14)

    def _draw_match_results(self, screen, state, layout):
        self._draw_overlay_backdrop(screen)
        panel = pygame.Rect(screen.get_width() // 2 - 300, screen.get_height() // 2 - 188, 600, 376)
        pygame.draw.rect(screen, PANEL, panel, border_radius=18)
        pygame.draw.rect(screen, GOLD, panel, width=2, border_radius=18)
        if state.winner_id is None:
            headline = "MATCH DRAW"
        else:
            headline = f"{state.player(state.winner_id).name.upper()} WINS"
        self._text_center_fit(screen, headline, (panel.centerx, panel.y + 48), "title", panel.width - 28)
        for index, player in enumerate(state.players):
            x = panel.x + panel.width // 4 + index * panel.width // 2
            self._text_center_fit(screen, player.name, (x, panel.y + 124), "ui", panel.width // 2 - 32)
            self._text_center(screen, str(player.match_points), (x, panel.y + 179), "number_match")
            self._text_center(screen, "MATCH POINTS", (x, panel.y + 211), "label_small")
        pygame.draw.rect(screen, (66, 94, 127), layout.match_continue, border_radius=9)
        self._text_center_fit(
            screen, "Play Again", layout.match_continue.center, "ui", layout.match_continue.width - 16,
        )

    def _draw_overlay_backdrop(self, screen):
        screen.blit(self._overlay(screen.get_size(), (4, 8, 17, 220)), (0, 0))

    @staticmethod
    def _text(screen, text, position, role, max_width=None, *, variant=None, shadow=False):
        screen.blit(render_text(text, role, max_width, variant=variant, shadow=shadow), position)

    @staticmethod
    def _text_right(screen, text, right_top, role, max_width=None, *, variant=None, shadow=False):
        rendered = render_text(text, role, max_width, variant=variant, shadow=shadow)
        screen.blit(rendered, (right_top[0] - rendered.get_width(), right_top[1]))

    @staticmethod
    def _text_center(screen, text, position, role, *, variant=None, shadow=False):
        rendered = render_text(text, role, variant=variant, shadow=shadow)
        screen.blit(rendered, rendered.get_rect(center=position))

    @staticmethod
    def _text_center_fit(screen, text, position, role, max_width, *, variant=None, shadow=False):
        rendered = render_text(text, role, max_width, variant=variant, shadow=shadow)
        screen.blit(rendered, rendered.get_rect(center=position))

    @staticmethod
    def _card_name_lines(name, max_width):
        lines = wrap_text(name, "card_name", max_width)
        if len(lines) > 2:
            lines = lines[:2]
            lines[1] = lines[1].rstrip(" .") + "..."
        return lines

    @staticmethod
    def _draw_centered_name_lines(surface, lines, center, max_width, *, variant=None, top=None):
        line_height = get_font("card_name").get_linesize()
        if top is None:
            first_center_y = center[1] - ((len(lines) - 1) * line_height) // 2
        else:
            first_center_y = top + line_height // 2
        for index, line in enumerate(lines):
            rendered = render_text(line, "card_name", max_width, variant=variant)
            rect = rendered.get_rect(center=(center[0], first_center_y + index * line_height))
            surface.blit(rendered, rect)
        return (len(lines) - 1) * line_height

    @staticmethod
    def _text_center_spaced(screen, text, position, role, spacing, max_width):
        rendered = render_text_with_spacing(text, role, spacing, max_width)
        screen.blit(rendered, rendered.get_rect(center=position))

