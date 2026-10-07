"""Live audio I/O: microphone in, transformed voice out, via PortAudio.

The main output can be any device: your headphones, or the input side of a
virtual cable so other apps hear the transformed voice as a microphone. An
optional *monitor* device gets a copy, so you can hear yourself while the main
output feeds Discord/OBS/Zoom.

On Windows, prefer devices on the "Windows WASAPI" host API: they give far
lower latency than the MME defaults.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np

from .chain import QUALITY, Settings, VoiceChain

try:  # sounddevice needs the PortAudio shared library; keep DSP importable without it
    import sounddevice as sd
except OSError as exc:  # pragma: no cover - depends on the host
    sd = None
    _SD_ERROR = exc
else:
    _SD_ERROR = None


def _require_sd():
    if sd is None:
        raise RuntimeError(f"Audio I/O unavailable: {_SD_ERROR}. Install PortAudio "
                           "(Linux: `sudo apt install libportaudio2`).")
    return sd


@dataclass
class Device:
    index: int
    name: str
    hostapi: str
    inputs: int
    outputs: int
    default_samplerate: float

    @property
    def label(self) -> str:
        return f"[{self.index}] {self.name} ({self.hostapi})"


def list_devices() -> list[Device]:
    s = _require_sd()
    apis = s.query_hostapis()
    out = []
    for i, d in enumerate(s.query_devices()):
        out.append(Device(i, d["name"], apis[d["hostapi"]]["name"], d["max_input_channels"],
                          d["max_output_channels"], d["default_samplerate"]))
    return out


def preferred_hostapi() -> str | None:
    """The low-latency host API to favour on this platform, if present."""
    if sys.platform.startswith("win"):
        return "Windows WASAPI"
    if sys.platform == "darwin":
        return "Core Audio"
    return None


def default_device(kind: str) -> int | None:
    """Default input/output index, preferring the low-latency host API's default."""
    s = _require_sd()
    want = preferred_hostapi()
    if want:
        for api in s.query_hostapis():
            if api["name"] == want:
                idx = api["default_input_device" if kind == "input" else "default_output_device"]
                if idx >= 0:
                    return idx
    d = s.default.device[0 if kind == "input" else 1]
    return None if d is None or d < 0 else int(d)


def find_device(query: str | int | None, kind: str) -> int | None:
    """Resolve an index or a case-insensitive name fragment to a device index."""
    if query is None or query == "":
        return default_device(kind)
    if isinstance(query, int) or str(query).isdigit():
        return int(query)
    need = "inputs" if kind == "input" else "outputs"
    matches = [d for d in list_devices() if str(query).lower() in d.name.lower() and getattr(d, need) > 0]
    if not matches:
        raise ValueError(f"no {kind} device matches {query!r}")
    pref = preferred_hostapi()
    matches.sort(key=lambda d: d.hostapi != pref)
    return matches[0].index


class RingBuffer:
    """Single-producer/single-consumer float ring buffer for the monitor feed."""

    def __init__(self, capacity: int):
        self._buf = np.zeros(capacity, dtype=np.float32)
        self._r = self._w = self._n = 0
        self._lock = threading.Lock()

    def write(self, x: np.ndarray) -> None:
        with self._lock:
            cap = len(self._buf)
            v = x[-cap:]
            m = len(v)
            end = self._w + m
            if end <= cap:
                self._buf[self._w:end] = v
            else:
                k = cap - self._w
                self._buf[self._w:] = v[:k]
                self._buf[:m - k] = v[k:]
            self._w = end % cap
            self._n += m
            if self._n > cap:  # overflow: drop oldest
                self._r = self._w
                self._n = cap

    def read(self, m: int) -> np.ndarray:
        out = np.zeros(m, dtype=np.float32)
        with self._lock:
            take = min(m, self._n)
            cap = len(self._buf)
            end = self._r + take
            if end <= cap:
                out[:take] = self._buf[self._r:end]
            else:
                k = cap - self._r
                out[:k] = self._buf[self._r:]
                out[k:take] = self._buf[:take - k]
            self._r = end % cap
            self._n -= take
        return out


