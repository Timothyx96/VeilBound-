import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"c:\Users\Wxnter\Documents\VeilBound"
REF = ROOT + r"\veilbound\assets\data\NEW_DESCRIPTIONS_AND EFFECTS.json"

ref = json.load(open(REF, encoding="utf-8-sig"))
print("TOTAL:", len(ref))
print("KEYS:", sorted({k for r in ref for k in r}))
print()
for i, r in enumerate(ref):
    print(f"--- [{i}] {r.get('character_name')}")
    for k, v in r.items():
        if k == "character_name":
            continue
        print(f"      {k}: {v!r}")
