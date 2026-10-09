import numpy as np
import pytest
from scipy.signal import butter, hilbert, sosfiltfilt

from voicechanger.chain import QUALITY, Settings, VoiceChain, quality_spec
from voicechanger.dsp.effects import Biquad, Compressor, Drive, Limiter, NoiseGate, Reverb, lin_to_db
from voicechanger.dsp.spectral import SpectralVoice
from voicechanger.presets import MIC_SETUP, PRESETS, apply_preset, make_settings

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


def voice_with_pitch(f0_hz, f0=150.0, sr=SR):
    """voice_like's harmonics on a pitch that follows ``f0_hz`` (Hz per sample; ``f0`` is its centre).

    Each harmonic keeps a fixed level, so any change in it at the output is the shifter's doing.
    """
    ph = 2 * np.pi * np.cumsum(f0_hz) / sr
    x = sum(np.sin(h * ph) / (1 + ((h * f0 - 700) / 300) ** 2) for h in range(1, 40) if h * f0 < sr / 2.2)
    return 0.2 * x / np.max(np.abs(x))


def dip_stats(env):
    """(share of time more than 6 dB below its median level, deepest dip in dB), edges trimmed."""
    env = env[4000:-4000]
    db = 20 * np.log10(env / np.median(env) + 1e-12)
    return float(np.mean(db < -6)), float(db.min())


def harmonic_dips(y, f0, harmonics, sr=SR):
    """Per harmonic of ``f0``: dip_stats of its level."""
    out = {}
    for h in harmonics:
        sos = butter(4, [(h - 0.33) * f0, (h + 0.33) * f0], btype="band", fs=sr, output="sos")
        out[h] = dip_stats(np.abs(hilbert(sosfiltfilt(sos, y))))
    return out


def tracked_harmonic_dips(y, f0_hz, harmonics, sr=SR):
    """harmonic_dips for a pitch that swings further than a fixed band can hold.

    Each harmonic is demodulated along its own trajectory (``f0_hz``: the
    expected fundamental at each sample of ``y``) and low-passed at 25 Hz, so
    even the upper ones are followed through their whole swing.
    """
    lp = butter(4, 25.0, fs=sr, output="sos")
    ph = 2 * np.pi * np.cumsum(f0_hz) / sr
    out = {}
    for h in harmonics:
        z = y * np.exp(-1j * h * ph)
        out[h] = dip_stats(np.abs(sosfiltfilt(lp, z.real) + 1j * sosfiltfilt(lp, z.imag)))
    return out


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


def test_always_pad_takes_a_short_block_without_losing_alignment():
    """The APO's periods are whole hops, but the engine can deliver a partial one."""
    x = voice_like(seconds=1.0)
    sizes = [512] * 40 + [100] + [512] * 50
    with pytest.raises(ValueError):  # without the padding a short block has no output ready
        SpectralVoice(SR, fft_size=2048, block_size=512).process(x[:100])
    v = SpectralVoice(SR, fft_size=2048, block_size=512, always_pad=True)
    assert v.latency == 2048 - 1
    edges = np.cumsum([0] + sizes)
    y = np.concatenate([v.process(x[a:b]) for a, b in zip(edges, edges[1:])])
    L = v.latency
    assert np.max(np.abs(y[L + 10000:] - x[10000:len(y) - L])) < 1e-6
    assert VoiceChain(SR, "balanced", always_pad=True).latency == v.latency


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


@pytest.mark.parametrize("quality", list(QUALITY))
def test_shifted_harmonics_do_not_drop_out_while_the_voice_glides(quality):
    """A speaking voice glides in pitch all the time, so its partials keep
    moving between FFT bins. Each one's phase follows it (by sinusoid, matched
    where frames overlap): kept per bin, it was lost on every move and the
    out-of-phase frame cancelled the partial for about a frame."""
    t = np.arange(4 * SR) / SR
    x = voice_with_pitch(150 * 2 ** (0.5 * np.sin(2 * np.pi * 5.5 * t) / 12))  # +/-0.5 st at 5.5 Hz
    s = make_settings("deeper")
    f0 = 150 * 2 ** (s.pitch / 12)
    dips = {}
    for label, track in (("before", False), ("after", True)):
        y = VoiceChain(SR, quality, s, track_peaks=track).process_buffer(x)[SR:]
        dips[label] = harmonic_dips(y, f0, (1, 2, 3, 4, 5, 6, 8))
    worst = {k: max(share for share, _ in d.values()) for k, d in dips.items()}
    deepest = {k: min(low for _, low in d.values()) for k, d in dips.items()}
    assert worst["before"] > 0.01  # the measurement does see the dropouts
    assert worst["after"] < 0.01, dips["after"]
    assert deepest["after"] > deepest["before"] / 3, dips


