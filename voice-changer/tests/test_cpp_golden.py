"""Holds the C++ DSP (the code that runs inside Windows' audio engine) to the
Python reference implementation, sample for sample.

Needs the `dsp_cli` binary: `cmake -S apo -B apo/build && cmake --build apo/build`.
Skipped when it hasn't been built.
"""

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from voicechanger.chain import QUALITY, Settings, VoiceChain
from voicechanger.dsp.rng import hash_uniform_vec
from voicechanger.dsp.spectral import MODES
from voicechanger.presets import PRESETS, make_settings

sys.path.insert(0, str(Path(__file__).parent))
from test_character import MAXED, PINNED  # noqa: E402
from test_dsp import voice_like  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = [ROOT / "apo/build/dsp_cli", ROOT / "apo/build/Release/dsp_cli.exe", ROOT / "apo/build/dsp_cli.exe"]
CLI = next((p for p in CANDIDATES if p.exists()), None)
if os.environ.get("VC_DSP_CLI"):
    CLI = Path(os.environ["VC_DSP_CLI"])

pytestmark = pytest.mark.skipif(CLI is None, reason="dsp_cli not built")
SR = 48000
TOL = 1e-4  # max abs difference; in practice the two agree to float32 rounding of the output


def settings_ini(settings, quality: str) -> str:
    lines = [f"quality={quality}"]
    for k, v in settings.to_dict().items():
        lines.append(f"{k}={int(v) if isinstance(v, bool) else v}")
    return "\n".join(lines) + "\n"


def run_cpp(tmp_path, settings, quality, x, block, alt=None, period=0, sr=SR):
    (tmp_path / "s.ini").write_text(settings_ini(settings, quality))
    x.astype("<f4").tofile(tmp_path / "in.f32")
    cmd = [str(CLI), str(sr), str(block), str(tmp_path / "s.ini"), str(tmp_path / "in.f32"), str(tmp_path / "out.f32")]
    if alt is not None:
        (tmp_path / "alt.ini").write_text(settings_ini(alt, quality))
        cmd += [str(tmp_path / "alt.ini"), str(period)]
    report = dict(line.split() for line in subprocess.run(cmd, check=True, capture_output=True,
                                                           text=True).stdout.splitlines())
    # VoiceChain::process runs on the audio thread inside the APO: it must never allocate.
    assert report["allocations"] == "0", f"{report['allocations']} heap allocations on the audio path"
    return np.fromfile(tmp_path / "out.f32", "<f4").astype(np.float64)


