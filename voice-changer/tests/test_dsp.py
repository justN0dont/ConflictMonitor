import numpy as np
import pytest

from voicechanger.chain import QUALITY, Settings, VoiceChain
from voicechanger.dsp.effects import Biquad, Compressor, Limiter, NoiseGate, Reverb, lin_to_db
from voicechanger.dsp.spectral import SpectralVoice
from voicechanger.presets import PRESETS, apply_preset, make_settings

SR = 48000


def voice_like(f0=150.0, seconds=2.0, sr=SR):
    """Harmonic-rich tone with a fixed formant-like resonance at ~700 Hz."""
    t = np.arange(int(sr * seconds)) / sr
    x = np.zeros_like(t)
    for h in range(1, 40):
        f = f0 * h
        if f > sr / 2.2:
            break
        x += np.sin(2 * np.pi * f * t) / (1 + ((f - 700) / 300) ** 2)
    return 0.2 * x / np.max(np.abs(x))


def stream(proc, x, block=256):
    return np.concatenate([proc.process(x[i:i + block]) for i in range(0, len(x), block)])


def dominant_hz(y, sr=SR, lo=60, hi=2000):
    y = y[len(y) // 2:]
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    freqs = np.fft.rfftfreq(len(y), 1 / sr)
    band = (freqs > lo) & (freqs < hi)
    return freqs[band][np.argmax(spec[band])]


def lowest_harmonic_hz(y, sr=SR, fmin=50, fmax=600):
    """Fundamental by autocorrelation (robust to a weak first harmonic)."""
    y = y[len(y) // 2:]
    y = y - y.mean()
    n = len(y)
    spec = np.fft.rfft(y, 2 * n)
    ac = np.fft.irfft(np.abs(spec) ** 2)[:n]
    lo, hi = int(sr / fmax), int(sr / fmin)
    lag = lo + int(np.argmax(ac[lo:hi]))
    # parabolic interpolation for sub-sample lag
    a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
    lag = lag + 0.5 * (a - c) / (a - 2 * b + c)
    return sr / lag


def centroid_hz(y, sr=SR):
    y = y[len(y) // 2:]
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    freqs = np.fft.rfftfreq(len(y), 1 / sr)
    return float(np.sum(freqs * spec) / np.sum(spec))


# ---- spectral voice -------------------------------------------------------

@pytest.mark.parametrize("fft,ov", [(1024, 4), (1024, 8), (2048, 8)])
def test_identity_is_transparent_with_reported_latency(fft, ov):
    x = voice_like()
    v = SpectralVoice(SR, fft_size=fft, overlap=ov, block_size=256 if fft == 1024 else 512)
    y = stream(v, x, 256 if fft == 1024 else 512)
    L = v.latency
    err = np.max(np.abs(y[L + SR // 2:] - x[SR // 2:len(x) - L]))
    assert err < 1e-6


def test_odd_block_sizes_still_line_up():
    x = voice_like(seconds=1.0)
    v = SpectralVoice(SR, block_size=None)
    y = stream(v, x, block=100)
    L = v.latency
    assert np.max(np.abs(y[L + 10000:] - x[10000:len(x) - L])) < 1e-6


@pytest.mark.parametrize("semis", [-12, -5, 3, 7, 12])
@pytest.mark.parametrize("f0,fft", [(150, 1024), (110, 2048)])
def test_pitch_shift_moves_the_fundamental(semis, f0, fft):
    x = voice_like(f0=f0)
    v = SpectralVoice(SR, fft_size=fft, block_size=256)
    v.set_pitch(semis)
    y = stream(v, x)
    want = f0 * 2 ** (semis / 12)
    got = lowest_harmonic_hz(y, fmax=max(600, 1.5 * want))
    assert abs(got - want) / want < 0.02
    assert abs(lin_to_db(np.std(y[SR:]) / np.std(x[SR:]))) < 1.5  # loudness held


def test_pitch_shift_adds_no_sidebands():
    """A pure tone must come out as a pure tone: everything off the target
    frequency at least 40 dB down (per-bin vocoder shifting fails this)."""
    t = np.arange(2 * SR) / SR
    for f, semis in [(300, 12), (440, -5), (220, 7)]:
        v = SpectralVoice(SR, fft_size=1024, block_size=256)
        v.set_pitch(semis)
        y = stream(v, 0.3 * np.sin(2 * np.pi * f * t))[SR:]
        spec = np.abs(np.fft.rfft(y * np.blackman(len(y))))
        freqs = np.fft.rfftfreq(len(y), 1 / SR)
        target = f * 2 ** (semis / 12)
        off = np.abs(freqs - target) > 15
        assert lin_to_db(spec[off].max() / spec.max()) < -40, (f, semis)


def test_pitch_shift_preserves_formants():
    """Shift pitch up an octave: harmonics move, the resonance near 700 Hz stays."""
    x = voice_like(f0=110)
    v = SpectralVoice(SR, block_size=256)
    v.set_pitch(12)
    y = stream(v, x)
    assert abs(centroid_hz(y) - centroid_hz(x)) / centroid_hz(x) < 0.2


def test_formant_shift_moves_brightness_but_not_pitch():
    x = voice_like(f0=150)
    v = SpectralVoice(SR, block_size=256)
    v.set_formant(5)
    y = stream(v, x)
    assert abs(lowest_harmonic_hz(y) - 150) < 5
    assert centroid_hz(y) > 1.2 * centroid_hz(x)


def test_robot_uses_fixed_pitch():
    x = voice_like(f0=173)
    v = SpectralVoice(SR, block_size=256)
    v.set_mode("robot")
    v.set_robot_pitch(100)
    got = lowest_harmonic_hz(stream(v, x))
    assert abs(got - 100) < 5


@pytest.mark.parametrize("mode", ["robot", "whisper"])
def test_replacement_modes_keep_loudness(mode):
    x = voice_like()
    v = SpectralVoice(SR, block_size=256)
    v.set_mode(mode)
    y = stream(v, x)
    ratio_db = lin_to_db(np.std(y[SR:]) / np.std(x[SR:]))
    assert -6 < ratio_db < 3


def test_silence_stays_silent_and_finite():
    v = SpectralVoice(SR, block_size=256)
    v.set_pitch(7)
    y = stream(v, np.zeros(SR))
    assert np.all(np.isfinite(y)) and np.max(np.abs(y)) < 1e-6


# ---- effects --------------------------------------------------------------

def test_highpass_removes_rumble():
    t = np.arange(SR) / SR
    y = stream(Biquad(SR, "highpass", 300), np.sin(2 * np.pi * 50 * t))
    assert lin_to_db(np.std(y[SR // 2:]) / 0.707) < -25


def test_gate_closes_on_noise_and_opens_on_voice():
    g = NoiseGate(SR, threshold_db=-40)
    quiet = stream(g, 0.001 * np.random.default_rng(0).standard_normal(SR))
    assert lin_to_db(np.std(quiet[SR // 2:]) / 0.001) < -60
    loud = stream(g, voice_like(seconds=0.5))
    assert np.std(loud[SR // 8:]) > 0.5 * np.std(voice_like(seconds=0.5))


def test_limiter_never_exceeds_ceiling():
    t = np.arange(SR) / SR
    lim = Limiter(SR, ceiling_db=-1)
    y = stream(lim, 3 * np.sin(2 * np.pi * 220 * t))
    assert np.max(np.abs(y)) <= lim.ceiling + 1e-9


def test_compressor_reduces_loud_signal():
    t = np.arange(SR) / SR
    c = Compressor(SR, threshold_db=-20, ratio=4)
    stream(c, 0.5 * np.sin(2 * np.pi * 200 * t))
    assert c.gain_reduction_db < -5


def test_reverb_adds_a_decaying_tail():
    r = Reverb(SR)
    r.mix = 0.5
    imp = np.zeros(SR * 2)
    imp[0] = 1.0
    y = stream(r, imp)
    early = np.std(y[int(0.05 * SR):int(0.15 * SR)])
    late = np.std(y[int(1.0 * SR):int(1.1 * SR)])
    assert early > 1e-4 and late < early and np.all(np.isfinite(y))


# ---- full chain -----------------------------------------------------------

@pytest.mark.parametrize("name", list(PRESETS))
def test_every_preset_runs_clean(name):
    chain = VoiceChain(SR, "balanced", make_settings(name))
    y = chain.process_buffer(voice_like(seconds=1.0))
    assert np.all(np.isfinite(y))
    assert np.max(np.abs(y)) <= chain._limiter.ceiling + 1e-6
    assert np.std(y) > 1e-3


@pytest.mark.parametrize("quality", list(QUALITY))
def test_chain_is_fast_enough_for_real_time(quality):
    import time
    chain = VoiceChain(SR, quality, make_settings("giant"))
    x = voice_like(seconds=2.0)
    b = chain.block_size
    t0 = time.perf_counter()
    for i in range(0, len(x) - b, b):
        chain.process(x[i:i + b])
    rt = (time.perf_counter() - t0) / 2.0
    assert rt < 0.5, f"{quality} used {rt:.0%} of real time"


def test_bypass_is_delay_matched_dry():
    s = Settings(bypass=True)
    chain = VoiceChain(SR, "balanced", s)
    x = voice_like(seconds=1.0)
    y = chain.process_buffer(x)
    assert np.max(np.abs(y - x.astype(np.float32))) < 1e-6


def test_settings_change_live_and_preset_applies_in_place():
    s = Settings()
    chain = VoiceChain(SR, "balanced", s)
    x = voice_like(f0=150, seconds=2.0)
    apply_preset(s, "chipmunk")
    assert chain.settings.pitch == PRESETS["chipmunk"]["pitch"]
    y = chain.process_buffer(x)
    assert lowest_harmonic_hz(y) > 150 * 1.5


def test_unknown_setting_rejected():
    with pytest.raises(KeyError):
        Settings().update(nope=1)
