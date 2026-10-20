import io, json

base = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data'
out = []
for fn in ('anime_bedriegers_bedrogen.json', 'anime_bedriegers_bedrogen.json.bak'):
    data = json.load(io.open(base + '\\' + fn, encoding='utf-8'))
    out.append('#### ' + fn + ' keys=' + str(list(data.keys())))
    cards = data.get('cards', [])
    out.append('count=' + str(len(cards)))
    for c in cards:
        out.append(f"-- {c.get('id')} | {c.get('name')} | type={c.get('type')} | base={c.get('base_points')} | effect_name={c.get('effect_name')} | effect_text={c.get('effect_text')}")
        if c.get('ability'):
            out.append('   ABILITY ' + json.dumps(c['ability'], ensure_ascii=False))
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_roster2.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('done')
