import io, json

path = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data\anime_bedriegers_bedrogen.json'
data = json.load(io.open(path, encoding='utf-8'))
print(type(data))
if isinstance(data, dict):
    print(list(data.keys())[:10])
    cards = data['cards']
out = []
for c in cards:
    ab = c.get('ability')
    out.append(f"== {c.get('id')} | name={c.get('name')} | type={c.get('type')} | base={c.get('base_points')}")
    if ab:
        out.append('   ' + json.dumps(ab, ensure_ascii=False))
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_roster.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('cards', len(cards))
