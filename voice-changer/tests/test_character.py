"""Character controls: tremor, jitter, breath and gravel.

Their randomness is counter-based (voicechanger/dsp/rng.py), so the C++ port
can match the Python reference exactly. Their defaults are neutral and must
leave the output bit-identical to the implementation from before they existed.
"""

import importlib
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from voicechanger.chain import QUALITY, Settings, VoiceChain
from voicechanger.dsp.effects import GravelModulator, lin_to_db
from voicechanger.dsp.rng import hash_uniform, hash_uniform_vec
from voicechanger.dsp.spectral import SpectralVoice
from voicechanger.presets import PRESETS, make_settings

sys.path.insert(0, str(Path(__file__).parent))
from test_dsp import stream, voice_like  # noqa: E402

SR = 48000
ROOT = Path(__file__).resolve().parents[1]
PRE_CHARACTER = "0e53a0517c76c052e250f263ab27b5f2be2d33e1"  # last commit before these controls
NEUTRAL = {k: getattr(Settings(), k)
           for k in ("tremor_hz", "tremor_depth", "jitter", "breath", "gravel", "gravel_hz")}
MAXED = {"tremor_hz": 15.0, "tremor_depth": 3.0, "jitter": 1.0, "breath": 1.0,
         "gravel": 1.0, "gravel_hz": 200.0}


def f0_track(y, sr=SR, win=0.04, hop=0.005, fmin=80, fmax=400):
    """Frame-wise f0 by autocorrelation. Returns (Hz per frame, frames per second)."""
    w, h = int(win * sr), int(hop * sr)
    lo, hi = int(sr / fmax), int(sr / fmin)
    norm = np.arange(w, 0, -1)  # unbiased: longer lags overlap fewer samples
    track = []
    for i in range(0, len(y) - w, h):
        seg = y[i:i + w] - np.mean(y[i:i + w])
        ac = np.fft.irfft(np.abs(np.fft.rfft(seg, 2 * w)) ** 2)[:w] / norm
        lag = lo + int(np.argmax(ac[lo:hi]))
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        track.append(sr / (lag + 0.5 * (a - c) / (a - 2 * b + c)))
    return np.array(track), 1.0 / hop


def semitone_track(y):
    track, rate = f0_track(y)
    st = 12 * np.log2(track / np.median(track))
    return st - st.mean(), track, rate


def run_voice(x, block=512, fft=2048, **controls):
    v = SpectralVoice(SR, fft_size=fft, block_size=block)
    v.set_pitch(controls.get("pitch", 0.0))
    v.set_mode(controls.get("mode", "normal"))
    v.set_tremor(controls.get("tremor_hz", 5.5), controls.get("tremor_depth", 0.0))
    v.set_jitter(controls.get("jitter", 0.0))
    v.set_breath(controls.get("breath", 0.0))
    return stream(v, x, block)


def run_chain(x, quality="balanced", chain=None, **settings):
    chain = chain or VoiceChain(SR, quality, Settings(**settings))
    b = chain.block_size
    return np.concatenate([chain.process(x[i:i + b]) for i in range(0, len(x) - b + 1, b)])


# ---- counter-based RNG ----------------------------------------------------

# Pinned so the C++ port (apo/dsp/rng.h) can be checked against exact values.
PINNED = [
    (1, 0, 0.4244488753016006),
    (1, 1, 0.35928268644838135),
    (2, 0, 0.6505612717481737),
    (2, 12345, 0.4597680444543074),
    (3, 1025, 0.7054835140616976),
    (3, 2**40 + 7, 0.21386074011883394),
    (4, 0, 0.37628034249448505),
    (4, 2**63 - 1, 0.7442557025849479),
    (5, 7, 0.4901708443162024),
    (5, 1000000, 0.37621356497128244),
]


@pytest.mark.parametrize("stream_id,counter,want", PINNED)
def test_hash_uniform_pinned_values(stream_id, counter, want):
    assert hash_uniform(stream_id, counter) == want
    assert hash_uniform_vec(stream_id, np.array([counter], dtype=np.uint64))[0] == want


