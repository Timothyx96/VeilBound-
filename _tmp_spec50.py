import io, json
p = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data\NEW_DESCRIPTIONS_AND EFFECTS.json'
data = json.load(io.open(p, encoding='utf-8'))
out = []
for i, e in enumerate(data):
    out.append('=== [%d] %s | %s\n    %s' % (i, e.get('character_name'), e.get('effect_name'), e.get('description')))
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_spec50.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('count', len(data))
