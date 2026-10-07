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

## Quick start (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/) and tick "Add to PATH".
2. Download **VoiceChanger-windows** from the latest successful run of the
   [voice-changer workflow](../../actions/workflows/voice-changer.yml) (Actions tab →
   newest green run → Artifacts) and unzip it. It contains the app plus the
   compiled `VoiceChangerAPO.dll`.
3. Double-click `run-windows.bat`. The first run installs the dependencies.

## Use it in every app (no virtual cable)

In the **Use in all apps** panel, pick your microphone and click **Install**.
Windows asks for administrator permission once. From then on, every program
that records from that microphone (Discord, Zoom, Teams, OBS, games,
browsers) receives the changed voice. You don't pick a special device anywhere;
your normal mic is simply changed.

- **Sliders and presets apply live**, in about 0.1 s, to every app at once. The
  app doesn't need to stay open: close it and the mic keeps the last voice you set.
- **Monitor** plays your (already changed) mic to your headphones, so you hear
  what others hear.
- **Bypass** switches the effect off without uninstalling.
- **Remove** puts the microphone back exactly as it was.

How it works: the voice processing is compiled into a Windows audio *system
effect* (APO), the same mechanism sound-card vendors use for noise suppression
and that Equalizer APO uses. It is attached to the mic's processing chain,
which is why every app hears it. Install records every original registry value
first, and Remove restores each one.

Worth knowing:

- **Latency:** the effect adds ~43 ms (`balanced`) or ~21 ms (`low-latency`).
  That's fine for calls and streaming.
- **Driver effects:** if your mic's driver had its own effect (some Realtek and
  laptop mics apply noise suppression or beamforming this way), it is replaced
  while the voice changer is installed, and returns when you Remove. Drivers
  that use Windows 11 composite effect lists keep theirs, with ours first.
- **"Raw" audio:** a few apps can ask Windows for unprocessed audio, which skips
  *all* effects, for example Zoom's "Original sound for musicians" or
  exclusive-mode pro-audio apps. Turn that option off in the app.
- **After big Windows or audio-driver updates**, Windows sometimes resets the
  mic's effects. If the voice stops changing, click Install again.
- **Emergency off switch:** Sound settings → your microphone → turn off
  *Audio enhancements*. Or run `python -m voicechanger apo uninstall` from the folder.
- The status line shows which apps are currently using the changed mic. A
  diagnostic log is in `%ProgramData%\VoiceChanger\apo.log`.

Headphones are recommended so the mic doesn't pick up your speakers.

### Without installing anything system-wide

- **In-app preview:** pick your mic and headphones under *Devices* and press
  **Start**. Nothing is installed.
- **Recordings / voice-overs:** **Record**, or **Process WAV file…**.
- **macOS / Linux:** `./run-mac-linux.sh`. To feed other apps there, use a
  virtual device (BlackHole on macOS; on Linux,
  `pactl load-module module-null-sink sink_name=vc` plus
  `pactl load-module module-remap-source master=vc.monitor source_name=vc_mic`).

## Command line

```bash
python -m voicechanger devices                       # list devices (use WASAPI ones on Windows)
python -m voicechanger presets
python -m voicechanger live --preset deeper --output "CABLE Input" --monitor "Headphones"
python -m voicechanger live --pitch -3 --formant 3 --record take1.wav
python -m voicechanger file in.wav out.wav --preset robot

# Windows, system-wide (asks for admin when needed)
python -m voicechanger apo list                       # microphones, * = installed
python -m voicechanger apo install --endpoint "{id}"  # change that mic for every app
python -m voicechanger apo status                     # which apps are using it now
python -m voicechanger apo uninstall                  # restore everything
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
cmake -S apo -B apo/build && cmake --build apo/build   # C++ DSP, for the golden test
pytest
```

The suite checks that pass-through is bit-exact, that pitch accuracy is within
2% for normal and deep voices, that formants are preserved, that shifting adds
no sidebands, that loudness holds, that the limiter never exceeds its ceiling,
that every preset runs clean, and that each quality tier runs comfortably
faster than real time. It also checks that the C++ code inside the Windows
effect matches the Python reference sample for sample (within 1e-4), and that
install/uninstall restores the registry exactly across several kinds of mic.

On Windows, CI also builds the APO with MSVC and runs `apo_harness.exe`, which
loads the DLL the way the audio engine does. It negotiates formats, processes
audio and checks the pitch, applies live settings changes, swaps quality
mid-stream, falls back to pass-through when the settings are removed, and unloads.

### Building the APO yourself

Visual Studio 2022 with the "Desktop development with C++" workload:

```bat
cmake -S apo -B apo/build -A x64
cmake --build apo/build --config Release
copy apo\build\Release\VoiceChangerAPO.dll voicechanger\bin\
```