def run_py(settings, quality, x, block, alt=None, period=0, sr=SR):
    """Blocks alternate between ``settings`` and ``alt`` every ``period`` blocks, like dsp_cli."""
    chain = VoiceChain(sr, quality, settings)
    assert chain.block_size == block
    out = []
    for b, i in enumerate(range(0, len(x) - block + 1, block)):
        chain.settings = alt if alt is not None and (b // period) % 2 else settings
        out.append(chain.process(x[i:i + block]))
    return np.concatenate(out)


def assert_match(tmp_path, settings, quality, x, label, alt=None, period=0, sr=SR):
    block = QUALITY[quality][2]
    py = run_py(settings, quality, x, block, alt, period, sr)
    cpp = run_cpp(tmp_path, settings, quality, x, block, alt, period, sr)[:len(py)]
    peak = np.max(np.abs(py))
    assert np.std(py) > 1e-4, f"{label}: silent output proves nothing"
    err = np.max(np.abs(py - cpp))
    # Tighter for quiet outputs (peak < 0.1), so a case can't pass just by being quiet.
    assert err < TOL * min(1.0, 10 * peak), f"{label} @ {quality}: max abs difference {err:.2e} (peak {peak:.2e})"


def test_cpp_hash_uniform_matches_pinned_values():
    for stream_id, counter, want in PINNED:
        out = subprocess.run([str(CLI), "--hash", str(stream_id), str(counter)],
                             check=True, capture_output=True, text=True).stdout
        assert float(out) == want, (stream_id, counter)


@pytest.mark.parametrize("stream_id", [1, 2, 3, 4, 5])
def test_cpp_hash_uniform_matches_python_bit_for_bit(stream_id):
    rng = np.random.default_rng(stream_id)
    counters = np.concatenate((np.arange(300, dtype=np.uint64), rng.integers(0, 2**64 - 1, 300, dtype=np.uint64)))
    out = subprocess.run([str(CLI), "--hash", str(stream_id), *map(str, counters)],
                         check=True, capture_output=True, text=True).stdout.split()
    assert np.array_equal(np.array(out, dtype=np.float64), hash_uniform_vec(stream_id, counters))


# Whisper included: its noise comes from the counter-based RNG, so it is exact too.
@pytest.mark.parametrize("preset", list(PRESETS))
def test_cpp_matches_python(tmp_path, preset):
    x = voice_like(f0=140, seconds=1.5).astype(np.float32).astype(np.float64)
    assert_match(tmp_path, make_settings(preset), "balanced", x, preset)


@pytest.mark.parametrize("quality", list(QUALITY))
@pytest.mark.parametrize("preset", ["feminine", "whisper"])
def test_cpp_matches_python_every_quality(tmp_path, quality, preset):
    x = voice_like(f0=180, seconds=1.0).astype(np.float32).astype(np.float64)
    assert_match(tmp_path, make_settings(preset), quality, x, preset)


# 44.1 kHz uses the 48 kHz sizes; 96 kHz doubles the FFT (quality_spec), as
# the system effect does on a 96 kHz microphone.
@pytest.mark.parametrize("sr", [44100, 96000])
@pytest.mark.parametrize("quality", list(QUALITY))
@pytest.mark.parametrize("preset", ["deeper", "old_lady"])
def test_cpp_matches_python_at_other_rates(tmp_path, sr, quality, preset):
    x = voice_like(f0=140, seconds=1.0, sr=sr).astype(np.float32).astype(np.float64)
    assert_match(tmp_path, make_settings(preset), quality, x, f"{preset} at {sr} Hz", sr=sr)


CHARACTER = {
    "tremor": dict(tremor_depth=2.0, tremor_hz=6.0),
    "tremor+pitch": dict(pitch=-5, tremor_depth=1.5, tremor_hz=4.0),
    "jitter": dict(jitter=1.0),
    "jitter+pitch": dict(pitch=3, jitter=1.0),
    "breath": dict(breath=1.0),
    "gravel-20Hz": dict(gravel=1.0, gravel_hz=20.0),
    "gravel-150Hz": dict(gravel=1.0, gravel_hz=150.0),
    "robot+breath+tremor": dict(mode="robot", robot_hz=90, breath=0.6, tremor_depth=3.0, tremor_hz=15.0),
    "whisper+tremor": dict(mode="whisper", tremor_depth=2.0, tremor_hz=5.0),
    "formant+breath": dict(formant=4, breath=0.7),
    "giant+everything-maxed": dict(PRESETS["giant"], **MAXED),
    # Out of range: the Python setters and the C++ parser must clamp alike.
    "clamped-high": dict(pitch=2, tremor_hz=100.0, tremor_depth=50.0, jitter=7.0, breath=2.0, gravel=9.0,
                         gravel_hz=1e5),
    "clamped-low": dict(pitch=-2, tremor_hz=0.01, tremor_depth=2.0, jitter=-2.0, breath=-1.0, gravel=0.8,
                        gravel_hz=1.0),
}


@pytest.mark.parametrize("quality", list(QUALITY))
@pytest.mark.parametrize("case", list(CHARACTER))
def test_cpp_matches_python_with_character(tmp_path, case, quality):
    x = voice_like(f0=140, seconds=1.5).astype(np.float32).astype(np.float64)
    assert_match(tmp_path, Settings(**CHARACTER[case]), quality, x, case)


# Settings changing every few blocks, as from the UI: the tremor, jitter and
# gravel phases must keep running while their depth is zero or bypass is on.
LIVE = {
    "character-on-off": (dict(pitch=-3), dict(pitch=-3, tremor_depth=3.0, jitter=1.0, breath=0.5, gravel=0.8)),
    "bypass-toggle": (dict(pitch=4, tremor_depth=1.0, jitter=0.5, breath=0.3, gravel=0.7, gravel_hz=35.0),
                      dict(pitch=4, tremor_depth=1.0, jitter=0.5, breath=0.3, gravel=0.7, gravel_hz=35.0,
                           bypass=True)),
    "rates-change": (dict(mode="robot", tremor_depth=2.0, tremor_hz=3.0, gravel=1.0, gravel_hz=30.0),
                     dict(mode="robot", tremor_depth=2.0, tremor_hz=12.0, gravel=1.0, gravel_hz=170.0)),
    # Bypass is the raw microphone: the input gain applies only to the effect.
    "bypass-toggle-gain": (dict(pitch=-3, input_gain_db=9.0), dict(pitch=-3, input_gain_db=9.0, bypass=True)),
}


@pytest.mark.parametrize("quality", list(QUALITY))
@pytest.mark.parametrize("case", list(LIVE))
def test_cpp_matches_python_with_live_changes(tmp_path, case, quality):
    x = voice_like(f0=150, seconds=1.5).astype(np.float32).astype(np.float64)
    a, b = LIVE[case]
    assert_match(tmp_path, Settings(**a), quality, x, case, alt=Settings(**b), period=3)


# Exact digital silence (a muted mic, BUFFER_SILENT) scales every bin to a
# signed zero; the phase memory must not depend on which sign each language
# ends up with, or robot harmonics come out with different phases ever after.
@pytest.mark.parametrize("quality", list(QUALITY))
def test_cpp_matches_python_robot_after_digital_silence(tmp_path, quality):
    x = np.concatenate((np.zeros(SR // 4), voice_like(f0=150, seconds=0.75)))
    assert_match(tmp_path, Settings(mode="robot"), quality, x.astype(np.float32).astype(np.float64),
                 "robot after silence")


@pytest.mark.parametrize("quality", list(QUALITY))
def test_cpp_matches_python_whisper_then_robot_across_silence(tmp_path, quality):
    period = 12  # blocks: whisper through the silence, then robot once the voice starts
    silence = np.zeros(period * QUALITY[quality][2])
    x = np.concatenate((silence, voice_like(f0=150, seconds=1.0))).astype(np.float32).astype(np.float64)
    assert_match(tmp_path, Settings(mode="whisper"), quality, x, "whisper -> robot", alt=Settings(mode="robot"),
                 period=period)


def random_settings(rng: np.random.Generator, mode: str) -> Settings:
    """Every voice and character control across its full range, the rest across the UI's."""
    u = rng.uniform
    character = dict(tremor_hz=u(0.5, 15), tremor_depth=u(0, 3), jitter=u(0, 1), breath=u(0, 1),
                     gravel=u(0, 1), gravel_hz=u(10, 200))
    for k in ("tremor_depth", "jitter", "breath", "gravel"):
        if rng.random() < 0.25:  # neutral now and then, so the off paths get mixed in too
            character[k] = 0.0
    return Settings(
        pitch=u(-24, 24), formant=u(-12, 12), mode=mode, robot_hz=u(20, 2000), mix=u(0, 1), **character,
        input_gain_db=u(-12, 12), gate_enabled=bool(rng.random() < 0.5), gate_threshold_db=u(-80, -30),
        highpass_hz=u(20, 1000), lowpass_hz=u(1000, 20000), low_db=u(-12, 12), mid_db=u(-12, 12),
        high_db=u(-12, 12), drive=u(0, 1), comp_enabled=bool(rng.random() < 0.5), comp_threshold_db=u(-50, 0),
        comp_ratio=u(1, 20), comp_makeup_db=u(0, 24), reverb_mix=u(0, 1), reverb_room=u(0, 1),
        output_gain_db=u(-24, 12))


@pytest.mark.parametrize("seed", range(30))
def test_cpp_matches_python_fuzz(tmp_path, seed):
    # seed -> (quality, mode) cycles through all nine combinations
    quality = list(QUALITY)[seed % 3]
    mode = MODES[(seed // 3) % 3]
    rng = np.random.default_rng(1000 + seed)
    s = random_settings(rng, mode)
    x = voice_like(f0=rng.uniform(90, 260), seconds=1.0).astype(np.float32).astype(np.float64)
    assert_match(tmp_path, s, quality, x, f"seed {seed} ({mode}): {s}")
