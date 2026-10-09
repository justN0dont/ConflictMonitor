"""Voice calibration: measure a speaker's own pitch once, so the gender and age
presets move each voice into their range instead of by a fixed amount (see
presets.VOICE_FITS).

The result lives in a small per-user file; the GUI and ``--voice-f0`` use it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

CALIBRATION_SECONDS = 6.0
_FS = 16000  # analysis rate: plenty for 50-500 Hz voices, and cheap


def estimate_f0(x: np.ndarray, sample_rate: int) -> float | None:
    """Median speaking pitch in Hz, or None if too little of ``x`` is clear voice.

    YIN (de Cheveigné & Kawahara 2002) over 40 ms frames every 10 ms, taking
    only confidently voiced frames within 30 dB of the loudest, so pauses,
    breaths and room noise don't count. Needs half a second of clear voice.
    """
    x = np.asarray(x, dtype=np.float64)
    if sample_rate != _FS:
        g = np.gcd(int(sample_rate), _FS)
        x = resample_poly(x, _FS // g, int(sample_rate) // g)
    w, hop = int(0.04 * _FS), int(0.01 * _FS)
    tau_min, tau_max = _FS // 500, _FS // 50
    n_fft = 1 << int(np.ceil(np.log2(w + tau_max)))
    if len(x) < w + tau_max + hop:
        return None
    frames = np.lib.stride_tricks.sliding_window_view(x, w + tau_max)[::hop]
    a = frames[:, :w]
    energy = np.sum(a * a, axis=1)
    ok = energy > energy.max() * 1e-3
    if not ok.any():
        return None
    frames, a, energy = frames[ok], a[ok], energy[ok]
    # Difference function d(tau) = sum (x[t] - x[t+tau])^2 over the window, via FFT.
    cum = np.concatenate((np.zeros((len(frames), 1)), np.cumsum(frames * frames, axis=1)), axis=1)
    taus = np.arange(tau_max)
    shifted_energy = cum[:, taus + w] - cum[:, taus]
    corr = np.fft.irfft(np.conj(np.fft.rfft(a, n_fft)) * np.fft.rfft(frames, n_fft), n_fft)[:, :tau_max]
    d = energy[:, None] + shifted_energy - 2 * corr
    cmnd = np.ones_like(d)  # cumulative mean normalised difference
    cmnd[:, 1:] = d[:, 1:] * taus[1:] / np.maximum(np.cumsum(d[:, 1:], axis=1), 1e-20)
    f0 = []
    for row in cmnd:
        below = np.flatnonzero(row[tau_min:] < 0.2)
        if len(below):
            t = below[0] + tau_min
            while t + 1 < tau_max and row[t + 1] < row[t]:
                t += 1  # down to the dip's floor
            f0.append(_FS / t)
    if len(f0) < 50:  # under half a second of clear voice
        return None
    return float(np.median(f0))


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "VoiceChanger" / "voice.json"


def load_voice_f0(path: Path | None = None) -> float | None:
    try:
        hz = float(json.loads((path or config_path()).read_text())["voice_f0"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return hz if 50.0 <= hz <= 500.0 else None


def save_voice_f0(hz: float, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"voice_f0": round(float(hz), 1)}))