def test_hash_uniform_vectorised_matches_scalar_and_stays_in_range():
    counters = np.concatenate((np.arange(2000), np.arange(2000) * 7919 + 10**12))
    for s in range(1, 6):
        vec = hash_uniform_vec(s, counters)
        assert np.array_equal(vec, [hash_uniform(s, int(c)) for c in counters])
        assert vec.min() >= 0.0 and vec.max() < 1.0
    big = hash_uniform_vec(3, np.arange(200000))
    assert abs(big.mean() - 0.5) < 0.005 and abs(big.var() - 1 / 12) < 0.002
    # different streams at the same counter are unrelated (tremor rate vs jitter, say)
    same = np.arange(5000)
    assert abs(np.corrcoef(hash_uniform_vec(1, same), hash_uniform_vec(2, same))[0, 1]) < 0.05


# ---- neutral defaults change nothing --------------------------------------

@pytest.fixture(scope="module")
def old_impl(tmp_path_factory):
    """The chain and shifter as they were before the character controls, from git."""
    pkg = tmp_path_factory.mktemp("pre_character") / "vc_pre_character"
    try:
        for name in ("__init__.py", "chain.py", "dsp/__init__.py", "dsp/effects.py", "dsp/spectral.py"):
            src = subprocess.run(["git", "show", f"{PRE_CHARACTER}:./voicechanger/{name}"],
                                 cwd=ROOT, check=True, capture_output=True, text=True).stdout
            (pkg / name).parent.mkdir(parents=True, exist_ok=True)
            (pkg / name).write_text(src)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("pre-character sources are not in this git checkout")
    sys.path.insert(0, str(pkg.parent))
    try:
        return (importlib.import_module("vc_pre_character.chain"),
                importlib.import_module("vc_pre_character.dsp.spectral"))
    finally:
        sys.path.remove(str(pkg.parent))


# Whisper drew from an unseeded generator before, so it has no fixed output to compare.
COMPARABLE = [p for p in PRESETS if PRESETS[p].get("mode") != "whisper"]


@pytest.mark.parametrize("preset", COMPARABLE)
def test_neutral_chain_is_bit_identical_to_before(old_impl, preset):
    old_chain, _ = old_impl
    s = make_settings(preset).update(**NEUTRAL)
    x = voice_like(f0=140, seconds=1.5)
    new = run_chain(x, chain=VoiceChain(SR, "balanced", s))
    old = run_chain(x, chain=old_chain.VoiceChain(SR, "balanced", s))
    assert np.array_equal(new, old)


def test_neutral_bypass_toggling_is_bit_identical_to_before(old_impl):
    old_chain, _ = old_impl
    s_new, s_old = make_settings("deeper"), make_settings("deeper")
    new, old = VoiceChain(SR, "balanced", s_new), old_chain.VoiceChain(SR, "balanced", s_old)
    x = voice_like(f0=140, seconds=1.5)
    b = new.block_size
    for j, i in enumerate(range(0, len(x) - b + 1, b)):
        s_new.bypass = s_old.bypass = 30 <= j < 60
        assert np.array_equal(new.process(x[i:i + b]), old.process(x[i:i + b]))


@pytest.mark.parametrize("fft,ov,block,controls", [
    (1024, 8, 256, {}),
    (1024, 4, 256, {"pitch": 5, "formant": -2}),
    (2048, 8, 512, {"pitch": -7}),
    (2048, 8, 512, {"formant": 3}),
    (1024, 8, 256, {"mode": "robot", "robot": 150, "pitch": 2}),
    (2048, 8, None, {"pitch": 4, "formant": 1}),
])
def test_neutral_shifter_is_bit_identical_to_before(old_impl, fft, ov, block, controls):
    _, old_spectral = old_impl
    x = voice_like(f0=160, seconds=1.6)  # whole 512-sample blocks
    outs = []
    for cls in (SpectralVoice, old_spectral.SpectralVoice):
        v = cls(SR, fft_size=fft, overlap=ov, block_size=block)
        v.set_pitch(controls.get("pitch", 0))
        v.set_formant(controls.get("formant", 0))
        v.set_mode(controls.get("mode", "normal"))
        v.set_robot_pitch(controls.get("robot", 110))
        outs.append(stream(v, x, block or 300))
    assert np.array_equal(outs[0], outs[1])


