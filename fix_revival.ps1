import os

rules_path = os.path.join('veilbound', 'shared', 'rules.py')

with open(rules_path, 'r') as f:
    content = f.read()

old_block = '''            self._emit(
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
            )'''

new_block = '''            self._emit(
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
                },
            )'''

if old_block in content:
    content = content.replace(old_block, new_block)
    with open(rules_path, 'w') as f:
        f.write(content)
    print('Successfully updated rules.py')
else:
    print('ERROR: Could not find the expected block')
