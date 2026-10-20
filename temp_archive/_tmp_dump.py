import json, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
d = json.load(open('veilbound/assets/data/NEW_DESCRIPTIONS_AND EFFECTS.json', encoding='utf-8-sig'))
print('N =', len(d))
for i, item in enumerate(d, 1):
    print(f"[{i}] {item['character_name']} | {item['effect_name']}")
    print(f"    {item['description']}")
    extra = {k: v for k, v in item.items() if k not in ('character_name', 'effect_name', 'description')}
    if extra:
        print(f"    EXTRA: {json.dumps(extra, ensure_ascii=False)}")
