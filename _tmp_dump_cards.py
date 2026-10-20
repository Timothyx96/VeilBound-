import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"c:\Users\Wxnter\Documents\VeilBound"
GAME = ROOT + r"\veilbound\assets\data\anime_bedriegers_bedrogen.json"
SPEC = ROOT + r"\veilbound\assets\data\NEW_DESCRIPTIONS_AND EFFECTS.json"

data = json.load(open(GAME, encoding="utf-8-sig"))
cards = data["cards"]
out = []
for c in cards:
    out.append(f"=== {c['id']} | {c['name']} | type={c.get('type')} | base={c.get('base_points')} | effect_name={c.get('effect_name')!r}")
    out.append(f"    effect_text: {c.get('effect_text')!r}")
    ab = c.get("ability")
    if ab:
        out.append(f"    ability.id={ab['id']} name={ab['name']!r} trig={ab['trigger']} limit={ab.get('activation_limit')}")
        out.append(f"      desc: {ab.get('description')!r}")
        out.append(f"      conditions: {json.dumps(ab.get('conditions'), ensure_ascii=False)}")
        out.append(f"      effects: {json.dumps(ab.get('effects'), ensure_ascii=False)}")
        out.append(f"      targeting: {json.dumps(ab.get('targeting'), ensure_ascii=False)}")
        out.append(f"      presentation: {json.dumps(ab.get('presentation'), ensure_ascii=False)}")
    else:
        out.append("    ability: None")
open(ROOT + r"\_tmp_cards_full.txt", "w", encoding="utf-8").write("\n".join(out))

spec = json.load(open(SPEC, encoding="utf-8-sig"))
print("SPEC TYPE:", type(spec), "KEYS:", list(spec.keys()) if isinstance(spec, dict) else len(spec))
sout = []
if isinstance(spec, dict):
    for k, v in spec.items():
        sout.append(f"@@@ KEY {k} :: {json.dumps(v, ensure_ascii=False, indent=3)}")
else:
    for i, v in enumerate(spec):
        sout.append(f"@@@ [{i}] :: {json.dumps(v, ensure_ascii=False, indent=3)}")
open(ROOT + r"\_tmp_spec_full.txt", "w", encoding="utf-8").write("\n".join(sout))
print("WROTE")