# A partial moves hop * fft_size / rate**2 times as many bins per hop as at
# balanced 48 kHz: 4x at high-quality, 9x at 16 kHz (a Bluetooth headset mic).
FURTHER_PER_HOP = [("balanced", 48000), ("high-quality", 48000), ("balanced", 16000)]


@pytest.mark.parametrize("quality,sr", FURTHER_PER_HOP)
@pytest.mark.parametrize("preset", ["feminine", "chipmunk"])
def test_upper_harmonics_do_not_drop_out_while_a_raised_voice_glides(preset, quality, sr):
    """Raising the pitch speeds up every partial's glide, the upper ones most,
    and at high-quality and 16 kHz each also moves several times further per
    hop in bins. Matched to the previous frame's partials within a fixed 2
    bins, h8-h14 dropped out there 8-23% of the time, 30-60 dB down; the
    window now scales with the hop, FFT size and sample rate."""
    t = np.arange(4 * sr) / sr
    f0_hz = 150 * 2 ** (3 * np.sin(2 * np.pi * 0.7 * t) / 12)  # speech-like +/-3 st drift
    x = voice_with_pitch(f0_hz, sr=sr)
    s = make_settings(preset)
    shifted = f0_hz * 2 ** (s.pitch / 12)  # process_buffer lines the output up with the input
    dips = {}
    for label, track in (("before", False), ("after", True)):
        y = VoiceChain(sr, quality, s, track_peaks=track).process_buffer(x)
        dips[label] = tracked_harmonic_dips(y[sr:], shifted[sr:], (1, 2, 3, 5, 8, 11, 14), sr)
    worst = {k: max(share for share, _ in d.values()) for k, d in dips.items()}
    deepest = {k: min(low for _, low in d.values()) for k, d in dips.items()}
    assert worst["before"] > 0.01  # the measurement does see the dropouts
    assert worst["after"] < 0.01, dips["after"]
    assert deepest["after"] > deepest["before"] / 3, dips


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


@pytest.mark.parametrize("mode", ["normal", "robot", "whisper"])
def test_digital_silence_leaves_the_phase_memory_at_zero(mode):
    """Silence scales bins to signed zeros; their angle (0 or +-pi, depending
    on how the multiply rounds) must not end up in the phase memory."""
    v = SpectralVoice(SR, block_size=256)
    v.set_mode(mode)
    stream(v, voice_like(seconds=0.32))  # whole blocks
    stream(v, np.zeros(256 * 48))
    assert np.all(v._sum_phase == 0.0)


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


def syllables(seconds=3.0):
    """voice_like swelling and fading three times a second, so a level detector has speech-like work."""
    t = np.arange(int(SR * seconds)) / SR
    x = voice_like(seconds=seconds) * (0.1 + np.clip(np.sin(2 * np.pi * 3 * t), 0, 1))
    return x / np.sqrt(np.mean(x ** 2))


def run_drive(x, amount, block=512):
    d = Drive(SR)
    return np.concatenate([d.process(x[i:i + block], amount) for i in range(0, len(x), block)])


def distortion_db(x, y, n=960):
    """Energy in y that no scaled copy of x explains, per 20 ms, relative to y's."""
    num = den = 0.0
    for i in range(0, len(x) - n, n):
        a, b = x[i:i + n], y[i:i + n]
        g = (a @ b) / max(a @ a, 1e-20)
        num += np.sum((b - g * a) ** 2)
        den += b @ b
    return 10 * np.log10(num / den)


@pytest.mark.parametrize("amount", [0.2, 0.6, 1.0])
def test_drive_sounds_the_same_on_a_quiet_or_a_loud_mic(amount):
    """Same grit and same level change from a quiet laptop mic to a hot headset.
    (Its makeup is fitted to real speech; test_preset_loudness covers loudness.)"""
    grit, gain = [], []
    for level_db in (-40, -28, -16):
        x = syllables() * 10 ** (level_db / 20)
        y = run_drive(x, amount)
        grit.append(distortion_db(x, y))
        gain.append(lin_to_db(np.std(y) / np.std(x)))
    assert np.ptp(grit) < 0.5 and np.ptp(gain) < 0.2, (grit, gain)