@pytest.mark.parametrize("rate_only", [{"tremor_hz": 9.0}, {"gravel_hz": 120.0}])
def test_rates_do_nothing_without_their_depth(rate_only):
    x = voice_like(seconds=1.0)
    assert np.array_equal(run_chain(x, **rate_only), run_chain(x))


@pytest.mark.parametrize("control", [{"tremor_depth": 1.0}, {"jitter": 0.5}, {"breath": 0.3},
                                     {"gravel": 0.5}])
def test_each_control_reaches_the_output(control):
    x = voice_like(seconds=1.0)
    assert np.max(np.abs(run_chain(x, **control) - run_chain(x))) > 1e-3


# ---- tremor and jitter ----------------------------------------------------

# Moderate settings: very fast, deep swings make partials cross FFT bins
# quickly, and the shifter's peak-locked phases then confuse an f0 tracker.
@pytest.mark.parametrize("hz,depth", [(5.5, 1.0), (4.0, 0.5), (7.0, 0.7)])
def test_tremor_swings_pitch_at_its_rate_and_depth(hz, depth):
    x = voice_like(f0=150, seconds=4.0)
    y = VoiceChain(SR, "balanced", Settings(tremor_hz=hz, tremor_depth=depth)).process_buffer(x)
    st, _, rate = semitone_track(y[SR:])
    n = len(st)
    spec = np.abs(np.fft.rfft(st * np.hanning(n), 16 * n))
    freqs = np.fft.rfftfreq(16 * n, 1 / rate)
    band = (freqs > 1) & (freqs < 20)
    assert abs(freqs[band][np.argmax(spec[band])] - hz) < 0.6
    peak_st = np.sqrt(2) * np.std(st)  # a sine's peak is sqrt(2) x its RMS
    assert abs(peak_st - depth) / depth < 0.35


def test_tremor_also_swings_loudness_in_step_with_pitch():
    """Real vibrato gets louder as it goes sharp: about 1.5 dB per semitone."""
    x = voice_like(f0=150, seconds=4.0)
    y = run_voice(x, tremor_hz=5.0, tremor_depth=1.0)[SR:]
    st, _, rate = semitone_track(y)
    h, w = int(SR / rate), int(0.04 * SR)  # same frames as the f0 track
    level = np.array([lin_to_db(np.std(y[i:i + w])) for i in range(0, len(y) - w, h)])
    level -= level.mean()
    # Regress rather than compare swings: the shifter adds a little loudness
    # wobble of its own while pitch moves, but it isn't in step with the pitch.
    db_per_st = np.dot(st, level) / np.dot(st, st)
    assert 1.0 < db_per_st < 2.2


def test_jitter_roughens_pitch_but_keeps_its_centre():
    x = voice_like(f0=150, seconds=4.0)
    spreads = []
    for j in (0.0, 0.3, 1.0):
        y = VoiceChain(SR, "balanced", Settings(jitter=j)).process_buffer(x)
        st, track, _ = semitone_track(y[SR:])
        assert abs(np.median(track) - 150) / 150 < 0.02
        spreads.append(np.std(st))
    assert spreads[0] < 0.01
    assert spreads[1] > 2 * spreads[0] + 0.01 and spreads[2] > 1.5 * spreads[1]


# ---- breath ---------------------------------------------------------------

def harmonic_to_noise_db(y, f0, width=12.0, fmax=8000.0):
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    freqs = np.fft.rfftfreq(len(y), 1 / SR)
    use = (freqs > 0.5 * f0) & (freqs < fmax)
    near = np.abs(freqs - f0 * np.round(freqs / f0)) < width
    return lin_to_db(np.sqrt(spec[use & near].sum() / spec[use & ~near].sum()))


