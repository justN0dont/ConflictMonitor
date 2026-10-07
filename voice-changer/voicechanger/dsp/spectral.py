"""Streaming phase-vocoder voice transformer with independent pitch and formant control.

Each analysis frame's magnitude spectrum is split into two parts:

* a smooth spectral **envelope** (the vocal tract, i.e. the formants), found by
  cepstral liftering, and
* a flat **excitation** (the vocal folds, i.e. the harmonics).

Pitch shifting moves only the excitation, and formant shifting resamples only
the envelope. Moving pitch without dragging the formants along is what keeps a
lowered or raised voice sounding human, not like a chipmunk or a slowed tape.

The excitation can also be replaced outright, with a fixed-pitch harmonic comb
(robot) or flat noise (whisper). Both are still shaped by the speaker's own
envelope, so the words stay intelligible.

Character controls colour the result per frame: tremor (a slow pitch and
loudness wobble), jitter (random pitch unsteadiness) and breath (aspiration
noise under the speaker's envelope). Their randomness comes from the
counter-based ``hash_uniform``, keyed on the frame count, so the C++ port
produces the same frames whatever the block size.
"""

from __future__ import annotations

import math

import numpy as np

from .rng import hash_uniform, hash_uniform_vec

MODES = ("normal", "robot", "whisper")
TWO_PI = 2.0 * np.pi
_KMAX = 6      # kernel table spans +/- this many bins
_KRES = 256    # table points per bin
_LOBE = 3      # bins drawn either side of each resynthesised peak


def _dirichlet(x: np.ndarray, n: int) -> np.ndarray:
    """sum_{m<n} exp(i 2 pi x m / n), for |x| well below n."""
    num = np.sin(np.pi * x)
    den = np.sin(np.pi * x / n)
    with np.errstate(invalid="ignore", divide="ignore"):
        mag = np.where(np.abs(den) < 1e-12, float(n), num / den)
    return np.exp(1j * np.pi * x * (n - 1) / n) * mag


def _wrap(p: np.ndarray) -> np.ndarray:
    return p - TWO_PI * np.round(p / TWO_PI)


def _noise_phasors(stream: int, first: int, count: int) -> np.ndarray:
    """exp(i * 2 pi u) for counter-based uniforms u, counters first..first+count-1.

    cos and sin directly: cheaper than a complex exp, and the same values.
    """
    ph = TWO_PI * hash_uniform_vec(stream, np.arange(first, first + count, dtype=np.int64))
    out = np.empty(count, dtype=np.complex128)
    out.real = np.cos(ph)
    out.imag = np.sin(ph)
    return out


