# Veilbound

Veilbound is an offline, local two-player card game prototype built with Python and Pygame. The shared rules engine does not import Pygame; card definitions are JSON and the desktop client owns rendering and animation.

## Setup

Python 3.10 or newer is required. From the project directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks venv activation, use `.\.venv\Scripts\python.exe` in place of `python` for the following commands.

## Run

```powershell
python -m veilbound.client
```

Two players share the window. On each turn, the active player must drag a card from their hand into the next empty slot of a row; turns alternate until every slot in all four rows is filled. Players cannot pass or skip placement. The opening player gets the existing start-of-turn draw after the initial five-card hand. Press **Esc** to quit.

Press **F11** or **Alt+Enter** to toggle fullscreen. Press the same shortcut again to return to the previous window size.

Each row fills from left to right; only its next empty slot accepts a card. Placing a card reveals the previous card in that row, leaving the newest card face down. Once every slot is filled, the remaining face-down cards are revealed one at a time from Row 1 through Row 4 before scoring begins. Row totals count revealed cards only. A row winner gains points equal to that row's capacity. A tied row has no winner and awards no points. A Continue button can advance the round-result screen early; otherwise it advances after the configured presentation time. Match points carry forward across two rounds.

The character roster contains 50 unique IDs. At the start of a match, each player independently receives a random 40-character pool, with no duplicates within that player's pool. Pools may overlap between players. Each pool, hand, and used-card list persists through both rounds: cards in hand stay available, cards not drawn remain in the draw pile, and played cards are permanently used for that match. An empty draw pile never recycles used cards.

Character effects are descriptive text only. Scoring uses each character's `base_points`; effect text never changes game rules or scores. During reveals, a large card panel shows the character name, base points, effect name, and effect description together. Hovering over a character also shows its effect details.

## Tests

```powershell
python -m pytest -q
```

Coverage includes roster and 40-card pool validation, random-deal population, cross-round pool persistence and no card reuse, four rows and randomized capacities, sequential slot placement, turn alternation and hidden cards, reveal order, base-point totals, ties, point awards, round resets, animation settings, and image-loading fallbacks.

## Structure

```text
assets/
  art/characters/    Character PNG/JPG artwork
  art/card_backs/    Optional shared card-back art
  art/ui/            Board slot frames
  backgrounds/       Configurable battlefield backgrounds
  sounds/            Reveal and next-row sound effects
  fonts/             Optional bundled fonts
config/settings.json Central animation, audio, and background settings
veilbound/
  assets/data/       anime_bedriegers_bedrogen.json character definitions
  client/            Pygame app, assets, renderer, layout, input
  shared/            Models, config, engine, definitions, scoring
tests/               pytest gameplay and image-loading tests
```

## Add a character

1. Add a PNG or JPG portrait under `assets/art/characters/`.
2. Add a character object to `veilbound/assets/data/anime_bedriegers_bedrogen.json` with a unique `id`, `name`, integer `base_points`, `effect_name`, `effect_text`, and project-relative `image` path.
3. The asset loader caches images and scales them to fit without changing their aspect ratio. Missing or unreadable images use the generated placeholder portrait.

The game reads each character's `id`, `name`, `base_points`, `effect_name`, `effect_text`, and `image` from the character file. Effect descriptions are display-only; scoring uses base points only. Image paths point into `assets/art/characters/`. If an image is missing, a generated placeholder portrait is used.

## Settings

Edit `config/settings.json` and restart the game to change the selected animation preset or background. The default `Slow` preset uses reveal and scoring timings at least ten times longer than the previous animation timings. `Normal` and `Fast` provide shorter alternatives. Individual millisecond values can also be edited in the JSON.

The settings file configures looping background music, the seven reveal sounds in `assets/sounds/Abillity_Sfx/`, the Next Row sound in `assets/sounds/New_row/`, and the card placement sound in `assets/sounds/card_place/`. Reveal sounds are chosen with equal probability, and each placement or reveal event plays at most once. Music can be disabled or its volume changed under `audio.background_music`. Missing or unsupported audio files are skipped safely.

The included background is registered as `anime_samurai_worlds`. Add another key and project-relative image path under `background.presets`, then set `background.selected` to that key. Backgrounds retain their aspect ratio; invalid paths fall back to the game's dark background.
