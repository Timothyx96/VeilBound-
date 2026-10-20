import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"c:\Users\Wxnter\Documents\VeilBound"
GAME = ROOT + r"\veilbound\assets\data\anime_bedriegers_bedrogen.json"
REF = ROOT + r"\veilbound\assets\data\NEW_DESCRIPTIONS_AND EFFECTS.json"

game = json.load(open(GAME, encoding="utf-8-sig"))["cards"]
ref = json.load(open(REF, encoding="utf-8-sig"))

gidx = {c["name"].strip().lower(): c for c in game}
ridx = {r["character_name"].strip().lower(): r for r in ref}

print("=== ONLY IN GAME ===")
for c in game:
    if c["name"].strip().lower() not in ridx:
        print(" ", c["id"], "|", c["name"], "|", c["type"], "|", c["base_points"],
              "|", c["effect_name"], "|", c["effect_text"])

print()
print("=== ONLY IN REF ===")
for r in ref:
    if r["character_name"].strip().lower() not in gidx:
        print(" ", r["character_name"], "|", r["effect_name"], "|", r["description"])

print()
print("=== CHANGED ===")
for r in ref:
    key = r["character_name"].strip().lower()
    c = gidx.get(key)
    if c is None:
        continue
    diffs = []
    if c["effect_name"] != r["effect_name"]:
        diffs.append(f"effect_name: {c['effect_name']!r} -> {r['effect_name']!r}")
    if c["effect_text"] != r["description"]:
        diffs.append(f"effect_text:\n      OLD: {c['effect_text']}\n      NEW: {r['description']}")
    if diffs:
        print(f"-- {c['id']} | {c['name']} | {c['type']} | base={c['base_points']} | ability={(c.get('ability') or {}).get('id')}")
        for d in diffs:
            print("     ", d)

print()
print("=== UNCHANGED COUNT ===", sum(
    1 for r in ref
    if r["character_name"].strip().lower() in gidx
    and gidx[r["character_name"].strip().lower()]["effect_name"] == r["effect_name"]
    and gidx[r["character_name"].strip().lower()]["effect_text"] == r["description"]
))
