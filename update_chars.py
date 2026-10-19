import json
import re

GAME = r'veilbound/assets/data/anime_bedriegers_bedrogen.json'
REF = r'veilbound/assets/data/NEW_DESCRIPTIONS_AND EFFECTS.json'

raw_bytes = open(GAME, 'rb').read()
print('first bytes:', raw_bytes[:20])
print('has CRLF:', b'\r\n' in raw_bytes)
text = raw_bytes.decode('utf-8-sig')
data = json.loads(text)
cards = data['cards']
print('game cards:', len(cards))

refs = json.loads(open(REF, encoding='utf-8-sig').read())
print('ref entries:', len(refs))

# Index existing cards by normalized name; detect ambiguity
index = {}
ambiguous_keys = set()
for c in cards:
    key = c['name'].strip().lower()
    if key in index:
        ambiguous_keys.add(key)
    index.setdefault(key, []).append(c)

plan = []
unmatched = []
ambiguous_skipped = []
for r in refs:
    name = r['character_name'].strip()
    key = name.lower()
    hits = index.get(key, [])
    if not hits:
        unmatched.append(name)
        continue
    if len(hits) > 1 or key in ambiguous_keys:
        ambiguous_skipped.append((name, [h['name'] for h in hits]))
        continue
    plan.append((cards.index(hits[0]), hits[0], r))

print('\n--- PLAN (matched) ---')
for idx, c, r in plan:
    print(f"[{idx}] '{c['name']}'")
    print(f"    effect_name: '{c['effect_name']}' -> '{r['effect_name']}'")
    print(f"    effect_text: '{c['effect_text'][:60]}...' -> '{r['description'][:60]}...'")

print('\n--- UNMATCHED ---', unmatched)
print('--- AMBIGUOUS ---', ambiguous_skipped)
print('Total matched:', len(plan))

# Uniqueness check of old value tokens in raw text
def count_token(key, val):
    return len(re.compile(re.escape(f'"{key}": "{val}"')).findall(text))

dup = []
for idx, c, r in plan:
    if count_token('effect_name', c['effect_name']) != 1:
        dup.append(('name', c['name'], c['effect_name']))
    if count_token('effect_text', c['effect_text']) != 1:
        dup.append(('text', c['name'], c['effect_text'][:50]))
print('non-unique tokens:', dup)

# Save plan for the apply step
import pickle
with open('update_plan.pkl', 'wb') as f:
    pickle.dump({'plan': plan}, f)
print('\nPlan saved to update_plan.pkl')