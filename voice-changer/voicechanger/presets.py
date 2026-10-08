"""Built-in voices. Each preset is a set of overrides on top of the defaults.

Rules of thumb behind the numbers: the average male to female fundamental gap
is about an octave, but formants differ by only about 15-20% (roughly 3
semitones). So convincing gender shifts move pitch further than formants.
Moving the two in *opposite* directions decorrelates them, which makes a
voice much harder to recognise (the "anonymous" preset).

Character voices lean on cues listeners use to judge age and grit rather than
on big shifts alone. Aged voices are shaky: a 4-7 Hz tremor (tremor_hz,
tremor_depth) plus cycle-to-cycle pitch jitter, with aspiration noise from a
leaky glottis (breath) and less high end. Gravel is vocal fry: irregular
low-rate pulsing (gravel, gravel_hz about 30-50 Hz) that also gives the
"tough guy", pirate and valley-girl timbres their texture. The character
presets trim output_gain_db or comp_makeup_db so each stays within about 3 dB
of "natural" in loudness; each was measured on low (110 Hz) and high (210 Hz)
voices. (Some earlier presets, such as demon, robot and radio, run a few dB
hotter through their drive.)
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
    # characters
    "old_lady": {
        "pitch": 8, "formant": 2, "tremor_hz": 6, "tremor_depth": 0.4, "jitter": 0.6, "breath": 0.25,
        "gravel": 0.2, "gravel_hz": 65, "highpass_hz": 130, "lowpass_hz": 10000, "low_db": -4, "mid_db": 2,
        "comp_makeup_db": 3.5
    },
    "old_man": {
        "pitch": -3, "formant": -2, "tremor_hz": 5, "tremor_depth": 0.35, "jitter": 0.6, "breath": 0.15,
        "gravel": 0.25, "highpass_hz": 120, "lowpass_hz": 10000, "low_db": -3, "high_db": -2,
        "comp_makeup_db": 4
    },
    "kid": {
        "pitch": 12, "formant": 5, "breath": 0.12, "jitter": 0.3, "highpass_hz": 120, "low_db": -2,
        "mid_db": 2, "high_db": 2, "lowpass_hz": 14000, "output_gain_db": -0.5
    },
    "tough_guy": {
        "pitch": -5, "formant": -3, "gravel": 0.4, "gravel_hz": 45, "low_db": 4, "mid_db": 6, "high_db": -1,
        "lowpass_hz": 12000, "drive": 0.15, "comp_threshold_db": -22, "comp_ratio": 5, "comp_makeup_db": 0,
        "output_gain_db": -1.5, "gate_threshold_db": -46
    },
    "pirate": {
        "pitch": -3, "formant": -1.5, "jitter": 0.3, "breath": 0.15, "gravel": 0.4, "gravel_hz": 45,
        "low_db": 1.5, "mid_db": 4, "high_db": -3, "lowpass_hz": 8000, "drive": 0.12,
        "comp_threshold_db": -18, "comp_makeup_db": 0, "output_gain_db": -4.5
    },
    "valley_girl": {
        "pitch": 4, "formant": 2, "breath": 0.1, "gravel": 0.25, "gravel_hz": 35, "low_db": -2, "mid_db": 3,
        "high_db": 3, "output_gain_db": -0.5
    },
    "announcer": {
        "pitch": -4, "formant": -2, "breath": 0.01, "highpass_hz": 60, "low_db": 3, "high_db": 3.5,
        "comp_threshold_db": -23, "comp_ratio": 4, "comp_makeup_db": 3.5, "reverb_mix": 0.1,
        "reverb_room": 0.3
    },
    "villain": {
        "pitch": -5, "formant": -3, "gravel": 0.3, "gravel_hz": 42, "highpass_hz": 65, "lowpass_hz": 10000,
        "low_db": 4, "mid_db": -1, "high_db": -3, "comp_makeup_db": 4.5, "reverb_mix": 0.22,
        "reverb_room": 0.85
    },
    "raspy": {
        "formant": -1, "breath": 0.25, "gravel": 0.4, "gravel_hz": 35, "lowpass_hz": 11000, "low_db": 2.5,
        "high_db": -2, "drive": 0.12, "comp_threshold_db": -16, "comp_makeup_db": 0, "output_gain_db": -4.5
    },
    "ghost": {
        "pitch": 2, "formant": 2, "tremor_hz": 3.5, "tremor_depth": 0.7, "jitter": 0.3, "breath": 0.4,
        "highpass_hz": 200, "lowpass_hz": 10000, "low_db": -3, "mid_db": -3, "high_db": 2,
        "comp_threshold_db": -26, "comp_makeup_db": 6.5, "reverb_mix": 0.35, "reverb_room": 0.9
    },
    "ogre": {
        "pitch": -12, "formant": -6, "gravel": 0.7, "gravel_hz": 30, "highpass_hz": 45, "low_db": 4,
        "mid_db": 4, "drive": 0.2, "comp_makeup_db": 0, "reverb_mix": 0.1, "reverb_room": 0.5,
        "output_gain_db": -3
    },
    "alien": {
        "pitch": 6, "formant": -4, "tremor_hz": 9, "tremor_depth": 0.6, "gravel": 0.3, "gravel_hz": 180,
        "mid_db": 4, "high_db": 2, "reverb_mix": 0.12, "reverb_room": 0.3, "comp_makeup_db": 4.5
    },
    "megaphone": {
        "highpass_hz": 600, "lowpass_hz": 4000, "low_db": -9, "mid_db": 10, "high_db": -6, "drive": 0.65,
        "gate_threshold_db": -45, "comp_threshold_db": -12, "comp_ratio": 4, "comp_makeup_db": 0,
        "reverb_mix": 0.1, "reverb_room": 0.3, "output_gain_db": -10.5
    },
    "vibrato": {
        "tremor_hz": 5.5, "tremor_depth": 0.5, "low_db": -1.5, "mid_db": 2.5, "high_db": 5,
        "reverb_mix": 0.35, "reverb_room": 0.75, "comp_makeup_db": 3.4
    },
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
