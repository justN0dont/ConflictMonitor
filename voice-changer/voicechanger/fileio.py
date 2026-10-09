"""Offline processing of WAV files through the same chain used live."""

from __future__ import annotations

import numpy as np
from scipy.io import wavfile

from .chain import Settings, VoiceChain


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Read any PCM/float WAV as mono float64 in [-1, 1]."""
    sr, data = wavfile.read(path)
    if data.dtype.kind == "i":
        data = data.astype(np.float64) / float(np.iinfo(data.dtype).max)
    elif data.dtype.kind == "u":  # 8-bit WAV is unsigned
        data = (data.astype(np.float64) - 128.0) / 128.0
    else:
        data = data.astype(np.float64)
    if data.ndim == 2:
        data = data.mean(axis=1)
    return data, int(sr)


def write_wav(path: str, audio: np.ndarray, sample_rate: int) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    wavfile.write(path, sample_rate, pcm)


def process_file(src: str, dst: str, settings: Settings, quality: str = "high-quality") -> float:
    """Transform ``src`` into ``dst``. Returns the duration processed, in seconds."""
    audio, sr = read_wav(src)
    out = VoiceChain(sr, quality, settings).process_buffer(audio)
    write_wav(dst, out, sr)
    return len(audio) / sr