def energy_above(y, hz=3000.0):
    spec = np.abs(np.fft.rfft(y)) ** 2
    return spec[np.fft.rfftfreq(len(y), 1 / SR) > hz].sum()


@pytest.mark.parametrize("pitch", [0, -4])
def test_breath_trades_harmonics_for_airy_noise_at_steady_loudness(pitch):
    x = voice_like(f0=150, seconds=4.0)
    f0 = 150 * 2 ** (pitch / 12)
    amounts = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    outs = [run_voice(x, pitch=pitch, breath=b)[SR:] for b in amounts]
    hnr = [harmonic_to_noise_db(y, f0) for y in outs]
    assert all(a > b for a, b in zip(hnr, hnr[1:])), hnr
    hf = [energy_above(y) for y in outs]
    assert all(a < b for a, b in zip(hf, hf[1:])), hf
    # Checked up to 0.6: the noise reuses whisper's incoherent overlap-add
    # compensation, which leaves pure noise (breath 1) about 3 dB down.
    levels = [lin_to_db(np.std(y) / np.std(outs[0])) for y in outs[:4]]
    assert all(abs(d) < 2.0 for d in levels), levels


def test_breath_never_reaches_the_phase_memory():
    """Noise phases must not be carried into the next frame's partials."""
    x = voice_like(seconds=0.5)
    for controls in ({"pitch": 3}, {"pitch": 0}, {"mode": "robot"}):
        a = SpectralVoice(SR, block_size=128)
        b = SpectralVoice(SR, block_size=128)
        for v in (a, b):
            v.set_pitch(controls.get("pitch", 0))
            v.set_mode(controls.get("mode", "normal"))
        b.set_breath(0.7)
        for i in range(0, len(x) - 127, 128):
            ya, yb = a.process(x[i:i + 128]), b.process(x[i:i + 128])
            assert np.array_equal(a._sum_phase, b._sum_phase)
        assert np.max(np.abs(ya - yb)) > 1e-4  # ...while the audio itself did change


# ---- gravel ---------------------------------------------------------------

def envelope_spectrum(y):
    e = np.abs(y) - np.mean(np.abs(y))
    spec = np.abs(np.fft.rfft(e * np.hanning(len(e)))) ** 2
    return np.fft.rfftfreq(len(e), 1 / SR), spec


@pytest.mark.parametrize("hz", [30.0, 50.0, 80.0])
def test_gravel_pulses_the_envelope_at_its_rate(hz):
    x = voice_like(f0=150, seconds=4.0)
    shares = []
    for amount in (0.0, 0.6):
        y = VoiceChain(SR, "balanced", Settings(gravel=amount, gravel_hz=hz)).process_buffer(x)[SR:]
        freqs, spec = envelope_spectrum(y)
        near = (freqs > 0.7 * hz) & (freqs < 1.3 * hz)
        shares.append(spec[near].sum() / spec[(freqs > 1) & (freqs < 1000)].sum())
        if amount:
            low = (freqs > 10) & (freqs < 120)
            assert abs(freqs[low][np.argmax(spec[low])] - hz) < 0.15 * hz
    assert shares[0] < 1e-4 and shares[1] > 0.01, shares


def test_gravel_is_independent_of_block_size():
    x = np.random.default_rng(1).standard_normal(SR)
    outs = []
    for block in (256, 512, 100):
        g = GravelModulator(SR)
        outs.append(np.concatenate([g.process(x[i:i + block], 0.8, 60.0) for i in range(0, len(x), block)]))
    assert np.max(np.abs(outs[0] - outs[1])) < 1e-9
    assert np.max(np.abs(outs[0] - outs[2])) < 1e-9


