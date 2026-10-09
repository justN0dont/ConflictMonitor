"""Voice calibration: measuring a speaker's pitch, and fitting the gender and age presets to it."""

import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import resample_poly

from voicechanger.calibrate import estimate_f0, load_voice_f0, save_voice_f0
from voicechanger.cli import main
from voicechanger.fileio import read_wav, write_wav
from voicechanger.presets import PRESETS, VOICE_FITS, fit_to_voice, make_settings

sys.path.insert(0, str(Path(__file__).parent))
from test_dsp import SR, lowest_harmonic_hz, voice_like, voice_with_pitch  # noqa: E402


def talking(f0, seconds=6.0, sr=SR):
    """A voice wandering +/-2 semitones around f0, in four-syllable phrases with pauses."""
    t = np.arange(int(seconds * sr)) / sr
    x = voice_with_pitch(f0 * 2 ** (2 * np.sin(2 * np.pi * 0.4 * t) / 12), f0=f0, sr=sr)
    return x * (np.sin(2 * np.pi * 0.8 * t) > -0.3)


@pytest.mark.parametrize("f0", [85, 120, 165, 210, 280])
def test_estimate_f0_finds_the_speaking_pitch(f0):
    got = estimate_f0(talking(f0), SR)
    assert got is not None and abs(got / f0 - 1) < 0.03, got


def test_estimate_f0_at_another_sample_rate():
    x = resample_poly(talking(150), 147, 160)  # 48 kHz -> 44.1 kHz
    assert abs(estimate_f0(x, 44100) / 150 - 1) < 0.03


def test_estimate_f0_gives_up_on_silence_and_noise():
    assert estimate_f0(np.zeros(6 * SR), SR) is None
    assert estimate_f0(0.05 * np.random.default_rng(1).standard_normal(6 * SR), SR) is None
    assert estimate_f0(talking(150, seconds=0.3), SR) is None  # too short to trust


def test_saved_calibration_round_trips_and_ignores_junk(tmp_path):
    p = tmp_path / "voice.json"
    assert load_voice_f0(p) is None
    save_voice_f0(117.26, p)
    assert load_voice_f0(p) == 117.3
    for junk in ("not json", '{"voice_f0": "x"}', '{"voice_f0": 5000}', "{}"):
        p.write_text(junk)
        assert load_voice_f0(p) is None


def test_presets_move_a_voice_into_their_range_and_leave_one_already_there():
    male, female = 114.0, 203.0
    assert fit_to_voice("feminine", male) == (7.5, 3.0)  # close to its fixed +7/+3
    assert fit_to_voice("feminine", female) == (0.0, 0.0)
    assert fit_to_voice("masculine", female) == (-8.5, -3.0)
    assert fit_to_voice("masculine", male) == (0.0, 0.0)
    assert fit_to_voice("kid", 95.0)[0] == 12.0  # never more than an octave
    assert fit_to_voice("deeper", male) is None  # an effect, not a target voice


@pytest.mark.parametrize("name", list(VOICE_FITS))
def test_fitted_presets_land_in_their_range(name):
    lo, hi, _ = VOICE_FITS[name]
    for f0 in (90, 115, 150, 205, 260):
        pitch, _ = fit_to_voice(name, f0)
        if abs(pitch) < 12:
            assert lo * 2 ** (-0.25 / 12) <= f0 * 2 ** (pitch / 12) <= hi * 2 ** (0.25 / 12), (f0, pitch)


def test_calibration_changes_only_pitch_and_formant():
    for name in PRESETS:
        plain, fitted = make_settings(name).to_dict(), make_settings(name, voice_f0=203.0).to_dict()
        changed = {k for k in plain if plain[k] != fitted[k]}
        assert changed <= {"pitch", "formant"}, (name, changed)
        assert bool(changed) <= (name in VOICE_FITS)


def test_cli_voice_f0_fits_the_preset(tmp_path):
    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    write_wav(str(src), voice_like(f0=205, seconds=1.0), SR)
    assert main(["file", str(src), str(dst), "--preset", "feminine", "--voice-f0", "205",
                 "--quality", "low-latency"]) == 0
    y, _ = read_wav(str(dst))
    assert abs(lowest_harmonic_hz(y) - 205) < 5  # already feminine: no shift
