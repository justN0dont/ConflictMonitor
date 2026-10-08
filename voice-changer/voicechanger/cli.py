"""Command line entry point.

    python -m voicechanger                 # open the GUI
    python -m voicechanger devices         # list audio devices
    python -m voicechanger presets         # list presets
    python -m voicechanger live --preset deeper --output "CABLE Input" --monitor "Headphones"
    python -m voicechanger file in.wav out.wav --preset robot
    python -m voicechanger file in.wav out.wav --pitch -5 --tremor-depth 0.6 --breath 0.3
    python -m voicechanger apo list                       # Windows: microphones
    python -m voicechanger apo install --endpoint {id}    # voice-change that mic in every app
    python -m voicechanger apo uninstall                  # put everything back
"""

from __future__ import annotations

import argparse
import json
import sys

from .chain import QUALITY, Settings
from .presets import PRESETS, make_settings

VOICE_FLAGS = {
    "pitch": float, "formant": float, "mode": str, "robot_hz": float, "mix": float,
    "tremor_depth": float, "tremor_hz": float, "jitter": float, "breath": float,
    "gravel": float, "gravel_hz": float,
    "gate_threshold_db": float, "highpass_hz": float, "lowpass_hz": float,
    "low_db": float, "mid_db": float, "high_db": float, "drive": float,
    "reverb_mix": float, "reverb_room": float, "input_gain_db": float, "output_gain_db": float,
}


def _add_voice_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--preset", default="natural", choices=sorted(PRESETS))
    p.add_argument("--settings", help="JSON file of settings (as saved by the GUI)")
    p.add_argument("--quality", choices=list(QUALITY))
    p.add_argument("--no-gate", action="store_true", help="disable the noise gate")
    for name, typ in VOICE_FLAGS.items():
        p.add_argument("--" + name.replace("_", "-"), dest=name, type=typ)


def _settings_from(args) -> Settings:
    s = make_settings(args.preset)
    if args.settings:
        with open(args.settings) as f:
            s.update(**json.load(f))
    s.update(**{k: getattr(args, k) for k in VOICE_FLAGS if getattr(args, k) is not None})
    if args.no_gate:
        s.gate_enabled = False
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voicechanger", description="Real-time voice changer")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="open the graphical app (default)")
    sub.add_parser("devices", help="list audio devices")
    sub.add_parser("presets", help="list built-in presets")

    live = sub.add_parser("live", help="run on the microphone from the console")
    live.add_argument("--input", "-i", help="input device index or name fragment (default: system mic)")
    live.add_argument("--output", "-o", help="output device index or name fragment")
    live.add_argument("--monitor", "-m", help="extra device to hear yourself on, e.g. headphones")
    live.add_argument("--samplerate", type=int, default=48000)
    live.add_argument("--record", help="also record the transformed voice to this WAV")
    _add_voice_args(live)

    apo = sub.add_parser("apo", help="Windows: install the voice changer into a microphone for all apps")
    apo.add_argument("action", choices=["list", "install", "uninstall", "status"])
    apo.add_argument("--endpoint", action="append", help="capture endpoint id from `apo list` (repeatable)")
    apo.add_argument("--dll", help="path to VoiceChangerAPO.dll (default: bundled build)")
    apo.add_argument("--dry-run", action="store_true", help="show what would change without changing it")

    f = sub.add_parser("file", help="transform a WAV file")
    f.add_argument("src")
    f.add_argument("dst")
    _add_voice_args(f)

    args = ap.parse_args(argv)
    cmd = args.cmd or "gui"

    if cmd == "presets":
        for name, overrides in PRESETS.items():
            print(f"{name:12s} {overrides or '(no changes)'}")
        return 0

    if cmd == "apo":
        from .system_mic import cli as apo_cli
        return apo_cli(args)

    if cmd == "file":
        from .fileio import process_file
        s = _settings_from(args)
        # Settings saved while A/B-ing would otherwise write the input back
        # out unchanged; the GUI's Process WAV ignores bypass the same way.
        s.bypass = False
        secs = process_file(args.src, args.dst, s, args.quality or "high-quality")
        print(f"wrote {args.dst} ({secs:.1f} s)")
        return 0

    if cmd == "devices":
        from .audio import default_device, list_devices
        di, do = default_device("input"), default_device("output")
        for d in list_devices():
            io = ("in " if d.inputs else "   ") + ("out" if d.outputs else "   ")
            mark = " <default in>" if d.index == di else " <default out>" if d.index == do else ""
            print(f"{io}  {d.label}{mark}")
        return 0

    if cmd == "live":
        from .audio import LiveEngine, wait_forever
        engine = LiveEngine(args.input, args.output, args.monitor, args.samplerate,
                            args.quality or "balanced", _settings_from(args))
        with engine:
            if args.record:
                engine.start_recording(args.record)
            print(f"running: preset={args.preset}  dsp latency {engine.chain.latency / args.samplerate * 1000:.1f} ms"
                  "  (Ctrl+C to stop)")
            wait_forever(engine)
        return 0

    from .gui import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
