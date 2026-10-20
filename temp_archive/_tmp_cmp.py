import io, json, difflib

base = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data'
spec = json.load(io.open(base + r'\NEW_DESCRIPTIONS_AND EFFECTS.json', encoding='utf-8'))
roster = json.load(io.open(base + r'\anime_bedriegers_bedrogen.json', encoding='utf-8'))['cards']

out = []
out.append('SPEC COUNT %d / ROSTER COUNT %d' % (len(spec), len(roster)))
out.append('')
for i, (s, c) in enumerate(zip(spec, roster)):
    same_name = s['character_name'].strip() == c.get('name', '').strip()
    same_en = s['effect_name'].strip() == (c.get('effect_name') or '').strip()
    same_txt = s['description'].strip() == (c.get('effect_text') or '').strip()
    ab = c.get('ability')
    out.append('[%d] %-24s | name_match=%s effect_name_match=%s effect_text_match=%s | ability=%s' % (
        i, c.get('name'), same_name, same_en, same_txt, (ab or {}).get('id')))
    if not same_txt:
        out.append('    SPEC : %s' % s['description'])
        out.append('    CARD : %s' % c.get('effect_text'))
    if not same_name:
        out.append('    SPEC NAME: %r' % s['character_name'])
    if not same_en:
        out.append('    SPEC EFFECT NAME: %r   CARD EFFECT NAME: %r' % (s['effect_name'], c.get('effect_name')))
    if ab:
        out.append('    cond=%s' % json.dumps(ab.get('conditions'), ensure_ascii=False))
        out.append('    eff =%s' % json.dumps(ab.get('effects'), ensure_ascii=False))
        out.append('    trig=%s lim=%s pres=%s' % (ab.get('trigger'), ab.get('activation_limit'), json.dumps(ab.get('presentation'), ensure_ascii=False)))
        if ab.get('targeting'):
            out.append('    tgt =%s' % json.dumps(ab.get('targeting'), ensure_ascii=False))
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_cmp.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('ok')
