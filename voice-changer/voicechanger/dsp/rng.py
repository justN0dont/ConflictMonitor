"""Counter-based random numbers that the C++ port reproduces bit for bit.

Value ``counter`` of stream ``stream`` is a pure function of the pair (a
SplitMix64 finaliser over a mix of both), not the next draw from a stateful
generator. So the numbers never depend on block size, on how many values an
earlier frame happened to draw, or on the language: apo/dsp/rng.h computes the
same 64-bit integers with plain uint64_t arithmetic.

Streams in use: 1 tremor-rate wobble, 2 jitter, 3 breath noise phase,
4 whisper noise phase, 5 gravel rate wobble.
"""

from __future__ import annotations

import numpy as np

_MASK = (1 << 64) - 1
_STREAM = 0xD1B54A32D192ED03
_COUNTER = 0x9E3779B97F4A7C15
_OFFSET = 0x632BE59BD9B4E019
_MIX1 = 0xBF58476D1CE4E5B9
_MIX2 = 0x94D049BB133111EB
_UNIT = 2.0 ** -53  # 53 random bits -> a double in [0, 1), exactly representable

# uint64 copies so the vectorised version stays in wrapping integer arithmetic
# (with a Python int operand, older NumPy promotes to float64 and loses mod 2**64).
_COUNTER64, _MIX1_64, _MIX2_64 = np.uint64(_COUNTER), np.uint64(_MIX1), np.uint64(_MIX2)
_S30, _S27, _S31, _S11 = np.uint64(30), np.uint64(27), np.uint64(31), np.uint64(11)


def hash_uniform(stream: int, counter: int) -> float:
    """Uniform in [0, 1), determined entirely by ``(stream, counter)``."""
    z = (stream * _STREAM + counter * _COUNTER + _OFFSET) & _MASK
    z = ((z ^ (z >> 30)) * _MIX1) & _MASK
    z = ((z ^ (z >> 27)) * _MIX2) & _MASK
    z ^= z >> 31
    return (z >> 11) * _UNIT


def hash_uniform_vec(stream: int, counters) -> np.ndarray:
    """``hash_uniform`` over an array of counters (same values, element for element).

    In place on one fresh array: it runs per FFT frame on the audio thread.
    """
    z = np.asarray(counters).astype(np.uint64)  # astype copies, so the caller's array is safe
    with np.errstate(over="ignore"):
        z *= _COUNTER64
        z += np.uint64((stream * _STREAM + _OFFSET) & _MASK)
        z ^= z >> _S30
        z *= _MIX1_64
        z ^= z >> _S27
        z *= _MIX2_64
        z ^= z >> _S31
    z >>= _S11
    return z.astype(np.float64) * _UNIT
