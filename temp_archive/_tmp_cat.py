import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"c:\Users\Wxnter\Documents\VeilBound")
names = sys.argv[1:]
out = []
for n in names:
    p = ROOT / n
    text = p.read_text(encoding="utf-8")
    out.append(f"########## {n} ({len(text.splitlines())} lines) ##########")
    for i, line in enumerate(text.splitlines(), 1):
        out.append(f"{i:5d}| {line}")
(ROOT / "_tmp_cat.txt").write_text("\n".join(out), encoding="utf-8")
print("WROTE", len("\n".join(out)))