class SpectralVoice:
    """Block-streaming pitch/formant shifter.

    ``process`` accepts blocks of any length. When ``block_size`` is a multiple
    of the hop, no extra buffering is needed and the delay is exactly
    ``fft_size - hop``. Otherwise ``hop - 1`` samples of padding are added so
    output is always available. ``latency`` reports the total either way.
    """

    def __init__(
        self,
        sample_rate: int,
        fft_size: int = 1024,
        overlap: int = 8,
        block_size: int | None = None,
        lifter_seconds: float = 0.0012,
    ) -> None:
        if fft_size & (fft_size - 1):
            raise ValueError("fft_size must be a power of two")
        if fft_size % overlap:
            raise ValueError("fft_size must be divisible by overlap")
        self.sample_rate = sample_rate
        self.fft_size = n = fft_size
        self.overlap = overlap
        self.hop = n // overlap
        pad = 0 if block_size and block_size % self.hop == 0 else self.hop - 1
        self.latency = n - self.hop + pad
        self._pad = pad

        self._window = 0.5 - 0.5 * np.cos(TWO_PI * np.arange(n) / n)
        # Hann analysis x Hann synthesis summed over `overlap` frames = 0.375 * overlap.
        self._scale = 1.0 / (0.375 * overlap)
        lifter = int(round(sample_rate * lifter_seconds))
        self._lifter = max(8, min(n // 4, lifter))
        q = np.arange(n)
        dist = np.minimum(q, n - q)
        self._lifter_win = np.where(dist < self._lifter, 1.0, np.where(dist == self._lifter, 0.5, 0.0))

        # Window kernel table, H(d) = sum_n w[n] exp(i 2 pi d n / N), in closed
        # form: Hann = 0.5 - 0.25 e^{+} - 0.25 e^{-}, each term a Dirichlet kernel.
        d = np.arange(-_KMAX * _KRES, _KMAX * _KRES + 1) / _KRES
        self._ktab = 0.5 * _dirichlet(d, n) - 0.25 * _dirichlet(d + 1, n) - 0.25 * _dirichlet(d - 1, n)

        half = n // 2 + 1
        self._k = np.arange(half, dtype=np.float64)
        self._expct = TWO_PI * self.hop / n
        # Breath noise is tilted toward the highs like real aspiration
        # (turbulence at the glottis is weak below ~1.5 kHz).
        fk = self._k * sample_rate / n
        self._breath_tilt = fk * fk / (fk * fk + 1500.0 ** 2)
        # Jitter glides to each new random target with a 25 ms time constant.
        self._jitter_coef = math.exp(-self.hop / (0.025 * sample_rate))
        # Noise frames are mutually incoherent, so overlap-add sums their power,
        # not their amplitude: coherent 0.375*o vs incoherent sqrt(35/128*o).
        self._noise_comp = 0.375 * overlap / math.sqrt(35.0 / 128.0 * overlap)

        self.pitch_ratio = 1.0
        self.formant_ratio = 1.0
        self.mode = "normal"
        self.robot_hz = 110.0
        self.tremor_hz = 5.5
        self.tremor_depth = 0.0
        self.jitter = 0.0
        self.breath = 0.0
        self.reset()

    # ---- parameters ------------------------------------------------------
    def set_pitch(self, semitones: float) -> None:
        self.pitch_ratio = float(2.0 ** (semitones / 12.0))

    def set_formant(self, semitones: float) -> None:
        """0 keeps the speaker's formants where they are."""
        self.formant_ratio = float(2.0 ** (semitones / 12.0))

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode

    def set_robot_pitch(self, hz: float) -> None:
        self.robot_hz = max(20.0, float(hz))

    # Character ranges match the C++ settings parser, so both agree on any input.
    def set_tremor(self, hz: float, depth_st: float) -> None:
        """Vibrato at ``hz``, swinging pitch +/- ``depth_st`` semitones (0 = off)."""
        self.tremor_hz = min(max(float(hz), 0.5), 15.0)
        self.tremor_depth = min(max(float(depth_st), 0.0), 3.0)

    def set_jitter(self, amount: float) -> None:
        """Random pitch unsteadiness, 0..1 (1 = up to +/-0.6 semitone)."""
        self.jitter = min(max(float(amount), 0.0), 1.0)

    def set_breath(self, amount: float) -> None:
        """Share of each frame's energy turned into aspiration noise, 0..1."""
        self.breath = min(max(float(amount), 0.0), 1.0)

    def reset(self) -> None:
        n, half = self.fft_size, self.fft_size // 2 + 1
        self._frame = np.zeros(n)
        self._pending = np.zeros(0)
        self._accum = np.zeros(n)
        self._out = np.zeros(self._pad)
        self._last_phase = np.zeros(half)
        self._sum_phase = np.zeros(half)
        self._frame_index = 0  # frames since reset: the counter for every random draw
        self._tremor_phase = 0.0
        self._jitter_state = 0.0

    # ---- streaming -------------------------------------------------------
    def process(self, x: np.ndarray) -> np.ndarray:
        hop = self.hop
        pending = np.concatenate((self._pending, np.asarray(x, dtype=np.float64)))
        frames = len(pending) // hop
        produced = [self._out]
        for f in range(frames):
            self._frame[:-hop] = self._frame[hop:]
            self._frame[-hop:] = pending[f * hop:(f + 1) * hop]
            produced.append(self._process_frame())
        self._pending = pending[frames * hop:]
        out = np.concatenate(produced)
        m = len(x)
        if len(out) < m:
            raise ValueError(f"block of {m} samples is not a multiple of the hop ({hop}); "
                             "construct SpectralVoice with block_size=None for arbitrary blocks")
        self._out = out[m:]
        return out[:m]

    def _character(self, f: int) -> tuple[float, float]:
        """This frame's pitch ratio and gain after tremor and jitter.

        Depends only on the frame count, never on the audio. At neutral
        settings the factors are 2**0 and 10**0, both exactly 1.0, so the
        output is bit-identical to running without them.
        """
        sr = self.sample_rate
        # The tremor rate wanders +/-10% frame to frame so it doesn't sound like
        # a metronome. Its phase runs even at zero depth, so the wobble depends
        # only on the frame count, not on when tremor was turned up.
        rate = self.tremor_hz * (1.0 + 0.2 * (hash_uniform(1, f) - 0.5))
        self._tremor_phase = (self._tremor_phase + TWO_PI * rate * self.hop / sr) % TWO_PI
        s = math.sin(self._tremor_phase)
        tremor_st = self.tremor_depth * s
        gain = 10.0 ** (1.5 * self.tremor_depth * s / 20.0)  # real vibrato swings loudness too
        target = self.jitter * 0.6 * (2.0 * hash_uniform(2, f) - 1.0)
        self._jitter_state = target + (self._jitter_state - target) * self._jitter_coef
        return self.pitch_ratio * 2.0 ** ((tremor_st + self._jitter_state) / 12.0), gain

    def _process_frame(self) -> np.ndarray:
        n, osamp = self.fft_size, self.overlap
        k = self._k
        half = len(k)
        f = self._frame_index
        r, gain = self._character(f)

        # analysis: magnitude + true per-bin frequency (in bins)
        spec = np.fft.rfft(self._frame * self._window)
        mag = np.abs(spec)
        phase = np.angle(spec)
        delta = _wrap(phase - self._last_phase - k * self._expct)
        self._last_phase = phase
        true_freq = k + delta * osamp / TWO_PI

        # Envelope by cepstral liftering.
        # Floor at -80 dB re the frame peak: the log of near-empty bins swings
        # wildly frame to frame and would otherwise modulate the envelope.
        cep = np.fft.irfft(np.log(mag + mag.max() * 1e-4 + 1e-12), n)
        log_env = np.fft.rfft(cep * self._lifter_win).real
        flat = mag / np.exp(log_env)

        # excitation: shift or replace
        if self.mode == "normal" and abs(r - 1.0) >= 1e-9:
            out_spec = self._shift_peaks(mag, true_freq, log_env, r)  # sets _sum_phase
        else:
            if self.mode == "normal":
                # Nothing to move: keep the analysis phases (bit-exact pass-through
                # when the formants stay put too).
                exc = flat * np.exp(1j * phase)
            elif self.mode == "robot":
                f0 = self.robot_hz * r * n / self.sample_rate
                harmonics = np.arange(1, int((half - 1) / f0) + 1) * f0
                exc = np.zeros(half, dtype=np.complex128)
                if len(harmonics):
                    idx = np.round(harmonics).astype(np.int64)
                    ph = _wrap(self._sum_phase[idx] + TWO_PI * harmonics / osamp)
                    exc[idx] = np.exp(1j * ph)
            else:  # whisper
                exc = _noise_phasors(4, f * half, half)

            # re-apply the (optionally shifted) envelope and resynthesise
            env = np.interp(k / self.formant_ratio, k, log_env)
            out_spec = exc * np.exp(env)
            if self.mode != "normal":
                # Replaced excitations carry no loudness of their own; match frame energy.
                out_energy = float(np.vdot(out_spec, out_spec).real)
                if out_energy > 0:
                    out_spec *= np.sqrt(float(np.dot(mag, mag)) / out_energy)
                if self.mode == "whisper":
                    out_spec *= self._noise_comp
            self._sum_phase = np.angle(out_spec)

        # Breath goes in after the phase memory is updated: noise phases must
        # never be propagated into the next frame's partials.
        if self.breath > 0 and self.mode != "whisper":
            out_spec = self._add_breath(out_spec, log_env, f)
        out_spec = out_spec * gain
        self._frame_index += 1
        return self._overlap_add(out_spec)

    def _add_breath(self, out_spec: np.ndarray, log_env: np.ndarray, f: int) -> np.ndarray:
        """Trade ``breath`` of the frame's energy for envelope-shaped noise.

        The noise follows the (formant-shifted) envelope, so it whispers the
        same vowel, tilted toward the highs where aspiration lives. The frame's
        energy is split between voice and noise rather than added to, so
        breathiness changes the texture more than the loudness.
        """
        k = self._k
        half = len(k)
        env = np.interp(k / self.formant_ratio, k, log_env)
        nmag = np.exp(env) * self._breath_tilt
        e_h = float(np.vdot(out_spec, out_spec).real)
        e_n = float(np.dot(nmag, nmag))
        if e_h <= 0 or e_n <= 0:
            return out_spec
        nmag *= math.sqrt(self.breath * e_h / e_n) * self._noise_comp
        return math.sqrt(1.0 - self.breath) * out_spec + nmag * _noise_phasors(3, f * half, half)

    def _overlap_add(self, out_spec: np.ndarray) -> np.ndarray:
        n, hop = self.fft_size, self.hop
        frame = np.fft.irfft(out_spec, n) * self._window * self._scale

        self._accum += frame
        out = self._accum[:hop].copy()
        self._accum[:-hop] = self._accum[hop:]
        self._accum[-hop:] = 0.0
        return out

    def _hann_kernel(self, d: np.ndarray) -> np.ndarray:
        """DFT of the analysis window at fractional bin offset ``d``.

        A windowed complex sinusoid at frequency f (in bins) with phase phi at
        frame start transforms to X[k] = exp(i*phi) * H(f - k). H comes from a
        precomputed 1/256-bin table.
        """
        pos = (np.clip(d, -_KMAX, _KMAX) + _KMAX) * _KRES
        i0 = np.minimum(pos.astype(np.int64), len(self._ktab) - 2)
        t = pos - i0
        return self._ktab[i0] * (1 - t) + self._ktab[i0 + 1] * t

    def _shift_peaks(self, mag, true_freq, log_env, r) -> np.ndarray:
        """Formant-aware pitch shift by peak resynthesis.

        Each spectral peak is measured as a sinusoid: exact frequency from the
        phase vocoder, complex amplitude by dividing out the window kernel.
        It is then redrawn at precisely ``r`` times its frequency as an
        analytic window main lobe. Shifting whole bins instead leaves each lobe
        up to half a bin away from the frequency its phase encodes, and
        overlap-add turns that mismatch into audible sidebands. Phase is carried
        frame to frame per output sinusoid (Laroche & Dolson's peak-locked
        update), so partials stay coherent.
        """
        half = len(mag)
        inner = mag[1:-1]
        floor = mag.max() * 1e-4
        peaks = np.flatnonzero((inner > mag[:-2]) & (inner >= mag[2:]) & (inner > floor)) + 1
        if len(peaks) == 0:
            return np.zeros(half, dtype=np.complex128)

        fp = true_freq[peaks]
        fo = fp * r
        # A real sinusoid's main-lobe peak sits within half a bin of its true
        # frequency. Window sidelobes are local maxima too, but they report the
        # parent's frequency, far from their own bin; redrawing them would
        # duplicate the parent.
        keep = (np.abs(fp - peaks) <= 0.75) & (fo > 0.5) & (fo < half - 1.5)
        peaks, fp, fo = peaks[keep], fp[keep], fo[keep]
        if len(peaks) == 0:
            return np.zeros(half, dtype=np.complex128)

        # Sinusoid amplitude, moved from the input envelope at its own frequency
        # to the (formant-shifted) envelope at its new one. Applying the
        # envelope per sinusoid rather than per bin keeps each lobe's shape exact.
        h = self._hann_kernel(fp - peaks)
        amp = mag[peaks] / np.maximum(np.abs(h), 1e-12)
        k = self._k
        amp *= np.exp(np.interp(fo / self.formant_ratio, k, log_env) - np.interp(fp, k, log_env))

        # phase at frame start, propagated from the previous output frame
        dest_bin = np.round(fo).astype(np.int64)
        ph = _wrap(self._sum_phase[dest_bin] + TWO_PI * fo / self.overlap)
        new_sin_phase = np.zeros(half)
        new_sin_phase[dest_bin] = ph
        self._sum_phase = new_sin_phase

        offs = np.arange(-_LOBE, _LOBE + 1)
        bins = dest_bin[:, None] + offs[None, :]
        vals = (amp * np.exp(1j * ph))[:, None] * self._hann_kernel(fo[:, None] - bins)
        ok = (bins >= 0) & (bins < half)
        b, v = bins[ok], vals[ok]
        out = np.bincount(b, weights=v.real, minlength=half) + 1j * np.bincount(b, weights=v.imag, minlength=half)

        # Keep loudness steady: shifting down packs more harmonics under the
        # envelope (louder), shifting up spreads them out (quieter).
        e_out = float(np.vdot(out, out).real)
        if e_out > 0:
            out *= np.sqrt(float(np.dot(mag, mag)) / e_out)
        return out
