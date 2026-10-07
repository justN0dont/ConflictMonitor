# Voice Changer

A native desktop voice changer. It reads your PC microphone directly through the
sound card driver (WASAPI on Windows, Core Audio on macOS, ALSA/PipeWire on
Linux), transforms the voice in real time and plays the result to any output
device. No browser is involved.

What makes it sound professional rather than like a toy:

- **Pitch and formants are independent.** Each frame is split into the vocal-tract
  envelope (formants, found by cepstral smoothing) and the vocal-fold excitation
  (harmonics). Pitch moves only the harmonics; formants move only the envelope.
  A deeper voice stays a human voice instead of a slowed-down tape, and gender
  shifts sound natural.
- **Clean shifting.** Every harmonic is measured as a sinusoid and redrawn at
  exactly the shifted frequency, with phase carried coherently from frame to
  frame. A pure tone comes out as a pure tone, with everything else more than
  40 dB down (this is tested). The common per-bin vocoder approach leaves
  audible warble here.
- **A full channel strip:** rumble filter, noise gate, tone filters, 3-band EQ,
  saturation, compressor, reverb, and a limiter that never lets output clip.
- **Robot** (fixed-pitch harmonic voice) and **whisper** modes keep your words
  intelligible because they reuse your own formants.
- Loudness stays steady whatever the shift.
- Bypass is delay-matched, so A/B comparison is honest.
- Monitor output, WAV recording, offline file rendering, saveable settings.

## Quick start

**Windows:** install [Python 3.10+](https://www.python.org/downloads/) (tick
"Add to PATH"), then double-click `run-windows.bat`. The first run installs the
dependencies.

**macOS / Linux:** `./run-mac-linux.sh` (Linux also needs `sudo apt install libportaudio2 python3-tk`).

Manually:

```bash
pip install -r requirements.txt
python -m voicechanger            # GUI
```

Use headphones. With speakers, the microphone hears the changed voice and feeds back.

## Getting the voice into Discord, Zoom, OBS, games…

The app can read your mic directly, but **other** apps only accept audio from
something Windows lists as a *recording device*. So the question is how the
transformed voice becomes one.

| Option | Extra install? | Works in every app? | Notes |
|---|---|---|---|
| **Virtual audio cable** (VB-Audio *VB-CABLE*, free; macOS: *BlackHole*) | a small signed driver | ✅ | Recommended. Set this app's **Output** to `CABLE Input`, pick `CABLE Output` as the mic in Discord etc. Set **Monitor** to your headphones to hear yourself. |
| **OBS only** | none | OBS only | If you only stream or record, OBS can capture this app's output directly ("Application Audio Capture" on Windows 10 2004+). No cable needed. |
| **Recording / voice-overs** | none | n/a | Use **Record** or **Process WAV file…**. No routing needed. |
| **Linux (PipeWire/PulseAudio)** | none | ✅ | `pactl load-module module-null-sink sink_name=vc` + `pactl load-module module-remap-source master=vc.monitor source_name=vc_mic`; output to `vc`, select `vc_mic` in apps. |

**Why a driver is unavoidable on Windows and macOS:** only a kernel or
audio-server driver can create a new microphone device. A normal program,
whether Python, C++ or a browser, cannot. Commercial voice changers (Voicemod,
Clownfish and others) avoid a *separate* download only because they ship their
own signed virtual-mic driver in their installer. Writing and code-signing one
is a project of its own (EV certificate, Microsoft attestation signing). A free
signed driver like VB-CABLE gives the same result today.

## Command line

```bash
python -m voicechanger devices                       # list devices (use WASAPI ones on Windows)
python -m voicechanger presets
python -m voicechanger live --preset deeper --output "CABLE Input" --monitor "Headphones"
python -m voicechanger live --pitch -3 --formant 3 --record take1.wav
python -m voicechanger file in.wav out.wav --preset robot
```

Devices can be given by index or by any part of the name.

## Quality vs latency

| Quality | DSP delay | Best for |
|---|---|---|
| `low-latency` | ~19 ms | higher voices (above ~150 Hz), fast CPU |
| `balanced` (default) | ~37 ms | any speaking voice, live use |
| `high-quality` | ~75 ms | file rendering |

Device buffering adds roughly 10–20 ms on WASAPI. The status bar shows the
measured total. Deep voices need the larger frames because neighbouring
harmonics must be resolvable to be shifted cleanly.

## Presets

`natural`, `deeper`, `feminine`, `masculine`, `anonymous` (pitch and formants
moved in opposite directions, which makes a voice hard to recognise),
`chipmunk`, `giant`, `demon`, `robot`, `android`, `whisper`, `radio`, `cathedral`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite checks that pass-through is bit-exact, that pitch accuracy is within
2% for normal and deep voices, that formants are preserved, that shifting adds
no sidebands, that loudness holds, that the limiter never exceeds its ceiling,
that every preset runs clean, and that each quality tier runs comfortably
faster than real time.
