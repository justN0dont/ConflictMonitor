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
- **Character controls** (tremor, shakiness, breathiness, gravel) add the cues
  that make a voice sound old, rough or eerie, which a pitch shift alone can't.
- Loudness stays steady whatever the shift.
- Bypass is the raw mic (input gain off too), delay-matched, so A/B comparison
  is honest.
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
- **Apps that already had the mic open** need to reopen it (e.g. leave and
  rejoin the Discord call). If the voice still doesn't change after Install,
  restart the PC once. Some audio drivers only reload their effects at boot.
- **After big Windows or audio-driver updates**, Windows sometimes resets the
  mic's effects. If the voice stops changing, click Install again.
- **Emergency off switch:** Sound settings → your microphone → turn off
  *Audio enhancements*. Or run `python -m voicechanger apo uninstall` from the folder.
- **Sample rates** up to 192 kHz are processed. Above that (a mic set to
  384 kHz in its Advanced properties) the effect can't keep up, so the audio
  passes through unchanged and the log says so.
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
python -m voicechanger file in.wav out.wav --preset old_man --breath 0.3

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

27 presets. Each one is a starting point: load it, then adjust any slider.
Pitch and formant amounts are in semitones.

Voices:

- `natural`: your own voice through the clean-up chain only (rumble filter,
  gate, gentle compression).
- `deeper`: pitch down 4, formants down 2, a little extra warmth.
- `feminine`: pitch up 6 and formants up 3 (between typical male and female
  voices, pitch differs far more than formants), lighter lows, a brighter top.
- `masculine`: pitch down 6 and formants down 3, more low end, a slightly darker top.
- `anonymous`: pitch down 3 and formants up 3. Moving them in opposite
  directions makes a voice hard to recognise.
- `chipmunk`: pitch and formants both up most of an octave, small and squeaky.
- `giant`: pitch down 10 with a much longer vocal tract (formants down 6),
  heavy lows and a large room.
- `demon`: an octave down with nearly your own formants, heavy distortion,
  booming lows and a big reverb.
- `robot`: a 110 Hz monotone carried by your own formants so words stay
  clear, with a mid push and light grit.
- `android`: a higher 220 Hz monotone, slightly raised formants, a touch of room.
- `whisper`: harmonics replaced by breath noise shaped by your formants, with
  a brighter top. An intelligible whisper.
- `radio`: a 400 Hz–3.4 kHz telephone band, a mid push, distortion and hard compression.
- `cathedral`: your own voice in a very large, long reverb.

Characters, built on the character controls below:

- `old_lady`: pitch up 8 with slightly raised formants, a gentle 6 Hz tremor,
  unsteady pitch, breathiness, a faint rasp and a thin tone with less chest.
- `old_man`: a little lower with a thinner chest, a 5 Hz tremor, unsteady
  pitch, some breath, a touch of gravel and a slightly darker top.
- `kid`: an octave up with a much smaller vocal tract (formants up 5), a little
  breathiness and unsteadiness, and a bright, forward tone.
- `tough_guy`: big and forceful. Pitch down 5 with a longer vocal tract, chest
  weight, a vocal-fry rattle, gritty drive, a barking midrange and heavy, punchy compression.
- `pirate`: gruff and weathered. A little deeper, with hoarse breath, fry
  gravel, light grit, a barky midrange and a softened top.
- `valley_girl`: bright and airy. Slightly higher pitch and formants, light
  breathiness, a mid and treble lift and a light, slow fry (the uptalk is up to you).
- `announcer`: a smooth, deep broadcast voice with lower pitch and formants,
  warm lows, a crisp top, steady compression and a touch of room.
- `villain`: menacing. Pitch down 5 with darker formants, a gravelly rattle, a
  warm close-up low end, a dark top and a long, dramatic reverb.
- `raspy`: a husky, smoky rasp at your own pitch, with breathy aspiration, a
  creaky rattle and a touch of warm drive.
