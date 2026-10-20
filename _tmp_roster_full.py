import io, json

base = r'c:\Users\Wxnter\Documents\VeilBound\veilbound\assets\data'
roster = json.load(io.open(base + r'\anime_bedriegers_bedrogen.json', encoding='utf-8'))
io.open(r'c:\Users\Wxnter\Documents\VeilBound\_tmp_roster_full.txt', 'w', encoding='utf-8').write(
    json.dumps(roster, indent=1, ensure_ascii=False))
print('ok')
