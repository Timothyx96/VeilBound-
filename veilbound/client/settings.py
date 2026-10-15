from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"

DEFAULT_PRESETS: dict[str, dict[str, int]] = {
    "Slow": {
        "card_flip_ms": 6200,
        "delay_between_reveals_ms": 8600,
        "card_information_ms": 12500,
        "score_count_ms": 20000,
        "row_result_ms": 25000,
        "row_transition_ms": 7200,
        "final_round_result_ms": 25000,
    },
    "Normal": {
        "card_flip_ms": 1500,
        "delay_between_reveals_ms": 1000,
        "card_information_ms": 2500,
        "score_count_ms": 2000,
        "row_result_ms": 2500,
        "row_transition_ms": 1000,
        "final_round_result_ms": 5000,
    },
    "Fast": {
        "card_flip_ms": 300,
        "delay_between_reveals_ms": 200,
        "card_information_ms": 600,
        "score_count_ms": 750,
        "row_result_ms": 900,
        "row_transition_ms": 250,
        "final_round_result_ms": 1500,
    },
}


@dataclass(frozen=True)
class AppSettings:
    animation_preset: str
    animation_ms: dict[str, int]
    audio: dict[str, Any]
    card_frame_size: tuple[float, float]
    background_name: str | None
    background_path: str | None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _valid_duration(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return fallback
    return milliseconds if milliseconds >= 0 else fallback


def _valid_scale(value: Any, fallback: float = 1.0) -> float:
    if isinstance(value, bool):
        return fallback
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(3.0, max(0.1, scale)) if math.isfinite(scale) else fallback


def load_settings(path: Path | None = None) -> AppSettings:
    """Load settings safely; malformed optional settings fall back to Slow/no art."""
    raw = _read_json(path or SETTINGS_PATH)
    animation = raw.get("animation", {})
    if not isinstance(animation, dict):
        animation = {}
    presets = animation.get("presets", {})
    if not isinstance(presets, dict):
        presets = {}

    preset_name = animation.get("preset", "Slow")
    if not isinstance(preset_name, str) or preset_name not in DEFAULT_PRESETS:
        preset_name = "Slow"
    supplied_preset = presets.get(preset_name, {})
    if not isinstance(supplied_preset, dict):
        supplied_preset = {}
    animation_ms = {
        name: _valid_duration(supplied_preset.get(name), default)
        for name, default in DEFAULT_PRESETS[preset_name].items()
    }

    audio = raw.get("audio", {})
    if not isinstance(audio, dict):
        audio = {}
    card_frame = raw.get("card_frame", {})
    if not isinstance(card_frame, dict):
        card_frame = {}
    placed_frame = card_frame.get("placed", {})
    if not isinstance(placed_frame, dict):
        placed_frame = {}
    card_frame_size = (
        _valid_scale(placed_frame.get("width")),
        _valid_scale(placed_frame.get("height")),
    )
    background = raw.get("background", {})
    if not isinstance(background, dict):
        background = {}
    background_presets = background.get("presets", {})
    if not isinstance(background_presets, dict):
        background_presets = {}
    valid_backgrounds = {
        name: value for name, value in background_presets.items()
        if isinstance(name, str) and isinstance(value, str) and value.strip()
    }
    background_name = background.get("selected")
    if not isinstance(background_name, str) or background_name not in valid_backgrounds:
        background_name = next(iter(valid_backgrounds), None)

    return AppSettings(
        animation_preset=preset_name,
        animation_ms=animation_ms,
        audio=audio,
        card_frame_size=card_frame_size,
        background_name=background_name,
        background_path=valid_backgrounds.get(background_name),
    )
