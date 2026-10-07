"""Block-based effects for the voice chain.

Everything here keeps its own state between blocks, so a stream can be fed
through in arbitrary block sizes. Filters run through ``scipy.signal.lfilter``
with carried initial conditions. Dynamics compute their gain once per block
and ramp it linearly across the block, which is inaudible at 5 ms blocks and
keeps per-sample Python loops off the audio thread.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter

from .rng import hash_uniform


def db_to_lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def lin_to_db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-12))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def biquad_coeffs(kind: str, fs: float, f0: float, q: float = 0.707, gain_db: float = 0.0):
    """RBJ audio-EQ-cookbook biquad. Returns normalised (b, a)."""
    f0 = min(max(f0, 1.0), fs * 0.49)
    w0 = 2 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    A = 10 ** (gain_db / 40)
    if kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "peaking":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
        a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif kind == "lowshelf":
        sq = 2 * math.sqrt(A) * alpha
        b = [A * ((A + 1) - (A - 1) * cw + sq), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sq)]
        a = [(A + 1) + (A - 1) * cw + sq, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sq]
    elif kind == "highshelf":
        sq = 2 * math.sqrt(A) * alpha
        b = [A * ((A + 1) + (A - 1) * cw + sq), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sq)]
        a = [(A + 1) - (A - 1) * cw + sq, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sq]
    else:
        raise ValueError(kind)
    b = np.array(b) / a[0]
    a = np.array(a) / a[0]
    return b, a


class Biquad:
    """A stateful biquad whose settings can change between blocks without clicks."""

    def __init__(self, fs: float, kind: str, f0: float, q: float = 0.707, gain_db: float = 0.0):
        self.fs, self.kind = fs, kind
        self._zi = np.zeros(2)
        self._key = None
        self.set(f0, q, gain_db)

    def set(self, f0: float, q: float = 0.707, gain_db: float = 0.0) -> None:
        key = (round(f0, 2), round(q, 3), round(gain_db, 2))
        if key != self._key:
            self._key = key
            self.b, self.a = biquad_coeffs(self.kind, self.fs, f0, q, gain_db)

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = lfilter(self.b, self.a, x, zi=self._zi)
        return y


class DelayLine:
    """Fixed integer delay, used to time-align the dry signal with the shifter."""

    def __init__(self, samples: int):
        self._buf = np.zeros(samples)

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(self._buf) == 0:
            return x
        joined = np.concatenate((self._buf, x))
        self._buf = joined[len(x):]
        return joined[:len(x)]


# ---------------------------------------------------------------------------
# Dynamics
# ---------------------------------------------------------------------------

def _ramp(start: float, end: float, n: int) -> np.ndarray:
    return np.linspace(start, end, n, endpoint=False) if n > 1 else np.array([end] * n)


def _coef(time_s: float, block: int, fs: float) -> float:
    """One-pole smoothing coefficient for a per-block update."""
    return math.exp(-block / max(time_s * fs, 1e-9))


class NoiseGate:
    """Downward expander/gate with hysteresis and hold.

    Opens above ``threshold_db`` and closes once the level falls 6 dB below it
    for longer than ``hold``. Keeps keyboard noise and room tone out of the
    pitch shifter, where they would otherwise turn into warbling artefacts.
    """

    def __init__(self, fs: float, threshold_db: float = -50.0, attack: float = 0.002,
                 release: float = 0.12, hold: float = 0.08, floor_db: float = -80.0):
        self.fs = fs
        self.threshold_db = threshold_db
        self.attack, self.release, self.hold = attack, release, hold
        self.floor = db_to_lin(floor_db)
        self.enabled = True
        self._gain = 1.0
        self._open = True
        self._held = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if not self.enabled or n == 0:
            self._gain = 1.0
            return x
        level = lin_to_db(float(np.sqrt(np.mean(x * x))))
        if level >= self.threshold_db:
            self._open, self._held = True, 0.0
        elif level < self.threshold_db - 6.0:
            self._held += n / self.fs
            if self._held >= self.hold:
                self._open = False
        # Smooth in dB so the release reaches the floor in `release` seconds
        # rather than crawling asymptotically toward it.
        target_db = 0.0 if self._open else lin_to_db(self.floor)
        cur_db = lin_to_db(self._gain)
        rate = -lin_to_db(self.floor) / (self.attack if target_db > cur_db else self.release)
        step = rate * n / self.fs
        new_db = min(cur_db + step, target_db) if target_db > cur_db else max(cur_db - step, target_db)
        new = db_to_lin(new_db)
        y = x * _ramp(self._gain, new, n)
        self._gain = new
        return y

    @property
    def is_open(self) -> bool:
        return self._open


class Compressor:
    """Feed-forward RMS compressor with soft knee and makeup gain."""

    def __init__(self, fs: float, threshold_db: float = -18.0, ratio: float = 3.0,
                 attack: float = 0.005, release: float = 0.1, knee_db: float = 6.0,
                 makeup_db: float = 0.0):
        self.fs = fs
        self.threshold_db, self.ratio = threshold_db, ratio
        self.attack, self.release = attack, release
        self.knee_db, self.makeup_db = knee_db, makeup_db
        self.enabled = True
        self._env_db = -120.0
        self._gain = 1.0
        self.gain_reduction_db = 0.0

    def _curve(self, level_db: float) -> float:
        over = level_db - self.threshold_db
        k = self.knee_db
        slope = 1.0 - 1.0 / max(self.ratio, 1.0)
        if 2 * over < -k:
            return 0.0
        if 2 * abs(over) <= k and k > 0:
            return -slope * (over + k / 2) ** 2 / (2 * k)
        return -slope * over

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if not self.enabled or n == 0:
            self.gain_reduction_db = 0.0
            return x
        level = lin_to_db(float(np.sqrt(np.mean(x * x))))
        c = _coef(self.attack if level > self._env_db else self.release, n, self.fs)
        self._env_db = level + (self._env_db - level) * c
        self.gain_reduction_db = self._curve(self._env_db)
        new = db_to_lin(self.gain_reduction_db + self.makeup_db)
        y = x * _ramp(self._gain, new, n)
        self._gain = new
        return y


class Limiter:
    """Brickwall-ish peak limiter: instant attack per block, smooth release,
    and a final soft clip so nothing above the ceiling ever reaches the device."""

    def __init__(self, fs: float, ceiling_db: float = -1.0, release: float = 0.08):
        self.fs = fs
        self.ceiling = db_to_lin(ceiling_db)
        self.release = release
        self._gain = 1.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x
        peak = float(np.max(np.abs(x)))
        target = min(1.0, self.ceiling / peak) if peak > 0 else 1.0
        if target < self._gain:
            new = target
            g = np.full(n, new)  # attack immediately for this block
        else:
            new = target + (self._gain - target) * _coef(self.release, n, self.fs)
            g = _ramp(self._gain, new, n)
        self._gain = new
        y = x * g
        # Soft-knee safety clip: linear up to 80% of the ceiling, then a tanh
        # shoulder that approaches (never exceeds) the ceiling.
        c = self.ceiling
        k = 0.8 * c
        a = np.abs(y)
        if a.max() <= k:
            return y
        return np.where(a <= k, y, np.sign(y) * (k + (c - k) * np.tanh((a - k) / (c - k))))


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def drive(x: np.ndarray, amount: float) -> np.ndarray:
    """tanh saturation. amount 0..1; loudness is roughly preserved."""
    if amount <= 0:
        return x
    k = 1.0 + 19.0 * amount
    return np.tanh(k * x) / math.tanh(k) if k > 1 else x


class GravelModulator:
    """Vocal fry / gravel: a fast, slightly irregular train of amplitude dips.

    Creaky voice is the vocal folds slapping shut in uneven pulses well below
    the speaking pitch. Pulling the level down with a raised-cosine dip at
    ``hz`` reproduces that rattle; ``amount`` sets how deep each dip goes. The
    rate wanders +/-25% every 256 samples, keyed on the absolute sample index
    through the counter-based RNG, so the result is the same for any block size
    and in the C++ port. The phase advances even at amount 0, so the pulse
    train depends only on elapsed time, never on when gravel was switched on.
    """

    TICK = 256  # samples per random rate step

    def __init__(self, fs: float):
        self.fs = fs
        self._phase = 0.0
        self._count = 0  # absolute sample index

    def process(self, x: np.ndarray, amount: float, hz: float) -> np.ndarray:
        # Same ranges as the C++ settings parser, so both agree on any input.
        amount = min(max(float(amount), 0.0), 1.0)
        hz = min(max(float(hz), 10.0), 200.0)
        n = len(x)
        if n == 0:
            return x
        # One draw per 256-sample tick, not per sample: a block spans only a few.
        tick = (self._count + np.arange(n)) // self.TICK
        first = self._count // self.TICK
        wobble = np.array([hash_uniform(5, t) for t in range(first, int(tick[-1]) + 1)])[tick - first]
        f = hz * (1.0 + 0.5 * (wobble - 0.5))
        ph = self._phase + np.cumsum(2 * math.pi * f / self.fs)
        self._phase = float(ph[-1] % (2 * math.pi))
        self._count += n
        if amount <= 0:
            return x
        m = 0.5 - 0.5 * np.cos(ph)
        return x * (1.0 - amount * m * m)


class _Comb:
    """Feedback comb with a one-pole lowpass in the loop (Freeverb's lowpass-feedback comb).

    y[n] = x[n] + fb * lp(y[n-D]). Because the feedback term only reaches back
    D samples, any chunk of up to D samples depends solely on history, so the
    whole chunk is computed with array ops. lfilter only handles the order-1
    loop lowpass.
    """

    def __init__(self, delay: int):
        self.d = delay
        self.hist = np.zeros(delay)  # last D outputs, oldest first
        self.lp_zi = np.zeros(1)

    def process(self, x: np.ndarray, fb: float, damp: float) -> np.ndarray:
        m = len(x)
        delayed = self.hist[:m]
        lp, self.lp_zi = lfilter([1.0 - damp], [1.0, -damp], delayed, zi=self.lp_zi)
        y = x + fb * lp
        self.hist = np.concatenate((self.hist[m:], y))
        return y


class _Allpass:
    """Schroeder allpass, y[n] = -g x[n] + x[n-D] + g y[n-D], chunked like _Comb."""

    def __init__(self, delay: int, g: float = 0.5):
        self.d, self.g = delay, g
        self.xh = np.zeros(delay)
        self.yh = np.zeros(delay)

    def process(self, x: np.ndarray) -> np.ndarray:
        m = len(x)
        y = -self.g * x + self.xh[:m] + self.g * self.yh[:m]
        self.xh = np.concatenate((self.xh[m:], x))
        self.yh = np.concatenate((self.yh[m:], y))
        return y


class Reverb:
    """Freeverb-style reverb: 8 parallel damped combs into 4 series allpasses."""

    COMBS = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)
    ALLPASS = (556, 441, 341, 225)

    def __init__(self, fs: float, room: float = 0.6, damp: float = 0.4):
        self.fs = fs
        s = fs / 44100.0
        self._combs = [_Comb(int(d * s)) for d in self.COMBS]
        self._aps = [_Allpass(int(d * s)) for d in self.ALLPASS]
        self._chunk = min(int(d * s) for d in self.ALLPASS)
        self.mix = 0.0
        self.set(room, damp)

    def set(self, room: float, damp: float) -> None:
        self.room, self.damp = room, damp
        self._fb = 0.7 + 0.28 * room

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.mix <= 0:
            return x
        c = self._chunk
        if len(x) > c:
            return np.concatenate([self.process(x[i:i + c]) for i in range(0, len(x), c)])
        wet = np.zeros_like(x)
        for comb in self._combs:
            wet += comb.process(x, self._fb, self.damp)
        wet *= 0.12  # calibrated: wet RMS ~= dry RMS on steady input at room=0.6
        for ap in self._aps:
            wet = ap.process(wet)
        return x * (1 - 0.5 * self.mix) + wet * self.mix
