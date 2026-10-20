import io, json, sys

path = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data\NEW_DESCRIPTIONS_AND EFFECTS.json'
data = json.load(io.open(path, encoding='utf-8'))
out = []
for i, e in enumerate(data, 1):
    out.append(f"{i}. [{e.get('character_name')}] [{e.get('effect_name')}] {e.get('description')}")
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_spec_dump.txt', 'w', encoding='utf-8').write('\n'.join(out))
print(len(data))
