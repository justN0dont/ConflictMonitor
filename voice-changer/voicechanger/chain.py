"""The complete voice chain, and the settings that drive it.

    input gain -> rumble HPF -> noise gate -> [pitch/formant shifter + aligned dry mix]
      -> tone HPF/LPF -> 3-band EQ -> drive -> compressor -> reverb
      -> output gain -> limiter

``VoiceChain.process`` is called from the audio thread with one mono block.
Settings live in a plain dataclass that the UI thread mutates. Each block reads
them fresh, so changes apply on the next block with no locking. (Attribute
assignment is atomic under the GIL.)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

import numpy as np

from .dsp.effects import (Biquad, Compressor, DelayLine, Limiter, NoiseGate, Reverb,
                          db_to_lin, drive)
from .dsp.spectral import SpectralVoice

QUALITY = {
    # name: (fft_size, overlap, block_size)
    # Frequency resolution must separate neighbouring harmonics: 1024 points at
    # 48 kHz resolves voices above ~150 Hz; 2048 handles deep voices (~90 Hz+).
    "low-latency": (1024, 8, 256),    # ~19 ms DSP delay; higher voices, fast CPU
    "balanced": (2048, 8, 512),       # ~37 ms; any speaking voice; 512-sample callbacks for headroom
    "high-quality": (4096, 8, 512),   # ~75 ms; offline/file rendering
}


@dataclass
class Settings:
    # voice
    pitch: float = 0.0           # semitones, -24..24
    formant: float = 0.0         # semitones, -12..12 (0 = keep your own formants)
    mode: str = "normal"         # normal | robot | whisper
    robot_hz: float = 110.0
    mix: float = 1.0             # 0 = dry, 1 = fully transformed
    # cleanup
    input_gain_db: float = 0.0
    gate_enabled: bool = True
    gate_threshold_db: float = -50.0
    # tone
    highpass_hz: float = 80.0
    lowpass_hz: float = 18000.0
    low_db: float = 0.0          # shelf @ 200 Hz
    mid_db: float = 0.0          # peak @ 1.5 kHz
    high_db: float = 0.0         # shelf @ 5 kHz
    drive: float = 0.0           # 0..1
    # dynamics / space
    comp_enabled: bool = True
    comp_threshold_db: float = -20.0
    comp_ratio: float = 3.0
    comp_makeup_db: float = 3.0
    reverb_mix: float = 0.0      # 0..1
    reverb_room: float = 0.6     # 0..1
    output_gain_db: float = 0.0
    bypass: bool = False

    def update(self, **values) -> "Settings":
        names = {f.name for f in fields(self)}
        for k, v in values.items():
            if k not in names:
                raise KeyError(f"unknown setting {k!r}")
            setattr(self, k, v)
        return self

    def to_dict(self) -> dict:
        return asdict(self)


class VoiceChain:
    def __init__(self, sample_rate: int, quality: str = "balanced", settings: Settings | None = None):
        fft_size, overlap, block = QUALITY[quality]
        self.sample_rate = fs = sample_rate
        self.quality = quality
        self.block_size = block
        self.settings = settings or Settings()

        self.voice = SpectralVoice(fs, fft_size=fft_size, overlap=overlap, block_size=block)
        self.latency = self.voice.latency
        self._dry = DelayLine(self.latency)
        self._bypass_delay = DelayLine(self.latency)

        self._rumble = Biquad(fs, "highpass", 80.0)
        self._gate = NoiseGate(fs)
        self._hp = Biquad(fs, "highpass", 80.0)
        self._lp = Biquad(fs, "lowpass", 18000.0)
        self._low = Biquad(fs, "lowshelf", 200.0)
        self._mid = Biquad(fs, "peaking", 1500.0, q=0.9)
        self._high = Biquad(fs, "highshelf", 5000.0)
        self._comp = Compressor(fs)
        self._reverb = Reverb(fs)
        self._limiter = Limiter(fs)
        self._reverb_room = None

        self.input_peak = 0.0
        self.output_peak = 0.0

    @property
    def gate_open(self) -> bool:
        return self._gate.is_open

    @property
    def gain_reduction_db(self) -> float:
        return self._comp.gain_reduction_db

    def process(self, block: np.ndarray) -> np.ndarray:
        s = self.settings
        x = np.asarray(block, dtype=np.float64) * db_to_lin(s.input_gain_db)
        self.input_peak = float(np.max(np.abs(x))) if len(x) else 0.0

        # Bypass still goes through a matching delay, so toggling it is an
        # honest A/B with no jump in timing.
        if s.bypass:
            self.voice.process(x)  # keep shifter state warm
            y = self._bypass_delay.process(x)
            self._dry.process(x)
            self.output_peak = float(np.max(np.abs(y))) if len(y) else 0.0
            return y.astype(np.float32)
        self._bypass_delay.process(x)

        x = self._rumble.process(x)
        self._gate.enabled = s.gate_enabled
        self._gate.threshold_db = s.gate_threshold_db
        x = self._gate.process(x)

        v = self.voice
        v.set_pitch(s.pitch)
        v.set_formant(s.formant)
        v.set_mode(s.mode)
        v.set_robot_pitch(s.robot_hz)
        wet = v.process(x)
        dry = self._dry.process(x)
        y = wet * s.mix + dry * (1.0 - s.mix)

        self._hp.set(s.highpass_hz)
        self._lp.set(s.lowpass_hz)
        self._low.set(200.0, 0.707, s.low_db)
        self._mid.set(1500.0, 0.9, s.mid_db)
        self._high.set(5000.0, 0.707, s.high_db)
        y = self._hp.process(y)
        y = self._lp.process(y)
        y = self._low.process(y)
        y = self._mid.process(y)
        y = self._high.process(y)
        y = drive(y, s.drive)

        c = self._comp
        c.enabled = s.comp_enabled
        c.threshold_db, c.ratio, c.makeup_db = s.comp_threshold_db, s.comp_ratio, s.comp_makeup_db
        y = c.process(y)

        if self._reverb_room != s.reverb_room:
            self._reverb.set(s.reverb_room, 0.4)
            self._reverb_room = s.reverb_room
        self._reverb.mix = s.reverb_mix
        y = self._reverb.process(y)

        y = self._limiter.process(y * db_to_lin(s.output_gain_db))
        self.output_peak = float(np.max(np.abs(y))) if len(y) else 0.0
        return y.astype(np.float32)

    def process_buffer(self, audio: np.ndarray) -> np.ndarray:
        """Offline: run a whole mono buffer through, compensating the delay so
        the output lines up sample-for-sample with the input."""
        b = self.block_size
        total = len(audio) + self.latency
        total += -total % b  # whole blocks only: the shifter is set up for this block size
        padded = np.concatenate((audio, np.zeros(total - len(audio))))
        out = np.concatenate([self.process(padded[i:i + b]) for i in range(0, len(padded), b)])
        return out[self.latency:self.latency + len(audio)]
