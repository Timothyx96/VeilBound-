"""Shared passive checks for abilities that protect their card owner."""


def passive_protection_blocks(target_card, attacker_card, cards) -> bool:
    """Whether a target's passive blocks this opposing ability.

    Broad immunity remains active even if an opposing suppression effect has
    marked the target's ability slot suppressed. Legacy elimination-only
    protection continues to respect suppression.
    """
    if attacker_card is None or target_card.owner_id == attacker_card.owner_id:
        return False
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