def test_gravel_phase_runs_while_switched_off():
    """The pulse train depends only on elapsed time, not on when gravel was turned up."""
    x = np.random.default_rng(2).standard_normal(SR // 2)
    late, always = GravelModulator(SR), GravelModulator(SR)
    late.process(x[:12000], 0.0, 50.0)
    always.process(x[:12000], 0.7, 50.0)
    assert np.max(np.abs(late.process(x[12000:], 0.7, 50.0) - always.process(x[12000:], 0.7, 50.0))) < 1e-9


def test_bypass_keeps_gravel_and_shifter_in_step():
    x = voice_like(seconds=1.6)
    s_toggled = Settings(gravel=0.5, gravel_hz=70.0)
    toggled = VoiceChain(SR, "balanced", s_toggled)
    steady = VoiceChain(SR, "balanced", Settings(gravel=0.5, gravel_hz=70.0))
    b = toggled.block_size
    for j, i in enumerate(range(0, len(x) - b + 1, b)):
        s_toggled.bypass = j < 60
        toggled.process(x[i:i + b])
        steady.process(x[i:i + b])
    assert toggled._gravel._count == steady._gravel._count
    assert toggled._gravel._phase == steady._gravel._phase
    assert toggled.voice._frame_index == steady.voice._frame_index


# ---- streaming properties -------------------------------------------------

@pytest.mark.parametrize("mode", ["normal", "robot", "whisper"])
def test_shifter_output_is_independent_of_block_size(mode):
    x = voice_like(seconds=1.0)
    controls = dict(pitch=-3, mode=mode, tremor_hz=6.0, tremor_depth=1.0, jitter=0.6, breath=0.4)
    outs = [run_voice(x, block=b, fft=1024, **controls) for b in (128, 256, 512)]
    assert np.array_equal(outs[0], outs[1]) and np.array_equal(outs[0], outs[2])


@pytest.mark.parametrize("mode", ["normal", "robot", "whisper"])
def test_fresh_chains_are_deterministic(mode):
    x = voice_like(seconds=1.0)
    s = dict(MAXED, mode=mode, pitch=-2, tremor_depth=1.5, breath=0.5, gravel=0.6, gravel_hz=60.0)
    assert np.array_equal(run_chain(x, **s), run_chain(x, **s))


@pytest.mark.parametrize("mode", ["normal", "robot", "whisper"])
def test_silence_stays_silent_with_every_control_maxed(mode):
    y = run_chain(np.zeros(SR), quality="balanced", mode=mode, pitch=5, **MAXED)
    assert np.all(np.isfinite(y)) and np.max(np.abs(y)) == 0.0


def test_out_of_range_controls_are_clamped_like_the_cpp_parser():
    v = SpectralVoice(SR)
    v.set_tremor(100.0, 9.0)
    v.set_jitter(-1.0)
    v.set_breath(1.5)
    assert (v.tremor_hz, v.tremor_depth, v.jitter, v.breath) == (15.0, 3.0, 0.0, 1.0)
    wild = dict(tremor_hz=100, tremor_depth=50, jitter=7, breath=2, gravel=9, gravel_hz=1e5)
    chain = VoiceChain(SR, "balanced", Settings(**wild))
    y = chain.process_buffer(voice_like(seconds=1.0))
    assert np.all(np.isfinite(y)) and np.max(np.abs(y)) <= chain._limiter.ceiling + 1e-6
    maxed = VoiceChain(SR, "balanced", Settings(**MAXED)).process_buffer(voice_like(seconds=1.0))
    assert np.array_equal(y, maxed)


@pytest.mark.parametrize("quality", list(QUALITY))
def test_chain_with_every_control_maxed_is_fast_enough_for_real_time(quality):
    x = voice_like(seconds=2.0)
    best = float("inf")
    for _ in range(3):  # best of three: time the code, not whatever else the machine is doing
        chain = VoiceChain(SR, quality, make_settings("giant").update(**MAXED))
        b = chain.block_size
        t0 = time.perf_counter()
        for i in range(0, len(x) - b, b):
            chain.process(x[i:i + b])
        best = min(best, (time.perf_counter() - t0) / 2.0)
        if best < 0.5:
            break
    assert best < 0.5, f"{quality} used {best:.0%} of real time"