def test_drive_grit_follows_the_amount_and_zero_is_off():
    x = syllables() * 10 ** (-28 / 20)
    grit = [distortion_db(x, run_drive(x, a)) for a in (0.1, 0.3, 0.6, 1.0)]
    assert all(b > a + 2 for a, b in zip(grit, grit[1:])), grit
    assert -45 < grit[0] and grit[-1] < -10, grit
    assert np.array_equal(run_drive(x, 0.0), x)


def test_eq_bands_sit_where_their_frequency_settings_put_them():
    """A boost at mid_hz lifts a tone there and leaves one two octaves away nearly alone."""
    t = np.arange(SR) / SR

    def level(tone_hz, **eq):
        s = Settings(gate_enabled=False, comp_enabled=False, highpass_hz=20.0, **eq)
        y = VoiceChain(SR, "balanced", s).process_buffer(0.05 * np.sin(2 * np.pi * tone_hz * t))
        return lin_to_db(np.std(y[SR // 2:]))

    for hz in (700.0, 3000.0):
        flat = level(hz)
        assert abs(level(hz, mid_hz=hz, mid_db=9.0) - flat - 9.0) < 0.5
        assert abs(level(hz, mid_hz=hz * 4, mid_db=9.0) - flat) < 1.5
    assert abs(level(100.0, low_hz=400.0, low_db=-6.0) - level(100.0) + 6.0) < 0.5
    assert abs(level(12000.0, high_hz=3000.0, high_db=4.0) - level(12000.0) - 4.0) < 0.5


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


@pytest.mark.parametrize("input_gain_db", [0.0, 12.0, -20.0])
def test_bypass_is_delay_matched_dry(input_gain_db):
    """Bypass is the raw microphone, without the input gain, for an honest A/B
    (and so the gain can't push it past full scale with the limiter out)."""
    s = Settings(bypass=True, input_gain_db=input_gain_db)
    chain = VoiceChain(SR, "balanced", s)
    x = voice_like(seconds=1.0)
    y = chain.process_buffer(x)
    assert np.max(np.abs(y - x.astype(np.float32))) < 1e-6


@pytest.mark.parametrize("sr,quality,fft", [
    (44100, "balanced", 2048), (48000, "low-latency", 1024), (72000, "balanced", 2048),
    (88200, "balanced", 4096), (96000, "low-latency", 2048), (96000, "high-quality", 8192),
    (192000, "balanced", 8192), (384000, "high-quality", 16384),
])
def test_fft_size_follows_the_sample_rate_like_the_apo(sr, quality, fft):
    """Same bin width at 96 kHz as at 48 kHz, so a file renders like the live effect."""
    assert quality_spec(quality, sr)[0] == fft
    chain = VoiceChain(sr, quality)
    assert chain.voice.fft_size == fft and chain.block_size == QUALITY[quality][2]


def test_pitch_shift_at_96_khz():
    sr = 96000
    y = VoiceChain(sr, "balanced", Settings(pitch=-5)).process_buffer(voice_like(f0=110, seconds=1.0, sr=sr))
    assert abs(lowest_harmonic_hz(y, sr) - 110 * 2 ** (-5 / 12)) < 3


def test_settings_change_live_and_preset_applies_in_place():
    s = Settings()
    chain = VoiceChain(SR, "balanced", s)
    x = voice_like(f0=150, seconds=2.0)
    apply_preset(s, "chipmunk")
    assert chain.settings.pitch == PRESETS["chipmunk"]["pitch"]
    y = chain.process_buffer(x)
    assert lowest_harmonic_hz(y) > 150 * 1.5


def test_switching_presets_keeps_the_mic_set_up():
    s = Settings(input_gain_db=7.5, gate_enabled=False, gate_threshold_db=-61.0)
    apply_preset(s, "radio")
    assert (s.input_gain_db, s.gate_enabled, s.gate_threshold_db) == (7.5, False, -61.0)
    assert s.highpass_hz == PRESETS["radio"]["highpass_hz"]
    # A preset that set one of these would be silently ignored in the app.
    assert not [p for p, v in PRESETS.items() if set(v) & set(MIC_SETUP)]


def test_unknown_setting_rejected():
    with pytest.raises(KeyError):
        Settings().update(nope=1)
