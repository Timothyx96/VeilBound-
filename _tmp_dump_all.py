import io, json

base = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data'
spec = json.load(io.open(base + r'\NEW_DESCRIPTIONS_AND EFFECTS.json', encoding='utf-8'))
roster = json.load(io.open(base + r'\anime_bedriegers_bedrogen.json', encoding='utf-8'))['cards']

out = []
out.append('SPEC COUNT %d / ROSTER COUNT %d' % (len(spec), len(roster)))
out.append('')
out.append('==== SPEC ====')
for i, s in enumerate(spec):
    out.append('[%d] name=%r effect=%r' % (i, s['character_name'], s['effect_name']))
    out.append('    desc: %s' % s['description'])
out.append('')
out.append('==== ROSTER ====')
for i, c in enumerate(roster):
    out.append('[%d] id=%s name=%r base=%s type=%r' % (i, c.get('id'), c.get('name'), c.get('base_points'), c.get('type')))
    out.append('    effect_name=%r' % c.get('effect_name'))
    out.append('    effect_text=%r' % c.get('effect_text'))
    ab = c.get('ability')
    if ab:
        out.append('    ABILITY:')
        for k, v in ab.items():
            out.append('        %s = %s' % (k, json.dumps(v, ensure_ascii=False)))
    else:
        out.append('    ABILITY: None')
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_all.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('ok', len(spec), len(roster))
