"""Built-in voices. Each preset is a set of overrides on top of the defaults.

Rules of thumb behind the numbers: the average male to female fundamental gap
is about an octave, but formants differ by only about 15-20% (roughly 3
semitones). So convincing gender shifts move pitch further than formants.
Moving the two in *opposite* directions decorrelates them, which makes a
voice much harder to recognise (the "anonymous" preset).
"""

from __future__ import annotations

from .chain import Settings

PRESETS: dict[str, dict] = {
    "natural": {},
    "deeper": {"pitch": -4, "formant": -2, "low_db": 2},
    "feminine": {"pitch": 6, "formant": 3, "low_db": -2, "high_db": 2},
    "masculine": {"pitch": -6, "formant": -3, "low_db": 2, "high_db": -1},
    "anonymous": {"pitch": -3, "formant": 3, "drive": 0.1},
    "chipmunk": {"pitch": 9, "formant": 8},
    "giant": {"pitch": -10, "formant": -6, "low_db": 4, "reverb_mix": 0.25, "reverb_room": 0.8},
    "demon": {"pitch": -12, "formant": -2, "drive": 0.35, "low_db": 5, "reverb_mix": 0.3, "reverb_room": 0.85},
    "robot": {"mode": "robot", "robot_hz": 110, "mid_db": 3, "drive": 0.15},
    "android": {"mode": "robot", "robot_hz": 220, "formant": 2, "reverb_mix": 0.1},
    "whisper": {"mode": "whisper", "high_db": 3, "comp_makeup_db": 6},
    "radio": {"highpass_hz": 400, "lowpass_hz": 3400, "mid_db": 6, "drive": 0.4, "comp_ratio": 6},
    "cathedral": {"reverb_mix": 0.55, "reverb_room": 0.95},
}


def make_settings(name: str) -> Settings:
    if name not in PRESETS:
        raise KeyError(f"unknown preset {name!r}; choose from {', '.join(PRESETS)}")
    return Settings().update(**PRESETS[name])


def apply_preset(settings: Settings, name: str) -> Settings:
    """Reset ``settings`` in place to a preset (in place, so a running chain picks it up)."""
    fresh = make_settings(name)
    for k, v in fresh.to_dict().items():
        setattr(settings, k, v)
    return settings
