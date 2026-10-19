import json
import pickle
import shutil

GAME = r'veilbound/assets/data/anime_bedriegers_bedrogen.json'
REF = r'veilbound/assets/data/NEW_DESCRIPTIONS_AND EFFECTS.json'

with open('update_plan.pkl', 'rb') as f:
    plan = pickle.load(f)['plan']

data = json.loads(open(GAME, encoding='utf-8-sig').read())
cards = data['cards']
matched_indices = {idx for idx, _, _ in plan}

# Context: which game cards have no reference counterpart (left unchanged)
unmatched_game = [c['name'] for i, c in enumerate(cards) if i not in matched_indices]
print('Game cards left unchanged (no reference match):', unmatched_game)

ref = json.loads(open(REF, encoding='utf-8-sig').read())
matched_ref_names = {r['character_name'].strip() for _, _, r in plan}
unmatched_ref = [r['character_name'] for r in ref if r['character_name'].strip() not in matched_ref_names]
print('Reference entries left unchanged (no game match):', unmatched_ref)

# Sanity: new values must not introduce unescaped JSON breaks
bad = [(idx, c['name'], k, v) for idx, c, r in plan for k, v in (('effect_name', r['effect_name']), ('effect_text', r['description'])) if '"' in v or '\n' in v]
assert not bad, bad

raw_bytes = open(GAME, 'rb').read()
text = raw_bytes.decode('utf-8-sig')

edits = 0
for idx, c, r in plan:
    for key, new_val in (('effect_name', r['effect_name']), ('effect_text', r['description'])):
        old_token = f'"{key}": "{c[key]}"'
        new_token = f'"{key}": "{new_val}"'
        n = text.count(old_token)
        assert n == 1, (idx, c['name'], key, n)
        if old_token != new_token:
            text = text.replace(old_token, new_token, 1)
            edits += 1
        else:
            print(f'  (unchanged: {c["name"]} {key} already matches reference)')

# Round-trip validation: file must still be valid JSON with identical structure
expected = {idx: {'effect_name': r['effect_name'], 'effect_text': r['description']} for idx, _, r in plan}
new_data = json.loads(text)
assert list(new_data.keys()) == list(data.keys())
assert len(new_data['cards']) == len(cards)
for i, (old_c, new_c) in enumerate(zip(cards, new_data['cards'])):
    old_keys = set(old_c); new_keys = set(new_c)
    assert old_keys == new_keys, (i, old_keys ^ new_keys)
    for k in old_keys:
        if k in expected.get(i, {}):
            assert new_c[k] == expected[i][k], (i, k, new_c[k])
        else:
            assert new_c[k] == old_c[k], (i, k, 'other field changed')

shutil.copy2(GAME, GAME + '.bak')
open(GAME, 'wb').write(text.encode('utf-8'))
print(f'Applied {edits} field updates to {GAME}')
print('Backup written to', GAME + '.bak')