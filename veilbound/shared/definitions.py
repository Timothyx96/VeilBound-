from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path

from .abilities import parse_ability, validate_ability_references
from .config import PLAYER_POOL_SIZE
from .models import CardDefinition


DATA_DIR = Path(__file__).resolve().parents[1] / "assets" / "data"
EXPECTED_ROSTER_SIZE = 50

logger = logging.getLogger(__name__)


def load_card_definitions(path: Path | None = None) -> dict[str, CardDefinition]:
    with (path or DATA_DIR / "anime_bedriegers_bedrogen.json").open(encoding="utf-8") as source:
        raw_data = json.load(source)
    raw_cards = raw_data.get("cards") if isinstance(raw_data, dict) else None
    if not isinstance(raw_cards, list):
        raise ValueError("Character data must contain a 'cards' list")

    definitions: dict[str, CardDefinition] = {}
    duplicate_ids: set[str] = set()
    missing_ids = 0
    for index, raw in enumerate(raw_cards):
        raw_id = raw.get("id") if isinstance(raw, dict) else None
        if raw_id is None or not str(raw_id).strip():
            missing_ids += 1
            logger.warning("Character roster entry %d is missing an ID; entry skipped.", index + 1)
            continue
        card_id = str(raw_id)
        if card_id in definitions:
            duplicate_ids.add(card_id)
            continue
        ability_raw = raw.get("ability")
        definition = CardDefinition(
            id=card_id,
            name=str(raw["name"]),
            image=str(raw.get("image") or f"assets/art/characters/{card_id}.png"),
            base_points=int(raw["base_points"]),
            effect_name=str(raw.get("effect_name", "")),
            effect_text=str(raw.get("effect_text", "")),
            full_card_art=str(raw.get("full_card_art", "")),
            # Malformed/unsupported abilities fail loudly with the card named.
            ability=(
                parse_ability(ability_raw, context=f"Character {card_id}")
                if ability_raw is not None
                else None
            ),
        )
        if not definition.name or not definition.image:
            raise ValueError(f"Character {definition.id} needs a name and image path")
        definitions[definition.id] = definition
    if duplicate_ids:
        logger.warning(
            "Character roster has duplicate IDs (duplicate entries skipped): %s",
            ", ".join(sorted(duplicate_ids)),
        )
    if missing_ids:
        logger.warning("Character roster has %d entry/entries missing an ID.", missing_ids)
    if len(definitions) < EXPECTED_ROSTER_SIZE:
        logger.warning(
            "Character roster is missing %d unique character(s): found %d, expected %d.",
            EXPECTED_ROSTER_SIZE - len(definitions), len(definitions), EXPECTED_ROSTER_SIZE,
        )
    elif len(definitions) > EXPECTED_ROSTER_SIZE:
        logger.warning(
            "Character roster has %d extra unique character(s): found %d, expected %d.",
            len(definitions) - EXPECTED_ROSTER_SIZE, len(definitions), EXPECTED_ROSTER_SIZE,
        )
    if not definitions:
        raise ValueError("At least one card definition is required")
    # Character references and duplicate ability ids can only be checked once
    # the complete roster is known.
    validate_ability_references(definitions)
    return definitions


def deal_player_pool(
    roster: Mapping[str, object] | Iterable[str],
    pool_size: int = PLAYER_POOL_SIZE,
    seed: int | None = None,
) -> list[str]:
    """Compatibility wrapper; deck rules live with the rest of the rules module."""
    from .rules import deal_player_pool as deal

    return deal(roster, pool_size, seed)

