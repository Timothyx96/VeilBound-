import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = r'c:\Users\Wxnter\Documents\VeilBound'
d = json.load(open(ROOT + r'\veilbound\assets\data\anime_bedriegers_bedrogen.json', encoding='utf-8'))
cards = d['cards']
out = []
for c in cards:
    out.append('=== %s | %s | type=%s | base=%s | effect_name=%s' % (
        c.get('id'), c.get('name'), c.get('type', ''), c.get('base_points'), c.get('effect_name', '')))
    out.append('    effect_text: %s' % c.get('effect_text', ''))
    ab = c.get('ability')
    if ab:
        out.append('    ability: ' + json.dumps(ab, ensure_ascii=False, indent=2).replace('\n', '\n    '))
    else:
        out.append('    ability: NONE')
out.append('TOP KEYS: %s' % list(d.keys()))
open(ROOT + r'\_tmp_cardsdump.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('cards:', len(cards))
