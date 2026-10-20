import io, json

base = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data'
spec = json.load(io.open(base + r'\NEW_DESCRIPTIONS_AND EFFECTS.json', encoding='utf-8'))
roster = json.load(io.open(base + r'\anime_bedriegers_bedrogen.json', encoding='utf-8'))['cards']

lines = []
lines.append('ROSTER COUNT %d, SPEC COUNT %d' % (len(roster), len(spec)))
lines.append('')
lines.append('=== ROSTER ===')
for i, c in enumerate(roster):
    ab = c.get('ability')
    abtxt = ''
    if ab:
        abtxt = ' AB[' + ab.get('id', '') + '|' + ab.get('name', '') + '|trig=' + str(ab.get('trigger')) +
                 '|cond=' + json.dumps(ab.get('conditions'), ensure_ascii=False) +
                 '|eff=' + json.dumps(ab.get('effects'), ensure_ascii=False) +
                 '|pres=' + json.dumps(ab.get('presentation'), ensure_ascii=False) +
                 '|tgt=' + json.dumps(ab.get('targeting'), ensure_ascii=False) +
                 '|lim=' + str(ab.get('activation_limit')) + ']'
    lines.append('[%d] id=%s name=%r base=%s effect_name=%r effect_text=%r%s' % (
        i, c.get('id'), c.get('name'), c.get('base_points'), c.get('effect_name'), c.get('effect_text'), abtxt))
lines.append('')
lines.append('=== SPEC ===')
for i, s in enumerate(spec):
    lines.append('[%d] %r | %r | %r' % (i, s.get('character_name'), s.get('effect_name'), s.get('description')))
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_map.txt', 'w', encoding='utf-8').write('\n'.join(lines))
print('ok')
