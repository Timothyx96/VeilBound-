import io, json

base = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data'
roster = json.load(io.open(base + r'\anime_bedriegers_bedrogen.json', encoding='utf-8'))['cards']
out = []
for i in (5, 16, 17, 18, 19, 28, 34, 36, 39, 40, 48, 0, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 20, 21, 22, 23, 24, 25, 26, 27, 29, 30, 31, 32, 33, 35, 37, 38, 41, 42, 43, 44, 45, 46, 47, 49):
    c = roster[i]
    out.append('[%d] %s | %r | base=%s type=%r' % (i, c.get('id'), c.get('name'), c.get('base_points'), c.get('type')))
    out.append('    effect_name=%r' % c.get('effect_name'))
    out.append('    effect_text=%r' % c.get('effect_text'))
    out.append('    image=%r full=%r' % (c.get('image'), c.get('full_card_art')))
    out.append('    ability=%s' % (c.get('ability') or {}).get('id'))
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_cards2.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('ok')
