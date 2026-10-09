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
  exactly the shifted frequency, with its phase followed from frame to frame
  as it moves. A pure tone comes out as a pure tone, with everything else more
  than 40 dB down, and harmonics stay whole while the pitch glides or wobbles
  (both tested). The common per-bin vocoder approach leaves audible warble here.
- **A full channel strip:** rumble filter, noise gate, tone filters, a 3-band
  EQ whose frequencies you can move, saturation that sounds the same however
  loud your mic is, compressor, reverb, and a limiter that never lets output clip.
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

Every preset is level-matched to `natural`: within about 0.7 dB on real male
and female speech, from a quiet laptop mic to a hot headset, so switching
voices never makes your listeners jump. Each sounds the same at any mic level,
too: the grit doesn't come and go with how loud you speak, and every preset
compresses as gently as `natural` (about 3 dB off the louder syllables at a
normal speaking level; `announcer` a little firmer). Switching presets keeps
your mic set-up (input gain and gate).

The gender and age presets assume a voice at the other end of the range:
`feminine`, `kid`, `old_lady` and `valley_girl` start from a lower voice,
`masculine` from a higher one. Pull Pitch back towards 0 if yours is already
most of the way there.

Voices:

- `natural`: your own voice through the clean-up chain only (rumble filter,
  gate, gentle 2:1 compression).
- `deeper`: pitch down 4 and formants down 1.5, with the low mids eased and a
  little presence and air added so the bigger voice stays clear.
- `feminine`: pitch up 7 and formants up 3 (between typical male and female
  voices, pitch differs far more than formants), lighter lows and a softened top.
- `masculine`: pitch down 7 and formants down 2.5, with the low mids eased and
  presence lifted so the deeper voice stays clear.
- `anonymous`: pitch down 3 and formants up 3, plus a touch of grit. Moving
  them in opposite directions makes a voice hard to recognise.
- `chipmunk`: pitch and formants both up most of an octave, small and squeaky,
  with some body kept and the sibilance softened.
- `giant`: pitch down 10 with a much longer vocal tract (formants down 6), big
  lows kept clear of mud, a presence lift so words stay intelligible, and a large room.
- `demon`: an octave down with nearly your own formants and a faint trace of
  your own voice underneath, heavy distortion and a big, dark reverb.
- `robot`: a 110 Hz monotone carried by your own formants so words stay
  clear, with a mid push and buzzy grit.
- `android`: a higher 220 Hz monotone, slightly raised formants, a softened
  top and a touch of room.
- `whisper`: harmonics replaced by breath noise shaped by your formants. An
  intelligible whisper.
- `radio`: a 350 Hz–3.4 kHz telephone band with a mid push and crunchy distortion.
- `cathedral`: your own voice in a very large, long reverb.

Characters, built on the character controls below:

- `old_lady`: pitch up 8 with slightly raised formants, a gentle 6 Hz tremor,
  unsteady pitch, breathiness, a faint rasp and a thin, soft tone.
- `old_man`: a touch lower with a slightly longer vocal tract, a 5 Hz tremor,
  unsteady pitch, breath, a little gravel, a thinner chest and a softer top.
- `kid`: an octave up with a much smaller vocal tract (formants up 5), a little
  breathiness and unsteadiness, some body in the low mids and a softened top.
- `tough_guy`: big and forceful. Pitch down 4 with a longer vocal tract, a
  vocal-fry rattle, grit and a barking midrange.
- `pirate`: gruff and weathered. A little deeper, with hoarse breath, fry
  gravel, grit, a barky midrange and a softened top.
- `valley_girl`: bright and airy. Higher pitch and formants, light
  breathiness, an airy top and a light, slow fry (the uptalk is up to you).
- `announcer`: a smooth, deep broadcast voice. Slightly lower pitch and
  formants, warm lows, presence and air, firmer compression and a touch of room.
- `villain`: menacing. Pitch down 4 with darker formants, a gravelly rattle, a
  dark top and a dramatic reverb.
- `raspy`: a husky, smoky rasp at your own pitch, with breathy aspiration, a
  creaky rattle and warm grit.
- `ghost`: an airy, hollow spectre. Slightly raised pitch and formants, a slow
  3.5 Hz waver, heavy breathiness, thin lows and a long, ethereal reverb.
- `ogre`: huge and monstrous. An octave down with a much longer vocal tract, a
  heavy, slow 30 Hz gravel growl, grit and a big low end.
- `alien`: pitch up 6 with formants down 4, a fast 9 Hz warble and a 180 Hz
  buzz for a metallic shimmer, in a small, bright room.
- `megaphone`: a bullhorn. A narrow 600 Hz–4 kHz band with a honky 1.5 kHz
  peak, a crunchy horn distortion and a touch of outdoor space.
- `vibrato`: a singer's vibrato, a steady 5.5 Hz swing of about half a semitone
  each way with a matching loudness pulse, a little presence and air, and a hall reverb.

## Character controls

The *Character* sliders add the cues listeners use to judge age and texture.
Tremor, Shakiness, Breathiness and Gravel start at zero, and at zero the
output is bit-for-bit the same as without them.

- **Tremor** (0–2 semitones, typically 0.3–0.7): a regular pitch swing of up to
  this much either way, with loudness rising and falling in step, as in a sung
  vibrato or an aged, shaky voice.
- **Tremor rate** (2–10 Hz): how fast it swings. Aged voices and vibrato sit
  around 5–7 Hz, slower sounds eerie, faster becomes a warble. Each cycle runs
  up to ±10% off it (about 6% on average), as natural tremor does, so it never
  sounds mechanical.
- **Shakiness** (0–1, typically 0.3–0.6): random, smoothly gliding pitch
  wander, about 0.1 semitone RMS at 1 with peaks near 0.4, the same at every
  quality setting and sample rate.
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
pitch at its set rate and depth with natural cycle-to-cycle variation, that
shakiness is the same at every quality and sample rate, that harmonics don't
drop out while tremor, shakiness or the speaker's own glides move the pitch
(at balanced and high quality, and on a 16 kHz headset mic), that breath keeps
loudness steady, and that gravel pulses at its rate whatever the block size.

It also checks that the C++ code inside the Windows effect matches the Python
reference sample for sample (within 1e-4). That covers every preset, each
character control alone and combined (including out-of-range values, which
both sides must clamp alike), settings changing mid-stream, exact digital
silence, a voice gliding an octave, 16, 44.1 and 96 kHz, and a randomized fuzz set that sweeps every control
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
