import sys
sys.path.insert(0, '.')
from veilbound.shared import abilities

print('SUPPORTED_EFFECT_TYPES with opposing:')
for t in abilities.SUPPORTED_EFFECT_TYPES:
    if 'opposing' in t:
        print('  ' + t)

broly = None
for char in abilities.CHARACTER_DATA:
    if 'broly' in char.get('id', '').lower():
        broly = char
        break

if broly:
    print()
    print('Broly ability:')
    print('  name: ' + str(broly.get('ability', {}).get('name')))
    print('  description: ' + str(broly.get('ability', {}).get('description')))
    for effect in broly.get('ability', {}).get('effects', []):
        print('  effect type: ' + str(effect.get('type')))
