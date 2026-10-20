import io, json, re, unicodedata

base = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data'
spec = json.load(io.open(base + r'\NEW_DESCRIPTIONS_AND EFFECTS.json', encoding='utf-8'))
roster = json.load(io.open(base + r'\anime_bedriegers_bedrogen.json', encoding='utf-8'))['cards']

def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = s.replace('_', ' ').replace('.', '')
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s

index = {}
for i, c in enumerate(roster):
    index.setdefault(norm(c.get('name')), []).append(i)

out = []
used = set()
for si, s in enumerate(spec):
    hits = index.get(norm(s['character_name']), [])
    out.append('SPEC[%d] %r' % (si, s['character_name']))
    if len(hits) != 1:
        out.append('   !! NO/AMBIGUOUS MATCH (%d hits)' % len(hits))
        continue
    ci = hits[0]
    used.add(ci)
    c = roster[ci]
    ab = c.get('ability')
    out.append('   -> CARD[%d] id=%s name=%r type=%r base=%s' % (ci, c.get('id'), c.get('name'), c.get('type'), c.get('base_points')))
    out.append('      effect_name: CARD=%r SPEC=%r  %s' % (c.get('effect_name'), s['effect_name'], 'SAME' if (c.get('effect_name') or '').strip()==s['effect_name'].strip() else 'DIFF'))
    same = (c.get('effect_text') or '').strip() == s['description'].strip()
    out.append('      effect_text %s' % ('SAME' if same else 'DIFF'))
    if not same:
        out.append('        CARD: %s' % c.get('effect_text'))
        out.append('        SPEC: %s' % s['description'])
    out.append('      ability=%s' % (ab or {}).get('id'))
    if ab:
        out.append('         cond=%s' % json.dumps(ab.get('conditions'), ensure_ascii=False))
        out.append('         eff =%s' % json.dumps(ab.get('effects'), ensure_ascii=False))
        out.append('         trig=%s lim=%s pres=%s' % (ab.get('trigger'), ab.get('activation_limit'), json.dumps(ab.get('presentation'), ensure_ascii=False)))
        if ab.get('targeting'):
            out.append('         tgt =%s' % json.dumps(ab.get('targeting'), ensure_ascii=False))
out.append('')
out.append('UNMATCHED CARDS: %s' % [(i, roster[i].get('name')) for i in range(len(roster)) if i not in used])
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_map2.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('ok')
