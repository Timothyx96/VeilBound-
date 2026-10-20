import io, json

base = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data'
spec = json.load(io.open(base + r'\NEW_DESCRIPTIONS_AND EFFECTS.json', encoding='utf-8'))
roster = json.load(io.open(base + r'\anime_bedriegers_bedrogen.json', encoding='utf-8'))['cards']
out = []
for i, (s, c) in enumerate(zip(spec, roster), 1):
    same_desc = s['description'].strip() == (c.get('effect_text') or '').strip()
    same_name = s['effect_name'].strip() == (c.get('effect_name') or '').strip()
    out.append(f"{i}. spec '{s['character_name']}' / '{s['effect_name']}'  || roster {c.get('id')} '{c.get('name')}' / '{c.get('effect_name')}'  desc_match={same_desc} name_match={same_name}  ability={'Y' if c.get('ability') else 'N'}")
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_diff2.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('ok')