- `ghost`: an airy, hollow spectre. Slightly raised pitch and formants, a slow
  3.5 Hz waver, heavy breathiness, thin lows and a long, ethereal reverb.
- `ogre`: huge and monstrous. An octave down with a much longer vocal tract, a
  heavy, slow 30 Hz gravel growl, grit and a big low end.
- `alien`: pitch up 6 with formants down 4, a fast 9 Hz warble and a 180 Hz
  buzz for a metallic shimmer, in a small, bright room.
- `megaphone`: a bullhorn. A narrow 600 Hz–4 kHz band with a honky 1.5 kHz
  peak, heavy drive, tight compression and a touch of outdoor space.
- `vibrato`: a singer's vibrato, a steady 5.5 Hz swing of about half a semitone
  each way with a matching loudness pulse, a brighter top and a lush hall reverb.

## Character controls

The *Character* sliders add the cues listeners use to judge age and texture.
Tremor, Shakiness, Breathiness and Gravel start at zero, and at zero the
output is bit-for-bit the same as without them.

- **Tremor** (0–2 semitones, typically 0.3–0.7): a regular pitch swing of up to
  this much either way, with loudness rising and falling in step, as in a sung
  vibrato or an aged, shaky voice.
- **Tremor rate** (2–10 Hz): how fast it swings. Aged voices and vibrato sit
  around 5–7 Hz, slower sounds eerie, faster becomes a warble. It drifts ±10%
  so it never sounds mechanical.
- **Shakiness** (0–1, typically 0.3–0.6): random, smoothly gliding pitch
  wander, up to ±0.6 semitone at 1.
- **Breathiness** (0–1, typically 0.1–0.4): turns that share of the voice's
  energy into breath noise shaped by your own formants, so the voice gets
  airier without getting louder. Whisper mode is all breath already, so it has
  no effect there.
- **Gravel** (0–1, typically 0.2–0.4, up to 0.7 for a growl): vocal fry. The
  level dips in quick, slightly irregular pulses, like vocal folds closing unevenly.
- **Gravel rate** (20–200 Hz): the pulse rate. 30–50 Hz sounds like creak;
  above ~100 Hz it becomes a buzz.

Tremor rate and Gravel rate do nothing until Tremor or Gravel is turned up.
Like every slider, these apply live: in the preview and system-wide in every
app, because the Windows effect runs the same processing. Their randomness is
deterministic, so a file render comes out the same every time. On the command
line they are `--tremor-depth`, `--tremor-hz`, `--jitter`, `--breath`,
`--gravel` and `--gravel-hz`, which accept wider ranges (tremor up to 3
semitones at 0.5–15 Hz, gravel down to 10 Hz).

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
faster than real time. For the character controls it checks that at zero they
leave the output bit-identical to before they existed, that tremor swings the
pitch at its set rate and depth, that breath keeps loudness steady, and that
gravel pulses at its rate whatever the block size.

It also checks that the C++ code inside the Windows effect matches the Python
reference sample for sample (within 1e-4). That covers every preset, each
character control alone and combined (including out-of-range values, which
both sides must clamp alike), settings changing mid-stream, exact digital
silence, 44.1 and 96 kHz, and a randomized fuzz set that sweeps every control
across its range in all three modes and quality tiers. Finally, it checks that
install/uninstall restores the registry exactly across several kinds of mic.

On Windows, CI also builds the APO with MSVC and runs `apo_harness.exe`, which
loads the DLL the way the audio engine does. It negotiates formats, processes
audio and checks the pitch, applies live settings changes (including the
character controls, checking that tremor swings the pitch at its rate), swaps
quality mid-stream, stays cheap through seconds of silence, keeps wet and dry
aligned across a partial period, passes 384 kHz through untouched, falls back
to pass-through when the settings are removed, and unloads.

### Building the APO yourself

Visual Studio 2022 with the "Desktop development with C++" workload:

```bat
cmake -S apo -B apo/build -A x64
cmake --build apo/build --config Release
copy apo\build\Release\VoiceChangerAPO.dll voicechanger\bin\
```