class WavRecorder:
    """Writes blocks from the audio thread to a 16-bit WAV on a worker thread."""

    def __init__(self, path: str, sample_rate: int):
        self.path = path
        self._q: queue.Queue = queue.Queue()
        self._wav = wave.open(path, "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(sample_rate)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def push(self, block: np.ndarray) -> None:
        self._q.put(block.copy())

    def _run(self) -> None:
        while True:
            block = self._q.get()
            if block is None:
                break
            pcm = np.clip(block, -1.0, 1.0)
            self._wav.writeframes((pcm * 32767).astype("<i2").tobytes())

    def close(self) -> None:
        self._q.put(None)
        self._thread.join()
        self._wav.close()


class LiveEngine:
    """Mic -> VoiceChain -> output device (+ optional monitor device)."""

    def __init__(self, input_device=None, output_device=None, monitor_device=None,
                 sample_rate: int = 48000, quality: str = "balanced",
                 settings: Settings | None = None, input_channel: int = 0):
        if quality not in QUALITY:
            raise ValueError(f"quality must be one of {list(QUALITY)}")
        self.input_device = find_device(input_device, "input")
        self.output_device = find_device(output_device, "output")
        self.monitor_device = None if monitor_device in (None, "") else find_device(monitor_device, "output")
        self.sample_rate = sample_rate
        self.quality = quality
        self.settings = settings or Settings()
        self.input_channel = input_channel
        self.monitor_enabled = self.monitor_device is not None
        self.chain = VoiceChain(sample_rate, quality, self.settings)
        self.xruns = 0
        self._streams: list = []
        self._monitor_buf: RingBuffer | None = None
        self._recorder: WavRecorder | None = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        s = _require_sd()
        bs = self.chain.block_size
        in_info = s.query_devices(self.input_device)
        out_info = s.query_devices(self.output_device)
        in_ch = max(1, min(in_info["max_input_channels"], self.input_channel + 1))
        out_ch = max(1, min(out_info["max_output_channels"], 2))

        if in_info["hostapi"] == out_info["hostapi"]:
            stream = s.Stream(device=(self.input_device, self.output_device), samplerate=self.sample_rate,
                              blocksize=bs, channels=(in_ch, out_ch), dtype="float32",
                              latency="low", callback=self._duplex_cb)
            self._streams = [stream]
        else:
            # PortAudio can't open one duplex stream across host APIs; bridge with a buffer.
            self._bridge = RingBuffer(bs * 16)
            self._streams = [
                s.InputStream(device=self.input_device, samplerate=self.sample_rate, blocksize=bs,
                              channels=in_ch, dtype="float32", latency="low", callback=self._input_cb),
                s.OutputStream(device=self.output_device, samplerate=self.sample_rate, blocksize=bs,
                               channels=out_ch, dtype="float32", latency="low", callback=self._bridge_out_cb),
            ]
        if self.monitor_device is not None:
            mon_info = s.query_devices(self.monitor_device)
            self._monitor_buf = RingBuffer(bs * 16)
            self._streams.append(s.OutputStream(
                device=self.monitor_device, samplerate=self.sample_rate, blocksize=bs,
                channels=max(1, min(mon_info["max_output_channels"], 2)), dtype="float32",
                latency="low", callback=self._monitor_cb))
        for st in self._streams:
            st.start()

    def stop(self) -> None:
        for st in self._streams:
            st.stop()
            st.close()
        self._streams = []
        self.stop_recording()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    @property
    def running(self) -> bool:
        return bool(self._streams)

    # ---- recording -------------------------------------------------------
    def start_recording(self, path: str) -> None:
        self.stop_recording()
        self._recorder = WavRecorder(path, self.sample_rate)

    def stop_recording(self) -> str | None:
        rec, self._recorder = self._recorder, None
        if rec:
            rec.close()
            return rec.path
        return None

    # ---- stats -----------------------------------------------------------
    @property
    def latency_ms(self) -> float:
        """Round-trip estimate: device input + output buffers + DSP delay."""
        dev = 0.0
        for st in self._streams[:2]:
            lat = st.latency
            dev += sum(lat) if isinstance(lat, tuple) else lat
        return 1000.0 * (dev + self.chain.latency / self.sample_rate)

    @property
    def cpu_load(self) -> float:
        return self._streams[0].cpu_load if self._streams else 0.0

    # ---- callbacks (audio thread) ---------------------------------------
    def _run_chain(self, indata: np.ndarray) -> np.ndarray:
        y = self.chain.process(indata[:, min(self.input_channel, indata.shape[1] - 1)])
        if self._monitor_buf is not None and self.monitor_enabled:
            self._monitor_buf.write(y)
        rec = self._recorder
        if rec is not None:
            rec.push(y)
        return y

    def _duplex_cb(self, indata, outdata, frames, t, status):
        if status:
            self.xruns += 1
        outdata[:] = self._run_chain(indata)[:, None]

    def _input_cb(self, indata, frames, t, status):
        if status:
            self.xruns += 1
        self._bridge.write(self._run_chain(indata))

    def _bridge_out_cb(self, outdata, frames, t, status):
        if status:
            self.xruns += 1
        outdata[:] = self._bridge.read(frames)[:, None]

    def _monitor_cb(self, outdata, frames, t, status):
        buf = self._monitor_buf
        outdata[:] = (buf.read(frames) if buf and self.monitor_enabled else np.zeros(frames, np.float32))[:, None]


def wait_forever(engine: LiveEngine, show_meters: bool = True) -> None:
    """Console loop for the CLI: show meters until Ctrl+C."""
    try:
        while True:
            time.sleep(0.1)
            if show_meters:
                c = engine.chain

                def bar(p):
                    db = 20 * np.log10(max(p, 1e-6))
                    n = int(max(0, min(30, (db + 60) / 2)))
                    return "#" * n + "-" * (30 - n)

                print(f"\rin {bar(c.input_peak)}  out {bar(c.output_peak)}  "
                      f"gate {'open ' if c.gate_open else 'shut '} GR {c.gain_reduction_db:5.1f} dB  "
                      f"lat {engine.latency_ms:5.1f} ms  cpu {engine.cpu_load * 100:4.0f}%  xruns {engine.xruns}",
                      end="", flush=True)
    except KeyboardInterrupt:
        print()
