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
"tough guy", pirate and valley-girl timbres their texture.

Shifting pitch moves a voice's energy around: a lowered voice gains a
sub-bass fundamental and loses presence, a raised one turns thin and bright.
So lowered presets high-pass at 65-80 Hz, ease the low mids with the low
shelf and lift presence around 2.5-3 kHz with the mid band; raised ones
high-pass at 110-150 Hz and soften the top with the high shelf.

Every preset matches "natural" in loudness within about 0.7 dB (K-weighted,
over speech, balanced quality) on real male and female speech (VCTK) at
-38, -30 and -22 dBFS speech RMS, from a quiet laptop mic to a hot headset;
tests/test_preset_loudness.py checks a synthetic version. Two things keep it
true at any mic level. Drive is level-independent (see effects.Drive), so it
neither adds gain on a quiet mic nor squashes harder on a loud one. And each
preset's compressor starts working where natural's does: one whose shifter,
EQ or effects leave its signal N dB quieter going into the compressor sets
its threshold N dB lower. output_gain_db then matches the level.
"""

from __future__ import annotations

from .chain import Settings

# Every preset starts from these: gentle 2:1 compression from -30 dBFS (about
# 3 dB off the louder syllables at a typical speaking level), so all voices
# share one density and keep the same loudness as each other at any mic level.
BASE = {"comp_threshold_db": -30, "comp_ratio": 2, "comp_makeup_db": 5}

PRESETS: dict[str, dict] = {
    "natural": {},
    "deeper": {
        "pitch": -4, "formant": -1.5, "highpass_hz": 75, "low_hz": 250, "low_db": -1.5, "mid_hz": 3000,
        "mid_q": 0.8, "mid_db": 2, "high_hz": 9000, "high_db": 1, "comp_threshold_db": -31, "output_gain_db": 1
    },
    "feminine": {
        "pitch": 7, "formant": 3, "highpass_hz": 120, "low_hz": 250, "low_db": -1, "high_hz": 8000,
        "high_db": -1, "comp_threshold_db": -31, "output_gain_db": 0.5
    },
    "masculine": {
        "pitch": -7, "formant": -2.5, "highpass_hz": 75, "low_hz": 300, "low_db": -2, "mid_hz": 2800,
        "mid_q": 0.8, "mid_db": 2.5, "high_hz": 9000, "high_db": 1, "comp_threshold_db": -31,
        "output_gain_db": 1
    },
    "anonymous": {
        "pitch": -3, "formant": 3, "highpass_hz": 90, "mid_hz": 2500, "mid_q": 0.8, "mid_db": 1, "drive": 0.15,
        "comp_threshold_db": -30
    },
    "chipmunk": {
        "pitch": 9, "formant": 8, "highpass_hz": 150, "low_hz": 300, "low_db": 2, "high_hz": 7000,
        "high_db": -2, "output_gain_db": -0.5
    },
    "giant": {
        "pitch": -10, "formant": -6, "highpass_hz": 65, "low_hz": 350, "low_db": -2.5, "mid_hz": 2500,
        "mid_q": 0.8, "mid_db": 3, "high_hz": 8000, "high_db": 1.5, "comp_threshold_db": -32,
        "reverb_mix": 0.15, "reverb_room": 0.7, "output_gain_db": 2.5
    },
    "demon": {
        "pitch": -12, "formant": -2, "mix": 0.85, "highpass_hz": 65, "lowpass_hz": 9000, "low_hz": 300,
        "low_db": -2, "mid_hz": 2200, "mid_q": 0.8, "mid_db": 2, "drive": 0.8, "comp_threshold_db": -33,
        "reverb_mix": 0.2, "reverb_room": 0.8, "output_gain_db": 3
    },
    "robot": {
        "mode": "robot", "robot_hz": 110, "highpass_hz": 120, "lowpass_hz": 12000, "mid_hz": 1800, "mid_q": 1,
        "mid_db": 1, "high_db": -1.5, "drive": 0.35, "comp_threshold_db": -33, "output_gain_db": 2
    },
    "android": {
        "formant": 2, "mode": "robot", "robot_hz": 220, "highpass_hz": 150, "high_db": -2,
        "comp_threshold_db": -33, "reverb_mix": 0.1, "reverb_room": 0.3, "output_gain_db": 2
    },
    "whisper": {
        "mode": "whisper", "highpass_hz": 150, "high_hz": 6000, "high_db": -1, "comp_threshold_db": -33,
        "output_gain_db": 2.5
    },
    "radio": {
        "highpass_hz": 350, "lowpass_hz": 3400, "mid_hz": 1800, "mid_q": 1.2, "mid_db": 4, "drive": 0.55,
        "comp_threshold_db": -32, "output_gain_db": 2
    },
    "cathedral": {
        "highpass_hz": 90, "low_hz": 250, "low_db": -1, "comp_threshold_db": -30, "reverb_mix": 0.45,
        "reverb_room": 0.9, "output_gain_db": 0.5
    },
    # characters
    "old_lady": {
        "pitch": 8, "formant": 2, "tremor_hz": 6, "tremor_depth": 0.35, "jitter": 0.5, "breath": 0.25,
        "gravel": 0.15, "gravel_hz": 65, "highpass_hz": 140, "lowpass_hz": 10000, "low_hz": 250, "low_db": -2,
        "mid_hz": 3000, "mid_q": 0.8, "mid_db": -2.5, "high_hz": 6500, "high_db": -2, "comp_threshold_db": -32,
        "output_gain_db": 1.5
    },
    "old_man": {
        "pitch": -1, "formant": -1, "tremor_hz": 5, "tremor_depth": 0.3, "jitter": 0.55, "breath": 0.2,
        "gravel": 0.2, "highpass_hz": 110, "lowpass_hz": 10000, "low_hz": 250, "low_db": -2, "mid_hz": 3000,
        "mid_q": 0.8, "mid_db": -1, "high_hz": 7000, "high_db": -2, "comp_threshold_db": -32,
        "output_gain_db": 2
    },
    "kid": {
        "pitch": 12, "formant": 5, "jitter": 0.3, "breath": 0.12, "highpass_hz": 150, "low_hz": 300,
        "low_db": 1, "mid_hz": 3500, "mid_q": 0.9, "mid_db": -1, "high_hz": 8000, "high_db": -2,
        "comp_threshold_db": -31
    },
    "tough_guy": {
        "pitch": -4, "formant": -2.5, "gravel": 0.35, "gravel_hz": 45, "highpass_hz": 80, "lowpass_hz": 12000,
        "low_hz": 300, "low_db": -1, "mid_hz": 1200, "mid_q": 0.8, "mid_db": 3, "high_hz": 8000, "high_db": -1,
        "drive": 0.25, "comp_threshold_db": -31, "output_gain_db": 1.5
    },
    "pirate": {
        "pitch": -3, "formant": -1.5, "jitter": 0.3, "breath": 0.15, "gravel": 0.35, "gravel_hz": 45,
        "highpass_hz": 80, "lowpass_hz": 9000, "low_hz": 300, "low_db": -1, "mid_hz": 1200, "mid_q": 0.8,
        "mid_db": 3, "high_hz": 6000, "high_db": -2, "drive": 0.2, "comp_threshold_db": -31, "output_gain_db": 1
    },
    "valley_girl": {
        "pitch": 5, "formant": 2, "breath": 0.12, "gravel": 0.2, "gravel_hz": 35, "highpass_hz": 130,
        "low_hz": 250, "low_db": -1, "high_hz": 9000, "high_db": 2, "comp_threshold_db": -32,
        "output_gain_db": 1
    },
    "announcer": {
        "pitch": -2.5, "formant": -1, "highpass_hz": 75, "low_hz": 120, "low_db": 1.5, "mid_hz": 3000,
        "mid_q": 0.7, "mid_db": 2.5, "high_hz": 10000, "high_db": 2, "comp_threshold_db": -30,
        "comp_ratio": 2.5, "reverb_mix": 0.06, "reverb_room": 0.3, "output_gain_db": 0.5
    },
    "villain": {
        "pitch": -4, "formant": -2.5, "gravel": 0.3, "gravel_hz": 42, "highpass_hz": 75, "lowpass_hz": 11000,
        "low_hz": 300, "low_db": -1, "mid_hz": 2500, "mid_q": 0.8, "mid_db": 1, "high_hz": 6000, "high_db": -3,
        "comp_threshold_db": -32, "reverb_mix": 0.15, "reverb_room": 0.7, "output_gain_db": 2.5
    },
    "raspy": {
        "formant": -1, "breath": 0.25, "gravel": 0.4, "gravel_hz": 35, "highpass_hz": 80, "lowpass_hz": 11000,
        "low_hz": 250, "low_db": 1, "high_hz": 7000, "high_db": -1.5, "drive": 0.25, "comp_threshold_db": -31,
        "output_gain_db": 1
    },
    "ghost": {
        "pitch": 2, "formant": 2, "tremor_hz": 3.5, "tremor_depth": 0.7, "jitter": 0.3, "breath": 0.4,
        "highpass_hz": 200, "lowpass_hz": 9000, "low_hz": 400, "low_db": -3, "mid_db": -3,
        "comp_threshold_db": -33, "reverb_mix": 0.35, "reverb_room": 0.9, "output_gain_db": 2.5
    },
    "ogre": {
        "pitch": -12, "formant": -6, "gravel": 0.6, "gravel_hz": 30, "highpass_hz": 65, "low_hz": 350,
        "low_db": -2, "mid_hz": 1800, "mid_q": 0.8, "mid_db": 3, "drive": 0.45, "comp_threshold_db": -34,
        "reverb_mix": 0.1, "reverb_room": 0.5, "output_gain_db": 4
    },
    "alien": {
        "pitch": 6, "formant": -4, "tremor_hz": 9, "tremor_depth": 0.6, "gravel": 0.3, "gravel_hz": 180,
        "highpass_hz": 100, "low_hz": 400, "low_db": -2, "mid_hz": 1800, "mid_q": 1, "mid_db": 3,
        "high_hz": 7000, "high_db": 1.5, "comp_threshold_db": -32, "reverb_mix": 0.12, "reverb_room": 0.3,
        "output_gain_db": 2.5
    },
    "megaphone": {
        "highpass_hz": 600, "lowpass_hz": 4000, "low_hz": 300, "low_db": -6, "mid_hz": 1500, "mid_q": 1.2,
        "mid_db": 8, "high_db": -4, "drive": 0.6, "comp_threshold_db": -32, "reverb_mix": 0.08,
        "reverb_room": 0.3, "output_gain_db": 2
    },
    "vibrato": {
        "tremor_hz": 5.5, "tremor_depth": 0.5, "highpass_hz": 90, "mid_hz": 2500, "mid_q": 0.8, "mid_db": 1.5,
        "high_hz": 9000, "high_db": 2.5, "comp_threshold_db": -30, "reverb_mix": 0.25, "reverb_room": 0.7,
        "output_gain_db": 1
    },
}


def make_settings(name: str) -> Settings:
    if name not in PRESETS:
        raise KeyError(f"unknown preset {name!r}; choose from {', '.join(PRESETS)}")
    return Settings().update(**{**BASE, **PRESETS[name]})


# These belong to the microphone and the room, not to a voice, so switching
# presets keeps them (and no preset sets them).
MIC_SETUP = ("input_gain_db", "gate_enabled", "gate_threshold_db")


def apply_preset(settings: Settings, name: str) -> Settings:
    """Reset ``settings`` in place to a preset (in place, so a running chain picks it up),
    keeping the mic set-up (MIC_SETUP)."""
    fresh = make_settings(name)
    for k, v in fresh.to_dict().items():
        if k not in MIC_SETUP:
            setattr(settings, k, v)
    return settings
