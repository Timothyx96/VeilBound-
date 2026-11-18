"""Shared passive checks for abilities that protect their card owner."""


def passive_protection_blocks(target_card, attacker_card, cards, state=None) -> bool:
    """Whether a target's passive blocks this opposing ability.

    Broad immunity remains active even if an opposing suppression effect has
    marked the target's ability slot suppressed. Legacy elimination-only
    protection continues to respect suppression.
    """
    if attacker_card is None or target_card.owner_id == attacker_card.owner_id:
        return False
    if state is not None:
        for row in state.rows:
            if not any(card is not None and card.id == target_card.id for card in row.slots):
                continue
            seals = dict(row.get("yata_mirror_seals") or {})
            if str(target_card.owner_id) in seals:
                return True
            break
    definition = cards.get(target_card.definition_id)
    effects = [
        effect.type
        for ability in (getattr(definition, "abilities", None) or [])
        for effect in ability.effects
    ]
    if "ignore_opposing_abilities" in effects:
        return True
    return (
        "cannot_be_eliminated_by_opposing_ability" in effects
        and not bool(target_card.get("ability_suppressed", False))
    )


def clear_yata_mirror_if_revealed(row) -> bool:
    """Clear Yata Mirror once every occupied slot in its row is resolved."""
    if not row.get("yata_mirror_seals"):
        return False
    # Adjacent placement reveals happen before a row is complete. Keep the
    # seal until the row's full reveal sequence, not just its cards placed so far.
    if not row.is_full:
        return False
    if any(
        card is not None
        and not getattr(card, "revealed", False)
        and not getattr(card, "eliminated", False)
        for card in row.slots
    ):
        return False
    row.yata_mirror_seals = {}
    return True
